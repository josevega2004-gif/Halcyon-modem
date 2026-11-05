# Narrow-bandwidth sensitivity sweep

Experimental only: these geometries are not in the production profile list or
the host selector.  All use a 960-point FFT (12.5 Hz carrier spacing), a
64-sample CP, and 32-block LDPC payloads at 12 symbols per block.  The channel
is AWGN.  S/N is the channel S/N in the geometry's own band; **2.7k S/N** is the
same value referred to the project's 2742 Hz reference at fixed noise density.

The table below is the **lowest tested S/N with block error at or below 10 %**,
which is the project's threshold criterion.  The true crossing lies between that
point and the next higher one, typically within 0.5-1 dB.  Each entry is 1-2
frames x 6 blocks except the 175 Hz two-frame refinement and the long-code runs
noted below.

## Best slow rung at each width

| Width | Carriers | Mode | Rate | ≤10 % at (own) | 2.7k S/N |
|---:|---:|---|---:|---:|---:|
| 100 Hz | 8 | 8FSK 1/4, 2304-bit code | 8.8 bps | −4.0 dB | **−18.4 dB** |
| 125 Hz | 10 | 8FSK 1/4 | 8.8 bps | −4.5 dB | −17.9 dB |
| 150 Hz | 12 | 8FSK 1/4 | 8.8 bps | −5.5 dB | −18.1 dB |
| 175 Hz | 14 | 8FSK 1/4 | 8.8 bps | −6.5 dB | **−18.5 dB** |
| 200 Hz | 16 | 8FSK 1/4 | 8.8 bps | −6.5 dB | −17.9 dB |
| 225 Hz | 18 | 8FSK 1/4 | 8.8 bps | −6.5 dB | −17.4 dB |
| 250 Hz | 20 | 8FSK 1/4, 2304-bit code | 8.8 bps | −8.0 dB | −18.4 dB |
| 300 Hz | 24 | 8FSK 1/4 | 8.8 bps | −8.5 dB | −18.1 dB |
| 350 Hz | 28 | 8FSK 1/4 | 8.8 bps | −8.5 dB | −17.4 dB |

The 100 Hz and 250 Hz long-code entries used `block_mult=64`; the rest used
`block_mult=32`.  The longer code bought about 0.5 dB at those two widths and
did not improve 175 Hz (58 % block error at −7.0 dB, versus 8 % at −6.5 dB for
the shorter code).

No configuration reached −20 dB at 2.7 kHz.  The best useful result is 175 Hz,
8FSK 1/4, 8.8 bps, at about −18.5 dB for ≤10 % block error.  The best 4-FSK
result was 4FSK 1/4 at 100 Hz, 5.9 bps, at about −18.9 dB; it buys 0.5 dB over
the 8-FSK rung for two-thirds the speed.  At 175 Hz 4-FSK 1/4 is also about
−18.5 dB, so it buys nothing there.

## Speed-versus-sensitivity knee

| Width | 8FSK 1/4 (8.8 bps) | 16FSK 1/4 (11.7 bps) | Pick |
|---:|---:|---:|---|
| 175 Hz | −18.5 dB | does not fit (14 carriers) | 8FSK |
| 200 Hz | −17.9 dB | −16.9 dB | 8FSK if the extra 1 dB matters |
| 225 Hz | −17.4 dB | −17.4 dB | **16FSK** (same S/N, 33 % faster) |
| 250 Hz | −17.9 dB | −16.9 dB | 8FSK if the extra 1 dB matters |
| 300 Hz | −18.1 dB | −17.1 dB | 8FSK if the extra 1 dB matters |
| 350 Hz | −17.4 dB | −17.4 dB | **16FSK** (same S/N, 33 % faster) |

16-FSK needs at least 16 carriers, so it cannot be used at 100-175 Hz with this
12.5 Hz spacing.

## 4-FSK control

Values are the 2.7k S/N at a ≤10 % block-error run; lower is better.

| Width | 4FSK 1/4 (5.9 bps) | 4FSK 1/2 (11.7 bps) | 8FSK 1/4 (8.8 bps) |
|---:|---:|---:|---:|
| 100 Hz | −18.9 dB | worse than −17.9 dB | −18.4 dB |
| 175 Hz | −18.5 dB | −16.5 dB | −18.5 dB |
| 250 Hz | −15.9 dB | −15.9 dB | −17.9 dB |
| 350 Hz | −16.4 dB* | −16.4 dB* | −17.4 dB |

\* 350 Hz 4-FSK still passed at −7.5 dB own-band; lower points were not
measured because it was already 1 dB behind 8-FSK.

4-FSK is not a win at a useful rate.  Its 1/4 rung is only competitive by
dropping to 5.9 bps, and its 1/2 rung is 1-2.5 dB less sensitive than 8FSK 1/4
or 16FSK 1/4 at the same 11.7 bps.  The 4-FSK result at 100 Hz is the one place
it looks better than 8-FSK, but only by 0.5 dB and at two-thirds the speed.

## Recommendation

- **Maximum sensitivity:** 175 Hz, 8FSK 1/4, 8.8 bps, about −18.5 dB at 2.7 kHz.
  This is the best point worth keeping; it is just short of the requested −20 dB.
- **Best speed/sensitivity compromise:** 225 Hz, 16FSK 1/4, 11.7 bps, at about
  −17.4 dB.  It gives a third more speed than 175 Hz for about 1.1 dB, and it is
  the same sensitivity as 350 Hz with less bandwidth.
- **Do not pick 4-FSK** at these widths: it is either below the 10 bps floor or
  less sensitive than 8/16-FSK at the same rate.
- **Defaults:** 175 Hz `micro` and 225 Hz `mini` are now host fallbacks in the
  bench, console and host ladder.  The host thresholds use the measured
  short-frame limits: `mini` from −15.5 dB and `micro` from −16.5 dB.  Those are
  stricter than the six-block bench thresholds because one-block short frames
  are marginal at the bench crossing.
- No 600-byte ARQ link was run at these points; the numbers are block-FER
  threshold data.

Raw point data: `results/narrow_sweep_a.csv`, `narrow_sweep_175.csv`,
`narrow_sweep_200_225.csv`, `narrow_sweep_250_350.csv`,
`narrow_sweep_250_350_low.csv`, `narrow_sweep_4fsk_a.csv`,
`narrow_sweep_4fsk_b.csv`, `narrow_sweep_175_best.csv`, and
`narrow_sweep_longcode.csv`.
