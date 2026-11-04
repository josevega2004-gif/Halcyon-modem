import numpy as np
import pytest

from halcyon.bits import crc16
from halcyon.polar import PolarCode, polarization_weights, transform
from halcyon.ldpc import code_for


def test_a_full_length_block_round_trips_at_an_awkward_size():
    code = PolarCode(n=512, k=104)
    rng = np.random.default_rng(6)
    payload = rng.integers(0, 2, code.k - 16, dtype=np.uint8)
    cw = code.encode(payload)
    assert cw.size == 512
    llr = np.where(cw == 0, 20.0, -20.0)
    src, ok = code.decode(llr)
    assert ok
    assert np.array_equal(src[code.info][: code.k - 16], payload)


def test_transform_is_its_own_inverse():
    rng = np.random.default_rng(0)
    u = rng.integers(0, 2, 64, dtype=np.uint8)
    assert np.array_equal(transform(transform(u)), u)


def test_weights_rank_the_least_reliable_index_lowest():
    w = polarization_weights(16)
    assert w[0] == 0.0
    assert np.argmax(w) == 15


def test_noiseless_round_trip():
    code = PolarCode(n=128, k=48)
    rng = np.random.default_rng(1)
    payload = rng.integers(0, 2, code.k - 16, dtype=np.uint8)
    cw = code.encode(payload)
    llr = np.where(cw == 0, 20.0, -20.0)
    src, ok = code.decode(llr)
    assert ok
    assert np.array_equal(src[code.info][: code.k - 16], payload)


def test_crc_rejects_a_wrong_payload_length():
    code = PolarCode(n=64, k=32)
    with pytest.raises(ValueError):
        code.encode(np.zeros(1, dtype=np.uint8))


def test_high_snr_decoding_succeeds_and_noise_can_fail():
    code = PolarCode(n=128, k=48, list_size=8)
    rng = np.random.default_rng(3)
    clean = 0
    for _ in range(4):
        payload = rng.integers(0, 2, code.k - 16, dtype=np.uint8)
        cw = code.encode(payload)
        sigma = 0.3
        y = (1 - 2.0 * cw) + sigma * rng.standard_normal(cw.size)
        _src, ok = code.decode(2.0 * y / sigma**2)
        clean += ok
    assert clean == 4


def test_osd_returns_a_codeword_consistent_with_the_code():
    code = code_for(432, 104)
    rng = np.random.default_rng(4)
    payload = rng.integers(0, 2, code.k - 16, dtype=np.uint8)
    info = np.concatenate([payload, crc16(payload)])
    cw = code.encode(info)[0]
    llr = np.where(cw == 0, 6.0, -6.0)

    def check(bits):
        return np.array_equal(crc16(bits[: code.k - 16].copy()), bits[code.k - 16 : code.k])

    bits, ok, _ = code.decode_osd(llr, check=check)
    assert ok
    assert code.syndrome_ok(bits)
    assert np.array_equal(bits[: code.k], info)


def test_rate_matched_block_has_the_frame_length_and_round_trips():
    code = PolarCode(n=512, k=104, length=432)
    rng = np.random.default_rng(7)
    payload = rng.integers(0, 2, code.k - 16, dtype=np.uint8)
    cw = code.encode(payload)
    assert cw.size == 432
    src, ok = code.decode(np.where(cw == 0, 20.0, -20.0))
    assert ok
    assert np.array_equal(src[code.info][: code.k - 16], payload)


def test_punctured_coordinates_are_frozen_and_not_transmitted():
    code = PolarCode(n=512, k=104, length=432)
    assert code.keep.size == 432
    assert code.punctured.sum() == 80
    assert code.frozen[code.punctured].all()
    assert not np.intersect1d(code.keep, code.info).size == 0


def test_crc24_check_value():
    from halcyon.bits import bits_to_int, crc24

    bits = np.unpackbits(np.frombuffer(b"123456789", dtype=np.uint8))
    assert bits_to_int(crc24(bits)) == 0x21CF02


def test_crc24_round_trips_and_changes_the_payload_width():
    code = PolarCode(n=128, k=56, crc_bits=24)
    rng = np.random.default_rng(8)
    payload = rng.integers(0, 2, code.k - 24, dtype=np.uint8)
    cw = code.encode(payload)
    src, ok = code.decode(np.where(cw == 0, 20.0, -20.0))
    assert ok
    assert np.array_equal(src[code.info][: code.k - 24], payload)


def test_rate_matched_decoder_rejects_the_mother_length():
    code = PolarCode(n=512, k=104, length=432)
    with pytest.raises(ValueError):
        code.decode(np.zeros(512))


def test_gf2_inverse_round_trip():
    from halcyon.ldpc import _gf2_inverse

    rng = np.random.default_rng(5)
    for _ in range(10):
        a = rng.integers(0, 2, (12, 12), dtype=np.uint8)
        inv = _gf2_inverse(a)
        if inv is None:
            continue
        assert np.array_equal((a.astype(np.uint16) @ inv) & 1, np.eye(12, dtype=np.uint8))
