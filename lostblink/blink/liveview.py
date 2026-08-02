"""Requesting and renewing Blink live-view sessions.

The REST side of live view. The transport side lives in :mod:`lostblink.proxy`.

Two things here are easy to get wrong and are the reason most attempts fail:

* The endpoint **version differs by device class** -- ``/v6`` for cameras,
  ``/v2`` for owls (Mini) and doorbells. The wrong one 404s.
* The liveview command must **not** be polled to completion. Unlike every other
  Blink command, it stays ``complete: false`` for the whole session -- the
  command *is* the session handle. Polling it blocks until timeout and throws
  away the stream URL that was already sitting in the POST response.

See ``docs/protocol/01-blink-rest-api.md`` and ``docs/protocol/04-session-lifecycle.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

#: Fall back to the older camera endpoint if v6 is rejected. blinkpy still ships
#: v5; Blink broke v5 once already (blinkpy#367) so we lead with v6.
CAMERA_API_VERSIONS = ("v6", "v5")
OWL_API_VERSION = "v2"
DOORBELL_API_VERSION = "v2"

#: Defaults used when the response omits them. Observed values are always these.
DEFAULT_DURATION = 300.0
DEFAULT_CONTINUE_INTERVAL = 30.0

#: Start the replacement session this many seconds before the current one is
#: killed, so the handover has time to reach a keyframe. See the overlap diagram
#: in docs/protocol/04-session-lifecycle.md.
RENEW_LEAD_SECONDS = 20.0

_BUSY_RE = re.compile(r"busy", re.IGNORECASE)
_URL_TAIL_RE = re.compile(r"://([^/]+)/")


class LiveViewError(RuntimeError):
    """The liveview request failed."""


class LiveViewBusy(LiveViewError):
    """The sync module is running another command. Retryable with backoff."""


class LiveViewRateLimited(LiveViewError):
    """Blink is rate-limiting us. Back off hard, account-wide."""


class LiveViewRefused(LiveViewError):
    """Local policy refused the request (battery, budget, cooldown)."""


def redact(url: str) -> str:
    """Strip the session credential out of a live-view URL for logging.

    The opaque path *is* the authorisation -- anyone who reads it from a log can
    watch the camera. Never log one whole.
    """
    match = _URL_TAIL_RE.search(url)
    scheme = url.split("://", 1)[0] if "://" in url else "?"
    return f"{scheme}://{match.group(1)}/…" if match else f"{scheme}://…"


@dataclass(frozen=True, slots=True)
class LiveSession:
    """A live-view grant from the API."""

    url: str
    command_id: int | None
    network_id: int | None
    duration: float = DEFAULT_DURATION
    continue_interval: float = DEFAULT_CONTINUE_INTERVAL
    started_at: float = field(default_factory=time.monotonic)

    @classmethod
    def from_response(
        cls, data: dict[str, Any], network_id: int | None = None
    ) -> LiveSession:
        url = data.get("server")
        if not url:
            message = data.get("message") or "no 'server' field in response"
            raise LiveViewError(f"liveview returned no stream url: {message}")
        return cls(
            url=url,
            command_id=data.get("command_id") or data.get("id"),
            network_id=data.get("network_id") or network_id,
            duration=_as_float(data.get("duration"), DEFAULT_DURATION),
            continue_interval=_as_float(
                data.get("continue_interval"), DEFAULT_CONTINUE_INTERVAL
            ),
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.duration - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def seconds_until_renew(self) -> float:
        """How long to wait before opening the replacement session."""
        return max(0.0, self.duration - RENEW_LEAD_SECONDS - self.elapsed)

    def __str__(self) -> str:
        return f"{redact(self.url)} ({self.remaining:.0f}s left)"


@dataclass(slots=True)
class LiveViewPolicy:
    """Safety rails. Live view is expensive in battery terms -- see the table in
    ``docs/protocol/04-session-lifecycle.md``.

    Defaults are deliberately conservative. A battery camera left in ``always``
    mode dies in one to three days.
    """

    max_sessions_per_hour: int = 6
    min_seconds_between_sessions: float = 60.0
    daily_seconds_budget: float = 1800.0
    min_battery: str = "low"  # refuse below this: "ok" > "low" > anything else

    _starts: list[float] = field(default_factory=list, repr=False)
    _spent_today: float = field(default=0.0, repr=False)
    _budget_day: int | None = field(default=None, repr=False)
    _last_refusal_log: float = field(default=0.0, repr=False)

    def check(self, *, battery: str | None = None) -> None:
        """Raise :class:`LiveViewRefused` if a session is not allowed right now."""
        now = time.monotonic()
        self._roll_day()

        if battery is not None and self.min_battery == "ok" and battery != "ok":
            raise LiveViewRefused(f"battery is {battery!r}, policy requires 'ok'")

        if self._starts and now - self._starts[-1] < self.min_seconds_between_sessions:
            wait = self.min_seconds_between_sessions - (now - self._starts[-1])
            raise LiveViewRefused(f"cooling down, {wait:.0f}s remaining")

        cutoff = now - 3600.0
        self._starts = [t for t in self._starts if t > cutoff]
        if len(self._starts) >= self.max_sessions_per_hour:
            raise LiveViewRefused(
                f"hourly limit reached ({self.max_sessions_per_hour} sessions)"
            )

        if self._spent_today >= self.daily_seconds_budget:
            raise LiveViewRefused(
                f"daily budget spent ({self.daily_seconds_budget:.0f}s)"
            )

    def record_start(self) -> None:
        self._roll_day()
        self._starts.append(time.monotonic())

    def record_duration(self, seconds: float) -> None:
        self._roll_day()
        self._spent_today += max(0.0, seconds)

    def should_log_refusal(self) -> bool:
        """Rate-limit refusal logging to once an hour, not once per attempt."""
        now = time.monotonic()
        if now - self._last_refusal_log < 3600.0:
            return False
        self._last_refusal_log = now
        return True

    def _roll_day(self) -> None:
        today = datetime.now(UTC).toordinal()
        if self._budget_day != today:
            self._budget_day = today
            self._spent_today = 0.0


class LiveViewClient:
    """Issues liveview requests against a blinkpy ``Blink`` instance.

    We reach through blinkpy for the HTTP layer rather than reimplementing auth:
    auth is the fastest-moving part of Blink's surface and the part most likely
    to get an account flagged.
    """

    def __init__(self, blink: Any) -> None:
        self._blink = blink

    async def request(
        self,
        *,
        network_id: int,
        device_id: int,
        device_type: str = "camera",
        timeout: float = 30.0,
    ) -> LiveSession:
        """Ask for a live-view session, retrying past a busy sync module.

        ``device_type`` is one of ``camera``, ``owl`` (Mini) or ``lotus``
        (doorbell) -- it selects both the URL segment and the API version.
        """
        deadline = time.monotonic() + timeout
        delay = 1.0
        last_error: Exception | None = None

        for endpoint in self._endpoints(network_id, device_id, device_type):
            while True:
                try:
                    data = await self._post(endpoint)
                except LiveViewRateLimited:
                    raise
                except LiveViewError as exc:
                    last_error = exc
                    break  # try the next API version

                message = str(data.get("message") or "")
                if message and _BUSY_RE.search(message):
                    if time.monotonic() >= deadline:
                        raise LiveViewBusy(f"sync module still busy: {message}")
                    log.info("liveview busy, retrying in %.0fs: %s", delay, message)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
                    continue

                session = LiveSession.from_response(data, network_id)
                log.info("live session granted: %s", session)
                return session

        raise LiveViewError(f"liveview request failed: {last_error}")

    def _endpoints(
        self, network_id: int, device_id: int, device_type: str
    ) -> list[str]:
        account = self._blink.account_id
        base = f"/api/{{v}}/accounts/{account}/networks/{network_id}"

        if device_type == "owl":
            return [f"{base.format(v=OWL_API_VERSION)}/owls/{device_id}/liveview"]
        if device_type in ("lotus", "doorbell"):
            return [
                f"{base.format(v=DOORBELL_API_VERSION)}/doorbells/{device_id}/liveview"
            ]
        return [
            f"{base.format(v=version)}/cameras/{device_id}/liveview"
            for version in CAMERA_API_VERSIONS
        ]

    async def _post(self, endpoint: str) -> dict[str, Any]:
        """POST the liveview intent and return the decoded body.

        Note the deliberate difference from blinkpy's own
        ``api.request_camera_liveview``: that helper calls ``wait_for_command``,
        which polls the command to completion. The liveview command never
        completes while the session is alive, so that call blocks until timeout
        and discards the stream URL. We must not poll.

        ``http_post``'s ``data`` is a raw string body and ``json`` is a boolean
        selecting whether to decode the response -- not the payload.
        """
        from blinkpy import api  # imported late; blinkpy is heavy

        url = f"{self._blink.urls.base_url}{endpoint}"
        body = json.dumps({"intent": "liveview", "motion_event_start_time": ""})
        response = await api.http_post(self._blink, url, data=body, json=True)

        if isinstance(response, dict):
            return response

        status = getattr(response, "status", None)
        if status == 429:
            raise LiveViewRateLimited("rate limited by Blink (429)")
        if status == 409:
            raise LiveViewBusy("sync module busy (409)")
        raise LiveViewError(f"unexpected liveview response: status={status}")

    async def stop(self, session: LiveSession) -> None:
        """Best-effort release of the command so the camera stops streaming.

        Failure here is not worth surfacing: the session expires on its own, and
        we are usually already tearing down.
        """
        if session.command_id is None or session.network_id is None:
            return
        try:
            # blinkpy 0.23 has no request_command_done helper, so post directly.
            from blinkpy import api

            url = (
                f"{self._blink.urls.base_url}/network/{session.network_id}"
                f"/command/{session.command_id}/done/"
            )
            await api.http_post(self._blink, url)
            log.debug("released live command %s", session.command_id)
        except Exception as exc:
            log.debug("could not release live command: %s", exc)


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def utcnow() -> datetime:
    """Timezone-aware now.

    Upstream compares a naive ``datetime.now()`` against Blink's UTC timestamps
    (bug B-14), which silently shifts the query window by the local offset.
    Everything here is aware, always.
    """
    return datetime.now(UTC)


def utc_days_ago(days: float) -> datetime:
    return utcnow() - timedelta(days=days)
