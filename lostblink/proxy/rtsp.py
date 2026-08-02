"""The RTSPS live-view path (``rtsps://``) for XT / XT2 cameras.

A Python port of ``src/lib/proxy.ts`` (class ``RtspToH264Proxy``) from
BitWise-0x/homebridge-blink-security, which is GPL-3.0. Derivative work; see
``NOTICE.md``.

We speak RTSP ourselves rather than handing the URL to ffmpeg, because ffmpeg
cannot do this one. Two independent reasons, both fatal (full write-up in
``docs/protocol/03-rtsp-mpegts.md``):

1. Blink's server starts pushing interleaved RTP as soon as ``SETUP`` completes,
   before ``PLAY``. ffmpeg's RTSP state machine discards everything received
   before it reaches ``RTSP_STATE_STREAMING`` -- and the initial burst contains
   the IDR keyframe. No flag changes this; the discard happens below the demuxer
   options. The result is the long-running "SIGILL / can't accept the MPEG-TS
   fragments" folklore.

2. The payload is not H.264. The SDP advertises ``a=rtpmap:33 MP2T/90000`` --
   MPEG-2 Transport Stream over RTP. Reassembling it as RFC 6184 NAL units, which
   is what most integrations try, produces garbage.

So: negotiate, strip the interleaved framing, strip the RTP headers, and emit the
MPEG-TS underneath. Layering is
``TLS -> RTSP interleaved -> RTP -> MPEG-TS -> H.264 + AAC``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .base import LiveTunnel, TunnelError

log = logging.getLogger(__name__)

#: The reference client identifies itself as the Blink player. Keep it.
USER_AGENT = "Immedia WalnutPlayer"

RESPONSE_TIMEOUT = 10.0
_READ_SIZE = 65536

_INTERLEAVE_MARKER = 0x24  # '$'
_VIDEO_CHANNEL = 0x00  # matches interleaved=0-1 requested in SETUP
_RTP_MIN = 12

_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)
_VIDEO_SECTION_RE = re.compile(r"m=video[\s\S]*?(?=^m=|\Z)", re.MULTILINE)
_CONTROL_RE = re.compile(r"^a=control:(.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class RtspUrl:
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, url: str) -> RtspUrl:
        parts = urlsplit(url)
        if parts.scheme not in ("rtsp", "rtsps"):
            raise ValueError(f"not an RTSP url: {parts.scheme!r}")
        if not parts.hostname:
            raise ValueError("RTSP url has no host")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return cls(host=parts.hostname, port=parts.port or 443, path=path)

    def __str__(self) -> str:
        """Redacted -- the path is the session credential."""
        return f"rtsps://{self.host}:{self.port}/…"


def extract_track_url(sdp: str, base_uri: str) -> str:
    """Pull the video track's control URL out of the SDP.

    Guessing ``/trackID=0`` works on some Blink units and 454s on others, so we
    always read it from the ``m=video`` section's ``a=control:``. Absolute values
    are used as-is; relative ones are appended to the base URI.
    """
    section = _VIDEO_SECTION_RE.search(sdp)
    if not section:
        log.debug("RTSP: no m=video section in SDP, falling back to base URI")
        return base_uri

    control = _CONTROL_RE.search(section.group(0))
    if not control:
        return base_uri

    value = control.group(1).strip()
    if value.startswith(("rtsp://", "rtsps://")):
        return value
    if value == "*":
        return base_uri
    return f"{base_uri.rstrip('/')}/{value}"


class RtspTunnel(LiveTunnel):
    """Negotiates RTSP over TLS and emits the MPEG-TS carried inside RTP."""

    protocol = "rtsp"

    def __init__(self, host: str, path: str, *, port: int = 443) -> None:
        super().__init__(host, port)
        self.path = path
        self._buf = bytearray()
        self._cseq = 0
        self._session: str | None = None
        self._rtp_frames = 0

    @classmethod
    def from_url(cls, url: str) -> RtspTunnel:
        parsed = RtspUrl.parse(url)
        log.debug("RTSP target: %s", parsed)
        return cls(parsed.host, parsed.path, port=parsed.port)

    @property
    def _uri(self) -> str:
        # Request-URIs use the rtsp:// scheme even though the socket is TLS.
        return f"rtsp://{self.host}{self.path}"

    async def _handshake(self) -> None:
        uri = self._uri

        # OPTIONS is informational; some units do not implement it. Never fatal.
        try:
            await self._request("OPTIONS", uri)
        except (TimeoutError, TunnelError) as exc:
            log.debug("RTSP: OPTIONS failed, continuing: %s", exc)

        _, sdp = await self._request(
            "DESCRIBE", uri, headers={"Accept": "application/sdp"}
        )
        if not sdp:
            raise TunnelError("RTSP: DESCRIBE returned no SDP")

        track_url = extract_track_url(sdp, uri)
        log.debug("RTSP: setup track %s", track_url.rsplit("/", 1)[-1])

        # Interleaved TCP is mandatory: we are inside a TLS tunnel, so UDP is
        # not available to us.
        setup_headers, _ = await self._request(
            "SETUP",
            track_url,
            headers={"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        session = _header_value(setup_headers, "Session")
        if session:
            # Strip any ";timeout=60" suffix.
            self._session = session.split(";", 1)[0].strip()

        # Send PLAY even though this server has almost certainly already
        # started: not every unit auto-plays, and it is harmless on those that do.
        play_headers = {"Range": "npt=0.000-"}
        if self._session:
            play_headers["Session"] = self._session
        await self._request("PLAY", uri, headers=play_headers)

        log.debug("RTSP: negotiated, %d bytes already buffered", len(self._buf))

    async def _pump(self) -> None:
        """Drain interleaved RTP frames and emit their MPEG-TS payloads."""
        assert self._reader is not None

        # Anything left over from negotiation is usually already video.
        self._consume_interleaved()

        while True:
            chunk = await self._reader.read(_READ_SIZE)
            if not chunk:
                log.debug("RTSP: upstream closed after %d frames", self._rtp_frames)
                return
            self._buf += chunk
            self._consume_interleaved()

    # -- RTSP request/response --------------------------------------------

    async def _request(
        self, method: str, uri: str, headers: dict[str, str] | None = None
    ) -> tuple[bytes, str]:
        """Send an RTSP request and return ``(raw_headers, body)``."""
        self._cseq += 1
        lines = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {self._cseq}",
            f"User-Agent: {USER_AGENT}",
        ]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

        self._send(request)
        if self._writer is not None:
            await self._writer.drain()

        try:
            raw_headers, body = await asyncio.wait_for(
                self._read_response(), RESPONSE_TIMEOUT
            )
        except TimeoutError:
            raise TunnelError(f"RTSP: {method} timed out") from None

        status = raw_headers.split(b"\r\n", 1)[0].decode("ascii", "replace")
        log.debug("RTSP: %s -> %s", method, status)
        if " 200 " not in status and method != "OPTIONS":
            raise TunnelError(f"RTSP: {method} failed: {status}")

        return raw_headers, body

    async def _read_response(self) -> tuple[bytes, str]:
        """Read one RTSP text response, skipping interleaved video as it arrives.

        This is the part that catches people out. Because the server auto-plays
        after SETUP, binary RTP frames are already arriving and are interleaved
        with the text responses we are waiting for. If you try to parse those
        bytes as ASCII headers, DESCRIBE appears to hang forever.
        """
        assert self._reader is not None

        while True:
            self._skip_interleaved_frames()

            end = self._buf.find(b"\r\n\r\n")
            if end != -1:
                header_block = bytes(self._buf[:end])
                match = _CONTENT_LENGTH_RE.search(header_block)
                content_length = int(match.group(1)) if match else 0
                body_start = end + 4

                if len(self._buf) >= body_start + content_length:
                    body = bytes(
                        self._buf[body_start : body_start + content_length]
                    ).decode("utf-8", "replace")
                    del self._buf[: body_start + content_length]
                    return header_block, body

            chunk = await self._reader.read(_READ_SIZE)
            if not chunk:
                raise TunnelError("RTSP: connection closed during negotiation")
            self._buf += chunk

    def _skip_interleaved_frames(self) -> None:
        """Drop any ``$``-framed binary sitting in front of an RTSP response."""
        while self._buf and self._buf[0] == _INTERLEAVE_MARKER:
            if len(self._buf) < 4:
                return
            frame_len = 4 + int.from_bytes(self._buf[2:4], "big")
            if len(self._buf) < frame_len:
                return
            del self._buf[:frame_len]

    # -- interleaved RTP de-framing ---------------------------------------

    def _consume_interleaved(self) -> None:
        """Strip ``$`` framing and RTP headers; emit the MPEG-TS remainder."""
        buf = self._buf

        while buf:
            if buf[0] != _INTERLEAVE_MARKER:
                # Mid-stream desync. Resynchronise on the next marker rather
                # than aborting -- this is recoverable.
                nxt = buf.find(_INTERLEAVE_MARKER, 1)
                if nxt < 0:
                    buf.clear()
                    return
                log.debug("RTSP: resynchronising, skipped %d bytes", nxt)
                del buf[:nxt]
                continue

            if len(buf) < 4:
                return
            payload_len = int.from_bytes(buf[2:4], "big")
            frame_len = 4 + payload_len
            if len(buf) < frame_len:
                return  # partial frame, wait for more

            channel = buf[1]
            if channel == _VIDEO_CHANNEL and payload_len > _RTP_MIN:
                # Copy the packet out before touching `buf`: a live memoryview
                # export pins the bytearray and makes `del buf[...]` raise
                # BufferError.
                packet = bytes(buf[4:frame_len])
                payload = _rtp_payload(memoryview(packet))
                if payload is not None:
                    self._emit(bytes(payload))
                self._rtp_frames += 1
            # channel 1 is RTCP -- nothing useful for us.

            del buf[:frame_len]


def _header_value(raw_headers: bytes, name: str) -> str | None:
    """Pull a single header out of a raw RTSP response block, case-insensitively."""
    needle = name.lower().encode("ascii")
    for line in raw_headers.split(b"\r\n"):
        key, sep, value = line.partition(b":")
        if sep and key.strip().lower() == needle:
            return value.strip().decode("ascii", "replace")
    return None


def _rtp_payload(packet: memoryview) -> memoryview | None:
    """Return the payload of an RTP packet, or ``None`` if it has none.

    Header length is not a fixed 12 bytes: CSRC entries and a header extension
    both push the payload back, and a fixed offset silently corrupts the TS.
    """
    if len(packet) < _RTP_MIN:
        return None

    first = packet[0]
    header_len = _RTP_MIN + (first & 0x0F) * 4  # CSRC count

    if first & 0x10:  # X -- header extension present
        if len(packet) < header_len + 4:
            return None
        ext_words = int.from_bytes(packet[header_len + 2 : header_len + 4], "big")
        header_len += 4 + ext_words * 4

    end = len(packet)
    if first & 0x20:  # P -- padding present
        pad = packet[end - 1]
        if 0 < pad <= end - header_len:
            end -= pad

    if header_len >= end:
        return None
    return packet[header_len:end]


def is_rtsp_url(url: str | None) -> bool:
    return bool(url) and url.startswith(("rtsp://", "rtsps://"))
