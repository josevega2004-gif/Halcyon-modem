# Short-block decoder comparison

The narrow ladder needs a code that works at 432–576 bits. This compares the
LDPC decoder already in the modem with two alternatives, on the same block and
the same channel:

* **LDPC** — log-domain sum-product, 50 iterations, syndrome exit.
* **LDPC + OSD-2** — sum-product, then ordered-statistics reprocessing when it
  fails: re-encode from the k most reliable columns, then flip pairs among the
  10 least reliable information bits.
* **Polar CA-SCL** — a power-of-two polar code with a CRC-16 tail and a list of
  8 successive-cancellation paths, ranked by that CRC.

All three carry the same information bits (payload + 16 CRC) in the same block,
and a block counts as delivered only when its CRC-16 passes, which is what the
modem accepts. BPSK over AWGN; decode time is per block in Python on one core.

## 512-bit block, k = 104 (88 payload + 16 CRC)

No rate matching needed: 512 is a power of two. 60 blocks per point.

| Eb/N0 | LDPC | LDPC + OSD-2 | polar CA-SCL L8 |
|------:|-----:|-------------:|----------------:|
| 0 dB | 66.7 % (39 ms) | 66.7 % (57 ms) | **38.3 %** (193 ms) |
| 1 dB | 25.0 % (25 ms) | 25.0 % (32 ms) | **5.0 %** (180 ms) |
| 2 dB | 1.7 % (12 ms) | 1.7 % (14 ms) | **0.0 %** (181 ms) |
| 3 dB | 0.0 % (8 ms) | 0.0 % (8 ms) | 0.0 % (179 ms) |

## 432-bit frame block (18 carriers x 12 symbols at QPSK)

This is the narrow profile's block, and the shape of the two rungs that were
dropped. The polar code has no 432-bit instance, so it uses a 512-bit mother
code with its 80 least reliable coordinates punctured: the encoder drops them
and the decoder erases them. They are frozen, so no information is removed,
only observations. 30 blocks per point.

k = 104, rate 104/432 = 0.241 (the QPSK 1/4 rung):

| Eb/N0 | LDPC | LDPC + OSD-2 | polar CA-SCL L8 |
|------:|-----:|-------------:|----------------:|
| 0 dB | 83.3 % (40 ms) | 83.3 % (54 ms) | **26.7 %** (172 ms) |
| 1 dB | 20.0 % (23 ms) | 20.0 % (25 ms) | **3.3 %** (176 ms) |
| 2 dB | 0.0 % (12 ms) | 0.0 % (12 ms) | 0.0 % (172 ms) |
| 3 dB | 0.0 % (7 ms) | 0.0 % (8 ms) | 0.0 % (174 ms) |

k = 216, rate 0.5 (the QPSK 1/2 rung):

| Eb/N0 | LDPC | LDPC + OSD-2 | polar CA-SCL L8 |
|------:|-----:|-------------:|----------------:|
| 2 dB | **10.0 %** (14 ms) | 10.0 % (15 ms) | 13.3 % (262 ms) |
| 3 dB | 0.0 % (5 ms) | 0.0 % (5 ms) | 0.0 % (263 ms) |
| 4 dB | 0.0 % (3 ms) | 0.0 % (3 ms) | 0.0 % (294 ms) |
| 5 dB | 0.0 % (3 ms) | 0.0 % (3 ms) | 0.0 % (274 ms) |

## What it means

**At the low-rate rung, polar comes out about 1.2 dB ahead.** Puncturing 80 of
512 coordinates costs something, but not the advantage: 27 % against 83 % at
0 dB and 3 % against 20 % at 1 dB, with both closing at 2 dB. This is the (432,
104) corner where the LDPC construction was weak -- the rung that had to be
dropped -- and it is exactly the corner where a CRC-aided list decoder is
supposed to win.

**At rate 1/2 there is nothing to gain.** Polar is 13 % against LDPC's 10 % at
2 dB, which is four against three failed blocks out of 30: inside the noise on
this sample, and both are closed at 3 dB. Puncturing a rate-0.42 mother code up
to rate 0.5 gives away what the list decoder would otherwise win back.

**OSD-2 still changes nothing**, at either k. It matched plain LDPC exactly at
every point; at these S/Ns the failures are not order-limited.

**Cost.** Polar is 15–30x the LDPC decode time in Python (170–290 ms against
3–40 ms per block), because the list keeps eight paths through all 512 bit
decisions. A compiled SCL decoder would narrow that, but it remains the more
expensive option.

## Caveats

* 30 blocks per point puts roughly +-8 percentage points on a 10 % figure. The
  k = 104 advantage at 0 and 1 dB is much larger than that; the k = 216 row is
  not resolvable.
* Puncturing the least reliable coordinates is one rate-matching choice. 5G
  uses a mix of puncturing and shortening, and shortening the 80 coordinates
  properly could recover part of the loss; that needs a mother code of higher
  dimension and a decoder that enforces the shortening constraints, which this
  one does not.
* Python timings; a C++ SCL decoder would be several times faster.
* This is a code-level measurement over AWGN, not an on-air or full-OFDM one:
  the modem still codes every frame with the LDPC and the header stays LDPC.
  Wiring polar into the payload path would change the frame format, the C++
  port and the threshold tables, and would be worth doing only after the
  k = 104 result is confirmed on the OFDM chain.

Reproduce with:

```
PYTHONPATH=. python3 tools/compare_short_block.py --trials 60 --snr 0 1 2 3
PYTHONPATH=. python3 tools/compare_short_block.py --length 432 --k 104 --trials 30 --snr 0 1 2 3
PYTHONPATH=. python3 tools/compare_short_block.py --length 432 --k 216 --trials 30 --snr 2 3 4 5
```

## Tuning: list size, CRC width, OSD parameters

Polar at the 432-bit block, k = 104, 12 blocks per point.  A *false accept* is
a block whose CRC passes with the wrong payload.

| Eb/N0 | L=8 | L=32 | L=64 | L=64, CRC-24 |
|------:|----:|-----:|-----:|-------------:|
| 0 dB | 16.7 % (196 ms) | 16.7 % (710 ms) | **8.3 %** (1 281 ms) | 16.7 % (1 263 ms) |
| 1 dB | 0.0 % (183 ms) | 0.0 % (650 ms) | 0.0 % (1 247 ms) | 0.0 % (1 219 ms) |
| 2 dB | 0.0 % (187 ms) | 0.0 % (655 ms) | 0.0 % (1 404 ms) | -- |

False accepts: zero in every run.  With 16 CRC bits and a list of 64, the
false-alarm probability is about 64/2^16 = 0.1 % per block, two orders of
magnitude below the 10 % block error we operate at, and the measurement agrees.

What the sweep says:

* **List 64 helps at 0 dB, at 7x the time.** It halved the miss rate in this
  sample (one block of 12 against two), while L=32 was indistinguishable from
  L=8. Twelve blocks is a small sample -- one block is 8 points -- so the L=64
  gain is suggestive, not established, but the direction is the expected one.
* **A longer CRC does not buy sensitivity.** It removes false accepts that were
  not happening, and it costs 8 payload bits at fixed k. CRC-16 is enough for
  this list size.
* **Channel-state weighting has nothing to act on here.** In AWGN the LLRs are
  already 2y/sigma^2; scaling by a per-bit amplitude only matters under fading,
  which needs the OFDM chain and its per-carrier estimates, not this harness.

LDPC at the same block, 30 blocks per point; every OSD variant runs only after
sum-product fails, so these are already hybrid BP+OSD results.

| Eb/N0 | BP only | BP50 + OSD2 w10 | BP10 + OSD2 w10 | BP50 + OSD2 w16 | BP50 + OSD3 w16 |
|------:|--------:|----------------:|----------------:|----------------:|----------------:|
| 0 dB | 80 % (78 ms) | 80 % (116 ms) | 100 % (56 ms) | 80 % (103 ms) | 80 % (86 ms) |
| 1 dB | 10 % (31 ms) | 10 % (33 ms) | 60 % (43 ms) | 10 % (39 ms) | 10 % (35 ms) |

* OSD-2 and OSD-3 change nothing at either point, with a 10- or 16-bit window.
* **Starving BP before OSD is much worse**, not better: 10 iterations gives
  100 % at 0 dB against 80 %, and 60 % at 1 dB against 10 %.  The sorter needs
  converged LLRs; the hybrid order was already right.  The gain is still zero
  because these failures are not order-limited -- re-encoding from the most
  reliable columns does not reach the transmitted word, which is what the
  syndrome/CRC diagnostics showed earlier.

Reproduce the tuning sweep with
`python3 tools/tune_decoders.py --trials-polar 12 --trials-osd 30`.
