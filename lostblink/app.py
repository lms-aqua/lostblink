"""Application orchestration.

One :class:`CameraWorker` per camera, each an independent asyncio task, all fed
by a **single** shared API refresh per cycle. Upstream calls a full
``blink.refresh()`` once per camera per poll (bug B-13) and processes cameras
strictly in series (B-20).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blink.liveview import (
    LiveSession,
    LiveViewClient,
    LiveViewError,
    LiveViewPolicy,
    LiveViewRateLimited,
    LiveViewRefused,
)
from .config import Config
from .media.ffmpeg import FFmpeg, FFmpegError
from .proxy import TunnelError, open_tunnel
from .stream.publisher import LivePublisher

log = logging.getLogger(__name__)


class CameraState(enum.StrEnum):
    STARTING = "starting"
    STILL = "still"          # publishing the looped last frame
    LIVE = "live"            # publishing a real live session
    NO_MEDIA = "no_media"    # nothing to show yet; not an error
    BACKOFF = "backoff"
    AUTH_REQUIRED = "auth_required"


@dataclass(slots=True)
class WorkerStats:
    """Supervision state.

    Upstream monkey-patches ``failure_count`` and ``datetime_started`` onto
    StreamServer instances from outside the class (bug B-17), which is one
    refactor away from an AttributeError in the main loop.
    """

    failures: int = 0
    live_sessions: int = 0
    last_record: str | None = None
    next_attempt_at: float = 0.0
    state: CameraState = CameraState.STARTING

    def backoff(self, base: float, ceiling: float) -> float:
        """Exponential backoff with a ceiling. A camera can always come back.

        Upstream pops a camera out of its dict after ``max_failures`` and never
        re-adds it, so a transient blip during startup loses that camera until
        someone restarts the container (bug B-18).
        """
        delay = min(base * (2 ** min(self.failures, 6)), ceiling)
        self.next_attempt_at = time.monotonic() + delay
        return delay

    @property
    def ready(self) -> bool:
        return time.monotonic() >= self.next_attempt_at


class CameraWorker:
    """Owns one camera's RTSP stream for the life of the process."""

    def __init__(
        self,
        name: str,
        config: Config,
        ffmpeg: FFmpeg,
        liveview: LiveViewClient,
        cameras: dict[str, Any],
    ) -> None:
        self.name = name
        self.slug = _slugify(name)
        self.config = config
        self.stats = WorkerStats()
        self._ffmpeg = ffmpeg
        self._liveview = liveview
        self._cameras = cameras
        self._policy = LiveViewPolicy(
            max_sessions_per_hour=config.live.max_sessions_per_hour,
            min_seconds_between_sessions=config.live.min_seconds_between_sessions,
            daily_seconds_budget=config.live.daily_seconds_budget,
            min_battery="ok" if config.live.require_battery_ok else "any",
        )
        if name in config.live.mains_powered:
            # Mains-powered devices do not need the battery rails.
            self._policy.min_battery = "any"
        self._publisher: LivePublisher | None = None
        self._live_task: asyncio.Task[None] | None = None

    @property
    def rtsp_url(self) -> str:
        return self.config.rtsp.url_for(self.slug)

    @property
    def camera(self) -> Any:
        return self._cameras.get(self.name)

    # -- motion ------------------------------------------------------------

    def motion_advanced(self) -> bool:
        """Has a new recording appeared since we last looked?

        Keyed purely on ``last_record``. Upstream also requires
        ``motion_detected`` to be true at the instant of polling, so any clip
        that starts and finishes between two polls is silently skipped (B-05).
        """
        camera = self.camera
        if camera is None:
            return False
        last_record = (camera.attributes or {}).get("last_record")
        if not last_record or last_record == self.stats.last_record:
            return False
        first_observation = self.stats.last_record is None
        self.stats.last_record = last_record
        return not first_observation

    @property
    def battery(self) -> str | None:
        camera = self.camera
        return (camera.attributes or {}).get("battery") if camera else None

    def wants_live(self, motion: bool) -> bool:
        mode = self.config.live.mode
        if mode == "always":
            return True
        if mode == "on_motion":
            return motion
        return False

    # -- live sessions -----------------------------------------------------

    async def ensure_live(self) -> None:
        """Start a live session if one is not already running."""
        if self._live_task is not None and not self._live_task.done():
            return
        self._live_task = asyncio.create_task(
            self._run_live(), name=f"live-{self.slug}"
        )

    async def _run_live(self) -> None:
        """Open a session, publish it, and renew it until told to stop."""
        try:
            self._policy.check(battery=self.battery)
        except LiveViewRefused as exc:
            if self._policy.should_log_refusal():
                log.info("%s: live view refused: %s", self.name, exc)
            return

        camera = self.camera
        if camera is None:
            return

        started = time.monotonic()
        session: LiveSession | None = None
        try:
            session = await self._request_session(camera)
            self._policy.record_start()
            self.stats.live_sessions += 1

            tunnel = await open_tunnel(session.url)
            publisher = LivePublisher(
                self.name,
                self.rtsp_url,
                self._ffmpeg,
                transport=self.config.rtsp.transport,
            )
            await publisher.start(tunnel)
            self._publisher = publisher
            self.stats.state = CameraState.LIVE

            await self._renew_loop(camera, session, publisher)

        except LiveViewRateLimited as exc:
            log.warning("%s: %s -- pausing live view for 5 minutes", self.name, exc)
            self.stats.next_attempt_at = time.monotonic() + 300
        except (LiveViewError, TunnelError, FFmpegError) as exc:
            log.warning("%s: live view failed: %s", self.name, exc)
            self.stats.failures += 1
        except asyncio.CancelledError:
            raise
        finally:
            self._policy.record_duration(time.monotonic() - started)
            if self._publisher is not None:
                await self._publisher.stop()
                self._publisher = None
            if session is not None:
                await self._liveview.stop(session)
            if self.stats.state == CameraState.LIVE:
                self.stats.state = CameraState.STILL

    async def _renew_loop(
        self, camera: Any, session: LiveSession, publisher: LivePublisher
    ) -> None:
        """Keep the stream alive across the 300s session cap.

        The publisher's ffmpeg process is never restarted -- only the tunnel
        underneath it is swapped, at a keyframe.
        """
        while self.config.live.mode == "always" and publisher.running:
            await asyncio.sleep(session.seconds_until_renew())
            if not publisher.running:
                return
            try:
                self._policy.check(battery=self.battery)
                session = await self._request_session(camera)
                self._policy.record_start()
                tunnel = await open_tunnel(session.url)
            except (LiveViewError, TunnelError) as exc:
                log.warning(
                    "%s: could not renew, falling back to stills: %s", self.name, exc
                )
                return
            await publisher.handover(tunnel)

        # on_motion mode: ride out this one session, then stop.
        remaining = session.remaining
        if remaining > 0:
            log.debug("%s: holding live for %.0fs", self.name, remaining)
            await asyncio.sleep(remaining)

    async def _request_session(self, camera: Any) -> LiveSession:
        return await self._liveview.request(
            network_id=int(camera.network_id),
            device_id=int(camera.camera_id),
            device_type=_device_type(camera),
            timeout=self.config.blink.request_timeout,
        )

    async def stop(self) -> None:
        task = self._live_task
        self._live_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._publisher is not None:
            await self._publisher.stop()
            self._publisher = None


class Application:
    """Top-level service: discover cameras, poll once per cycle, fan out."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.workers: dict[str, CameraWorker] = {}
        self._blink: Any = None
        self._ffmpeg = FFmpeg(
            config.ffmpeg.binary,
            config.ffmpeg.probe_binary,
            loglevel=config.ffmpeg.loglevel,
            hwaccel=config.ffmpeg.hwaccel,
            hwaccel_device=config.ffmpeg.hwaccel_device,
        )
        self._running = False

    async def start(self, blink: Any) -> None:
        self._blink = blink
        self._ffmpeg.check_available()
        self.config.paths.ensure()

        liveview = LiveViewClient(blink)
        discovered = list(blink.cameras.keys())
        selected = self.config.cameras.selected(discovered)

        if not selected:
            raise RuntimeError(
                f"no cameras selected (discovered: {discovered or 'none'})"
            )

        for name in selected:
            self.workers[name] = CameraWorker(
                name, self.config, self._ffmpeg, liveview, blink.cameras
            )

        log.info("watching %d camera(s): %s", len(selected), ", ".join(selected))
        self._warn_about_battery_drain(selected)

        self._running = True
        await self._poll_loop()

    def _warn_about_battery_drain(self, selected: list[str]) -> None:
        if self.config.live.mode != "always":
            return
        battery = [n for n in selected if n not in self.config.live.mains_powered]
        if battery:
            log.warning(
                "live.mode='always' on %s. Continuous live view drains Blink "
                "batteries in 1-3 days. List mains-powered cameras under "
                "live.mains_powered, or use 'on_motion'.",
                ", ".join(battery),
            )

    async def _poll_loop(self) -> None:
        """One shared refresh per cycle, then fan out to every camera."""
        while self._running:
            try:
                await self._blink.refresh(force=True)
            except Exception as exc:
                log.warning("refresh failed, retrying next cycle: %s", exc)
                await asyncio.sleep(self.config.blink.poll_interval)
                continue

            results = await asyncio.gather(
                *(self._tick(worker) for worker in self.workers.values()),
                return_exceptions=True,
            )
            for worker, result in zip(self.workers.values(), results, strict=True):
                if isinstance(result, Exception):
                    # A transient error must not kill a healthy stream (B-19).
                    log.warning("%s: %s", worker.name, result)
                    worker.stats.failures += 1

            await asyncio.sleep(self.config.blink.poll_interval)

    async def _tick(self, worker: CameraWorker) -> None:
        motion = worker.motion_advanced()
        if motion:
            log.info("%s: new recording", worker.name)
        if worker.wants_live(motion) and worker.stats.ready:
            await worker.ensure_live()

    async def close(self) -> None:
        self._running = False
        await asyncio.gather(
            *(worker.stop() for worker in self.workers.values()),
            return_exceptions=True,
        )
        self.workers.clear()


def _slugify(name: str) -> str:
    """Camera name -> RTSP path segment."""
    cleaned = "".join(c if c.isalnum() or c in "-_ " else "" for c in name)
    return cleaned.strip().lower().replace(" ", "_") or "camera"


def _device_type(camera: Any) -> str:
    """Map a blinkpy camera object to a liveview endpoint family.

    Only used to pick the URL; the actual protocol is always decided by the
    scheme of the URL that comes back.
    """
    product = str(getattr(camera, "product_type", "") or "").lower()
    if "mini" in product or "owl" in product:
        return "owl"
    if "doorbell" in product or "lotus" in product:
        return "lotus"
    class_name = type(camera).__name__.lower()
    if "mini" in class_name or "owl" in class_name:
        return "owl"
    if "doorbell" in class_name:
        return "lotus"
    return "camera"


def credentials_path(config: Config) -> Path:
    return config.paths.credentials
