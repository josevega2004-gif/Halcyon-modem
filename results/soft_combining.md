# Soft combining across burst retransmissions

A failed burst's per-block LLRs are kept, and the retransmission is decoded on the
sum of both looks instead of starting cold. Measured on the 492 Hz 10.4 bps rung
(16-FSK 1/4 r2), AWGN, 3 blocks per burst, 3 bursts per point; the two looks use
independent noise realisations.

| channel S/N | blocks decoded, first attempt | after one combining step |
|---:|---:|---:|
| -8.5 dB | 0 / 9 | **5 / 9** |
| -8.0 dB | 0 / 9 | **8 / 9** |

Every block that decodes after combining is one the first attempt had no chance
at, and the payload rate stays 10.4 bps: the extra sensitivity is bought with
airtime, not with a slower rung. Each combining step adds roughly 3 dB of Eb/N0,
so the gain saturates as retransmissions accumulate — it is a floor-lifter for
links that would otherwise not close at all, not a substitute for a good path.

## It does not fix the ACK path (measured, unresolved)

The combining benefit is real at the modem and it is wired into the link, but a
link still cannot complete at these S/Ns, and the reason is the acknowledgement,
not the payload. At −7.4 dB channel S/N a 32-byte transfer decoded 2/2 blocks on
2 of 3 bursts once the previous LLRs were added, while **every ACK was lost**, so
the sender never learned and the transfer ended with 0 of 256 bits delivered.

Diagnosed directly on the ACK waveform (12-symbol rate-1/4 header, no payload):

| channel S/N | ACK header decoded (4 seeds) |
|---:|---|
| −7.4 dB | 0 / 4 |
| −6.0 dB | 0 / 4 |
| −4.0 dB | 0 / 4 |
| 0.0 dB | 4 / 4 |

Repeating the ACK header twice — mirroring the data frame's own header repetition —
did not move that curve (still 0/4 at −4.0 dB), which rules out coding and points
at acquisition/CFO estimation on a burst this short: the same 24-symbol header
decodes at −7.4 dB when it is part of a full-length data frame. That is an open
defect, and until it is fixed the effective floor of a link is set by its ACKs.

## A second look is not a free win at the edge

Feeding a *decoded* block's own LLRs back does not reliably decode again. At
7.0 dB on QPSK 7/8 — 2.5 dB under that rung's threshold — the stored LLRs
converged at 1x, but the same vector scaled by 0.5x or 2x did not; at 8 and
9 dB every scale converged. The decision at the threshold is sensitive to LLR
scale inside the sum-product stage, so combining is applied only to blocks that
failed, and the console skips the second look once the first one decodes.
