"""Non-coherent M-ary FSK for the low-speed, narrow-band modes.

Why a separate modulation rather than a smaller QAM constellation: below about
40 bps in a few hundred hertz the binding constraint stops being spectral
efficiency and becomes the ability to *detect* something without knowing the
channel phase.  Non-coherent MFSK needs no channel estimate at all -- the
receiver compares tone energies, so it does not care that the phase rotated
between pilots -- and it degrades gracefully instead of falling off a cliff.

Each OFDM symbol carries **one** tone out of M, so the symbol rate and the
occupied bandwidth are decoupled: the tone spacing (one bin, or two for the
narrowest profile) sets orthogonality, and the code rate sets the information
rate.  M = 16 gives 4 bits/symbol, M = 8 gives 3 -- the smaller alphabet is
worth about 1 dB and costs a third of the rate, which is the usual trade at the
bottom of a ladder.

Tones are Gray-labelled, so an error into an adjacent tone costs one bit.
"""

from __future__ import annotations

import numpy as np

_CACHE: dict[int, "MFSK"] = {}


class MFSK:
    """Gray-labelled M-ary orthogonal FSK: bits <-> tone index, tones -> LLRs."""

    def __init__(self, m: int = 16):
        if m < 2 or (m & (m - 1)):
            raise ValueError("M must be a power of two")
        self.m = int(m)
        self.bps = int(np.log2(self.m))
        gray = np.array([k ^ (k >> 1) for k in range(self.m)], dtype=np.int64)
        self.gray = gray
        word_to_tone = np.empty(self.m, dtype=np.int64)
        for k in range(self.m):
            word_to_tone[gray[k]] = k
        self._word_to_tone = word_to_tone
        self._tone_bits = np.array(
            [
                [(gray[k] >> (self.bps - 1 - j)) & 1 for j in range(self.bps)]
                for k in range(self.m)
            ],
            dtype=np.uint8,
        )
        self._m0 = [np.nonzero(self._tone_bits[:, j] == 0)[0] for j in range(self.bps)]
        self._m1 = [np.nonzero(self._tone_bits[:, j] == 1)[0] for j in range(self.bps)]

    def map_bits(self, bits: np.ndarray) -> np.ndarray:
        """(N*bps,) bits -> (N,) tone indices."""
        b = np.asarray(bits, dtype=np.uint8).ravel()
        if b.size % self.bps:
            raise ValueError(f"bit count must be a multiple of {self.bps}")
        w = b.reshape(-1, self.bps).astype(np.int64)
        val = np.zeros(w.shape[0], dtype=np.int64)
        for j in range(self.bps):
            val = (val << 1) | w[:, j]
        return self._word_to_tone[val]

    def demap(self, energy: np.ndarray, scale: float = 2.0) -> np.ndarray:
        """(N, M) tone energies -> (N, bps) LLRs, positive meaning bit 0.

        Max-log: for each bit, the strongest tone that would make it 1 against
        the strongest that would make it 0.  ``scale`` converts normalised
        energies into LLR magnitude, and it is measured rather than assumed.
        """
        E = np.asarray(energy, dtype=np.float64)
        out = np.empty((E.shape[0], self.bps))
        s = max(float(scale), 1e-6)
        for j in range(self.bps):
            out[:, j] = (E[:, self._m0[j]].max(axis=1) - E[:, self._m1[j]].max(axis=1)) / s
        return out


def mfsk(m: int = 16) -> MFSK:
    inst = _CACHE.get(int(m))
    if inst is None:
        inst = MFSK(int(m))
        _CACHE[int(m)] = inst
    return inst


# Default alphabet, kept so existing 16-FSK call sites keep working.
M, BPS = 16, 4


def map_bits(bits: np.ndarray) -> np.ndarray:
    return mfsk(16).map_bits(bits)


def demap(energy: np.ndarray, scale: float = 2.0) -> np.ndarray:
    return mfsk(16).demap(energy, scale)
