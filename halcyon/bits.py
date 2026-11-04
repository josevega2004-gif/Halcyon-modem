"""Bit and byte helpers plus CRC-16 (CCITT-FALSE).

Hard decisions everywhere in this project use ``uint8`` arrays of 0/1 bits,
most-significant-bit first when a field is serialised to bits.
"""

from __future__ import annotations

import numpy as np

_TABLE: np.ndarray | None = None
_TABLE24: np.ndarray | None = None


def _crc_table() -> np.ndarray:
    global _TABLE
    if _TABLE is None:
        t = np.zeros(256, np.uint16)
        for i in range(256):
            c = i << 8
            for _ in range(8):
                c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
            t[i] = c
        _TABLE = t
    return _TABLE


def int_to_bits(value: int, width: int) -> np.ndarray:
    """MSB-first bit vector of ``value`` in ``width`` bits."""
    if value < 0 or value >= (1 << width):
        raise ValueError(f"{value} does not fit in {width} bits")
    return np.array([(value >> (width - 1 - i)) & 1 for i in range(width)], dtype=np.uint8)


def bits_to_int(bits: np.ndarray) -> int:
    """MSB-first bit vector to integer."""
    v = 0
    for b in np.asarray(bits, dtype=np.uint8).ravel():
        v = (v << 1) | int(b)
    return v


def bytes_to_bits(data: bytes) -> np.ndarray:
    if not data:
        return np.zeros(0, dtype=np.uint8)
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    b = np.asarray(bits, dtype=np.uint8).ravel()
    pad = (-len(b)) % 8
    if pad:
        b = np.concatenate([b, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(b.astype(bool)).tobytes()


def crc16(bits: np.ndarray) -> np.ndarray:
    """CRC-16/CCITT-FALSE over an arbitrary bit vector, returned as 16 bits."""
    b = np.asarray(bits, dtype=np.uint8).ravel()
    pad = (-len(b)) % 8
    data = np.concatenate([b, np.zeros(pad, dtype=np.uint8)]) if pad else b
    table = _crc_table()
    crc = 0xFFFF
    for byte in np.packbits(data.astype(bool)):
        crc = ((crc << 8) & 0xFFFF) ^ int(table[((crc >> 8) ^ int(byte)) & 0xFF])
    return int_to_bits(crc, 16)


def crc16_ok(bits: np.ndarray, check: np.ndarray) -> bool:
    return bool(np.array_equal(crc16(bits), np.asarray(check, dtype=np.uint8)))


def _crc24_table() -> np.ndarray:
    global _TABLE24
    if _TABLE24 is None:
        t = np.zeros(256, np.uint32)
        poly = 0x864CFB
        for i in range(256):
            c = i << 16
            for _ in range(8):
                c = ((c << 1) ^ poly) & 0xFFFFFF if c & 0x800000 else (c << 1) & 0xFFFFFF
            t[i] = c
        _TABLE24 = t
    return _TABLE24


def crc24(bits: np.ndarray) -> np.ndarray:
    """CRC-24/OPENPGP over an arbitrary bit vector, returned as 24 bits."""
    b = np.asarray(bits, dtype=np.uint8).ravel()
    pad = (-len(b)) % 8
    data = np.concatenate([b, np.zeros(pad, dtype=np.uint8)]) if pad else b
    table = _crc24_table()
    crc = 0xB704CE
    for byte in np.packbits(data.astype(bool)):
        crc = ((crc << 8) & 0xFFFFFF) ^ int(table[((crc >> 16) ^ int(byte)) & 0xFF])
    return int_to_bits(crc, 24)
