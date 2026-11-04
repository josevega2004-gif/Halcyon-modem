# Halcyon emission bandwidth and emission type

Measured 99 % occupied bandwidth of the transmitted waveform (Welch PSD,
Blackman window), against 47 CFR 97.307(f)(3) 2.8 kHz and the 2.7 kHz that
IARU Region 1 band plans allow in their wide data segments.

| profile | carriers | data / pilots | carrier span | 99 % occupied | 99.9 % | FCC 2.8 kHz | IARU 2.7 kHz |
|---|---:|---:|---:|---:|---:|---|---|
| default (FCC 2.8 kHz) | 117 | 87 / 30 | 2742 Hz | 2729 Hz | 3778 Hz | pass | over |
| IARU R1 narrow (2.7 kHz) | 111 | 83 / 28 | 2602 Hz | 2588 Hz | 3858 Hz | pass | pass |
| robust (562 Hz) | 24 | 18 / 6 | 562 Hz | 608 Hz | 3146 Hz | pass | pass |
| compact (492 Hz) | 21 | 15 / 6 | 492 Hz | 541 Hz | 3154 Hz | pass | pass |

**Not a spread-spectrum emission.** The 117 subcarriers each carry
independent coded bits at 23.4 baud, with no spreading code, no frequency
hopping and no chipping, so there is no processing gain and the information
rate (12889 bps peak, 4.7 b/s/Hz in
the occupied bandwidth) is a large fraction of the emission bandwidth -- the
opposite of a spread signal.  47 CFR 97.305(c) authorizes SS emissions only at
222 MHz and above, so on HF an SS emission is unauthorized rather than merely
discouraged.

The default layout fits the FCC limit with 71 Hz to
spare.  HF data under 97.307(f)(3) must use a specified digital code; 97.309(a)(4)
covers any technique whose characteristics are documented publicly (the clause
written for CLOVER, G-TOR and Pactor), which this repository is.
