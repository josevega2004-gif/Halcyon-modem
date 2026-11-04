"""HPSDR protocol 1 encoding, checked against the documented byte layout.

There is no radio in this checkout, so these tests pin the wire format only:
discovery, the 1032-byte frame header, register packing and the IQ word order.
Discovery and streaming themselves are unexercised.
"""

from __future__ import annotations

import numpy as np
import pytest

from halcyon.hl2 import (
    FRAME_BYTES,
    HermesLite,
    build_frame,
    discovery_request,
    pack_iq,
    parse_discovery,
    parse_iq,
    spectrum_row,
    start_packet,
)


def reply(mac=b"\x00\x1c\xc0\xa2\x13\xdd", major=74, board=6, nrx=4, minor=2):
    p = bytearray(60)
    p[0:2] = b"\xef\xfe"
    p[2] = 0x02
    p[3:9] = mac
    p[9] = major
    p[10] = board
    p[13] = nrx
    p[15] = minor
    return bytes(p)


# ----------------------------------------------------------------- discovery
def test_discovery_request_is_the_sixty_three_byte_broadcast():
    req = discovery_request()
    assert len(req) == 63
    assert req[:3] == b"\xef\xfe\x02"
    assert req[3:] == bytes(60)


def test_discovery_reply_names_the_board_and_its_address():
    got = parse_discovery(reply(), "192.168.1.40")
    assert got["ip"] == "192.168.1.40"
    assert got["mac"] == "00:1C:C0:A2:13:DD"
    assert got["board_id"] == 0x06
    assert got["board"] == "Hermes Lite 2"
    assert got["firmware"] == "74.2"
    assert got["receivers"] == 4


def test_a_hermes_emulation_board_is_not_reported_as_an_hl2():
    got = parse_discovery(reply(board=0x01))
    assert got["board_id"] == 0x01
    assert "emulation" in got["board"]


def test_short_or_foreign_packets_are_not_discoveries():
    assert parse_discovery(b"") is None
    assert parse_discovery(b"\x00\x01\x02" + bytes(40)) is None
    assert parse_discovery(b"\xef\xfe\x02" + bytes(3)) is None


# --------------------------------------------------------------------- frames
def test_data_frame_header_and_sequence():
    f = build_frame(0x01020304, [(0x02, 14_074_000), (0x00, 0x02000004)])
    assert len(f) == FRAME_BYTES
    assert f[0:4] == b"\xef\xfe\x01\x02"
    assert f[4:8] == b"\x01\x02\x03\x04"
    # sub-frame 1 starts at 8: sync, then C0 = addr << 1
    assert f[8:11] == b"\x7f\x7f\x7f"
    assert f[11] == 0x02 << 1
    assert f[12:16] == (14_074_000).to_bytes(4, "big")
    # sub-frame 2 at 520 carries the second register
    assert f[520:523] == b"\x7f\x7f\x7f"
    assert f[523] == 0x00
    assert f[524:528] == (0x02000004).to_bytes(4, "big")


def test_mox_rides_on_every_slot_and_the_request_bit_is_separate():
    f = build_frame(7, [(0x01, 14_000_000), (0x09, 0x8), ], mox=True)
    assert f[11] & 0x01 and f[523] & 0x01, "MOX is level-sensitive per sub-frame"
    assert f[11] >> 1 == 0x01 and f[523] >> 1 == 0x09
    plain = build_frame(7, [(0x01, 14_000_000)], mox=False)
    assert not plain[11] & 0x01


def test_a_single_slot_still_leaves_a_well_formed_second_sub_frame():
    f = build_frame(1, [(0x00, 4)])
    assert f[520:523] == b"\x7f\x7f\x7f"


def test_start_and_stop_are_sixty_four_byte_commands():
    start = start_packet(run=True)
    assert len(start) == 64 and start[:4] == b"\xef\xfe\x04\x01"
    stop = start_packet(run=False)
    assert stop[:4] == b"\xef\xfe\x04\x00"
    assert start_packet(run=True, disable_watchdog=True)[3] == 0x81


# ------------------------------------------------------------------------- IQ
def test_transmit_iq_lands_in_both_sub_frames_as_big_endian_words():
    iq = np.zeros(126, dtype=np.complex64)
    iq[0] = 0.5 + 0.25j
    iq[63] = -0.5 - 0.25j  # first sample of the second sub-frame
    f = bytearray(FRAME_BYTES)
    used = pack_iq(f, iq)
    assert used == 126
    first = f[16:24]
    assert first[0:4] == bytes(4), "audio/EADDR words stay zero"
    assert int.from_bytes(first[4:6], "big", signed=True) == int(round(0.5 * 32767))
    assert int.from_bytes(first[6:8], "big", signed=True) == int(round(0.25 * 32767))
    second = f[528:536]
    assert int.from_bytes(second[4:6], "big", signed=True) < 0


def test_receive_iq_reads_twenty_four_bit_big_endian_words():
    pkt = bytearray(FRAME_BYTES)
    pkt[0:2] = b"\xef\xfe"
    pkt[2] = 0x01
    pkt[3] = 0x06
    for sub in (0, 1):
        base = 8 + sub * 512 + 8
        # 63 samples of (I, Q) = (1000, -2000)
        for k in range(63):
            off = base + k * 8
            pkt[off : off + 3] = (1000).to_bytes(3, "big")
            pkt[off + 3 : off + 6] = (0x1000000 - 2000).to_bytes(3, "big")
    iq = parse_iq(bytes(pkt), receivers=1)
    assert iq.size == 126
    assert np.allclose(iq.real, 1000 / 8388607.0, atol=1e-6)
    assert np.allclose(iq.imag, -2000 / 8388607.0, atol=1e-6)


def test_a_transmit_frame_is_not_mistaken_for_receive_iq():
    f = build_frame(1, [(0x00, 4)], tx=np.ones(126, dtype=np.complex64))
    assert parse_iq(f).size == 0


# ------------------------------------------------------------------- registers
def test_config_word_carries_rate_receivers_and_duplex():
    r = HermesLite("127.0.0.1", rate=192_000, receivers=1)
    assert r._config_word() == (0b10 << 24) | (0 << 3) | 0x04
    r.configure(rate=48_000)
    assert r._config_word() >> 24 == 0
    r.receivers = 4
    assert (r._config_word() >> 3) & 0xF == 3


def test_lna_word_uses_the_full_range_encoding():
    # [6] selects full range, [5:0] is gain + 12 dB
    assert HermesLite("127.0.0.1", lna_db=16)._lna_word() == (1 << 6) | 28
    assert HermesLite("127.0.0.1", lna_db=-12)._lna_word() == (1 << 6) | 0
    assert HermesLite("127.0.0.1", lna_db=48)._lna_word() == (1 << 6) | 60


def test_drive_word_is_the_top_nibble_plus_pa_enable():
    w = HermesLite("127.0.0.1", drive=8)._gain_word()
    assert w >> 28 == 8
    assert (w >> 19) & 1 == 1
    assert HermesLite("127.0.0.1", drive=99).drive == 15


def test_round_robin_cycles_every_register_we_own():
    r = HermesLite("127.0.0.1")
    seen = set()
    for i in range(0, 10, 2):
        for addr, _ in r._round_robin(i):
            seen.add(addr)
    assert seen == {0x00, 0x01, 0x02, 0x09, 0x0A}


# -------------------------------------------------------------------- display
def test_spectrum_row_is_a_bounded_line_of_the_requested_width():
    iq = (np.random.default_rng(1).standard_normal(4096)
          + 1j * np.random.default_rng(2).standard_normal(4096)).astype(np.complex64)
    row = spectrum_row(iq, 400)
    assert len(row) == 400
    assert all(0 <= v <= 255 for v in row)
    assert len(spectrum_row(np.zeros(0, dtype=np.complex64), 400)) == 400


# -------------------------------------------------------------- console wiring
def test_console_refuses_radio_calls_without_a_radio_connected():
    from halcyon.ui import _radio_connect, _radio_set, radio_spectrum, radio_status

    assert radio_status() == {"connected": False}
    with pytest.raises(ValueError, match="address"):
        _radio_connect({"ip": "  "})
    with pytest.raises(ValueError, match="no radio"):
        _radio_set({"ptt": True})
    with pytest.raises(ValueError, match="no radio"):
        radio_spectrum()


def test_resampling_moves_between_the_radio_and_the_modem_rate():
    from halcyon.ui import _resample

    x = np.exp(2j * np.pi * 0.1 * np.arange(480)).astype(np.complex64)
    assert _resample(x, 12_000, 12_000).size == 480
    assert _resample(x, 48_000, 12_000).size == 120
    assert _resample(x, 12_000, 48_000).size == 1920
    assert _resample(np.zeros(0, dtype=np.complex64), 48_000, 12_000).size == 0
