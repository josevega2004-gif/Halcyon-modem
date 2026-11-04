import numpy as np
import pytest

from halcyon.conv import ConvolutionalCode


def test_noiseless_round_trip_at_both_constraint_lengths():
    rng = np.random.default_rng(2)
    for k in (9, 14):
        code = ConvolutionalCode(k)
        bits = rng.integers(0, 2, 40, dtype=np.uint8)
        cw = code.encode(bits)
        assert cw.size == 2 * (bits.size + k - 1)
        decoded, ended = code.decode(np.where(cw == 0, 8.0, -8.0))
        assert ended
        assert np.array_equal(decoded, bits)


def test_noisy_decoding_corrects_a_handful_of_flipped_bits():
    rng = np.random.default_rng(3)
    code = ConvolutionalCode(9)
    bits = rng.integers(0, 2, 60, dtype=np.uint8)
    cw = code.encode(bits)
    llr = np.where(cw == 0, 4.0, -4.0)
    llr[[3, 27, 64]] *= -1.0
    decoded, ended = code.decode(llr)
    assert ended
    assert np.array_equal(decoded, bits)


def test_unknown_constraint_length_is_rejected():
    with pytest.raises(ValueError):
        ConvolutionalCode(11)


def test_polynomial_outside_the_constraint_length_is_rejected():
    with pytest.raises(ValueError):
        ConvolutionalCode(3, polys=(0o17, 0o5))


def test_decoder_needs_whole_symbols():
    code = ConvolutionalCode(9)
    with pytest.raises(ValueError):
        code.decode(np.zeros(5))
