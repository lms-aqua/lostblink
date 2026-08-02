"""Tests for MPEG-TS keyframe detection.

This is what makes session handover invisible. Splice anywhere other than a
random access point and the decoder gets P-frames referencing a reference
picture that belongs to a stream which no longer exists.
"""

from __future__ import annotations

from lostblink.stream.publisher import (
    TS_PACKET_SIZE,
    TS_SYNC_BYTE,
    find_keyframe_offset,
)


def ts_packet(*, keyframe: bool = False, payload_start: bool = True) -> bytes:
    """Build a 188-byte TS packet, optionally flagged as a random access point."""
    packet = bytearray(TS_PACKET_SIZE)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x40 if payload_start else 0x00  # payload_unit_start_indicator
    packet[2] = 0x64  # PID low bits
    if keyframe:
        packet[3] = 0x30  # adaptation field + payload
        packet[4] = 0x07  # adaptation_field_length
        packet[5] = 0x40  # random_access_indicator
    else:
        packet[3] = 0x10  # payload only
    return bytes(packet)


class TestFindKeyframeOffset:
    def test_finds_a_keyframe_at_the_start(self) -> None:
        assert find_keyframe_offset(ts_packet(keyframe=True) * 2) == 0

    def test_finds_a_keyframe_after_several_normal_packets(self) -> None:
        data = ts_packet() * 3 + ts_packet(keyframe=True) + ts_packet()
        assert find_keyframe_offset(data) == 3 * TS_PACKET_SIZE

    def test_returns_none_when_there_is_no_keyframe(self) -> None:
        assert find_keyframe_offset(ts_packet() * 10) is None

    def test_returns_none_on_an_empty_buffer(self) -> None:
        assert find_keyframe_offset(b"") is None

    def test_returns_none_on_a_partial_packet(self) -> None:
        assert find_keyframe_offset(ts_packet(keyframe=True)[:100]) is None

    def test_ignores_a_random_access_flag_without_payload_start(self) -> None:
        # Mid-access-unit continuation is not a safe splice point.
        data = ts_packet(keyframe=True, payload_start=False) * 2
        assert find_keyframe_offset(data) is None

    def test_honours_the_start_offset(self) -> None:
        data = ts_packet(keyframe=True) + ts_packet() + ts_packet(keyframe=True)
        assert find_keyframe_offset(data, TS_PACKET_SIZE) == 2 * TS_PACKET_SIZE

    def test_resynchronises_past_leading_junk(self) -> None:
        data = b"\x00\x11\x22" + ts_packet(keyframe=True) + ts_packet()
        assert find_keyframe_offset(data) == 3

    def test_does_not_lock_onto_a_stray_0x47_in_payload_data(self) -> None:
        # 0x47 is a common byte inside video payload. A candidate boundary is
        # only accepted if the next packet also starts with a sync byte.
        stray = bytearray(ts_packet())
        stray[20] = TS_SYNC_BYTE
        data = bytes(stray) + ts_packet(keyframe=True) + ts_packet()
        assert find_keyframe_offset(data) == TS_PACKET_SIZE

    def test_finds_a_keyframe_in_a_realistic_gop(self) -> None:
        gop = ts_packet(keyframe=True) + ts_packet() * 29
        data = ts_packet() * 12 + gop
        assert find_keyframe_offset(data) == 12 * TS_PACKET_SIZE

    def test_zero_length_adaptation_field_is_not_a_keyframe(self) -> None:
        packet = bytearray(ts_packet())
        packet[3] = 0x30
        packet[4] = 0x00  # adaptation_field_length == 0, no flags byte follows
        assert find_keyframe_offset(bytes(packet) * 2) is None
