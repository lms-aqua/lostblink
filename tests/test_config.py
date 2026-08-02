"""Configuration loading tests, including the shipped example file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lostblink.config import (
    MIN_POLL_INTERVAL,
    CamerasConfig,
    Config,
    ConfigError,
    RtspConfig,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.json"


class TestExampleConfig:
    def test_the_shipped_example_actually_loads(self) -> None:
        # A broken example is the first thing every new user hits.
        config = Config.load(EXAMPLE)
        assert config.live.mode == "on_motion"
        assert config.rtsp.port == 8554
        assert config.paths.work == Path("/working")

    def test_example_defaults_to_the_battery_safe_mode(self) -> None:
        # 'always' on a battery camera flattens it in 1-3 days. Never the default.
        assert Config.load(EXAMPLE).live.mode != "always"


class TestLoading:
    def test_missing_file_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "absent.json")

    def test_malformed_json_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            Config.load(path)

    def test_a_json_array_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "arr.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigError, match="JSON object"):
            Config.load(path)

    def test_an_empty_object_yields_all_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("{}", encoding="utf-8")
        config = Config.load(path)
        assert config.live.mode == "off"
        assert config.blink.poll_interval == 30.0

    def test_unknown_keys_are_ignored_not_fatal(self, tmp_path: Path) -> None:
        # A typo should tell you it did nothing, not refuse to start.
        path = tmp_path / "typo.json"
        path.write_text(json.dumps({"rtsp": {"prot": 9999}}), encoding="utf-8")
        assert Config.load(path).rtsp.port == 8554

    def test_accepts_the_legacy_rtsp_server_key(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps({"rtsp_server": {"address": "mtx", "port": 9554}}),
            encoding="utf-8",
        )
        assert Config.load(path).rtsp.port == 9554

    def test_credentials_come_from_the_environment_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # So they can be Docker secrets rather than sitting in a bind mount.
        monkeypatch.setenv("LOSTBLINK_USERNAME", "env@example.com")
        monkeypatch.setenv("LOSTBLINK_PASSWORD", "envpass")
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"blink": {"login": {"username": "file@x", "password": "fp"}}}),
            encoding="utf-8",
        )
        config = Config.load(path)
        assert config.blink.username == "env@example.com"
        assert config.blink.password == "envpass"

    def test_credentials_fall_back_to_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"blink": {"login": {"username": "a@b.c", "password": "pw"}}}),
            encoding="utf-8",
        )
        config = Config.load(path)
        assert config.blink.username == "a@b.c"


class TestValidation:
    def test_poll_interval_is_clamped_to_the_floor(self, tmp_path: Path) -> None:
        # Faster does not get fresher data (blinkpy throttles) but does risk a
        # temporary ban -- upstream ships poll_interval: 1 (bug B-13).
        path = tmp_path / "fast.json"
        path.write_text(json.dumps({"blink": {"poll_interval": 1}}), encoding="utf-8")
        assert Config.load(path).blink.poll_interval == MIN_POLL_INTERVAL

    def test_rejects_an_unknown_live_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"live": {"mode": "turbo"}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="live.mode"):
            Config.load(path)

    def test_rejects_non_positive_history_days(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        path.write_text(json.dumps({"blink": {"history_days": 0}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="history_days"):
            Config.load(path)


class TestCameraSelection:
    def test_empty_enabled_means_everything(self) -> None:
        cameras = CamerasConfig()
        assert cameras.selected(["Front", "Back"]) == ["Front", "Back"]

    def test_disabled_wins_over_enabled(self) -> None:
        cameras = CamerasConfig(enabled=["Front", "Back"], disabled=["Back"])
        assert cameras.selected(["Front", "Back"]) == ["Front"]

    def test_matching_is_case_insensitive(self) -> None:
        # The Blink app is inconsistent about capitalisation and this is a
        # recurring support question.
        cameras = CamerasConfig(enabled=["front door"])
        assert cameras.selected(["Front Door"]) == ["Front Door"]

    def test_unknown_enabled_name_is_skipped_not_fatal(self) -> None:
        cameras = CamerasConfig(enabled=["Front", "Ghost"])
        assert cameras.selected(["Front"]) == ["Front"]

    def test_disabling_everything_yields_nothing(self) -> None:
        cameras = CamerasConfig(disabled=["Front", "Back"])
        assert cameras.selected(["Front", "Back"]) == []


class TestRtspConfig:
    def test_builds_a_stream_url(self) -> None:
        rtsp = RtspConfig(address="mediamtx", port=8554)
        assert rtsp.url_for("front_door") == "rtsp://mediamtx:8554/front_door"
