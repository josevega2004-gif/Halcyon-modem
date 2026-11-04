import numpy as np

from halcyon.bits import bits_to_bytes, bits_to_int, bytes_to_bits, crc16, int_to_bits


def test_int_bits_roundtrip():
    for v, w in [(0, 1), (5, 4), (255, 8), (1234, 11)]:
        assert bits_to_int(int_to_bits(v, w)) == v


def test_crc16_known_value():
    # CRC-16/CCITT-FALSE check value for "123456789" is 0x29B1.
    assert bits_to_int(crc16(bytes_to_bits(b"123456789"))) == 0x29B1


def test_crc_detects_flip():
    bits = bytes_to_bits(b"the quick brown fox")
    c = crc16(bits)
    bad = bits.copy()
    bad[3] ^= 1
    assert not np.array_equal(crc16(bad), c)


def test_byte_roundtrip():
    data = bytes(range(37))
    assert bits_to_bytes(bytes_to_bits(data)) == data
