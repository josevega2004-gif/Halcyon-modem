"""BCH product code with iterative Chase-2 decoding.

A product code encodes k x k information bits twice: every row with a BCH code,
then every column with the same code.  The result is a block whose minimum
distance is the square of the row code's -- 49 for the (31, 16, 3) code used
here -- and whose bits can be decoded algebraically, with no matrix inversion.

Decoding is Chase-2: take the p least reliable bits of a row, try every flip
pattern over them, keep the decodings that are valid BCH words, and choose the
one that correlates best with the channel.  Rows and columns alternate for a
couple of iterations, and each pass hands the next one a soft value made of the
channel LLR plus a competition-based extrinsic term, so the row pass and the
column pass actually inform each other.

The block is 961 bits carrying 256 information bits (240 payload + CRC-16), a
rate of 0.266.  It is a 1990s shape: cheap, deterministic, no iterations of
belief propagation and no Gaussian elimination, which is exactly what makes it
worth measuring against the LDPC rather than assuming.
"""

from __future__ import annotations

import numpy as np

from .bch import BCHCode


class ProductCode:
    """A two-dimensional product code with a Chase-2 row/column decoder."""

    def __init__(self, base: BCHCode | None = None):
        self.bch = base if base is not None else BCHCode(m=5, t=3)
        self.n = self.bch.n
        self.k = self.bch.k
        self.info_bits = self.k * self.k
        self.block_bits = self.n * self.n

    # ------------------------------------------------------------------ encode
    def encode(self, info: np.ndarray) -> np.ndarray:
        bits = np.asarray(info, dtype=np.uint8).ravel()
        if bits.size != self.info_bits:
            raise ValueError(f"expected {self.info_bits} bits, got {bits.size}")
        rows = np.empty((self.k, self.n), dtype=np.uint8)
        for i in range(self.k):
            rows[i] = self.bch.encode(bits[i * self.k : (i + 1) * self.k])
        full = np.empty((self.n, self.n), dtype=np.uint8)
        for j in range(self.n):
            full[:, j] = self.bch.encode(rows[:, j])
        return full.reshape(-1)

    # ------------------------------------------------------------------ decode
    def decode(self, llr: np.ndarray, iterations: int = 2,
               p: int = 4) -> np.ndarray:
        """Soft-decision decode; returns the hard block in row-major order.

        The returned bits are the algebraic decisions of the last pass, not the
        sign of the relaxed LLR: a corrected bit can keep the sign of a strong
        but wrong channel value while the extrinsic term is still too small to
        outvote it, and taking the sign there throws the correction away.
        """
        soft = np.asarray(llr, dtype=np.float64).reshape(self.n, self.n).copy()
        hard = (soft < 0).astype(np.uint8)
        for _ in range(max(1, int(iterations))):
            for axis in (0, 1):
                work = soft if axis == 0 else soft.T
                decisions = hard if axis == 0 else hard.T
                for i in range(self.n):
                    best, relaxed = self._chase(work[i], p)
                    decisions[i] = best
                    work[i] = relaxed
                soft = work if axis == 0 else work.T
                hard = decisions if axis == 0 else decisions.T
        return hard.reshape(-1)

    def _chase(self, llr: np.ndarray, p: int) -> tuple[np.ndarray, np.ndarray]:
        channel = np.asarray(llr, dtype=np.float64)
        hard = (channel < 0).astype(np.uint8)
        order = np.argsort(np.abs(channel))[: max(1, int(p))]

        candidates: list[tuple[float, np.ndarray]] = []
        for mask in range(1 << len(order)):
            trial = hard.copy()
            for j, index in enumerate(order):
                if (mask >> j) & 1:
                    trial[index] ^= 1
            decoded, ok = self.bch.decode(trial)
            if ok:
                metric = float(np.dot(1.0 - 2.0 * decoded, channel))
                candidates.append((metric, decoded))
        if not candidates:
            return hard, channel

        candidates.sort(key=lambda item: item[0], reverse=True)
        best_metric, best = candidates[0]
        relaxed = channel.copy()
        for i in range(self.n):
            opposite = [m for m, word in candidates if word[i] != best[i]]
            if opposite:
                weight = min(max((best_metric - max(opposite)) / 4.0, 0.0), 4.0)
                relaxed[i] = channel[i] + 0.7 * (1.0 - 2.0 * best[i]) * weight
        return best, relaxed
