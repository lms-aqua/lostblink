"""Publishing live MPEG-TS to MediaMTX, across session boundaries.

Blink hard-caps a live session at 300 seconds. The naive response -- let it die,
then open a new one -- leaves a two-to-five second hole while the liveview
request round-trips and TLS reconnects, and Frigate treats a hole like that as a
stream failure.

So the ffmpeg process publishing to MediaMTX is **never restarted**. Only the
tunnel feeding its stdin is swapped, at a keyframe, with the replacement session
already warm. Full diagram in ``docs/protocol/04-session-lifecycle.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..media.ffmpeg import FFmpeg
from ..proxy import LiveTunnel, TunnelError

log = logging.getLogger(__name__)

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

#: Give a replacement tunnel this long to produce a keyframe before giving up
#: on a seamless handover and just cutting across.
HANDOVER_TIMEOUT = 15.0

#: Cap on bytes buffered while waiting for the splice point. At Blink bitrates
#: this is several seconds of video -- far more than a handover needs.
_MAX_SPLICE_BUFFER = 4 * 1024 * 1024

TunnelFactory = Callable[[], Awaitable[LiveTunnel]]


def find_keyframe_offset(data: bytes, start: int = 0) -> int | None:
    """Return the offset of a TS packet that begins a random access point.

    Splicing mid-GOP produces visible corruption -- the decoder is handed
    P-frames referencing a reference picture from a stream that no longer
    exists. MPEG-TS marks the safe points for us: the adaptation field's
    ``random_access_indicator`` flag.

    Layout of the bytes we care about::

        [0] 0x47                       sync
        [1] bit 6 = payload_unit_start_indicator
        [3] bits 5-4 = adaptation_field_control (2 or 3 => AF present)
        [4] adaptation_field_length
        [5] bit 6 = random_access_indicator
    """
    offset = _resync(data, start)
    if offset is None:
        return None

    while offset + TS_PACKET_SIZE <= len(data):
        if data[offset] != TS_SYNC_BYTE:
            nxt = _resync(data, offset + 1)
            if nxt is None:
                return None
            offset = nxt
            continue

        adaptation = (data[offset + 3] >> 4) & 0x03
        has_af = adaptation in (0b10, 0b11)
        payload_start = bool(data[offset + 1] & 0x40)

        if has_af and payload_start:
            af_length = data[offset + 4]
            if af_length > 0 and data[offset + 5] & 0x40:  # random_access_indicator
                return offset

        offset += TS_PACKET_SIZE

    return None


def _resync(data: bytes, start: int) -> int | None:
    """Find a plausible TS packet boundary, verified against the next packet."""
    offset = start
    while True:
        offset = data.find(bytes([TS_SYNC_BYTE]), offset)
        if offset < 0:
            return None
        nxt = offset + TS_PACKET_SIZE
        # A lone 0x47 is common inside payload data; require the next sync too.
        if nxt + 1 > len(data) or data[nxt] == TS_SYNC_BYTE:
            return offset
        offset += 1


class LivePublisher:
    """Keeps one ffmpeg process publishing while tunnels come and go underneath."""

    def __init__(
        self,
        name: str,
        rtsp_url: str,
        ffmpeg: FFmpeg,
        *,
        transport: str = "tcp",
    ) -> None:
        self.name = name
        self.rtsp_url = rtsp_url
        self._ffmpeg = ffmpeg
        self._transport = transport
        self._process: asyncio.subprocess.Process | None = None
        self._tunnel: LiveTunnel | None = None
        self._feeder: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.bytes_published = 0
        self.handovers = 0

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, tunnel: LiveTunnel) -> None:
        """Spawn ffmpeg and begin feeding it from ``tunnel``."""
        args = self._ffmpeg.live_publish_args(
            self.rtsp_url, transport=self._transport
        )
        log.debug("%s: exec %s", self.name, " ".join(args))
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tunnel = tunnel
        self._feeder = asyncio.create_task(
            self._feed(tunnel), name=f"feed-{self.name}"
        )
        # Held so the task is not garbage-collected mid-flight.
        self._stderr_task = asyncio.create_task(
            self._log_stderr(), name=f"stderr-{self.name}"
        )
        log.info("%s: publishing live to %s", self.name, self.rtsp_url)

    async def handover(self, tunnel: LiveTunnel) -> bool:
        """Swap the source to ``tunnel`` at its first keyframe.

        Returns ``True`` if the splice landed on a keyframe. ``False`` means we
        cut across anyway after :data:`HANDOVER_TIMEOUT` -- a brief glitch, but
        far better than dropping the RTSP session.
        """
        if not self.running:
            raise RuntimeError(f"{self.name}: publisher is not running")

        log.debug("%s: warming replacement tunnel", self.name)
        buffer = bytearray()
        clean = False
        splice_at = 0

        try:
            async with asyncio.timeout(HANDOVER_TIMEOUT):
                async for chunk in tunnel:
                    # Rescan only from just before the new bytes, so a long wait
                    # does not become quadratic in buffer size.
                    scan_from = max(0, len(buffer) - TS_PACKET_SIZE)
                    buffer += chunk
                    offset = find_keyframe_offset(buffer, scan_from)
                    if offset is not None:
                        splice_at = offset
                        clean = True
                        break
                    if len(buffer) > _MAX_SPLICE_BUFFER:
                        log.warning(
                            "%s: no keyframe in %d KiB, splicing anyway",
                            self.name,
                            len(buffer) // 1024,
                        )
                        break
        except TimeoutError:
            log.warning(
                "%s: replacement produced no keyframe in %.0fs, splicing anyway",
                self.name,
                HANDOVER_TIMEOUT,
            )

        # Stop the old feeder before writing anything from the new tunnel, or
        # the two interleave and the TS is unrecoverable.
        await self._stop_feeder()
        old, self._tunnel = self._tunnel, tunnel
        if old is not None:
            await old.close()

        self._write(bytes(buffer[splice_at:]))
        self._feeder = asyncio.create_task(
            self._feed(tunnel), name=f"feed-{self.name}"
        )
        self.handovers += 1
        log.info(
            "%s: handover #%d complete (%s)",
            self.name,
            self.handovers,
            "on keyframe" if clean else "mid-GOP, expect a brief glitch",
        )
        return clean

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True

        await self._stop_feeder()
        if self._tunnel is not None:
            await self._tunnel.close()
            self._tunnel = None

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            try:
                process.stdin.close()
            except (BrokenPipeError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(process.wait(), 5.0)
        except TimeoutError:
            log.debug("%s: ffmpeg did not exit, killing", self.name)
            process.kill()
            await process.wait()

        log.info(
            "%s: stopped after %.1f MiB, %d handover(s)",
            self.name,
            self.bytes_published / 1048576,
            self.handovers,
        )

    # -- internals ---------------------------------------------------------

    async def _feed(self, tunnel: LiveTunnel) -> None:
        try:
            async for chunk in tunnel:
                if not self._write(chunk):
                    return
                # Yield so a handover can interrupt us promptly.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except TunnelError as exc:
            log.warning("%s: tunnel ended: %s", self.name, exc)

    def _write(self, data: bytes) -> bool:
        process = self._process
        if not data or process is None or process.stdin is None:
            return False
        if process.returncode is not None:
            log.warning("%s: ffmpeg exited (%s)", self.name, process.returncode)
            return False
        try:
            process.stdin.write(data)
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            log.warning("%s: ffmpeg stdin closed: %s", self.name, exc)
            return False
        self.bytes_published += len(data)
        return True

    async def _stop_feeder(self) -> None:
        feeder, self._feeder = self._feeder, None
        if feeder is None:
            return
        feeder.cancel()
        try:
            await feeder
        except (asyncio.CancelledError, Exception):
            pass

    async def _log_stderr(self) -> None:
        """Surface ffmpeg's complaints, minus the expected start-up noise.

        The first bytes of a Blink stream are mid-GOP, so ffmpeg reliably emits
        "non-existing PPS" and similar until the first keyframe. That is normal
        and would otherwise fill the log on every session.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            if loop.time() - started < 2.0 and _is_startup_noise(text):
                log.debug("%s: ffmpeg: %s", self.name, text)
            else:
                log.warning("%s: ffmpeg: %s", self.name, text)


_STARTUP_NOISE = (
    "non-existing pps",
    "no frame",
    "decode_slice_header error",
    "missing picture in access unit",
    "corrupt decoded frame",
)


def _is_startup_noise(line: str) -> bool:
    lowered = line.lower()
    return any(fragment in lowered for fragment in _STARTUP_NOISE)
