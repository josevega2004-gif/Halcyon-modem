"""OFDM physical layer: 2.75 kHz-wide waveform, comb pilots, timing/frequency sync.

Band plan (audio baseband, 12 kHz sampling, complex baseband):

    2.75 kHz of occupied bandwidth, inside the 2.8 kHz limit the FCC now applies
    across 160-10 m in place of the old baud-rate limits (47 CFR 97.307(f)(3)).
    The measured 99 % occupied bandwidth is 2 729 Hz -- see
    ``python -m halcyon.bench --mode compliance``.  IARU Region 1 band plans cap
    the wide data segments at 2.7 kHz, which is tighter than the FCC, so
    ``Phy(**NARROW)`` gives a narrower layout for those segments.

    23.4 Hz subcarrier spacing (512-point FFT), 117 active subcarriers between
    246 Hz and 2988 Hz, every 4th one carrying a known BPSK pilot, the rest
    carrying data.  A 64-sample cyclic prefix (5.3 ms) covers the delay spread
    of a typical HF path.

Each subcarrier carries independent coded bits; nothing is spread, hopped or
chip-modulated, and the information rate is a large fraction of the occupied
bandwidth.  That matters legally: SS emissions are authorized in 47 CFR
97.305(c) only at 222 MHz and above, never on HF.

Sync is a matched filter against a random-BPSK preamble (two identical OFDM
symbols) searched over carrier frequency offset, so an SSB tuning error of
+-90 Hz is acquired in one pass.  A random sequence is used rather than a
Zadoff-Chu one because its ambiguity function is sharp: a timing shift cannot
masquerade as a carrier offset.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve


def _decimate(x: np.ndarray, q: int) -> np.ndarray:
    """Low-pass then take every q-th sample (complex safe, no aliasing)."""
    span = 2 * q
    k = np.arange(-span, span + 1)
    h = np.sinc(k / q) * np.hamming(k.size)
    h /= h.sum()
    return np.convolve(x, h, mode="same")[::q]


# Narrower layout for the IARU Region 1 wide-data segments, which are 2.7 kHz
# wide against the FCC's 2.8 kHz: 111 carriers spanning 2 601.6 Hz, measuring
# about 2 590 Hz at 99 % and so clear of both limits.  Thresholds in MCS_TABLE
# are measured for the default 117-carrier geometry; a narrow link carries
# about 5 % less on every mode.
NARROW = {"bin_lo": 11, "bin_hi": 121}

# Narrow, high-noise profile: 24 carriers over 562.5 Hz, 18 of them carrying
# data.  It exists for bad-band conditions rather than for sensitivity: for a
# fixed noise density it collects 6.9 dB less noise than the 2 742 Hz layout, so
# it works at a proportionally lower absolute signal level, and it can sit in a
# clear 600 Hz gap while the wide layout has to swallow whatever QRM is anywhere
# in 2.7 kHz.  Per-bit sensitivity is unchanged (see the README), so it is the
# smaller bandwidth, not a coding trick, that buys the margin.
#
# The header needs `header_syms = 10`: 18 data carriers at 2 bits/symbol give 36
# coded bits per symbol, so the 75-bit header plus CRC needs at least 3 symbols
# at rate 1/4, and 10 symbols leaves the same rate-1/4 protection the wide
# profile has (360 coded bits for 90 info bits).  That is 480 ms of header per
# burst -- real airtime, which is why this profile is for connecting, not for
# throughput.
#
# Blocks are 12 symbols rather than 4: with only 18 data carriers a 4-symbol
# block is 144 coded bits, and LDPC needs far more than that to work at a low
# rate (measured: QPSK 1/4 and 3/4 never got below 25 % block error at any S/N,
# because the short code has no distance).  12 symbols gives 432 bits per block
# at QPSK and 864 at 16QAM.
ROBUST = {
    "bin_lo": 11,
    "bin_hi": 34,
    "header_syms": 10,
    "syms_per_block": 12,
    "profile": "robust",
}

# 500 Hz-class layout for the low-speed modes: 21 carriers over 492 Hz, 15 of
# them data and 6 pilots, 12-symbol blocks.  This is the band-plan-friendly
# shape of a slow level -- a narrow emission is what lets a mode sit in a
# 500 Hz gap, and (for a fixed noise density) it collects 7.5 dB less noise than
# the 2.74 kHz layout, so it works at a proportionally lower signal level.
#
# The header needs 12 symbols here, not 2: 15 data carriers at 2 bits/symbol is
# 30 coded bits per symbol, so 12 symbols at rate 1/4 give 90 info bits for the
# 75-bit field set -- the same protection the wide profile has and enough to
# decode before the payload.
COMPACT = {
    "bin_lo": 11,
    "bin_hi": 31,
    "header_syms": 12,
    "syms_per_block": 12,
    "profile": "compact",
}

@dataclass
class Phy:
    fs: int = 12000
    n_fft: int = 512
    cp: int = 64
    bin_lo: int = 11
    bin_hi: int = 127
    pilot_step: int = 4
    preamble_syms: int = 2
    # Coded header symbols.  Two is enough at 87 data carriers; a narrow profile
    # needs more of them to reach the same coding rate, see ``ROBUST``.
    header_syms: int = 2
    # Payload symbols per coding block.  Long blocks matter more than wide bands:
    # a narrow profile needs more symbols per block to keep the code long enough.
    syms_per_block: int = 4
    # Which ladder in ``frame.TABLES`` carries this profile's thresholds.
    profile: str = "wide"
    seed: int = 0x5A17

    def __post_init__(self) -> None:
        self.active = np.arange(self.bin_lo, self.bin_hi + 1)
        self.pilots = self.active[:: self.pilot_step]
        pset = set(int(p) for p in self.pilots)
        self.data = np.array([int(b) for b in self.active if int(b) not in pset])
        self.n_data = int(self.data.size)
        self.n_pilots = int(self.pilots.size)
        self.sym_len = self.n_fft + self.cp
        self.sym_time = self.sym_len / self.fs
        self.bandwidth_hz = float(self.active.size * self.fs / self.n_fft)
        rng = np.random.default_rng(self.seed)
        self.pilot_syms = rng.choice([-1.0, 1.0], size=self.n_pilots).astype(complex)
        # Random BPSK over the active bins: unlike a Zadoff-Chu sequence it has a
        # sharp ambiguity function, so a timing shift cannot masquerade as a
        # carrier offset (ZC preambles are Doppler-tolerant by construction).
        self.preamble_freq = rng.choice([-1.0, 1.0], size=self.active.size).astype(complex)
        self.preamble = np.tile(self._symbol(self.preamble_freq, use_pilots=False), self.preamble_syms)
        # 16-FSK tones for the non-coherent low-speed modes: one active carrier
        # per symbol, so the payload needs no channel estimate and no pilots.
        self.mfsk_n = int(min(16, self.active.size))
        self.mfsk_pos = np.arange(self.mfsk_n)
        self.mfsk_bins = self.active[: self.mfsk_n]
        self._tone_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self.mfsk_bins_for(self.mfsk_n)

    # ------------------------------------------------------------------ helpers
    def _symbol(self, data_vals: np.ndarray, use_pilots: bool = True) -> np.ndarray:
        grid = np.zeros(self.n_fft, dtype=complex)
        if use_pilots:
            grid[self.pilots] = self.pilot_syms
            grid[self.data] = data_vals
        else:
            grid[self.active] = data_vals
        t = np.fft.ifft(grid) * np.sqrt(self.n_fft)
        return np.concatenate([t[-self.cp :], t])

    def modulate(self, data_syms: np.ndarray) -> np.ndarray:
        """(S, n_data) complex symbols -> contiguous time-domain waveform."""
        data_syms = np.atleast_2d(np.asarray(data_syms, dtype=complex))
        if data_syms.shape[1] != self.n_data:
            raise ValueError(f"expected {self.n_data} data carriers")
        out = np.empty(data_syms.shape[0] * self.sym_len, dtype=complex)
        for s in range(data_syms.shape[0]):
            out[s * self.sym_len : (s + 1) * self.sym_len] = self._symbol(data_syms[s])
        return out

    # ------------------------------------------------------------------ 16-FSK
    def mfsk_bins_for(self, m_tones: int) -> tuple[np.ndarray, np.ndarray]:
        """(carrier bins, positions within ``active``) for an M-tone alphabet.

        Eight tones are spaced two bins apart on the narrow profiles: with a
        rectangular symbol the bins are only orthogonal at exact symbol timing, so
        doubling the spacing costs 164 Hz of a 352 Hz band and buys tolerance to
        the timing estimate.
        """
        got = self._tone_cache.get(int(m_tones))
        if got is None:
            if m_tones == 8 and self.active.size >= 15:
                bins = self.active[::2][:8]
            else:
                bins = self.active[: int(m_tones)]
            got = (bins, np.searchsorted(self.active, bins))
            self._tone_cache[int(m_tones)] = got
        return got

    def modulate_mfsk(self, tones: np.ndarray, m_tones: int = 16) -> np.ndarray:
        """(S,) tone indices -> waveform with exactly one active carrier each."""
        tones = np.atleast_1d(np.asarray(tones, dtype=int))
        pos = self.mfsk_bins_for(int(m_tones))[1]
        out = np.empty(tones.size * self.sym_len, dtype=complex)
        vals = np.zeros(self.active.size, dtype=complex)
        for s_, t in enumerate(tones):
            vals[:] = 0.0
            vals[int(pos[int(t)])] = 1.0
            out[s_ * self.sym_len : (s_ + 1) * self.sym_len] = self._symbol(vals, use_pilots=False)
        return out

    def demodulate_mfsk(self, rx: np.ndarray, n_syms: int, m_tones: int = 16) -> np.ndarray:
        """Non-coherent detection: normalised tone energies, shape (S, mfsk_n).

        No equalisation and no pilots: the receiver never needs the channel
        phase, which is the whole point of the mode.  Dividing by the per-symbol
        median energy normalises the LLR scale, since 15 of the 16 tones carry
        noise only.
        """
        rx = np.asarray(rx)
        Y = np.empty((n_syms, self.n_fft), dtype=complex)
        for s_ in range(n_syms):
            seg = rx[s_ * self.sym_len + self.cp : s_ * self.sym_len + self.sym_len]
            Y[s_] = np.fft.fft(seg) if seg.size >= self.n_fft else 0.0
        E = np.abs(Y[:, self.mfsk_bins_for(int(m_tones))[0]]) ** 2
        med = np.median(E, axis=1, keepdims=True)
        return E / np.maximum(med, 1e-12)

    # ----------------------------------------------------------- demodulation
    def _smooth_pilots(self, Hp: np.ndarray) -> np.ndarray:
        """3-tap average across pilots, then across adjacent symbols.

        Raw pilot estimates are noisy, and interpolating straight from them costs
        about 2.0 dB of EVM at any practical S/N (measured: 120 QPSK symbols over
        AWGN, excess EVM against perfect CSI).  Averaging three neighbouring
        pilots and three neighbouring symbols brings that to ~0.5 dB.

        Both averages are safe for the channel this modem targets: pilot spacing
        is 4 bins (94 Hz), so a 3-pilot average spans ~188 Hz, well inside the
        coherence bandwidth of a 1-3 ms path; and 3 symbols are 144 ms against a
        fading coherence time near 1 s.  With only two symbols (the header) the
        time average is skipped rather than biased.
        """
        k = np.array([0.25, 0.5, 0.25])
        P = np.pad(Hp, ((0, 0), (1, 1)), mode="edge")
        Hs = k[0] * P[:, :-2] + k[1] * P[:, 1:-1] + k[2] * P[:, 2:]
        if Hs.shape[0] >= 3:
            T = np.pad(Hs, ((1, 1), (0, 0)), mode="edge")
            Hs = k[0] * T[:-2] + k[1] * T[1:-1] + k[2] * T[2:]
        return Hs

    def demodulate(self, rx: np.ndarray, n_syms: int) -> tuple[np.ndarray, float, float]:
        """Return (equalised data symbols (S,n_data), noise var, SNR dB)."""
        rx = np.asarray(rx)
        Y = np.empty((n_syms, self.n_fft), dtype=complex)
        for s in range(n_syms):
            seg = rx[s * self.sym_len + self.cp : s * self.sym_len + self.sym_len]
            if seg.size < self.n_fft:
                Y[s] = 0.0
                continue
            Y[s] = np.fft.fft(seg)
        Hp = Y[:, self.pilots] / self.pilot_syms[None, :]
        Hs = self._smooth_pilots(Hp)
        Hd = np.empty((n_syms, self.n_data), dtype=complex)
        for s in range(n_syms):
            Hd[s] = np.interp(self.data, self.pilots, Hs[s].real) + 1j * np.interp(
                self.data, self.pilots, Hs[s].imag
            )
        X = Y[:, self.data] / np.where(np.abs(Hd) < 1e-12, 1e-12, Hd)
        # Noise estimate from the 3-point curvature of the pilot channel estimate:
        # for a smooth channel the residual is dominated by noise (factor 1.5).
        if self.n_pilots >= 3:
            res = Hp[:, 1:-1] - 0.5 * (Hp[:, :-2] + Hp[:, 2:])
            var_h = float(np.mean(np.abs(res) ** 2) / 1.5)
        else:  # pragma: no cover
            var_h = 1.0
        # The residual lives in channel units; equalisation divides it out, so
        # refer it to the mean channel power before using it for LLR scaling.
        hpow = float(np.mean(np.abs(Hp) ** 2)) or 1.0
        var = max(var_h / hpow, 1e-12)
        post = float(np.mean(np.abs(X) ** 2))
        snr_db = 10.0 * np.log10(max(post / var - 1.0, 1e-4))
        return X, var, snr_db

    # -------------------------------------------------------------------- sync
    def acquire(
        self,
        rx: np.ndarray,
        max_cfo_hz: float = 90.0,
        coarse_step_hz: float = 8.0,
        decim: int = 4,
    ) -> tuple[int, float] | None:
        """Find (frame start sample, carrier frequency offset in Hz)."""
        rx = np.asarray(rx)
        pre = self.preamble
        if rx.size < pre.size + 8:
            return None
        # --- coarse: decimated full search
        rxd = _decimate(rx, decim)
        pred = _decimate(pre, decim)
        fsd = self.fs / decim
        idx = np.arange(rxd.size)
        best = (-1.0, 0.0, 0)
        for f in np.arange(-max_cfo_hz, max_cfo_hz + 1e-9, coarse_step_hz):
            y = rxd * np.exp(-2j * np.pi * f * idx / fsd)
            c = np.abs(fftconvolve(y, pred[::-1].conj(), mode="valid"))
            i = int(np.argmax(c))
            if c[i] > best[0]:
                best = (float(c[i]), float(f), i * decim)
        if best[0] <= 0:
            return None
        # --- fine: full-rate search on a window around the coarse peak
        i0 = best[2]
        lo = max(0, i0 - 3 * self.sym_len)
        hi = min(rx.size, i0 + 4 * self.sym_len)
        seg = rx[lo:hi]
        if seg.size < pre.size:
            return None
        sidx = np.arange(lo, hi)
        best_fine = (-1.0, best[1], 0)
        for f in np.arange(best[1] - coarse_step_hz, best[1] + coarse_step_hz + 1e-9, 1.0):
            y = seg * np.exp(-2j * np.pi * f * sidx / self.fs)
            c = np.abs(fftconvolve(y, pre[::-1].conj(), mode="valid"))
            i = int(np.argmax(c))
            if c[i] > best_fine[0]:
                best_fine = (float(c[i]), float(f), i)
        start = lo + best_fine[2]
        # Deliberately *not* refined further.  The matched filter's peak is flat
        # over the guard interval, so ``start`` can land a sample or two either
        # side of the true boundary; measured on the 492 Hz profile at 1 dB the
        # raw bit error rate was identical (about 5 %) at -1, 0 and +1 samples, so
        # the alignment does not need to be exact -- what broke those frames was
        # the receiver treating a one-sample overrun as a truncated capture (see
        # ``Modem.receive``).
        return int(start), float(best_fine[1])

    def derotate(self, rx: np.ndarray, cfo_hz: float) -> np.ndarray:
        n = np.arange(np.asarray(rx).size)
        return np.asarray(rx) * np.exp(-2j * np.pi * cfo_hz * n / self.fs)

    # ------------------------------------------------------------ bit plumbing
    def bits_to_symbols(self, bits: np.ndarray, bps: int) -> np.ndarray:
        """(S*n_data*bps) bits -> (S, n_data) complex symbols, carrier-major."""
        from .qam import qam

        bits = np.asarray(bits, dtype=np.uint8).ravel()
        if bits.size % (self.n_data * bps):
            raise ValueError("bit count does not fill whole OFDM symbols")
        s_count = bits.size // (self.n_data * bps)
        grid = bits.reshape(s_count * self.n_data, bps)
        syms = qam(bps).map_bits(grid)
        return syms.reshape(s_count, self.n_data)

    def symbols_to_llr(self, syms: np.ndarray, bps: int, noise_var: float) -> np.ndarray:
        """(S, n_data) equalised symbols -> bits in the same order as above."""
        from .qam import qam

        syms = np.asarray(syms)
        llr = qam(bps).demap(syms.ravel(), noise_var)
        return llr.ravel()
