# Halcyon and xara: 600-byte link comparison

Both modems transferred the same 600-byte payload over AWGN. Halcyon used its
wide profile (2.742 kHz) and adaptive burst ARQ; xara 0.1.3 used its 2300 Hz
mode in the unregistered configuration, driven through its two-station audio
harness. The S/N column is the channel ratio in the 2.7 kHz passband, computed
from the measured transmit power and the injected noise, not from either
modem's S/N estimate.

| S/N (2.7 kHz) | xara delivered | xara goodput | Halcyon goodput | Halcyon complete |
|--------------:|---------------:|-------------:|----------------:|-----------------:|
| -8.0 dB | 508 / 600 B, timeout | -- | 0 bps | 0 % |
| -6.0 dB | 600 B | 63 bps | 0 bps | 0 % |
| -4.4 dB | 600 B | 121 bps | 0 bps | 0 % |
| -2.1 dB | 600 B | 121 bps | 168 bps | 100 % |
| -0.1 dB | 600 B | 121 bps | 345 bps | 100 % |
| 1.7 dB | 600 B | 121 bps | 576 bps | 100 % |
| 3.9 dB | 600 B | 121 bps | 730 bps | 100 % |
| 5.9 dB | 600 B | 121 bps | 730 bps | 100 % |
| 8.4 dB | 600 B | 121 bps | 917 bps | 100 % |
| 10.0 dB | 600 B | 121 bps | 1376 bps | 100 % |
| 11.9 dB | 600 B | 121 bps | 1546 bps | 100 % |

All Halcyon rows were remeasured after the ACK and sync fixes below. The xara
values are historical measurements and were not rerun.

## The low-S/N zeros were partly a control-frame bug

At -2 dB the data block decoded on every attempt, but the ACK header was lost
8/8 and the link discarded the good data until it gave up. The ACK was sent with
one header copy while the low-MCS data header was repeated four times. The ACK
now follows the data rung's header repetition; narrow profiles get a four-copy
control floor. After the fix, the wide profile completes 600 bytes at 168 bps
at -2.1 dB and 47 bps at -3 dB, instead of the previous 0 %.

The remaining -4.4 dB zero is not the same bug. The wide profile's bottom rung
is 227 bps at a measured -3.0 dB threshold. Starting the link directly at MCS 0
and allowing 16 attempts delivered 120 bytes at -4 dB, but only at 18 bps --
too slow and still short of xara's 121 bps. Retrying the existing bottom rung is
not enough.

## Narrow compact: 600 bytes at -6, -8 and -12 dB

The 492 Hz compact ladder is the narrow-geometry answer. At a fixed noise
density it collects 7.5 dB less noise than the 2.74 kHz layout, and its 16-FSK
payload needs no channel estimate. Measured on the same 600-byte payload, with
S/N referred to the 2.7 kHz channel and eight attempts per burst:

| channel S/N (2.7 kHz) | geometry | bandwidth | rung | delivered | goodput | frames | airtime |
|---:|---|---:|---|---:|---:|---:|---:|
| -6.0 dB | compact | 492 Hz | 16FSK 3/4 (62 bps) | 4 800 / 4 800 bits | **55.5 bps** | 1 | 86.4 s |
| -8.0 dB | compact | 492 Hz | 16FSK 1/2 (42 bps) | 4 800 / 4 800 bits | **36.5 bps** | 2 | 131.4 s |
| -8.0 dB | deep test | 375 Hz | 16FSK 3/4 (62 bps) | 4 800 / 4 800 bits | **55.2 bps** | 1 | 86.9 s |
| -10.0 dB | deep test | 375 Hz | 16FSK 1/2 (42 bps) | 4 800 / 4 800 bits | **27.0 bps** | 3 | 177.7 s |
| -8.0 dB | nano test | 234 Hz | 8FSK 1/2 (31 bps) | 4 800 / 4 800 bits | **27.0 bps** | 2 | 177.6 s |
| -10.0 dB | nano test | 234 Hz | 8FSK 1/4 (16 bps) | 4 800 / 4 800 bits | **12.8 bps** | 4 | 373.7 s |
| -12.0 dB | compact | 492 Hz | 16FSK 1/4 (21 bps) | 4 800 / 4 800 bits | **5.9 bps** | 11 | 812.3 s |
| -12.0 dB | deep test | 375 Hz | 16FSK 1/4 (21 bps) | 4 800 / 4 800 bits | 6.6 bps | 10 | 730.7 s |
| -12.0 dB | nano test | 234 Hz | 8FSK 1/4 (16 bps) | 4 800 / 4 800 bits | **11.4 bps** | 6 | 422.3 s |

All runs completed with no header failures or retransmissions. The 10 bps rung
was not used. The 375 Hz deep test is 1.2 dB narrower than compact and wins
clearly at -8 and -10 dB; the 234 Hz nano test is narrower still and wins at
-12 dB because its 16 bps 8-FSK rung needs less signal than the 21 bps 16-FSK
rung. Narrower is not automatically faster: at -8 dB nano carries half of
deep's goodput. The host uses compact normally, deep from -4 dB and nano at
-12 dB, with no slower fallback: mini and micro remain test profiles in the
bench and console.

## Closing -4 to -6 dB without a 10 bps mode

The direct change is to make the wide bottom rung twice as long: 8x repetition
instead of 4x. That is about 114 bps, needs roughly 3 dB less signal, and puts
the target near -5.5 dB -- between xara's 121 bps at -4.4 dB and 63 bps at
-6 dB. Its header and ACK would repeat with it, so the control frames get the
same margin. This keeps the existing MCS index and does not add a slow mode.

The change is a protocol/table change, not a link bug: it needs a fresh
threshold measurement, a matching update to the C++ ladder, and a 600-byte link
run at -6, -5 and -4 dB before it can be published. A new wide rung at 60-120
bps is the alternative, but inserting it at the bottom shifts every existing
MCS index, so 8x repetition of the current bottom rung is the smaller change.

## Deep-FSK attempt: not shipped

xara's low levels are narrow 8/16-FSK with turbo coding, not repeated OFDM.
A Halcyon-only 16-FSK profile was built to test that direction: 128-point FFT,
83.3 baud, 16 non-coherent tones, a 32-symbol header and LDPC rates 1/8, 1/4
and 1/2. It used Halcyon's own preamble, frame and code; no xara tone table,
frame or turbo code was copied, so it could not interoperate at any speed.

The data path did reach -4 dB: one 1 536-bit block decoded at rates 1/8 (42 bps),
1/4 (83 bps) and 1/2 (167 bps) at approximately -4, -2 and 0 dB respectively.
The link did not. At -4 dB the data header and block decoded, but the ACK header
failed 12/12 with four copies and still failed 11/12 with sixteen copies, even
though a data header decoded at the same S/N. Longer headers and a data-style
control header were also tried and did not fix it. The prototype was removed
rather than shipped with a threshold its control channel cannot meet.

The experiment did expose two real bugs that remain fixed: ACK headers now
repeat with the data rung, and acquisition now chooses the earliest strong
correlation peak instead of locking onto a later repeated header.
