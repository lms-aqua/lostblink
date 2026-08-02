"""IMMI protocol tests.

These are pure-function tests against the wire format documented in
``docs/protocol/02-immi-protocol.md`` -- no network, no Blink account. Every
offset asserted here is load-bearing: get one wrong and the server closes the
socket a second after connecting with no error message.
"""

from __future__ import annotations

import struct

import pytest

from lostblink.proxy.base import LiveTunnel
from lostblink.proxy.immi import (
    CONNECTION_HEADER_SIZE,
    HEADER_SIZE,
    KEEPALIVE_PACKET,
    LATENCY_PACKET,
    MSG_KEEPALIVE,
    MSG_LATENCY,
    MSG_VIDEO,
    ImmiTunnel,
    ImmiUrl,
    build_connection_header,
    is_immi_url,
)

SAMPLE_URL = (
    "immis://lv3-app-u002.immedia-semi.com:443/"
    "abc123def456ghi7__IMDS_SM2SERIAL01?client_id=247"
)


class TestImmiUrl:
    def test_parses_all_fields(self) -> None:
        url = ImmiUrl.parse(SAMPLE_URL)
        assert url.host == "lv3-app-u002.immedia-semi.com"
        assert url.port == 443
        assert url.connection_id == "abc123def456ghi7"
        assert url.serial == "SM2SERIAL01"
        assert url.client_id == 247

    def test_truncates_oversized_fields_to_16_bytes(self) -> None:
        # The protocol truncates rather than validating, so we must too --
        # otherwise we would write past the fixed-size header fields.
        url = ImmiUrl.parse(
            "immis://h/" + "c" * 40 + "__IMDS_" + "s" * 40 + "?client_id=1"
        )
        assert len(url.connection_id) == 16
        assert len(url.serial) == 16

    def test_missing_serial_is_allowed(self) -> None:
        url = ImmiUrl.parse("immis://host/conn123?client_id=5")
        assert url.connection_id == "conn123"
        assert url.serial == ""

    def test_missing_client_id_defaults_to_zero(self) -> None:
        assert ImmiUrl.parse("immis://host/conn123").client_id == 0

    def test_non_numeric_client_id_defaults_to_zero(self) -> None:
        assert ImmiUrl.parse("immis://host/conn?client_id=abc").client_id == 0

    def test_default_port_is_443(self) -> None:
        assert ImmiUrl.parse("immis://host/conn").port == 443

    @pytest.mark.parametrize("url", ["rtsps://host/path", "https://host/x", "nonsense"])
    def test_rejects_non_immi(self, url: str) -> None:
        with pytest.raises(ValueError):
            ImmiUrl.parse(url)

    def test_rejects_empty_connection_id(self) -> None:
        with pytest.raises(ValueError, match="connection id"):
            ImmiUrl.parse("immis://host/__IMDS_serial")

    def test_str_redacts_the_credential(self) -> None:
        # The connection id IS the authorisation. It must never reach a log.
        rendered = str(ImmiUrl.parse(SAMPLE_URL))
        assert "abc123def456ghi7" not in rendered
        assert "lv3-app-u002.immedia-semi.com" in rendered


class TestConnectionHeader:
    def test_is_exactly_122_bytes(self) -> None:
        assert len(build_connection_header(247, "conn", "serial")) == 122
        assert CONNECTION_HEADER_SIZE == 122

    def test_field_offsets(self) -> None:
        header = build_connection_header(247, "CONNID0123456789", "SERIAL0123456789")

        assert struct.unpack_from(">I", header, 0)[0] == 0x28  # magic
        assert struct.unpack_from(">I", header, 4)[0] == 16  # serial length
        assert header[8:24] == b"SERIAL0123456789"
        assert struct.unpack_from(">I", header, 24)[0] == 247  # client id
        assert header[28] == 0x01
        assert header[29] == 0x08
        assert struct.unpack_from(">I", header, 30)[0] == 64  # token length
        assert struct.unpack_from(">I", header, 98)[0] == 16  # conn id length
        assert header[102:118] == b"CONNID0123456789"
        assert struct.unpack_from(">I", header, 118)[0] == 1  # trailer

    def test_token_field_is_all_zeros(self) -> None:
        # Not an oversight in the port: auth rides entirely on the connection id.
        header = build_connection_header(1, "c", "s")
        assert header[34:98] == bytes(64)

    def test_short_values_are_zero_padded(self) -> None:
        header = build_connection_header(1, "ab", "cd")
        assert header[8:24] == b"cd" + bytes(14)
        assert header[102:118] == b"ab" + bytes(14)

    def test_empty_serial_is_accepted(self) -> None:
        assert build_connection_header(1, "conn", "")[8:24] == bytes(16)

    def test_oversized_values_do_not_overflow_their_fields(self) -> None:
        header = build_connection_header(1, "c" * 99, "s" * 99)
        assert len(header) == 122
        assert header[24:28] == struct.pack(">I", 1)  # client id not clobbered
        assert struct.unpack_from(">I", header, 118)[0] == 1  # trailer intact


class TestKeepalivePackets:
    def test_keepalive_is_nine_zero_bytes_after_the_type(self) -> None:
        assert len(KEEPALIVE_PACKET) == HEADER_SIZE == 9
        assert KEEPALIVE_PACKET[0] == MSG_KEEPALIVE == 0x0A
        assert KEEPALIVE_PACKET[1:] == bytes(8)

    def test_latency_packet_shape(self) -> None:
        assert len(LATENCY_PACKET) == 33
        assert LATENCY_PACKET[0] == MSG_LATENCY == 0x12
        seq, length = struct.unpack_from(">II", LATENCY_PACKET, 1)
        assert seq == 1000
        assert length == 24
        assert len(LATENCY_PACKET) == HEADER_SIZE + length


def _frame(msg_type: int, payload: bytes, seq: int = 0) -> bytes:
    return bytes([msg_type]) + struct.pack(">II", seq, len(payload)) + payload


class _FakeReader:
    """Feeds a fixed list of chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


async def _drain(chunks: list[bytes]) -> bytes:
    """Run the de-framer over ``chunks`` and return everything it emitted."""
    tunnel = ImmiTunnel("host", "conn", client_id=1)
    tunnel._reader = _FakeReader(chunks)  # type: ignore[assignment]
    out = bytearray()
    tunnel._emit = lambda data: out.extend(data)  # type: ignore[method-assign]
    await tunnel._pump()
    return bytes(out)


class TestFrameStripping:
    @pytest.mark.asyncio
    async def test_extracts_video_payload(self) -> None:
        assert await _drain([_frame(MSG_VIDEO, b"\x47" + b"TS" * 10)]) == (
            b"\x47" + b"TS" * 10
        )

    @pytest.mark.asyncio
    async def test_drops_control_frames(self) -> None:
        chunks = [
            _frame(MSG_LATENCY, b"\x00" * 24),
            _frame(MSG_VIDEO, b"VIDEO"),
            _frame(MSG_KEEPALIVE, b""),
            _frame(0x99, b"unknown"),
            _frame(MSG_VIDEO, b"MORE"),
        ]
        assert await _drain(chunks) == b"VIDEOMORE"

    @pytest.mark.asyncio
    async def test_reassembles_a_payload_split_across_reads(self) -> None:
        # Payloads routinely exceed one TLS record. Treating each read as a whole
        # frame yields a stream that almost decodes and then desyncs.
        whole = _frame(MSG_VIDEO, b"A" * 5000)
        chunks = [whole[:9], whole[9:1000], whole[1000:4000], whole[4000:]]
        assert await _drain(chunks) == b"A" * 5000

    @pytest.mark.asyncio
    async def test_handles_a_header_split_mid_way(self) -> None:
        whole = _frame(MSG_VIDEO, b"payload")
        assert await _drain([whole[:3], whole[3:7], whole[7:]]) == b"payload"

    @pytest.mark.asyncio
    async def test_multiple_frames_in_one_read(self) -> None:
        blob = _frame(MSG_VIDEO, b"one") + _frame(MSG_VIDEO, b"two")
        assert await _drain([blob]) == b"onetwo"

    @pytest.mark.asyncio
    async def test_forwards_payloads_not_starting_with_the_ts_sync_byte(self) -> None:
        # Regression guard for the documented trap: filtering on 0x47 drops the
        # PAT/PMT tables, which costs you audio and delays stream start.
        assert await _drain([_frame(MSG_VIDEO, b"\x11\x22\x33")]) == b"\x11\x22\x33"

    @pytest.mark.asyncio
    async def test_zero_length_video_frame_is_harmless(self) -> None:
        chunks = [_frame(MSG_VIDEO, b""), _frame(MSG_VIDEO, b"after")]
        assert await _drain(chunks) == b"after"

    @pytest.mark.asyncio
    async def test_counts_frames_by_type(self) -> None:
        tunnel = ImmiTunnel("host", "conn")
        tunnel._reader = _FakeReader(  # type: ignore[assignment]
            [_frame(MSG_VIDEO, b"a"), _frame(MSG_VIDEO, b"b"), _frame(MSG_LATENCY, b"")]
        )
        tunnel._emit = lambda data: None  # type: ignore[method-assign]
        await tunnel._pump()
        assert tunnel.frame_stats == {MSG_VIDEO: 2, MSG_LATENCY: 1}


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("immis://h/c", True),
            ("immi://h/c", True),
            ("rtsps://h/c", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_immi_url(self, url: str | None, expected: bool) -> None:
        assert is_immi_url(url) is expected

    def test_from_url_builds_a_configured_tunnel(self) -> None:
        tunnel = ImmiTunnel.from_url(SAMPLE_URL)
        assert isinstance(tunnel, LiveTunnel)
        assert tunnel.host == "lv3-app-u002.immedia-semi.com"
        assert tunnel.connection_id == "abc123def456ghi7"
        assert tunnel.client_id == 247
        assert tunnel.protocol == "immi"
