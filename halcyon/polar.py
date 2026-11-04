"""Polar codes with CRC-aided successive-cancellation list decoding.

The narrow profile uses short codewords (432-576 bits).  At that length a
belief-propagation LDPC decoder is working at the edge of what its graph can
support, which is where the floors in the measurements come from.  Polar codes
are the short-block alternative: successive cancellation over a list of
candidate paths, ranked by a CRC, is what 5G uses for control blocks of this
size.

The mother block length must be a power of two, so a frame-sized block is
carried by the next power-of-two mother code with its least reliable
coordinates punctured: the encoder drops them and the decoder erases them.  At
432 bits that is a 512-bit code with 80 erasures, which leaves the frame
geometry alone.

LLRs follow the same convention as :mod:`halcyon.ldpc`: positive means bit 0.

This is an experimental decoder, not part of the frame format: no MCS uses it
yet.  ``results/short_block_decoders.md`` holds the measured comparison with
the LDPC codes at n=512, k=104, and ``tools/compare_short_block.py`` reruns it.
"""

from __future__ import annotations

import numpy as np

from .bits import crc16, crc24


def polarization_weights(n: int) -> np.ndarray:
    """Polarisation weight of every source index; higher is more reliable.

    This is the beta-expansion ordering (beta = 2**(1/4)) used to place
    information bits without running full density evolution.
    """
    if n < 2 or (n & (n - 1)) != 0:
        raise ValueError("polar block length must be a power of two")
    m = n.bit_length() - 1
    idx = np.arange(n)
    w = np.zeros(n, dtype=np.float64)
    for j in range(m):
        bit = (idx >> (m - 1 - j)) & 1
        w += bit * (2.0 ** (j / 4.0))
    return w


def transform(u: np.ndarray) -> np.ndarray:
    """x = u F^(otimes m), the polar transform (its own inverse)."""
    x = np.array(u, dtype=np.uint8, copy=True)
    step = 1
    while step < x.size:
        for start in range(0, x.size, 2 * step):
            x[start:start + step] ^= x[start + step:start + 2 * step]
        step *= 2
    return x


def _f(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Check-node LLR (min-sum form of the box-plus operator)."""
    sign = np.where(a < 0, -1.0, 1.0) * np.where(b < 0, -1.0, 1.0)
    return sign * np.minimum(np.abs(a), np.abs(b))


def _g(a: np.ndarray, b: np.ndarray, beta: np.ndarray) -> np.ndarray:
    """Variable-node LLR once the left branch's codeword bits are known."""
    return b + (1.0 - 2.0 * np.asarray(beta, dtype=np.float64)) * a


def _softplus(x: float) -> float:
    return float(np.logaddexp(0.0, x))


def _keep(entries: list, limit: int) -> list:
    if len(entries) <= limit:
        return entries
    entries.sort(key=lambda e: e[0])
    return entries[:limit]


class PolarCode:
    """A (n, k) polar code with a CRC-16 inside k, optionally punctured."""

    def __init__(self, n: int = 512, k: int = 104,
                 crc_bits: int = 16, list_size: int = 8,
                 length: int | None = None):
        """A (length, k) code; k includes the CRC-16 tail.

        ``length`` is the transmitted block size.  When it is below the
        power-of-two mother length ``n``, the ``n - length`` least reliable
        coordinates are punctured; they are frozen, so no information is lost,
        only observations.
        """
        if k <= crc_bits:
            raise ValueError("k must exceed the CRC width")
        if crc_bits not in (16, 24):
            raise ValueError("crc_bits must be 16 or 24")
        self._crc = crc16 if crc_bits == 16 else crc24
        self.n = int(n)
        self.k = int(k)
        self.length = self.n if length is None else int(length)
        if not self.k < self.length <= self.n:
            raise ValueError("need k < length <= n")
        self.crc_bits = int(crc_bits)
        self.list_size = int(list_size)

        weights = polarization_weights(self.n)
        # The polar transform used here is F^(otimes m) without the bit-reversal
        # permutation, so the reliability sequence has to be read through it:
        # channel i corresponds to index bitrev(i).  Skipping this puts the
        # information on the *least* reliable channels, which still round-trips
        # noiselessly and then fails at every usable S/N -- measured as 100 %
        # block error at 3 dB before the permutation was applied.
        m = self.n.bit_length() - 1
        rev = np.array([int(format(i, f"0{m}b")[::-1], 2) for i in range(self.n)])
        order = np.argsort(-weights[rev], kind="stable")
        self.info = np.sort(order[: self.k])
        self.frozen = np.ones(self.n, dtype=bool)
        self.frozen[self.info] = False

        self.punctured = np.zeros(self.n, dtype=bool)
        if self.length < self.n:
            drop = self.n - self.length
            self.punctured[np.sort(order[-drop:])] = True
            if not self.frozen[self.punctured].all():
                raise ValueError("length is too short to rate-match this k")
        self.keep = np.flatnonzero(~self.punctured)

    # ------------------------------------------------------------------ encode
    def encode(self, payload: np.ndarray) -> np.ndarray:
        """Encode k - crc_bits payload bits; returns the length transmitted bits."""
        bits = np.asarray(payload, dtype=np.uint8).ravel()
        if bits.size != self.k - self.crc_bits:
            raise ValueError(
                f"expected {self.k - self.crc_bits} payload bits, got {bits.size}"
            )
        info = np.concatenate([bits, self._crc(bits)])
        u = np.zeros(self.n, dtype=np.uint8)
        u[self.info] = info
        return transform(u)[self.keep]

    # ------------------------------------------------------------------ decode
    def decode(self, llr: np.ndarray) -> tuple[np.ndarray, bool]:
        """List-decode; returns (source bits, CRC-accepted)."""
        llr = np.asarray(llr, dtype=np.float64).ravel()
        if llr.size != self.length:
            raise ValueError(f"expected {self.length} LLRs, got {llr.size}")
        if self.length != self.n:
            full = np.zeros(self.n, dtype=np.float64)
            full[self.keep] = llr
            llr = full
        entries = self._node([(0.0, llr.copy())], 0, self.n)
        entries.sort(key=lambda e: e[0])
        for _cost, _parent, _cw, src in entries:
            info = src[self.info]
            body = info[: self.k - self.crc_bits]
            check = info[self.k - self.crc_bits :]
            if np.array_equal(self._crc(body.copy()), check):
                return src, True
        return entries[0][3], False

    def _node(self, items: list, offset: int, size: int) -> list:
        """Expand every path through one subtree.

        ``items`` holds (cost, alpha) per surviving path; the return value is
        (cost, parent_index, codeword, source_bits) per path.  Pruning happens
        at the leaves, where the list actually branches.
        """
        if size == 1:
            out = []
            for i, (cost, alpha) in enumerate(items):
                a = float(np.ravel(alpha)[0])
                if self.frozen[offset]:
                    out.append((cost, i, np.zeros(1, np.uint8), np.zeros(1, np.uint8)))
                else:
                    out.append((cost + _softplus(-a), i, np.zeros(1, np.uint8),
                                np.zeros(1, np.uint8)))
                    out.append((cost + _softplus(a), i, np.ones(1, np.uint8),
                                np.ones(1, np.uint8)))
            return _keep(out, self.list_size)

        half = size // 2
        left_items = [(cost, _f(alpha[:half], alpha[half:])) for cost, alpha in items]
        left = self._node(left_items, offset, half)
        owners = []
        right_items = []
        for cost, parent, cw_left, src_left in left:
            alpha = items[parent][1]
            right_items.append((cost, _g(alpha[:half], alpha[half:], cw_left)))
            owners.append((cw_left, src_left))
        right = self._node(right_items, offset + half, half)
        out = []
        for cost, parent, cw_right, src_right in right:
            cw_left, src_left = owners[parent]
            # x = (xA ^ xB, xB) with xA the left subtree's codeword.  The parent
            # index must follow the path all the way back to this node's own
            # input, or the caller cannot pair a right path with its left one.
            out.append((
                cost,
                left[parent][1],
                np.concatenate([cw_left ^ cw_right, cw_right]),
                np.concatenate([src_left, src_right]),
            ))
        return out
