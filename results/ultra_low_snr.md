# Ultra-low-SNR architectures at 10-21 bps

Three coding shapes were measured on the same short message (40 payload bits +
CRC-16, 56 information bits) and the same channels the compact ladder uses.
Every block is accepted only when its CRC-16 passes.  All runs are Python on one
core; time is per block.

## Non-coherent 16-FSK (4 bits/tone, energies only)

The modem's low-speed mode: one tone out of 16 per symbol, no channel estimate.
32 tones carry 128 coded bits (rate 0.438); the K=14 code needs 140 coded bits,
so 35 tones, and its rate is 0.400.  40 blocks per point.

| Eb/N0 | LDPC (128, 56) | conv K=9 | conv K=14 |
|------:|---------------:|---------:|----------:|
| 4 dB | 57.5 % (16 ms) | 45.0 % (8 ms) | **35.0 %** (98 ms) |
| 5 dB | 27.5 % (7 ms) | 17.5 % (7 ms) | **10.0 %** (96 ms) |
| 6 dB | 5.0 % (3 ms) | 2.5 % (7 ms) | 5.0 % (93 ms) |
| 7 dB | 0.0 % (2 ms) | 0.0 % (7 ms) | 0.0 % (93 ms) |

The convolutional codes are about 0.5-1 dB ahead of the LDPC at 10 % block
error, and K=14 is ahead of K=9 by roughly another 0.3-0.5 dB at 5 dB -- but it
costs 13x the decode time for that.  At 6 dB the three converge: with 56
information bits in 32 tones, there is not much left for extra memory to buy.
This is the JS8Call shape and it holds up: small alphabet, non-coherent
detection, long-memory Viterbi.

Caveat: the detector is ideal (perfect tone orthogonality, no timing or
frequency error, no impulsive noise).  The ladder's own 10 bps rung measures
much worse in the full OFDM chain, so these numbers are a code comparison, not
a predicted on-air threshold.

## BCH product code with Chase-2 (BPSK/AWGN)

A (31, 16, 3)^2 product code: 961 bits carrying 240 payload + 16 CRC, rate
0.266, minimum distance 49.  Decoding is two iterations of row/column Chase-2
with p=4 least-reliable bits and a competition-based extrinsic term.  The LDPC
baseline is the (960, 256) code at the same rate.  20 blocks per point.

| Eb/N0 | BCH product + Chase-2 | LDPC (960, 256) |
|------:|----------------------:|----------------:|
| 2 dB | 100.0 % (445 ms) | **0.0 %** (22 ms) |
| 3 dB | 95.0 % (485 ms) | **0.0 %** (13 ms) |
| 4 dB | 40.0 % (495 ms) | **0.0 %** (9 ms) |
| 5 dB | 15.0 % (526 ms) | **0.0 %** (8 ms) |
| 6 dB | 5.0 % (550 ms) | **0.0 %** (7 ms) |

The product code is about 4 dB behind the LDPC and 60-80x slower here.  At 961
bits the product is too short for its structure to pay: the row code corrects
three errors in 31 bits, but at 2-4 dB the rows arrive with far more than that,
and the iteration cannot climb out.  Product codes earn their reputation at
much longer blocks (thousands of bits per dimension) and on burst channels,
where the second dimension spreads a burst across many rows; neither applies to
a 10-21 bps frame.  Chase-2 is deterministic and needs no matrix inversion, as
claimed, but determinism is not sensitivity.

## The Viterbi rung inside the real OFDM chain

The compact ladder now has a `16FSK 1/2 V9` rung beside the LDPC one: same 15
carriers, 12 symbols per block, `block_mult` 12, 576 coded bits and 144 symbols
per block, no repetition.  The convolutional rung carries 280 information bits
(264 payload) against 288 (272 payload) and runs at 40.5 bps against 41.7 bps.
Measured through `Phy(**COMPACT)` -- preamble, 12-symbol header, pilot-based
demodulation, interleaver, CRC acceptance -- on AWGN, 6 frames x 6 blocks per
point (36 blocks), fixed seeds:

| channel S/N | 16FSK 1/2 LDPC | 16FSK 1/2 V9 |
|------------:|---------------:|-------------:|
| -5.0 dB | 66.7 % | **44.4 %** |
| -4.5 dB | 22.2 % | **8.3 %** |
| -4.0 dB | **0.0 %** | 2.8 % |

At the 10 % block-error criterion the Viterbi rung crosses at about -4.5 dB
against -4.2 dB for the LDPC, so the gain on the real chain is roughly 0.3 dB;
at -5.0 dB it is 22 points ahead.  That is smaller than the 0.5-1 dB the ideal
non-coherent comparison showed, which is the expected cost of pilot
interpolation, header recovery and sync.

The 2.8 % at -4.0 dB is one block in 36; with 36 blocks per point the sampling
uncertainty is around +-0.5 dB, so treat this as "the Viterbi rung is slightly
better and no slower", not as a precise threshold shift.

The published compact threshold table (`results/thresholds_awgn_compact.md`) is
the established 4-frame measurement and predates this rung.  A 2-frame re-sweep
was noisier than those numbers and is not shipped; the rung is appended at the
end of `COMPACT_TABLE` so every existing MCS index, and that table, stays valid.

## What this means for the ladder

* **Do not add a product-code rung.** At this block size it loses about 4 dB to
  the LDPC that is already there, at 60x the decode cost.
* **The M-FSK + Viterbi shape is worth keeping** as the low-rate option: at
  10-21 bps it is 0.5-1 dB better than the LDPC on the same tones, and decode
  cost is trivial at K=9.
* **K=14 is a marginal buy.** It is the best of the three at 5 dB, but 13x the
  decode time and 3 extra tones per block for a few tenths of a decibel.  K=9 is
  the sensible default; K=14 is there if a future rung needs the last 0.3 dB.

Reproduce:

```
PYTHONPATH=. python3 tools/compare_low_rate.py --trials 40 --snr 4 5 6 7
PYTHONPATH=. python3 tools/compare_tpc.py --trials 20 --snr 2 3 4 5 6
PYTHONPATH=. python3 tools/conv_generators.py --constraint 14 --samples 200
```
