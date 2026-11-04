"""Rate-1/2 convolutional coding with soft-decision Viterbi decoding.

This is the coding half of the JS8Call-style low-speed shape: a small alphabet
of non-coherent tones, and a convolutional code strong enough to work at the
S/N where the tones are barely visible.  A constraint-length-9 code is the
standard one; K=14 is the "long memory" option, 32x the trellis for a few
tenths of a decibel.

The K=14 generator pair is not a published standard, so it was selected by
``tools/conv_generators.py``, which maximises free distance over candidate
pairs; run that tool to reproduce the choice.  The K=9 pair (561, 753) is the
long-standing NASA code.

Encoding is zero-terminated: K-1 zero bits are appended so the decoder can end
in the all-zero state.  On a 40-bit message that tail costs 4-7 % of the block,
which is the honest price of a feed-forward encoder at this length.

LLRs follow the rest of the project: positive means bit 0.
"""

from __future__ import annotations

import numpy as np

# (polynomials) per constraint length.  K=9 is the standard pair; K=14 comes
# from the candidate set reported by tools/conv_generators.py --constraint 14,
# chosen by measured block error at 3 dB, because free distance alone did not
# predict it: the largest-distance pair decoded worse than this one.
GENERATORS: dict[int, tuple[int, int]] = {
    9: (0o561, 0o753),
    14: (0o31767, 0o34275),
}


class ConvolutionalBlockCodec:
    """The modem's per-block code interface around a convolutional code.

    ``Modem`` expects ``encode(rows) -> (blocks, n)`` and
    ``decode(llr) -> (info_bits, converged, iterations)``; this adapts the
    one-shot convolutional encoder/decoder to that shape without teaching the
    modem about trellises.
    """

    def __init__(self, constraint: int = 9, length: int | None = None):
        self.code = ConvolutionalCode(constraint)
        self.length = None if length is None else int(length)

    def encode(self, rows: np.ndarray) -> np.ndarray:
        rows = np.atleast_2d(np.asarray(rows, dtype=np.uint8))
        out = np.zeros((rows.shape[0], self.length or 0), dtype=np.uint8)
        for i, row in enumerate(rows):
            cw = self.code.encode(row)
            if self.length is None:
                out = np.array([self.code.encode(r) for r in rows], dtype=np.uint8)
                break
            if cw.size > self.length:
                raise ValueError("convolutional code word does not fit the block")
            out[i, : cw.size] = cw
        return out

    def decode(self, llr: np.ndarray, max_iter: int = 50):
        bits, ended = self.code.decode(llr)
        return bits, ended, 1


class ConvolutionalCode:
    """A rate-1/2 (or rate-1/n) feed-forward convolutional code."""

    def __init__(self, constraint: int = 9, polys: tuple[int, ...] | None = None):
        self.k = int(constraint)
        if self.k < 2 or self.k > 16:
            raise ValueError("constraint length must be between 2 and 16")
        if polys is None:
            polys = GENERATORS.get(self.k, ())
            if not polys:
                raise ValueError(f"no generator pair known for K={self.k}")
        self.polys = tuple(int(p) for p in polys)
        self.n = len(self.polys)
        if any(p <= 0 or p >> self.k for p in self.polys):
            raise ValueError("each polynomial must fit in the constraint length")
        if any(p & 1 == 0 for p in self.polys):
            raise ValueError("each polynomial must depend on the current bit")

        self.states = 1 << (self.k - 1)
        self.mask = self.states - 1
        self._parity = np.array(
            [bin(i).count("1") & 1 for i in range(1 << self.k)], dtype=np.uint8
        )

        s = np.arange(self.states)
        b = np.array([0, 1])
        reg = (s[:, None] << 1) | b[None, :]          # (S, 2)
        self._next = reg & self.mask
        self._out = np.zeros((self.states, 2, self.n), dtype=np.uint8)
        for j, p in enumerate(self.polys):
            self._out[:, :, j] = self._parity[reg & p]

    # ------------------------------------------------------------------ encode
    def encode(self, bits: np.ndarray) -> np.ndarray:
        """Encode info bits, appending the K-1 zero tail."""
        b = np.asarray(bits, dtype=np.uint8).ravel()
        tail = np.concatenate([b, np.zeros(self.k - 1, dtype=np.uint8)])
        reg = 0
        out = np.empty(tail.size * self.n, dtype=np.uint8)
        for t, bit in enumerate(tail):
            reg = ((reg << 1) | int(bit)) & ((1 << self.k) - 1)
            for j, p in enumerate(self.polys):
                out[t * self.n + j] = self._parity[reg & p]
        return out

    # ------------------------------------------------------------------ decode
    def decode(self, llr: np.ndarray) -> tuple[np.ndarray, bool]:
        """Soft-decision Viterbi; returns (info bits without the tail, ended in 0)."""
        llr = np.asarray(llr, dtype=np.float64).ravel()
        if llr.size % self.n:
            raise ValueError("LLR count must be a multiple of the code rate")
        steps = llr.size // self.n
        half = self.states // 2
        idx = np.arange(half)

        # branch metric = correlation with ±1 symbols, positive means bit 0
        signs = 1.0 - 2.0 * self._out.astype(np.float64)   # (S, 2, n)
        metrics = np.full(self.states, -np.inf)
        metrics[0] = 0.0
        choice = np.zeros((steps, self.states), dtype=np.uint8)

        for t in range(steps):
            bm = signs @ llr[t * self.n : (t + 1) * self.n]
            cand0 = metrics + bm[:, 0]
            cand1 = metrics + bm[:, 1]
            for b, cand in ((0, cand0), (1, cand1)):
                lo, hi = cand[:half], cand[half:]
                pick = hi > lo
                metrics[2 * idx + b] = np.maximum(lo, hi)
                choice[t, 2 * idx + b] = pick

        state = 0
        bits = np.zeros(steps, dtype=np.uint8)
        for t in range(steps - 1, -1, -1):
            bits[t] = state & 1
            prev_high = choice[t, state]
            state = (state >> 1) | (int(prev_high) << (self.k - 2))
        ended = int(state) == 0
        info = bits[: steps - (self.k - 1)] if steps >= self.k - 1 else bits[:0]
        return info, ended
