"""The IMMI live-view protocol (``immis://``).

A Python port of ``src/lib/proxy.ts`` (class ``ImmiTunnel``) from
BitWise-0x/homebridge-blink-security, which is GPL-3.0. This file is therefore a
derivative work and ``lostblink`` is GPL-3.0. See ``NOTICE.md``.

Wire format is documented byte-by-byte in ``docs/protocol/02-immi-protocol.md``.
Short version::

    TLS to HOST:443
      -> 122-byte connection header (client)
      <- stream of 9-byte-framed messages
           type 0x00 VIDEO      : payload is MPEG-TS
           type 0x0A KEEPALIVE  : client -> server, every 10s
           type 0x12 LATENCY    : client -> server, every 1s

Used by Blink Mini / Mini 2 / Mini 2K+, Indoor and Outdoor (gen 3/4), the Video
Doorbell, and the wired Floodlight. XT/XT2 use RTSPS instead -- see ``rtsp.py``.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .base import LiveTunnel

log = logging.getLogger(__name__)

# -- frame constants ------------------------------------------------------

HEADER_SIZE = 9
MSG_VIDEO = 0x00
MSG_KEEPALIVE = 0x0A
MSG_LATENCY = 0x12

CONNECTION_HEADER_SIZE = 122
_MAGIC = 0x00000028
_TRAILER = 0x00000001
_SERIAL_LEN = 16
_TOKEN_LEN = 64
_CONNECTION_ID_LEN = 16

KEEPALIVE_INTERVAL = 10.0
LATENCY_INTERVAL = 1.0

#: 9-byte no-op: type 0x0A, zero sequence, zero payload length.
KEEPALIVE_PACKET = bytes([MSG_KEEPALIVE]) + bytes(8)

#: 33 bytes: type 0x12, seq=1000, len=24, then a 24-byte stats block whose only
#: non-zero byte is a 0x01 at payload offset 21. The server appears to care that
#: this arrives, not what it contains, so we send it verbatim as a constant --
#: which is exactly what the reference client does.
LATENCY_PACKET = bytes(
    [
        0x12, 0x00, 0x00, 0x03, 0xE8, 0x00, 0x00, 0x00, 0x18, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,
    ]
)

#: A single read is capped at this. TS payloads arrive in bursts; anything
#: larger just adds latency without improving throughput.
_READ_SIZE = 65536

_IMDS_RE = re.compile(r"__IMDS_(.+)$")


@dataclass(frozen=True, slots=True)
class ImmiUrl:
    """The parts of an ``immis://`` URL that the handshake needs.

    Layout::

        immis://HOST:443/CONNECTION_ID__IMDS_SERIAL?client_id=CAMERA_ID

    Both ``connection_id`` and ``serial`` are truncated to 16 bytes by the
    protocol, not validated -- longer values are silently cut, so we cut them
    here where it is visible.
    """

    host: str
    port: int
    connection_id: str
    serial: str
    client_id: int

    @classmethod
    def parse(cls, url: str) -> ImmiUrl:
        parts = urlsplit(url)
        if parts.scheme not in ("immi", "immis"):
            raise ValueError(f"not an IMMI url: {parts.scheme!r}")
        if not parts.hostname:
            raise ValueError("IMMI url has no host")

        path = parts.path.lstrip("/")
        connection_id = path.split("__", 1)[0]
        if not connection_id:
            raise ValueError("IMMI url has no connection id")

        match = _IMDS_RE.search(path)
        serial = match.group(1)[:_SERIAL_LEN] if match else ""

        raw_client_id = parse_qs(parts.query).get("client_id", ["0"])[0]
        try:
            client_id = int(raw_client_id)
        except ValueError:
            log.debug("IMMI url has non-numeric client_id %r, using 0", raw_client_id)
            client_id = 0

        return cls(
            host=parts.hostname,
            port=parts.port or 443,
            connection_id=connection_id[:_CONNECTION_ID_LEN],
            serial=serial,
            client_id=client_id,
        )

    def __str__(self) -> str:
        """Redacted. The connection id *is* the credential -- never log it whole."""
        tail = self.connection_id[-4:] if len(self.connection_id) > 4 else "?"
        return f"immis://{self.host}:{self.port}/…{tail} (client_id={self.client_id})"


def build_connection_header(
    client_id: int, connection_id: str, serial: str = ""
) -> bytes:
    """Build the 122-byte handshake blob.

    Big-endian, zero-filled, fixed offsets::

        [  0: 4] magic 0x00000028
        [  4: 8] serial length (16)
        [  8:24] serial, UTF-8 zero-padded
        [ 24:28] client id (uint32)
        [    28] 0x01
        [    29] 0x08
        [ 30:34] token length (64)
        [ 34:98] token -- ALL ZEROS, no auth token is required here
        [ 98:102] connection id length (16)
        [102:118] connection id, UTF-8 zero-padded
        [118:122] trailer 0x00000001

    The empty token field is not an oversight in the port: authorisation is
    carried entirely by the short-lived ``connection_id``.
    """
    buf = bytearray(CONNECTION_HEADER_SIZE)

    struct.pack_into(">I", buf, 0, _MAGIC)
    struct.pack_into(">I", buf, 4, _SERIAL_LEN)
    _write_padded(buf, 8, serial, _SERIAL_LEN)
    struct.pack_into(">I", buf, 24, client_id & 0xFFFFFFFF)
    buf[28] = 0x01
    buf[29] = 0x08
    struct.pack_into(">I", buf, 30, _TOKEN_LEN)
    # [34:98] token field stays zero.
    struct.pack_into(">I", buf, 98, _CONNECTION_ID_LEN)
    _write_padded(buf, 102, connection_id, _CONNECTION_ID_LEN)
    struct.pack_into(">I", buf, 118, _TRAILER)

    return bytes(buf)


def _write_padded(buf: bytearray, offset: int, value: str, size: int) -> None:
    """Write ``value`` as UTF-8 into a fixed-size zero-padded field."""
    if not value:
        return
    encoded = value.encode("utf-8")[:size]
    buf[offset : offset + len(encoded)] = encoded


class ImmiTunnel(LiveTunnel):
    """Streams MPEG-TS out of a Blink IMMI live-view session."""

    protocol = "immi"

    def __init__(
        self,
        host: str,
        connection_id: str,
        *,
        client_id: int = 0,
        serial: str = "",
        port: int = 443,
    ) -> None:
        super().__init__(host, port)
        self.connection_id = connection_id
        self.client_id = client_id
        self.serial = serial
        self._frame_counts: dict[int, int] = {}

    @classmethod
    def from_url(cls, url: str) -> ImmiTunnel:
        parsed = ImmiUrl.parse(url)
        log.debug("IMMI target: %s", parsed)
        return cls(
            parsed.host,
            parsed.connection_id,
            client_id=parsed.client_id,
            serial=parsed.serial,
            port=parsed.port,
        )

    async def _handshake(self) -> None:
        header = build_connection_header(
            self.client_id, self.connection_id, self.serial
        )
        self._send(header)
        if self._writer is not None:
            await self._writer.drain()
        log.debug(
            "IMMI handshake sent (%d bytes, client_id=%d, serial=%r)",
            len(header),
            self.client_id,
            self.serial,
        )

        # Both timers are mandatory. Missing the 10s keepalive gets the session
        # dropped at around the 30s continue_interval mark.
        self._spawn(
            self._keepalive(LATENCY_PACKET, LATENCY_INTERVAL, "latency"),
            name="immi-latency",
        )
        self._spawn(
            self._keepalive(KEEPALIVE_PACKET, KEEPALIVE_INTERVAL, "keepalive"),
            name="immi-keepalive",
        )

    async def _pump(self) -> None:
        """De-frame the 9-byte-header stream and emit VIDEO payloads.

        Payloads routinely span several TLS records, so ``remaining`` carries
        across reads. Treating each read as a whole frame produces a stream that
        almost decodes and then desyncs.
        """
        assert self._reader is not None
        buf = bytearray()
        remaining = 0
        msg_type = -1

        while True:
            chunk = await self._reader.read(_READ_SIZE)
            if not chunk:
                log.debug("IMMI: upstream closed")
                return
            buf += chunk

            while buf:
                if remaining > 0:
                    take = min(remaining, len(buf))
                    if msg_type == MSG_VIDEO:
                        # Forward every byte -- see the 0x47 warning in base._emit.
                        self._emit(bytes(buf[:take]))
                    del buf[:take]
                    remaining -= take
                    continue

                if len(buf) < HEADER_SIZE:
                    break  # partial header, wait for more

                msg_type = buf[0]
                seq, length = struct.unpack_from(">II", buf, 1)
                del buf[:HEADER_SIZE]
                remaining = length

                self._note_frame(msg_type, seq, length)

    def _note_frame(self, msg_type: int, seq: int, length: int) -> None:
        """Count frame types, and log the first sighting of anything unexpected."""
        seen = self._frame_counts.get(msg_type, 0)
        self._frame_counts[msg_type] = seen + 1
        if seen == 0 and msg_type not in (MSG_VIDEO, MSG_KEEPALIVE, MSG_LATENCY):
            log.debug(
                "IMMI: unknown frame type 0x%02x (seq=%d, len=%d)",
                msg_type,
                seq,
                length,
            )

    @property
    def frame_stats(self) -> dict[int, int]:
        """Frame counts by message type. Diagnostics only."""
        return dict(self._frame_counts)


def is_immi_url(url: str | None) -> bool:
    return bool(url) and url.startswith(("immi://", "immis://"))
