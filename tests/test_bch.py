import numpy as np

from halcyon.bch import BCHCode
from halcyon.product import ProductCode


def test_generator_defines_a_31_16_code():
    bch = BCHCode(m=5, t=3)
    assert bch.n == 31
    assert bch.k == 16
    assert bch.g.bit_length() - 1 == bch.n - bch.k


def test_systematic_encoding_and_noiseless_decoding():
    bch = BCHCode()
    rng = np.random.default_rng(0)
    for _ in range(5):
        message = rng.integers(0, 2, bch.k, dtype=np.uint8)
        word = bch.encode(message)
        decoded, ok = bch.decode(word)
        assert ok
        assert np.array_equal(decoded, word)
        assert np.array_equal(word[: bch.k], message)


def test_corrects_up_to_the_design_distance():
    bch = BCHCode()
    rng = np.random.default_rng(1)
    for errors in (1, 2, 3):
        for _ in range(5):
            message = rng.integers(0, 2, bch.k, dtype=np.uint8)
            word = bch.encode(message)
            damaged = word.copy()
            positions = rng.choice(bch.n, errors, replace=False)
            damaged[positions] ^= 1
            decoded, ok = bch.decode(damaged)
            assert ok
            assert np.array_equal(decoded, word)


def test_product_round_trip_has_the_expected_geometry():
    code = ProductCode()
    assert (code.block_bits, code.info_bits) == (961, 256)
    rng = np.random.default_rng(2)
    info = rng.integers(0, 2, code.info_bits, dtype=np.uint8)
    block = code.encode(info)
    assert block.size == 961
    llr = np.where(block == 0, 6.0, -6.0)
    bits = code.decode(llr, iterations=1)
    assert np.array_equal(bits.reshape(code.n, code.n)[: code.k, : code.k].ravel(), info)


def test_product_decoder_repairs_a_scattering_of_errors():
    code = ProductCode()
    rng = np.random.default_rng(3)
    info = rng.integers(0, 2, code.info_bits, dtype=np.uint8)
    block = code.encode(info)
    llr = np.where(block == 0, 4.0, -4.0)
    damaged = rng.choice(code.block_bits, 10, replace=False)
    llr[damaged] *= -1.0
    bits = code.decode(llr, iterations=2)
    assert np.array_equal(bits.reshape(code.n, code.n)[: code.k, : code.k].ravel(), info)
