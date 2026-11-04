"""Compare short-block decoding at a frame-sized block: LDPC, LDPC+OSD-2, polar.

    PYTHONPATH=. python3 tools/compare_short_block.py
    PYTHONPATH=. python3 tools/compare_short_block.py --length 432 --k 104 \\
        --snr 0 1 2 3 --trials 30

Every scheme carries the same k information bits (k - 16 payload + 16 CRC) in
the same block, over the same BPSK/AWGN channel, and a block counts as delivered
only when its CRC-16 passes -- which is what the modem accepts.

The polar code has no 432-bit instance: its mother length is a power of two, so
``--length 432`` uses a 512-bit code with its 80 least reliable coordinates
punctured (dropped at the encoder, erased at the decoder).  That is one
rate-matching choice, and the point of the run is to see what it costs.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from halcyon.bits import crc16
from halcyon.ldpc import code_for
from halcyon.polar import PolarCode


def crc_ok(bits: np.ndarray, k: int) -> bool:
    return bool(np.array_equal(crc16(bits[: k - 16].copy()), bits[k - 16 : k]))


def channel(cw: np.ndarray, rate: float, ebn0_db: float, rng) -> np.ndarray:
    """BPSK symbols at unit energy through AWGN, returned as LLRs."""
    sigma = np.sqrt(1.0 / (2.0 * rate * 10.0 ** (ebn0_db / 10.0)))
    y = (1.0 - 2.0 * cw) + sigma * rng.standard_normal(cw.size)
    return 2.0 * y / sigma**2


def run(scheme: str, code, trials: int, ebn0_db: float, seed: int,
        n: int, k: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    rate = k / n
    failures = 0
    started = time.perf_counter()
    for _ in range(trials):
        payload = rng.integers(0, 2, k - 16, dtype=np.uint8)
        if scheme == "polar":
            codeword = code.encode(payload)
        else:
            info = np.concatenate([payload, crc16(payload)])
            codeword = code.encode(info)[0]
        llr = channel(codeword, rate, ebn0_db, rng)
        if scheme == "ldpc":
            bits, _conv, _it = code.decode(llr)
            ok = crc_ok(bits, k)
        elif scheme == "osd":
            bits, _ok, _it = code.decode_osd(llr, check=lambda b: crc_ok(b, k))
            ok = crc_ok(bits, k)
        else:
            _src, ok = code.decode(llr)
        failures += not ok
    return failures / trials, (time.perf_counter() - started) / trials


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--snr", type=float, nargs="+", default=[1.0, 2.0, 3.0, 4.0])
    ap.add_argument("--length", type=int, default=512,
                    help="transmitted bits per block (default 512)")
    ap.add_argument("--k", type=int, default=104,
                    help="information bits per block, including the CRC")
    ap.add_argument("--list", type=int, default=8, help="polar list size")
    args = ap.parse_args()

    mother = 1 << (args.length - 1).bit_length()
    ldpc = code_for(args.length, args.k)
    polar = PolarCode(n=mother, k=args.k, list_size=args.list,
                      length=args.length)
    punctured = mother - args.length
    print(f"n={args.length}, k={args.k} ({args.k - 16} payload + 16 CRC), "
          f"{args.trials} blocks per point")
    if punctured:
        print(f"polar: {mother}-bit mother, {punctured} punctured coordinates")
    print()
    header = f"{'Eb/N0':>6} | " + " | ".join(f"{name:>18}" for name in
                                              ("LDPC", "LDPC + OSD-2", "polar CA-SCL L8"))
    print(header)
    print("-" * len(header))
    for snr in args.snr:
        cells = []
        for scheme, code in (("ldpc", ldpc), ("osd", ldpc), ("polar", polar)):
            bler, per_block = run(scheme, code, args.trials, snr, seed=17,
                                  n=args.length, k=args.k)
            cells.append(f"{100 * bler:6.1f}% {1e3 * per_block:7.1f}ms")
        print(f"{snr:6.1f} | " + " | ".join(f"{c:>18}" for c in cells))


if __name__ == "__main__":
    main()
