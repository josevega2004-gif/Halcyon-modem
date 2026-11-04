"""Gray-labelled BPSK / square-QAM mapping and max-log LLR demapping.

Constellations are normalised to unit average symbol power so that noise
variance can be used directly as the LLR scale.
"""

from __future__ import annotations

import numpy as np

_QAMS: dict[int, "QAM"] = {}


class QAM:
    def __init__(self, bps: int):
        if bps < 1 or bps > 8:
            raise ValueError("bits per symbol must be in 1..8")
        self.bps = bps
        if bps == 1:
            # BPSK on the real axis: bit 0 -> +1, bit 1 -> -1
            self.bits = np.array([[0], [1]], dtype=np.uint8)
            self.levels = np.array([1.0, -1.0])
        else:
            b = bps // 2
            nlev = 1 << b
            v = np.arange(nlev)
            gray = v ^ (v >> 1)              # Gray-labelled PAM levels
            raw = 2.0 * gray - (nlev - 1)
            scale = np.sqrt(2.0 * (nlev**2 - 1) / 3.0)  # unit total power
            self.levels = raw / scale
            self.bits = np.array(
                [[(i >> (b - 1 - j)) & 1 for j in range(b)] for i in range(nlev)],
                dtype=np.uint8,
            )
        m0 = []
        m1 = []
        for j in range(self.bits.shape[1]):
            m0.append(np.nonzero(self.bits[:, j] == 0)[0])
            m1.append(np.nonzero(self.bits[:, j] == 1)[0])
        self._m0 = m0
        self._m1 = m1

    # ------------------------------------------------------------------ mapping
    def map_bits(self, bits: np.ndarray) -> np.ndarray:
        """(N, bps) bits -> (N,) complex symbols."""
        bits = np.atleast_2d(np.asarray(bits, dtype=np.uint8))
        if bits.shape[1] != self.bps:
            raise ValueError("bit block width must equal bps")
        if self.bps == 1:
            return np.where(bits[:, 0] == 0, 1.0, -1.0).astype(complex)
        half = self.bps // 2
        i_idx = np.zeros(bits.shape[0], dtype=np.int64)
        for j in range(half):
            i_idx = (i_idx << 1) | bits[:, j]
        q_idx = np.zeros(bits.shape[0], dtype=np.int64)
        for j in range(half):
            q_idx = (q_idx << 1) | bits[:, half + j]
        return self.levels[i_idx] + 1j * self.levels[q_idx]

    # ---------------------------------------------------------------- demapping
    def demap(self, sym: np.ndarray, noise_var: float) -> np.ndarray:
        """(N,) symbols -> (N, bps) max-log LLRs (positive => bit 0)."""
        sym = np.asarray(sym).ravel()
        var = max(float(noise_var), 1e-9)
        if self.bps == 1:
            return (4.0 * sym.real / var).reshape(-1, 1)
        half = self.bps // 2
        lv = self.levels
        di = (sym.real[:, None] - lv[None, :]) ** 2
        dq = (sym.imag[:, None] - lv[None, :]) ** 2
        out = np.empty((sym.size, self.bps))
        for j in range(half):
            out[:, j] = (di[:, self._m1[j]].min(1) - di[:, self._m0[j]].min(1)) / var
            out[:, half + j] = (dq[:, self._m1[j]].min(1) - dq[:, self._m0[j]].min(1)) / var
        return out


def qam(bps: int) -> QAM:
    inst = _QAMS.get(bps)
    if inst is None:
        inst = QAM(bps)
        _QAMS[bps] = inst
    return inst
