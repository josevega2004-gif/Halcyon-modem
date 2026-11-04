import numpy as np

from halcyon.ldpc import LDPCCode, code_for


def max_four_cycles(H: np.ndarray, limit: int = 5) -> int:
    pairs: dict[tuple[int, int], int] = {}
    for j in range(H.shape[1]):
        rows = np.nonzero(H[:, j])[0]
        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                key = (int(rows[a]), int(rows[b]))
                pairs[key] = pairs.get(key, 0) + 1
    return sum(v * (v - 1) // 2 for v in pairs.values())


def test_encoding_is_a_codeword():
    code = code_for(632, 316)
    u = np.random.default_rng(1).integers(0, 2, code.k, dtype=np.uint8)
    cw = code.encode(u)[0]
    assert code.syndrome_ok(cw)
    assert np.array_equal(cw[: code.k], u)


def test_no_four_cycles():
    code = code_for(1264, 632)
    assert max_four_cycles(code.H) == 0


def test_clean_decoding():
    code = code_for(632, 316)
    rng = np.random.default_rng(2)
    u = rng.integers(0, 2, code.k, dtype=np.uint8)
    cw = code.encode(u)[0]
    llr = np.where(cw == 0, 12.0, -12.0)
    bits, ok, iters = code.decode(llr)
    assert ok and iters <= 2
    assert np.array_equal(bits, cw)


def test_awgn_decoding_at_high_snr():
    code = code_for(632, 316)
    rng = np.random.default_rng(3)
    u = rng.integers(0, 2, code.k, dtype=np.uint8)
    cw = code.encode(u)[0]
    x = 1.0 - 2.0 * cw
    llr = 2.0 * (x + 0.9 * rng.standard_normal(code.n)) / 0.81
    bits, ok, _ = code.decode(llr, max_iter=40)
    assert np.array_equal(bits[: code.k], u)


def test_rate_scaling():
    for n, k in [(316, 79), (1264, 632), (2528, 2104)]:
        code = LDPCCode(n, k)
        assert code.H.shape == (n - k, n)
        assert code.syndrome_ok(code.encode(np.zeros(k, np.uint8))[0])
