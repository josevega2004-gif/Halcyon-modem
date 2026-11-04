"""Prove the C++ modem and the Python modem speak the same air interface.

Three things are checked, in both directions:

1. Python transmits, C++ receives.
2. C++ transmits, Python receives.
3. For the same text and rung the two transmitters produce the *same samples*
   (to float32 rounding), which is what makes (1) and (2) hold for every rung
   rather than the ones that happened to be tested.

Then the C++ receiver is fed Python-modulated frames through an AWGN channel at
a few S/N values, so "it decodes" does not only mean "it decodes a clean file".

Run from the project root:  python3 cpp/tests/interop.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from halcyon.channel import make_channel  # noqa: E402
from halcyon.modem import Modem  # noqa: E402
from halcyon.ofdm import Phy  # noqa: E402

BIN = os.path.join(ROOT, "cpp", "build", "halcyon-modem")
DATA = os.path.join(ROOT, "cpp", "data", "wide.bin")
TMP = os.path.join(ROOT, "cpp", "build")
TEXT = "test de halcyon k"


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([BIN, *args, "--data", DATA], capture_output=True, text=True, cwd=ROOT)


def read_f32(path: str) -> np.ndarray:
    raw = np.fromfile(path, dtype="<f4")
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex128)


def write_f32(path: str, wave: np.ndarray) -> None:
    flat = np.empty(wave.size * 2, dtype="<f4")
    flat[0::2] = wave.real
    flat[1::2] = wave.imag
    flat.tofile(path)


def main() -> int:
    failures: list[str] = []
    if not os.path.exists(BIN):
        print(f"missing {BIN}; run make -C cpp first")
        return 2

    print("== C++ self-test (all rungs, no channel) ==")
    r = run(["selftest"])
    print(r.stdout.rstrip())
    if r.returncode != 0:
        failures.append("C++ self-test")

    modem = Modem(Phy())
    ladder = modem.phy  # geometry only; rung metadata comes from the console

    print("\n== Python transmits, C++ receives ==")
    for mcs in (0, 4, 9, 16):
        path = os.path.join(ROOT, "cpp", "data", "vec", f"py_mcs{mcs:02d}.f32")
        if not os.path.exists(path):
            failures.append(f"missing vector mcs {mcs}")
            continue
        got = json.loads(run(["rx", "--in", path]).stdout)
        ok = got.get("ok") and got.get("text") == TEXT
        print(
            f"  mcs {mcs:2d}: {'OK  ' if ok else 'FAIL'} text={got.get('text')!r} "
            f"snr={got.get('snr_db')} cfo={got.get('cfo_hz')} iters={got.get('iters')}"
        )
        if not ok:
            failures.append(f"py->c++ mcs {mcs}")

    print("\n== C++ transmits, Python receives ==")
    for mcs in (0, 4, 9, 16):
        out = os.path.join(TMP, f"cxx_mcs{mcs:02d}.f32")
        made = run(["tx", "--mcs", str(mcs), "--text", TEXT, "--out", out])
        if made.returncode != 0:
            failures.append(f"c++ tx mcs {mcs}")
            print(f"  mcs {mcs:2d}: FAIL tx: {made.stderr.strip()}")
            continue
        wave = read_f32(out)
        res = modem.receive(wave)
        ok = bool(res.ok) and res.payload.decode("ascii", "replace") == TEXT
        print(
            f"  mcs {mcs:2d}: {'OK  ' if ok else 'FAIL'} text={res.payload!r} "
            f"snr={res.snr_db:+.1f} blocks={res.blocks}"
        )
        if not ok:
            failures.append(f"c++->py mcs {mcs}")

    print("\n== Same samples from both transmitters ==")
    for mcs in (0, 4, 7, 9, 12, 16):
        py_wave, _ = modem.build(TEXT.encode(), mcs)
        out = os.path.join(TMP, f"cmp_mcs{mcs:02d}.f32")
        run(["tx", "--mcs", str(mcs), "--text", TEXT, "--out", out])
        cxx = read_f32(out)
        py = np.asarray(py_wave, np.complex128)
        if py.shape != cxx.shape:
            failures.append(f"shape mcs {mcs}: {py.shape} vs {cxx.shape}")
            print(f"  mcs {mcs:2d}: FAIL shape {py.shape} vs {cxx.shape}")
            continue
        diff = float(np.max(np.abs(py - cxx)))
        ok = diff < 1e-5
        print(f"  mcs {mcs:2d}: {'OK  ' if ok else 'FAIL'} max|py-cxx| = {diff:.2e}")
        if not ok:
            failures.append(f"samples mcs {mcs}")

    print("\n== C++ receive through an AWGN channel ==")
    for mcs, snr in ((4, 12.0), (7, 15.0), (9, 15.0), (16, 30.0)):
        py_wave, _ = modem.build(TEXT.encode(), mcs)
        noisy = make_channel(
            "awgn", snr, seed=11 + mcs, bandwidth_hz=modem.phy.bandwidth_hz
        )(np.asarray(py_wave, np.complex128))
        path = os.path.join(TMP, f"awgn_mcs{mcs:02d}.f32")
        write_f32(path, noisy)
        got = json.loads(run(["rx", "--in", path]).stdout)
        ok = got.get("ok") and got.get("text") == TEXT
        print(
            f"  mcs {mcs:2d} at {snr:4.0f} dB: {'OK  ' if ok else 'FAIL'} "
            f"snr={got.get('snr_db')} blocks_ok={got.get('blocks_ok')}"
        )
        if not ok:
            failures.append(f"awgn mcs {mcs} at {snr} dB")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all interop checks passed")
    del ladder
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
