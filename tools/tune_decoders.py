"""Tune the polar list decoder and LDPC OSD-2 at the 432-bit frame block.

    PYTHONPATH=. python3 tools/tune_decoders.py

Polar: k=104 in a 432-bit block (512-bit mother, 80 punctured coordinates),
CRC-16 against CRC-24, list sizes 8 / 32 / 64.  A block is a miss when the CRC
fails; a block is a *false accept* when the CRC passes but the payload is wrong,
which is what a larger list makes more likely and a longer CRC is meant to stop.

LDPC: the same block, sum-product then OSD-2, sweeping the number of BP
iterations before reprocessing, the reprocessing window, and whether pairs or
triples are flipped among the least reliable information bits.  This is the
"hybrid BP + OSD" question directly: OSD already only runs after BP fails, but
the LLRs it sorts are BP's, not the channel's.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from halcyon.bits import crc16
from halcyon.ldpc import code_for
from halcyon.polar import PolarCode

PAYLOAD = 88
K = 104
LENGTH = 432


def channel(cw: np.ndarray, rate: float, ebn0: float, rng) -> np.ndarray:
    sigma = np.sqrt(1.0 / (2.0 * rate * 10.0 ** (ebn0 / 10.0)))
    y = (1.0 - 2.0 * cw) + sigma * rng.standard_normal(cw.size)
    return 2.0 * y / sigma**2


def crc_ok(bits: np.ndarray, k: int = K) -> bool:
    return bits.size >= k and bool(
        np.array_equal(crc16(bits[: k - 16].copy()), bits[k - 16 : k])
    )


def polar_run(list_size: int, crc_bits: int, trials: int, ebn0: float,
              seed: int) -> tuple[float, int, float]:
    code = PolarCode(n=512, k=K, crc_bits=crc_bits, list_size=list_size,
                     length=LENGTH)
    payload_bits = K - crc_bits
    rng = np.random.default_rng(seed)
    misses = false_accepts = 0
    started = time.perf_counter()
    for _ in range(trials):
        payload = rng.integers(0, 2, payload_bits, dtype=np.uint8)
        cw = code.encode(payload)
        llr = channel(cw, K / LENGTH, ebn0, rng)
        src, ok = code.decode(llr)
        got = src[code.info][:payload_bits]
        if not ok:
            misses += 1
        elif not np.array_equal(got, payload):
            false_accepts += 1
    elapsed = (time.perf_counter() - started) / trials
    return misses / trials, false_accepts, elapsed


def ldpc_run(code, trials: int, ebn0: float, seed: int,
             osd: dict | None = None) -> tuple[float, int, float]:
    rng = np.random.default_rng(seed)
    misses = false_accepts = 0
    started = time.perf_counter()
    for _ in range(trials):
        payload = rng.integers(0, 2, PAYLOAD, dtype=np.uint8)
        info = np.concatenate([payload, crc16(payload)])
        cw = code.encode(info)[0]
        llr = channel(cw, K / LENGTH, ebn0, rng)
        if osd is None:
            bits, _conv, _it = code.decode(llr)
        else:
            bits, _ok, _it = code.decode_osd(
                llr, check=lambda b: crc_ok(b), **osd
            )
        if not crc_ok(bits):
            misses += 1
        elif not np.array_equal(bits[:PAYLOAD], payload):
            false_accepts += 1
    return misses / trials, false_accepts, (time.perf_counter() - started) / trials


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials-polar", type=int, default=15)
    ap.add_argument("--trials-osd", type=int, default=30)
    ap.add_argument("--snr", type=float, nargs="+", default=[0.0, 1.0, 2.0])
    args = ap.parse_args()

    print(f"polar, n={LENGTH} k={K}, {args.trials_polar} blocks per point")
    header = (f"{'Eb/N0':>6} | {'list':>5} | {'CRC':>4} | {'BLER':>7} | "
              f"{'false':>5} | {'ms':>7}")
    print(header)
    print("-" * len(header))
    for snr in args.snr:
        for list_size in (8, 32, 64):
            misses, fa, ms = polar_run(list_size, 16, args.trials_polar, snr, 41)
            print(f"{snr:6.1f} | {list_size:5d} | {16:4d} | {100*misses:6.1f}% | "
                  f"{fa:5d} | {1e3*ms:7.1f}")
        if snr <= 1.0:
            misses, fa, ms = polar_run(64, 24, args.trials_polar, snr, 41)
            print(f"{snr:6.1f} | {64:5d} | {24:4d} | {100*misses:6.1f}% | "
                  f"{fa:5d} | {1e3*ms:7.1f}")

    print()
    print(f"LDPC, n={LENGTH} k={K}, {args.trials_osd} blocks per point")
    ldpc = code_for(LENGTH, K)
    variants = (
        ("BP only", None),
        ("BP50 OSD2 w10", dict(max_iter=50, window=10, max_flips=2)),
        ("BP10 OSD2 w10", dict(max_iter=10, window=10, max_flips=2)),
        ("BP50 OSD2 w16", dict(max_iter=50, window=16, max_flips=2)),
        ("BP50 OSD3 w16", dict(max_iter=50, window=16, max_flips=3)),
    )
    header = f"{'Eb/N0':>6} | " + " | ".join(f"{name:>14}" for name, _ in variants)
    print(header)
    print("-" * len(header))
    for snr in (0.0, 1.0):
        cells = []
        for _name, osd in variants:
            misses, fa, ms = ldpc_run(ldpc, args.trials_osd, snr, 53, osd)
            cells.append(f"{100*misses:4.0f}% {1e3*ms:6.1f}ms")
        print(f"{snr:6.1f} | " + " | ".join(f"{c:>14}" for c in cells))


if __name__ == "__main__":
    main()
