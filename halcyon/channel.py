"""Channel models: AWGN, Watterson-style HF fading, tuning offset, timing error.

Reference point for SNR: signal-to-noise ratio measured in the occupied
2.75 kHz passband (the FCC's 2.8 kHz HF limit), which is how HF operators quote S/N.  Because the simulated complex baseband is 12 kHz wide, noise
power is spread accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def awgn(x: np.ndarray, snr_db: float, fs: float, bandwidth_hz: float, rng) -> np.ndarray:
    p = float(np.mean(np.abs(x) ** 2))
    noise_power = p * (fs / bandwidth_hz) / (10.0 ** (snr_db / 10.0))
    n = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    return x + np.sqrt(noise_power / 2.0) * n


def fractional_delay(x: np.ndarray, delay: float) -> np.ndarray:
    """Delay by a (possibly fractional) number of samples using a phase ramp.

    The ramps are applied to a zero-padded copy of ``x`` so the shift is *linear*.
    A circular shift -- transforming the burst at its own length -- wraps the last
    samples of the frame onto its start: that hands the receiver a free cyclic
    prefix and destroys the tail of every fading capture, which is exactly the
    effect a fading measurement is supposed to isolate.  Samples shifted in at
    the front (a negative delay) or past the end come from and go to the zero
    padding, as a real delay line would.
    """
    if abs(delay) < 1e-9:
        return x
    x = np.asarray(x)
    n = x.size
    total = n + int(np.ceil(abs(delay))) + 2
    f = np.fft.fftfreq(total)
    shifted = np.fft.ifft(np.fft.fft(x, total) * np.exp(-2j * np.pi * f * delay))
    return shifted[:n]


def doppler_process(n: int, fs: float, doppler_hz: float, rng) -> np.ndarray:
    """Complex Gaussian fading process with a Gaussian Doppler spectrum."""
    if doppler_hz <= 0:
        return np.ones(n, dtype=complex)
    nfft = 1 << int(np.ceil(np.log2(max(2 * n, 1024))))
    white = rng.standard_normal(nfft) + 1j * rng.standard_normal(nfft)
    f = np.fft.fftfreq(nfft, 1.0 / fs)
    shape = np.exp(-0.5 * (f / doppler_hz) ** 2)
    g = np.fft.ifft(np.fft.fft(white) * np.sqrt(shape))[:n]
    return g / np.sqrt(np.mean(np.abs(g) ** 2))


@dataclass
class Channel:
    """Composite HF channel: multipath fading -> offset -> timing -> noise."""

    snr_db: float
    fs: float = 12000.0
    bandwidth_hz: float = 2742.1875  # 117 active carriers at 23.4375 Hz
    cfo_hz: float = 0.0
    timing: float = 0.0
    # (delay in ms, relative gain in dB, doppler spread in Hz)
    taps: tuple[tuple[float, float, float], ...] = ((0.0, 0.0, 0.0),)
    seed: int = 0

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=complex)
        y = np.zeros(x.size, dtype=complex)
        for delay_ms, gain_db, fd in self.taps:
            g = 10.0 ** (gain_db / 20.0) * doppler_process(x.size, self.fs, fd, self.rng)
            y += g * fractional_delay(x, delay_ms * 1e-3 * self.fs)
        p_in = float(np.mean(np.abs(x) ** 2))
        p_out = float(np.mean(np.abs(y) ** 2))
        if p_out > 0:
            y *= np.sqrt(p_in / p_out)
        if abs(self.timing) > 1e-9:
            y = fractional_delay(y, self.timing)
        if self.cfo_hz:
            n = np.arange(y.size)
            y = y * np.exp(2j * np.pi * self.cfo_hz * n / self.fs)
        return awgn(y, self.snr_db, self.fs, self.bandwidth_hz, self.rng)


# Watterson presets: (CCIR-style) good / moderate / poor HF paths.
PRESETS: dict[str, tuple[tuple[float, float, float], ...]] = {
    "awgn": ((0.0, 0.0, 0.0),),
    "good": ((0.0, 0.0, 0.1), (2.0, -6.0, 0.1)),
    "moderate": ((0.0, 0.0, 0.5), (1.0, -3.0, 1.0)),
    "poor": ((0.0, 0.0, 1.0), (2.0, -3.0, 2.0), (4.0, -6.0, 1.0)),
}


def make_channel(kind: str, snr_db: float, seed: int = 0, **kw) -> Channel:
    return Channel(snr_db=snr_db, taps=PRESETS[kind], seed=seed, **kw)
