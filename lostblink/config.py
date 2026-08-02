"""Configuration loading and validation.

Upstream parses config with bare ``json.load`` and reads it through a module-level
global mutated by an import side effect (``config.py:37-38``), which makes the
whole package unimportable without a config file present. Here it is an explicit
dataclass tree, validated on load, passed to whatever needs it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

LiveMode = Literal["off", "on_motion", "always"]

#: Below this, blinkpy's own throttling makes the extra requests pure waste and
#: the ban risk becomes real. See bug B-13.
MIN_POLL_INTERVAL = 10.0


class ConfigError(ValueError):
    """The configuration file is missing something or has a bad value."""


@dataclass(slots=True)
class BlinkConfig:
    username: str = ""
    password: str = ""
    poll_interval: float = 30.0
    history_days: float = 7.0
    request_timeout: float = 30.0

    def validate(self) -> None:
        if self.poll_interval < MIN_POLL_INTERVAL:
            log.warning(
                "blink.poll_interval %.0fs is below the %.0fs floor; clamping. "
                "Polling faster does not get you fresher data (blinkpy throttles "
                "internally) but does risk a temporary ban.",
                self.poll_interval,
                MIN_POLL_INTERVAL,
            )
            self.poll_interval = MIN_POLL_INTERVAL
        if self.history_days <= 0:
            raise ConfigError("blink.history_days must be positive")


@dataclass(slots=True)
class LiveConfig:
    """Live-view behaviour. Off by default -- it costs real battery."""

    mode: LiveMode = "off"
    max_sessions_per_hour: int = 6
    min_seconds_between_sessions: float = 60.0
    daily_seconds_budget: float = 1800.0
    require_battery_ok: bool = True
    #: Cameras that are mains powered, so the battery rails do not apply.
    mains_powered: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.mode not in ("off", "on_motion", "always"):
            raise ConfigError(
                f"live.mode must be off|on_motion|always, got {self.mode!r}"
            )
        if self.max_sessions_per_hour < 1:
            raise ConfigError("live.max_sessions_per_hour must be at least 1")


@dataclass(slots=True)
class CamerasConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    max_failures: int = 5
    restart_delay_seconds: float = 30.0
    max_restart_delay_seconds: float = 900.0

    def selected(self, discovered: list[str]) -> list[str]:
        """Resolve the enabled/disabled lists against what was discovered.

        Empty ``enabled`` means "everything". ``disabled`` always wins. Names are
        matched case-insensitively because the Blink app is inconsistent about
        capitalisation and this is a common support question.
        """
        lowered = {name.lower(): name for name in discovered}
        chosen = (
            [lowered[n.lower()] for n in self.enabled if n.lower() in lowered]
            if self.enabled
            else list(discovered)
        )
        blocked = {n.lower() for n in self.disabled}

        for name in self.enabled:
            if name.lower() not in lowered:
                log.warning("camera %r is in 'enabled' but was not found", name)

        return [name for name in chosen if name.lower() not in blocked]


@dataclass(slots=True)
class RtspConfig:
    address: str = "mediamtx"
    port: int = 8554
    transport: Literal["tcp", "udp"] = "tcp"

    @property
    def base_url(self) -> str:
        return f"rtsp://{self.address}:{self.port}"

    def url_for(self, stream_name: str) -> str:
        return f"{self.base_url}/{stream_name}"


@dataclass(slots=True)
class FFmpegConfig:
    binary: str = "ffmpeg"
    probe_binary: str = "ffprobe"
    #: e.g. "qsv", "vaapi", "nvenc". Empty means software.
    hwaccel: str = ""
    hwaccel_device: str = ""
    loglevel: str = "error"
    still_video_duration: float = 0.5

    def validate(self) -> None:
        if self.still_video_duration <= 0:
            raise ConfigError("ffmpeg.still_video_duration must be positive")


@dataclass(slots=True)
class PathsConfig:
    work: Path = Path("/working")
    config: Path = Path("/config")

    def ensure(self) -> None:
        for path in (self.work, self.config):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def credentials(self) -> Path:
        return self.config / ".cred.json"


@dataclass(slots=True)
class Config:
    blink: BlinkConfig = field(default_factory=BlinkConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    cameras: CamerasConfig = field(default_factory=CamerasConfig)
    rtsp: RtspConfig = field(default_factory=RtspConfig)
    ffmpeg: FFmpegConfig = field(default_factory=FFmpegConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    log_level: str = "INFO"

    def validate(self) -> None:
        self.blink.validate()
        self.live.validate()
        self.ffmpeg.validate()

    @classmethod
    def load(cls, path: Path | str | None = None) -> Config:
        path = Path(path or os.getenv("LOSTBLINK_CONFIG", "config.json"))
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")

        config = cls.from_dict(raw)
        config.apply_credentials(raw)
        config.validate()
        return config

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        paths_raw = raw.get("paths", {})
        return cls(
            blink=_build(BlinkConfig, raw.get("blink", {}), skip={"login"})
            if "blink" in raw
            else BlinkConfig(),
            live=_build(LiveConfig, raw.get("live", {})),
            cameras=_build(CamerasConfig, raw.get("cameras", {})),
            rtsp=_build(RtspConfig, raw.get("rtsp", raw.get("rtsp_server", {}))),
            ffmpeg=_build(FFmpegConfig, raw.get("ffmpeg", {})),
            paths=PathsConfig(
                work=Path(paths_raw.get("work", paths_raw.get("videos", "/working"))),
                config=Path(paths_raw.get("config", "/config")),
            ),
            log_level=str(raw.get("log_level", "INFO")).upper(),
        )

    def apply_credentials(self, raw: dict[str, Any]) -> None:
        """Pull username/password out of the nested ``blink.login`` block.

        Environment variables win, so credentials can be supplied as Docker
        secrets rather than sitting in a bind-mounted file (bug B-23).
        """
        login = raw.get("blink", {}).get("login", {})
        self.blink.username = (
            os.getenv("LOSTBLINK_USERNAME") or login.get("username") or ""
        )
        self.blink.password = (
            os.getenv("LOSTBLINK_PASSWORD") or login.get("password") or ""
        )


def _build(cls: type, raw: dict[str, Any], skip: set[str] | None = None) -> Any:
    """Construct a dataclass from a dict, ignoring unknown keys.

    Unknown keys are warned about rather than rejected -- a typo in a config file
    should tell you it did nothing, not refuse to start.
    """
    skip = skip or set()
    fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {}
    for key, value in raw.items():
        if key in skip or key.startswith("_"):
            continue  # "_comment" and friends are documentation, not config
        if key not in fields:
            log.warning("ignoring unknown config key %s.%s", cls.__name__, key)
            continue
        kwargs[key] = value
    return cls(**kwargs)


def secure_credentials_file(path: Path) -> None:
    """Force ``0600`` on the credentials file, warning if it was looser.

    It holds the OAuth refresh token and hardware id -- full account access.
    Upstream writes it with the default umask into a bind-mounted directory
    (bug B-23).
    """
    if not path.exists():
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning(
                "%s was mode %o (readable by other users); tightening to 0600. "
                "Assume the token was exposed and consider re-authenticating.",
                path,
                mode,
            )
        path.chmod(0o600)
    except OSError as exc:
        # Windows and some bind mounts do not support this. Not fatal.
        log.debug("could not chmod %s: %s", path, exc)
