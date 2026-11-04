"""Compare a BCH product code with Chase-2 against the LDPC at a similar block.

    PYTHONPATH=. python3 tools/compare_tpc.py --trials 20 --snr 0 1 2 3 4

The product code is (31, 16, 3)^2: 961 bits carrying 256 information bits (240
payload + CRC-16), rate 0.266.  The LDPC baseline is the modem's (960, 256)
code, the nearest block the construction allows.  Both run on the same BPSK /
AWGN channel, and a block is delivered only when its CRC-16 passes.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from halcyon.bits import crc16
from halcyon.ldpc import code_for
from halcyon.product import ProductCode

PAYLOAD = 240
CRC = 16
INFO = PAYLOAD + CRC


def channel(cw: np.ndarray, rate: float, ebn0: float, rng) -> np.ndarray:
    sigma = np.sqrt(1.0 / (2.0 * rate * 10 ** (ebn0 / 10.0)))
    y = (1.0 - 2.0 * cw) + sigma * rng.standard_normal(cw.size)
    return 2.0 * y / sigma**2


def payload_bits(bits: np.ndarray, code) -> np.ndarray:
    """The information block: the top-left k x k corner, not the first k*k bits."""
    return bits.reshape(code.n, code.n)[: code.k, : code.k].ravel()


def crc_ok(bits: np.ndarray, code=None) -> bool:
    if code is not None:
        bits = payload_bits(bits, code)
    return bits.size >= INFO and bool(
        np.array_equal(crc16(bits[:PAYLOAD].copy()), bits[PAYLOAD:INFO])
    )


def run(scheme: str, code, trials: int, ebn0: float, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    rate = INFO / (961 if scheme == "tpc" else 960)
    failures = 0
    started = time.perf_counter()
    for _ in range(trials):
        payload = rng.integers(0, 2, PAYLOAD, dtype=np.uint8)
        info = np.concatenate([payload, crc16(payload)])
        if scheme == "tpc":
            cw = code.encode(info)
            llr = channel(cw, rate, ebn0, rng)
            bits = code.decode(llr, iterations=2, p=4)
            ok = crc_ok(bits, code)
        else:
            cw = code.encode(info)[0]
            llr = channel(cw, rate, ebn0, rng)
            bits, _conv, _it = code.decode(llr)
            ok = crc_ok(bits)
        failures += not ok
    return failures / trials, (time.perf_counter() - started) / trials


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--snr", type=float, nargs="+", default=[0.0, 1.0, 2.0, 3.0, 4.0])
    args = ap.parse_args()

    tpc = ProductCode()
    ldpc = code_for(960, INFO)
    print(f"BPSK, 240 payload + 16 CRC, {args.trials} blocks per point")
    print("TPC (961, 256) rate 0.266; LDPC (960, 256) rate 0.267")
    print()
    header = f"{'Eb/N0':>6} | {'BCH product + Chase-2':>24} | {'LDPC':>18}"
    print(header)
    print("-" * len(header))
    for snr in args.snr:
        cells = []
        for scheme, code in (("tpc", tpc), ("ldpc", ldpc)):
            bler, per_block = run(scheme, code, args.trials, snr, seed=31)
            cells.append(f"{100 * bler:6.1f}% {1e3 * per_block:8.1f}ms")
        print(f"{snr:6.1f} | {cells[0]:>24} | {cells[1]:>18}")


if __name__ == "__main__":
    main()
