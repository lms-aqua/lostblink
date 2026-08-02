"""RTSPS de-framing tests.

Covers the layering documented in ``docs/protocol/03-rtsp-mpegts.md``:
``TLS -> RTSP interleaved -> RTP -> MPEG-TS``. The RTP header-length maths in
particular is where a fixed-12-byte assumption silently corrupts the stream.
"""

from __future__ import annotations

import struct

import pytest

from lostblink.proxy.rtsp import (
    USER_AGENT,
    RtspTunnel,
    RtspUrl,
    _rtp_payload,
    extract_track_url,
    is_rtsp_url,
)

SAMPLE_URL = (
    "rtsps://lv3-app-u002.immedia-semi.com:443/"
    "opaque-session-path?client_id=247&blinkRTSP=true"
)

SDP = """v=0
o=- 0 0 IN IP4 127.0.0.1
s=Blink
m=video 0 RTP/AVP 33
a=rtpmap:33 MP2T/90000
a=control:trackID=0
m=audio 0 RTP/AVP 97
a=control:trackID=1
"""


class TestRtspUrl:
    def test_parses_host_port_and_path(self) -> None:
        url = RtspUrl.parse(SAMPLE_URL)
        assert url.host == "lv3-app-u002.immedia-semi.com"
        assert url.port == 443
        assert url.path.startswith("/opaque-session-path")

    def test_keeps_the_query_string_in_the_path(self) -> None:
        # Blink's opaque path includes the query; dropping it 404s.
        assert "client_id=247" in RtspUrl.parse(SAMPLE_URL).path

    def test_defaults_to_port_443(self) -> None:
        assert RtspUrl.parse("rtsps://host/path").port == 443

    @pytest.mark.parametrize("url", ["immis://h/c", "http://h/c", "garbage"])
    def test_rejects_non_rtsp(self, url: str) -> None:
        with pytest.raises(ValueError):
            RtspUrl.parse(url)

    def test_str_redacts_the_path(self) -> None:
        assert "opaque-session-path" not in str(RtspUrl.parse(SAMPLE_URL))

    def test_request_uri_uses_rtsp_scheme_over_a_tls_socket(self) -> None:
        # Correct RTSP-over-TLS behaviour: the socket is TLS, the URI is rtsp://.
        tunnel = RtspTunnel.from_url(SAMPLE_URL)
        assert tunnel._uri.startswith("rtsp://")
        assert not tunnel._uri.startswith("rtsps://")


class TestSdpParsing:
    def test_takes_control_from_the_video_section_not_the_audio_one(self) -> None:
        assert extract_track_url(SDP, "rtsp://h/base") == "rtsp://h/base/trackID=0"

    def test_absolute_control_url_is_used_verbatim(self) -> None:
        sdp = "m=video 0 RTP/AVP 33\na=control:rtsp://other/track9\n"
        assert extract_track_url(sdp, "rtsp://h/base") == "rtsp://other/track9"

    def test_falls_back_to_base_uri_when_there_is_no_video_section(self) -> None:
        assert extract_track_url("v=0\n", "rtsp://h/base") == "rtsp://h/base"

    def test_falls_back_when_the_video_section_has_no_control(self) -> None:
        sdp = "m=video 0 RTP/AVP 33\na=rtpmap:33 MP2T/90000\n"
        assert extract_track_url(sdp, "rtsp://h/base") == "rtsp://h/base"

    def test_asterisk_control_means_the_base_uri(self) -> None:
        sdp = "m=video 0 RTP/AVP 33\na=control:*\n"
        assert extract_track_url(sdp, "rtsp://h/base") == "rtsp://h/base"

    def test_does_not_duplicate_a_trailing_slash(self) -> None:
        sdp = "m=video 0 RTP/AVP 33\na=control:trackID=0\n"
        assert extract_track_url(sdp, "rtsp://h/base/") == "rtsp://h/base/trackID=0"


def _rtp(payload: bytes, *, csrc: int = 0, ext: bytes = b"", pad: int = 0) -> bytes:
    """Build an RTP packet with optional CSRCs, extension and padding."""
    flags = 0x80 | (csrc & 0x0F)
    if ext:
        flags |= 0x10
    if pad:
        flags |= 0x20

    packet = bytearray([flags, 33])  # PT 33 = MP2T
    packet += struct.pack(">HII", 1, 0, 0)  # seq, timestamp, ssrc
    packet += bytes(csrc * 4)
    if ext:
        packet += struct.pack(">HH", 0xBEDE, len(ext) // 4) + ext
    packet += payload
    if pad:
        packet += bytes(pad - 1) + bytes([pad])
    return bytes(packet)


def _interleaved(payload: bytes, channel: int = 0) -> bytes:
    return b"$" + bytes([channel]) + struct.pack(">H", len(payload)) + payload


class TestRtpPayload:
    def test_plain_packet(self) -> None:
        assert bytes(_rtp_payload(memoryview(_rtp(b"TSDATA")))) == b"TSDATA"

    def test_csrc_entries_shift_the_payload(self) -> None:
        # A fixed 12-byte header would return 4 bytes of CSRC as video data.
        assert bytes(_rtp_payload(memoryview(_rtp(b"TSDATA", csrc=1)))) == b"TSDATA"

    def test_header_extension_shifts_the_payload(self) -> None:
        packet = _rtp(b"TSDATA", ext=b"\x00\x01\x02\x03")
        assert bytes(_rtp_payload(memoryview(packet))) == b"TSDATA"

    def test_csrc_and_extension_together(self) -> None:
        packet = _rtp(b"TSDATA", csrc=2, ext=b"\x00" * 8)
        assert bytes(_rtp_payload(memoryview(packet))) == b"TSDATA"

    def test_padding_is_stripped(self) -> None:
        assert bytes(_rtp_payload(memoryview(_rtp(b"TSDATA", pad=4)))) == b"TSDATA"

    def test_runt_packet_yields_nothing(self) -> None:
        assert _rtp_payload(memoryview(b"\x80\x21\x00")) is None

    def test_header_only_packet_yields_nothing(self) -> None:
        assert _rtp_payload(memoryview(_rtp(b""))) is None

    def test_absurd_padding_length_is_ignored_rather_than_underrunning(self) -> None:
        packet = bytearray(_rtp(b"ABCD"))
        packet[0] |= 0x20
        packet[-1] = 200  # claims more padding than the packet holds
        payload = _rtp_payload(memoryview(bytes(packet)))
        # The bogus length is ignored rather than slicing back into the header.
        assert payload is not None
        assert len(payload) == 4
        assert bytes(payload).startswith(b"ABC")


def _consume(chunks: list[bytes]) -> bytes:
    """Feed chunks through the interleaved de-framer and collect the output."""
    tunnel = RtspTunnel("host", "/path")
    out = bytearray()
    tunnel._emit = lambda data: out.extend(data)  # type: ignore[method-assign]
    for chunk in chunks:
        tunnel._buf += chunk
        tunnel._consume_interleaved()
    return bytes(out)


class TestInterleavedDeframing:
    def test_extracts_mpegts_from_a_video_frame(self) -> None:
        assert _consume([_interleaved(_rtp(b"\x47TSPAYLOAD"))]) == b"\x47TSPAYLOAD"

    def test_ignores_rtcp_on_channel_one(self) -> None:
        chunks = [
            _interleaved(_rtp(b"VIDEO"), channel=0),
            _interleaved(b"\x80\xc8" + bytes(30), channel=1),
            _interleaved(_rtp(b"MORE"), channel=0),
        ]
        assert _consume(chunks) == b"VIDEOMORE"

    def test_reassembles_a_frame_split_across_reads(self) -> None:
        whole = _interleaved(_rtp(b"SPLITPAYLOAD"))
        assert _consume([whole[:2], whole[2:10], whole[10:]]) == b"SPLITPAYLOAD"

    def test_multiple_frames_in_one_read(self) -> None:
        blob = _interleaved(_rtp(b"one")) + _interleaved(_rtp(b"two"))
        assert _consume([blob]) == b"onetwo"

    def test_resynchronises_after_junk(self) -> None:
        # A mid-stream desync is recoverable; we scan to the next '$' rather
        # than aborting the session.
        assert _consume([b"\x00\x01\x02junk" + _interleaved(_rtp(b"OK"))]) == b"OK"

    def test_incomplete_trailing_frame_is_buffered_not_emitted(self) -> None:
        whole = _interleaved(_rtp(b"PAYLOAD"))
        assert _consume([whole[:-3]]) == b""

    def test_tiny_rtp_frames_are_skipped(self) -> None:
        # Under 12 bytes cannot be a valid RTP packet.
        assert _consume([_interleaved(b"\x80\x21\x00")]) == b""


class TestSkipInterleavedDuringNegotiation:
    def test_skips_video_frames_sitting_before_an_rtsp_response(self) -> None:
        # Blink auto-plays after SETUP, so binary RTP arrives interleaved with
        # the text responses. Parsing those as ASCII makes DESCRIBE hang.
        tunnel = RtspTunnel("host", "/path")
        tunnel._buf += _interleaved(_rtp(b"video")) + b"RTSP/1.0 200 OK\r\n"
        tunnel._skip_interleaved_frames()
        assert bytes(tunnel._buf).startswith(b"RTSP/1.0 200 OK")

    def test_leaves_a_partial_binary_frame_alone(self) -> None:
        tunnel = RtspTunnel("host", "/path")
        tunnel._buf += b"$\x00\x00\xff\x01\x02"  # claims 255 bytes, has 2
        tunnel._skip_interleaved_frames()
        assert bytes(tunnel._buf) == b"$\x00\x00\xff\x01\x02"

    def test_is_a_noop_when_the_buffer_starts_with_text(self) -> None:
        tunnel = RtspTunnel("host", "/path")
        tunnel._buf += b"RTSP/1.0 200 OK\r\n"
        tunnel._skip_interleaved_frames()
        assert bytes(tunnel._buf) == b"RTSP/1.0 200 OK\r\n"


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [("rtsps://h/p", True), ("rtsp://h/p", True), ("immis://h/c", False), ("", False)],
    )
    def test_is_rtsp_url(self, url: str, expected: bool) -> None:
        assert is_rtsp_url(url) is expected

    def test_identifies_as_the_blink_player(self) -> None:
        assert USER_AGENT == "Immedia WalnutPlayer"
