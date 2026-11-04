import math

import numpy as np

from halcyon.qam import qam


def test_noiseless_roundtrip_all_orders():
    rng = np.random.default_rng(0)
    for bps in (1, 2, 4, 6, 8):
        q = qam(bps)
        bits = rng.integers(0, 2, 512 * bps, dtype=np.uint8).reshape(-1, bps)
        hard = (q.demap(q.map_bits(bits), 1e-6) < 0).astype(np.uint8)
        assert np.array_equal(hard, bits), f"bps={bps}"


def test_unit_power():
    for bps in (2, 4, 6, 8):
        q = qam(bps)
        n = 1 << bps
        bits = np.array(
            [[(v >> (bps - 1 - j)) & 1 for j in range(bps)] for v in range(n)], dtype=np.uint8
        )
        sym = q.map_bits(bits)
        assert abs(np.mean(np.abs(sym) ** 2) - 1.0) < 1e-9


def test_uncoded_ber_matches_theory():
    q = qam(2)
    rng = np.random.default_rng(1)
    snr_db = 5.0
    var = 10 ** (-snr_db / 10)
    bits = rng.integers(0, 2, (60000, 2), dtype=np.uint8)
    sym = q.map_bits(bits)
    y = sym + np.sqrt(var / 2) * (
        rng.standard_normal(sym.size) + 1j * rng.standard_normal(sym.size)
    )
    ber = float(np.mean((q.demap(y, var) < 0).astype(np.uint8) != bits))
    # QPSK carries 2 bits/symbol, so Eb/N0 = Es/N0 - 3 dB.
    theory = 0.5 * math.erfc(np.sqrt(10 ** (snr_db / 10) / 2.0))
    assert abs(ber - theory) < 0.003
