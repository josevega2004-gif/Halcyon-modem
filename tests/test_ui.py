"""The console's server-side helpers, without starting a server."""

from __future__ import annotations

import numpy as np

from halcyon.ofdm import Phy
from halcyon.ui import _audio, _decode, _waterfall, modes_payload


def test_modes_payload_covers_every_profile_and_rung():
    d = modes_payload()
    assert [p["id"] for p in d["profiles"]] == [
        "wide", "robust", "compact", "deep", "nano", "mini", "micro"
    ]
    for p in d["profiles"]:
        assert p["modes"], p["id"]
        for m in p["modes"]:
            assert m["bps"] > 0
            assert m["cap_bytes"] > 0
            assert m["max_bytes"] >= m["cap_bytes"]
    # 8.8 bps is the one deliberate close-to-9 rung; every other is >=10.
    assert min(m["bps"] for p in d["profiles"] for m in p["modes"]) >= 8.8
    for p in d["profiles"]:
        for m in p["modes"]:
            if m["bps"] < 10:
                assert p["id"] == "micro"


def test_waterfall_is_a_time_by_frequency_image():
    phy = Phy()
    rng = np.random.default_rng(0)
    rx = rng.standard_normal(20000) + 1j * rng.standard_normal(20000)
    wf = _waterfall(rx, phy.fs, cols=32, rows=120)
    assert wf is not None
    assert wf["w"] == 120 and wf["h"] == 32
    raw = np.frombuffer(__import__("base64").b64decode(wf["data"]), dtype=np.uint8)
    assert raw.size == wf["w"] * wf["h"]


def test_audio_is_a_playable_wav():
    wav = _audio({"profile": "wide", "mcs": 4, "text": "hello"})
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) > 44


def test_decode_reports_the_frame_and_recovers_the_text():
    out = _decode(
        {
            "profile": "wide",
            "mcs": 4,
            "text": "cq de halcyon",
            "snr": 12.0,
            "channel": "awgn",
            "cfo": 0.0,
            "looks": 0,
        }
    )
    assert out["sent"]["label"] == "QPSK 1/2"
    assert out["timing"]["total_s"] > 0
    first = out["attempts"][0]
    assert first["header_ok"] and first["match"]
    assert first["decoded"] == "cq de halcyon"
    assert out["waterfall"]["w"] > 0


def test_second_look_only_runs_when_the_first_look_fails():
    clean = _decode(
        {"profile": "wide", "mcs": 4, "text": "cq de halcyon", "snr": 14.0, "looks": 1}
    )
    assert clean["attempts"][0]["ok"]
    assert len(clean["attempts"]) == 1

    weak = _decode(
        {"profile": "wide", "mcs": 6, "text": "second look demo", "snr": 5.0, "looks": 1}
    )
    assert not weak["attempts"][0]["ok"]
    assert len(weak["attempts"]) == 2
    assert weak["attempts"][1]["label"] == "summed with the stored look"


def test_decode_caps_the_message_at_the_burst_capacity():
    out = _decode({"profile": "wide", "mcs": 0, "text": "x" * 5000, "snr": 30.0, "looks": 0})
    assert out["trimmed"] is True
    assert out["sent"]["bytes"] == out["sent"]["block_bits"] // 8 * 12
