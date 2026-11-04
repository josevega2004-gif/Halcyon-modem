"""Compare low-rate coding on the non-coherent 16-FSK channel used at 10-21 bps.

    PYTHONPATH=. python3 tools/compare_low_rate.py --trials 40 --snr 0 1 2 3 4

Each scheme carries the same 56 information bits (40 payload + CRC-16) as 32
or 35 non-coherent 16-FSK tones, which is the shape of the compact ladder's
bottom rungs: 4 bits per tone, no channel estimate, energies only.

* LDPC -- the modem's (128, 56) code, soft LLRs, 50 sum-product iterations.
* conv K=9 -- the standard (561, 753) code, 128 coded bits, zero tail.
* conv K=14 -- 140 coded bits (the 56 info bits plus one pad bit, then the
  13-bit tail), so it spends 35 tones against 32.  That extra airtime is part
  of its cost and shows up in the Eb/N0 axis because the rate is lower.

The FSK LLR is the exact non-coherent one, from tone energies: the likelihood of
a tone energy is exponential, so each bit LLR is a log-sum-exp difference over
the tones that make it 0 and 1.  The modem's max-log demapper is an
approximation of this.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from halcyon.bits import crc16
from halcyon.conv import ConvolutionalCode
from halcyon.ldpc import code_for
from halcyon.mfsk import mfsk

M = 16
BPS = 4
PAYLOAD = 40
CRC = 16
INFO = PAYLOAD + CRC


def logsumexp(x: np.ndarray, axis: int = 1) -> np.ndarray:
    peak = x.max(axis=axis, keepdims=True)
    return (peak + np.log(np.exp(x - peak).sum(axis=axis, keepdims=True))).squeeze(axis)


def tone_bits() -> np.ndarray:
    unit = mfsk(M)
    return unit._tone_bits  # Gray labelling, (M, BPS)


def fsk_llr(energy: np.ndarray) -> np.ndarray:
    """(symbols, 16) tone energies -> (symbols, 4) bit LLRs, positive means 0.

    The likelihood of the transmitted tone grows with its energy (Rician),
    while a tone that was not sent has an exponential energy distribution; to
    first order the log-likelihood of a hypothesis is the energy in its tone,
    so the bit LLR is a log-sum-exp *difference of energies*, with the tones
    that make the bit 1 subtracted from the ones that make it 0.
    """
    labels = tone_bits()
    llr = np.empty((energy.shape[0], BPS))
    for j in range(BPS):
        zero = labels[:, j] == 0
        llr[:, j] = logsumexp(energy[:, zero]) - logsumexp(energy[:, ~zero])
    return llr


def fsk_channel(tones: np.ndarray, es: float, rng) -> np.ndarray:
    """Non-coherent orthogonal FSK with unit noise power per complex sample."""
    sym = np.zeros((tones.size, M), dtype=np.complex128)
    sym[np.arange(tones.size), tones] = np.sqrt(es)
    noise = (rng.standard_normal(sym.shape) + 1j * rng.standard_normal(sym.shape)) * np.sqrt(0.5)
    return np.abs(sym + noise) ** 2


def crc_ok(bits: np.ndarray) -> bool:
    return bits.size >= INFO and bool(
        np.array_equal(crc16(bits[:PAYLOAD].copy()), bits[PAYLOAD:INFO])
    )


def run(scheme: str, code, trials: int, ebn0: float, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    unit = mfsk(M)
    if scheme == "ldpc":
        info_bits, coded = INFO, 128
        rate = INFO / coded
    elif scheme == "conv9":
        info_bits, coded = INFO, 2 * (INFO + 8)
        rate = INFO / coded
    else:
        info_bits, coded = INFO, 2 * (INFO + 1 + 13)
        rate = INFO / coded
    es = rate * BPS * 10 ** (ebn0 / 10.0)
    failures = 0
    started = time.perf_counter()
    for _ in range(trials):
        payload = rng.integers(0, 2, PAYLOAD, dtype=np.uint8)
        info = np.concatenate([payload, crc16(payload)])
        if scheme == "ldpc":
            cw = code.encode(info)[0]
        elif scheme == "conv9":
            cw = code.encode(info)
        else:
            cw = code.encode(np.concatenate([info, np.zeros(1, np.uint8)]))
        assert cw.size == coded
        energy = fsk_channel(unit.map_bits(cw), es, rng)
        llr = fsk_llr(energy).ravel()
        if scheme == "ldpc":
            bits, _conv, _it = code.decode(llr)
        else:
            bits, _ended = code.decode(llr)
        failures += not crc_ok(bits)
    return failures / trials, (time.perf_counter() - started) / trials


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--snr", type=float, nargs="+", default=[0.0, 1.0, 2.0, 3.0, 4.0])
    args = ap.parse_args()

    ldpc = code_for(128, INFO)
    conv9 = ConvolutionalCode(9)
    conv14 = ConvolutionalCode(14)
    print(f"16-FSK, 40 payload + 16 CRC, {args.trials} blocks per point")
    print("rates: LDPC and K=9 0.438 (32 tones), K=14 0.400 (35 tones)")
    print()
    header = f"{'Eb/N0':>6} | " + " | ".join(
        f"{name:>18}" for name in ("LDPC (128,56)", "conv K=9", "conv K=14")
    )
    print(header)
    print("-" * len(header))
    for snr in args.snr:
        cells = []
        for scheme, code in (("ldpc", ldpc), ("conv9", conv9), ("conv14", conv14)):
            bler, per_block = run(scheme, code, args.trials, snr, seed=23)
            cells.append(f"{100 * bler:6.1f}% {1e3 * per_block:7.1f}ms")
        print(f"{snr:6.1f} | " + " | ".join(f"{c:>18}" for c in cells))


if __name__ == "__main__":
    main()
