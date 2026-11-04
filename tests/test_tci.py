"""TCI wire format: text commands, the 64-byte frame header, and audio frames."""

from __future__ import annotations

import numpy as np
import pytest

from halcyon.tci import (
    FMT_FLOAT32,
    FMT_INT16,
    HEADER_BYTES,
    STREAM_RX_AUDIO,
    STREAM_TX_AUDIO,
    TciClient,
    build_frame,
    decode_payload,
    mono,
    parse_header,
    parse_text,
)


def test_several_commands_can_share_one_text_frame():
    got = parse_text("vfo:0,0,14074000;modulation:0,usb;trx:0,false;")
    assert got == [("vfo", "0,0,14074000"), ("modulation", "0,usb"), ("trx", "0,false")]


def test_bare_words_have_no_arguments():
    got = parse_text("protocol:2.0;device:SunSDR2;ready")
    assert got[-1] == ("ready", "")
    assert got[0] == ("protocol", "2.0")


def test_stray_whitespace_and_empty_segments_are_tolerated():
    assert parse_text(" ;; keepalive ; ")[0] == ("keepalive", "")


def test_header_layout_is_sixteen_little_endian_words():
    frame = build_frame(np.zeros(2400, dtype=np.float32), rate=48000)
    assert len(frame) == HEADER_BYTES + 2400 * 4
    hdr = parse_header(frame)
    assert hdr["rate"] == 48000
    assert hdr["format"] == FMT_FLOAT32
    assert hdr["length"] == 2400, "length counts floats, not frames"
    assert hdr["stream"] == STREAM_TX_AUDIO
    assert hdr["channels"] == 2


def test_short_frames_are_rejected_rather_than_misread():
    assert parse_header(b"\x00" * 8) is None
    assert decode_payload(b"", {"format": FMT_FLOAT32}).size == 0


def test_float32_payload_round_trips():
    samples = np.linspace(-0.5, 0.5, 480, dtype=np.float32)
    frame = build_frame(samples, rate=48000, stream=STREAM_RX_AUDIO)
    out = decode_payload(frame, parse_header(frame))
    assert np.allclose(out, samples, atol=1e-6)


def test_int16_payload_is_scaled_to_unit_range():
    raw = np.array([-32768, 0, 16384], dtype="<i2").tobytes()
    frame = bytes(HEADER_BYTES) + raw
    hdr = dict(parse_header(frame))
    hdr["format"] = FMT_INT16
    out = decode_payload(frame, hdr)
    assert np.allclose(out, [-1.0, 0.0, 0.5], atol=1e-4)


def test_stereo_is_averaged_for_the_mono_modem_path():
    inter = np.array([1.0, 0.0, 0.0, 1.0], dtype=np.float32)
    assert np.allclose(mono(inter, 2), [0.5, 0.5])
    assert np.allclose(mono(np.array([0.3], dtype=np.float32), 1), [0.3])


# ------------------------------------------------------------------- the client
def test_client_tracks_state_from_text_frames():
    c = TciClient()
    c._text("protocol:2.0;device:SunSDR2 PRO;vfo:0,0,14074000;modulation:0,digl;ready")
    assert c.handshake["device"] == "SunSDR2 PRO"
    assert c.vfo_hz == 14_074_000
    assert c.mode == "DIGL"
    assert c.ptt is False
    c._text("trx:0,true;")
    assert c.ptt is True


def test_received_audio_frames_land_in_the_buffer():
    c = TciClient()
    pcm = (np.ones(1200, dtype=np.float32) * 0.25)
    stereo = np.repeat(pcm, 2)
    c._binary(build_frame(stereo, rate=48000, stream=STREAM_RX_AUDIO))
    assert c.rx_frames == 1
    got = c.drain_audio(0.1)
    assert got.size == 1200
    assert np.allclose(got, 0.25, atol=1e-6)


def test_iq_frames_become_complex_samples():
    iq = np.empty(200, dtype=np.float32)
    iq[0::2] = 0.5
    iq[1::2] = -0.25
    c = TciClient()
    c._binary(build_frame(iq, rate=192000, stream=0, channels=2))
    assert len(c.iq) == 1
    assert np.allclose(c.iq[0][:2], 0.5 - 0.25j)


class _FakeSocket:
    """Stands in for the WebSocket so the wire strings can be inspected."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, line: str) -> None:
        self.sent.append(line)


def test_ptt_and_vfo_are_the_trx_and_vfo_commands():
    c = TciClient()
    fake = _FakeSocket()
    c._ws = fake  # type: ignore[assignment]
    c.set_ptt(True)
    c.set_vfo(7_074_000)
    c.set_mode("USB")
    # every command is terminated by a semicolon, which is what the server parses
    assert fake.sent == ["trx:0,true;", "vfo:0,0,7074000;", "modulation:0,usb;"]


def test_console_refuses_tci_calls_without_a_session():
    from halcyon.ui import _tci_set, tci_status

    assert tci_status() == {"connected": False}
    with pytest.raises(ValueError, match="no TCI session"):
        _tci_set({"ptt": True})
