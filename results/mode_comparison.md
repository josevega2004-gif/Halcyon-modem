# Mode, speed and S/N: Halcyon vs xara and aria (VARA core)

All S/N figures are channel signal-to-noise ratio in a 2.742 kHz passband, at the per-mode threshold: block error <= 10 % for Halcyon, and the level's decode threshold for xara/aria. Halcyon's wide rows are `results/thresholds_awgn.md`, and the README table and `frame.py`'s wide `snr_channel` use the same values; its narrow profiles are measured in their own bands and converted at fixed noise density by 10 log10(2742.1875 / B). Speeds are mode rates, not measured ARQ link goodput.

xara and aria are one ladder, not two: aria is the VARA core, and xara's VARA-compatible level table in the local xara tree (`~/xara/crates/xara-phy/src/hf/level.rs`) is pinned to the core vectors (`~/xara/vectors/levels.json`) by `tests/levels.rs::all_records`, which compares the full records including `thr` and `netbps`. These are the core thresholds, not an independent measurement of xara. The core's reported-S/N axis uses a 2.5 kHz reference (`REF_2500_DB`); the last column subtracts 0.40 dB for 2.742 kHz. The table is therefore the core ladder that xara implements.

`mini` and `micro` are bench/console profiles only, not host fallbacks: their 600-byte link goodput measured 1.4–1.9 bps, below the 6 bps floor. The host ladder stops at `nano`.

## Halcyon

| Profile | Mode | Speed | S/N (2.742 kHz) |
|---|---:|---:|---:|
| wide | QPSK 1/4 r4 | 227 bps | -3.0 dB |
| wide | QPSK 1/8 L4 | 453 bps | -2.0 dB |
| wide | QPSK 1/4 L2 | 906 bps | 0.0 dB |
| wide | QPSK 1/3 | 1208 bps | +2.0 dB |
| wide | QPSK 1/2 | 1812 bps | +2.6 dB |
| wide | QPSK 3/4 | 2719 bps | +5.7 dB |
| wide | QPSK 7/8 | 3172 bps | +7.0 dB |
| wide | 16QAM 1/2 | 3625 bps | +8.6 dB |
| wide | 16QAM 2/3 | 4833 bps | +10.3 dB |
| wide | 16QAM 3/4 | 5438 bps | +11.5 dB |
| wide | 16QAM 7/8 | 6344 bps | +13.7 dB |
| wide | 64QAM 2/3 | 7250 bps | +15.6 dB |
| wide | 64QAM 3/4 | 8156 bps | +17.4 dB |
| wide | 64QAM 5/6 | 9062 bps | +18.8 dB |
| wide | 64QAM 7/8 | 9516 bps | +19.6 dB |
| wide | 256QAM 5/6 | 12083 bps | +24.1 dB |
| wide | 256QAM 8/9 | 12889 bps | +25.0 dB |
| robust | QPSK 1/3 | 250 bps | -4.9 dB |
| robust | QPSK 3/4 | 562 bps | -1.4 dB |
| robust | 16QAM 1/2 | 750 bps | +1.6 dB |
| compact | 16FSK 1/4 r2 | 10 bps | -15.0 dB |
| compact | 16FSK 1/4 | 21 bps | -13.5 dB |
| compact | 16FSK 1/2 | 42 bps | -11.5 dB |
| compact | 16FSK 3/4 | 62 bps | -10.0 dB |
| compact | BPSK 1/4 L2 | 78 bps | -7.0 dB |
| compact | 16FSK 1/2 V9 | 41 bps | -12.0 dB |
| deep | 16FSK 1/4 | 21 bps | -13.6 dB |
| deep | 16FSK 1/2 | 42 bps | -12.1 dB |
| deep | 16FSK 3/4 | 62 bps | -10.1 dB |
| nano | 8FSK 1/4 | 16 bps | -14.7 dB |
| nano | 8FSK 1/2 | 31 bps | -12.7 dB |
| mini | 16FSK 1/4 | 11.7 bps | -17.4 dB |
| micro | 8FSK 1/4 | 8.8 bps | -18.5 dB |

## xara / aria (VARA core)

| Bandwidth | Level | Mode | Speed | S/N (2.5 kHz) | S/N (2.742 kHz) |
|---:|---:|---|---:|---:|---:|
| 500 Hz | 1 | FSK 3b r2 | 18 bps | -13.6 dB | -14.0 dB |
| 500 Hz | 2 | FSK 3b r2 | 41 bps | -11.2 dB | -11.6 dB |
| 500 Hz | 3 | FSK 3b r3 | 61 bps | -10.4 dB | -10.8 dB |
| 500 Hz | 4 | PSK 1b r3 | 88 bps | -8.4 dB | -8.8 dB |
| 500 Hz | 5 | PSK 2b r3 | 177 bps | -5.4 dB | -5.8 dB |
| 500 Hz | 6 | PSK 2b r3 | 270 bps | -3.8 dB | -4.2 dB |
| 500 Hz | 7 | OFDM 2b r3 | 441 bps | -1.2 dB | -1.6 dB |
| 500 Hz | 8 | OFDM 2b r4 | 588 bps | +0.6 dB | +0.2 dB |
| 500 Hz | 9 | OFDM 2b r6 | 705 bps | +2.6 dB | +2.2 dB |
| 500 Hz | 10 | OFDM 3b r4 | 884 bps | +5.0 dB | +4.6 dB |
| 500 Hz | 11 | OFDM 3b r6 | 1060 bps | +6.4 dB | +6.0 dB |
| 500 Hz | 12 | OFDM 4b r7 | 1286 bps | +8.0 dB | +7.6 dB |
| 500 Hz | 13 | OFDM 5b r6 | 1543 bps | +10.6 dB | +10.2 dB |
| 2300 Hz | 1 | FSK 3b r2 | 18 bps | -13.6 dB | -14.0 dB |
| 2300 Hz | 2 | FSK 3b r2 | 41 bps | -11.2 dB | -11.6 dB |
| 2300 Hz | 3 | FSK 4b r3 | 82 bps | -9.4 dB | -9.8 dB |
| 2300 Hz | 4 | FSK 4b r3 | 175 bps | -6.6 dB | -7.0 dB |
| 2300 Hz | 5 | PSK 2b r3 | 270 bps | -3.6 dB | -4.0 dB |
| 2300 Hz | 6 | PSK 2b r3 | 363 bps | -2.4 dB | -2.8 dB |
| 2300 Hz | 7 | PSK 2b r3 | 549 bps | -0.6 dB | -1.0 dB |
| 2300 Hz | 8 | PSK 2b r3 | 735 bps | +0.4 dB | 0.0 dB |
| 2300 Hz | 9 | PSK 2b r3 | 922 bps | +1.3 dB | +0.9 dB |
| 2300 Hz | 10 | OFDM 2b r3 | 2011 bps | +5.4 dB | +5.0 dB |
| 2300 Hz | 11 | OFDM 2b r4 | 2682 bps | +7.2 dB | +6.8 dB |
| 2300 Hz | 12 | OFDM 2b r6 | 3219 bps | +8.5 dB | +8.1 dB |
| 2300 Hz | 13 | OFDM 3b r4 | 4025 bps | +11.2 dB | +10.8 dB |
| 2300 Hz | 14 | OFDM 3b r6 | 4830 bps | +13.2 dB | +12.8 dB |
| 2300 Hz | 15 | OFDM 4b r7 | 5872 bps | +14.2 dB | +13.8 dB |
| 2300 Hz | 16 | OFDM 5b r6 | 7050 bps | +17.2 dB | +16.8 dB |
| 2750 Hz | 1 | FSK 3b r2 | 18 bps | -13.6 dB | -14.0 dB |
| 2750 Hz | 2 | FSK 3b r2 | 41 bps | -11.2 dB | -11.6 dB |
| 2750 Hz | 3 | FSK 4b r3 | 82 bps | -9.4 dB | -9.8 dB |
| 2750 Hz | 4 | FSK 4b r3 | 175 bps | -6.6 dB | -7.0 dB |
| 2750 Hz | 5 | PSK 2b r3 | 270 bps | -3.6 dB | -4.0 dB |
| 2750 Hz | 6 | PSK 2b r3 | 363 bps | -2.4 dB | -2.8 dB |
| 2750 Hz | 7 | PSK 2b r3 | 549 bps | -0.6 dB | -1.0 dB |
| 2750 Hz | 8 | PSK 2b r3 | 735 bps | +0.4 dB | 0.0 dB |
| 2750 Hz | 9 | PSK 2b r3 | 922 bps | +1.3 dB | +0.9 dB |
| 2750 Hz | 10 | PSK 2b r3 | 1203 bps | +2.3 dB | +1.9 dB |
| 2750 Hz | 11 | OFDM 2b r3 | 2423 bps | +6.0 dB | +5.6 dB |
| 2750 Hz | 12 | OFDM 2b r4 | 3230 bps | +8.0 dB | +7.6 dB |
| 2750 Hz | 13 | OFDM 2b r6 | 3877 bps | +9.3 dB | +8.9 dB |
| 2750 Hz | 14 | OFDM 3b r4 | 4848 bps | +11.8 dB | +11.4 dB |
| 2750 Hz | 15 | OFDM 3b r6 | 5817 bps | +14.0 dB | +13.6 dB |
| 2750 Hz | 16 | OFDM 4b r7 | 7074 bps | +15.0 dB | +14.6 dB |
| 2750 Hz | 17 | OFDM 5b r6 | 8489 bps | +18.0 dB | +17.6 dB |
