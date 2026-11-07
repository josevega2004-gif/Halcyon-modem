# Halcyon

An open HF data modem: OFDM + LDPC + per-frame adaptive coding, occupying
**2.75 kHz** — inside the flat 2.8 kHz bandwidth limit the FCC now applies
across 160–10 m in place of the old 300/1200-baud symbol-rate caps.

Everything here is a working, tested implementation — not a design document.
Every number in the tables below was produced by the code in this repository
and can be reproduced with the commands shown.(Still is a work in progress an need some testing on the air).

```
python -m pytest -q                                          # 160 tests
python -m halcyon.bench --mode thresholds                    # S/N thresholds per mode
python -m halcyon.bench --mode rate                          # modem rate vs S/N
python -m halcyon.bench --mode goodput --message-bytes 4000  # end-to-end ARQ goodput
```

## Scope

Halcyon is an independent implementation: no source code from another modem is
used here, and none was derived from one. It speaks the command set PAT and
VarAC expect so those programs can drive it, but that is a command interface,
not interoperability -- an on-air link needs Halcyon at both ends. An earlier
revision of this file compared its measured thresholds with another product's
published speed ladder; that material has been removed, and nothing here claims
equivalence with any other modem. The figures below measure this code alone.
Use whichever modem suits your needs.

## Local console

```
python -m halcyon.ui --port 8730
```

Then open <http://127.0.0.1:8730/>.  One stdlib process serves two static pages
and a small JSON API; it binds loopback, and nothing leaves the machine.

The root page is a terminal in the shape an HF operator expects: a live
waterfall across the dial, a speed-level ladder down the side, an S-meter, and a
message box whose bursts go out through the simulated channel and come back as
received traffic — each burst's mode, block count and reported S/N sit in the
message header, and a burst that cannot close says so instead of pretending.
Fading, S/N and QSB are controls rather than constants.

`halcyon/ui/halcyon-standalone.html` is the same terminal as one file with no
server behind it: open it from disk and it runs. A browser cannot run the LDPC
and OFDM engine, so the file carries a bundle of bursts the modem measured while
it was built — every rung at 3 dB above and below its own threshold, for two
recorded phrases — and offers those phrases instead of a free-text box. It says
so on its face: the badge reads *STANDALONE FILE* and the radio panel explains
that a page in a browser has no UDP sockets. Rebuild it after changing the
modem with

```
PYTHONPATH=. python3 tools/build_standalone.py
```

`/console` is the bench view: frame anatomy, the capture as a waterfall and
spectrum, per-block CRC results, the offset the synchroniser found, a chart of
rate against measured threshold, and a short ARQ transfer with goodput.  Ticking
*Second look* there decodes the same burst again on the sum of its stored LLRs,
which is soft combining made visible.

## PAT and Winlink

PAT hardwires the host-mode ports -- 8300 for commands, 8301 for data -- so
something has to be listening there before it dials.  The packaged
`halcyon-switch` does the whole switch: it writes PAT's transport block, keeps
the value that was there before, and starts the host; `off` stops the host and
puts the old configuration back.

```
halcyon-switch on --mycall N0CALL
pat connect varahf:///N0CALL
halcyon-switch off
```

A 500 Hz start is a hard ceiling, not a fixed profile: the host may step down
to the 375 Hz deep or 234 Hz nano geometry when the simulated S/N is low, but
it will not select the 2.74 kHz wide profile even if PAT later announces a
wider BW.  Start at 2300 or 2750 to allow the wide profile.

`halcyon-switch status` reports what PAT is pointed at and whether the host is
listening; `halcyon-host` runs the service in the foreground when you would
rather watch it.  The full walk-through -- the `varahf` block, what PAT sends on
each port, and what to check when it cannot connect -- is in
[`docs/PAT.md`](docs/PAT.md), also installed as
`/usr/share/doc/halcyon/PAT.md`.

Verified against Pat v1.0.0 with its debug log on: PAT sends `PUBLIC ON`, `CWID
ON`, `COMPRESSION TEXT`, `MYCALL`, `LISTEN OFF`, `VERSION`, `BW2300`, `WINLINK
SESSION` and `CONNECT`, receives `CONNECTED`, and holds the link open through
the `IAMALIVE` heartbeats.  Two details cost real debugging time and are worth
knowing: PAT opens *two* command and two data connections while dialling and
treats the close of either as the modem vanishing, so the host serves every
client instead of evicting the previous one; and `COMPRESSION TEXT` means
compression *on*, which a `startswith("ON")` test gets backwards.

What still needs the other half is the Winlink B2F exchange.  PAT waits for the
far end to speak first, so it writes nothing until a station answers; with no
gateway or second station on the link there is no mail to exchange.  The
transport is real, the mail needs a peer.  A bare socket client against the same
two ports does move bytes through the modem: 68 B written to 8301 came back
unchanged, with `bytes_tx` and `bytes_rx` both 68.

## VarAC

VarAC drives the same two loopback ports.  `halcyon-switch varac --mycall
N0CALL` starts the host, asks it to identify itself, and prints the address,
port and bandwidth to enter in VarAC's modem settings; `halcyon-switch off`
stops it again.  [`docs/VARAC.md`](docs/VARAC.md) covers the settings, the
commands VarAC sends, and what is still needed for an on-air chat.

## Configuration

One optional file, `~/.config/halcyon/config.json` (or `$HALCYON_CONFIG`):

    {"ui": {"developer": true}}

It changes what the console *shows*, never how the modem works.  Some panels
exist to exercise the modem rather than to operate it: the propagation model
with its S/N slider, fading profiles and QSB switch, and the lab console at
`/console`.  Without the file they are absent -- not greyed out, not merely
disabled.  A missing, empty or malformed file is not an error, and the console
starts normally with the defaults.

`/api/config` reports the switch and the path being read.  The offline
single-file build keeps its propagation controls, because there the simulator
is the demonstration, and drops the lab link as it always did.

## CAT (Hamlib)

The **CAT** panel drives the rig through [Hamlib](https://hamlib.github.io/)'s
`rigctld`, so all ~320 supported rigs work without linking libhamlib into this
process. Tick *start rigctld for me*, pick a model (search the list), give the
serial device and baud rate, and **Connect CAT**: the console launches
`rigctld -m <model> -r <device> -s <baud>` on 127.0.0.1:4532 and attaches.

Already running `rigctld`? Untick the box and point the host field at it.

The panel sets and reads frequency, mode (with passband), PTT and the S-meter,
and repolls every 2.5 s so a knob turned on the radio shows up here. CAT is
control only — audio still comes from the HL2 or a TCI server; nothing here keys
the transmitter on its own.

```bash
python3 -c 'from halcyon.cat import list_rigs; print(list_rigs("ft-991")[:3])'
```

Rig 1 is Hamlib's **Dummy** rig, which is how the CAT path is tested end to end
with no hardware attached.

## C++ port (`cpp/`)

The modem core is also available as C++17, and it is wire-compatible with the
Python modem: a frame from either end decodes at the other.

```bash
make -C cpp                         # builds cpp/build/halcyon-modem
./cpp/build/halcyon-modem rungs     # the 17 wide rungs
./cpp/build/halcyon-modem selftest  # modulate + decode every rung
./cpp/build/halcyon-modem tx --mcs 4 --text "test de halcyon k" --out frame.f32
./cpp/build/halcyon-modem rx --in frame.f32
./cpp/build/halcyon-modem bench --mcs 4 --snr 2 --frames 20 --fading
python3 cpp/tests/interop.py        # both directions, sample identity, AWGN
```

**Ported:** the wide geometry (2 742 Hz, 117 carriers, 87 data), preamble and
pilot tracking, sync over ±90 Hz in two stages, all 17 wide rungs including
repetition and block multiplication, LDPC encoding and log-domain sum-product
decoding, Gray QAM to 256QAM with max-log LLRs, header framing with CRC-16,
interleaving, and the truncated/late-acquisition capture handling.

LLRs are scaled per cell by `1/|H|^2` for the header and the payload, so faded
carriers are trusted less than strong ones (`--llr flat` restores the old scalar
variance for the payload). On a two-tap 667 us channel this holds 0 % block and
frame errors at 8 dB where the flat payload path still has 25 % block errors,
and it also removed the header losses the scalar path had in fading. The
receiver can also run normalised min-sum (`--decoder ms`) and order-2 OSD with
CRC acceptance (`--decoder sposd` or `msosd`). Measured on the same blocks,
neither beats 40-iteration sum-product: 10-iteration min-sum has 95 % block
errors against 45 % at 2 dB, and OSD-2 recovered none of 68 failed blocks.
Those remain opt-in; the defaults are the measured best, and the numbers and
bench method are in `results/cpp_hybrid_decoder.md`.

**Not ported:** the robust (562 Hz) and compact (492 Hz) geometries — including
the non-coherent 16-FSK path — plus the ARQ link layer, the host-mode service, TCI, HL2,
CAT and the web UI. Those remain Python; this is the part that has to run in
real time.

**Should the two drift?** Shared constants are data, not duplicated derivations:
`tools/dump_cpp_data.py` writes `cpp/data/wide.bin` and
`cpp/include/halcyon_tables.hpp` (geometry, preamble, pilot signs, parity-check
matrices, interleaver permutations) from the Python definitions. Re-run it when
the Python wire format changes. A mismatched interleaver now aborts with
`interleaver table inconsistent for rung N` rather than writing past a buffer.

Two bugs the cross-check found, both now fixed: the frame header carries **60**
information bits (2+2+5+6+16+8+12+5+4), not the 59 that `frame.py`'s comment
claims, and the interleaver table is indexed by block count, so it needs 13
entries with slot 0 unused — 12 entries shifted every rung by one.

## Hermes Lite 2

The terminal's **Radio** panel speaks HPSDR protocol 1 to a Hermes Lite 2 on the
local network, the way the openHPSDR software does: it broadcasts the 63-byte
discovery request to `255.255.255.255:1024`, connects to the address the radio
replies from, sends the 64-byte start command, and then keeps a command-and-
control round-robin running (register `0x00` rate/receivers/duplex, `0x02` receive
NCO, `0x01` transmit NCO, `0x09` drive, `0x0a` LNA) at one 1032-byte frame per 126
samples, which also feeds the watchdog. While connected the waterfall carries the
receiver's own IQ instead of the synthesised noise floor, and *Listen & decode*
captures a few seconds of air and runs the modem's receive path over it.

Connecting never arms the transmitter: the panel starts in **Simulated**, and only
**On air** puts the burst into the radio, keyed with MOX at the drive trim you set.
One radio is half-duplex, so a burst it sends is not one it can hear — receiving
your own signal takes a second receiver on frequency.

Discovery, streaming and keying were exercised against a device answering as an
HL2 (board id `0x06`, `10.0.0.2`), and the packet layout is pinned by
`tests/test_hl2.py`. Nothing here has been tried into an antenna, and the drive
nibble is an AD9866 trim rather than a power control: check your licence
conditions before keying.

## Bandwidth and band plan

| | |
|---|---|
| Carrier span | **2 742 Hz** (117 carriers × 23.4375 Hz), 246 … 2 988 Hz |
| Measured 99 % occupied bandwidth | **2 729 Hz** (`--mode compliance`) |
| FCC limit, 160–10 m | **2 800 Hz** — 47 CFR 97.307(f)(3), 71 Hz spare |
| IARU Region 1 wide-data segments | **2 700 Hz** — 29 Hz tighter, use `Phy(**NARROW)` |
| Emission designator | `2K74J2D` — 2.74 kHz, single channel digital data |

Widening from a 2.4 kHz layout to 2.75 kHz adds 11 carriers (+10 % throughput
on every mode) while staying inside the FCC limit with room for filtering, audio
passband roll-off and tuning error.

### Why this is legal on the amateur bands

The bandwidth claim is measured, not asserted: `python -m halcyon.bench --mode
compliance` computes the 99 % occupied bandwidth of the real waveform
(`results/compliance.md`), which comes out at 2 729 Hz against a 2 800 Hz limit.

* **Bandwidth.** 47 CFR 97.307(f)(3) sets an authorized bandwidth of **2.8 kHz**
  for RTTY and data on 160–10 m, replacing the old 300-baud symbol-rate cap
  ([FCC 23-93](https://docs.fcc.gov/public/attachments/FCC-23-93A1.pdf), adopted
  November 2023). 60 m is 97.307(f)(14), also 2.8 kHz. The 2200 m and 630 m bands
  were left with symbol-rate limits and are out of scope here. 97.307(a) still
  requires no more bandwidth than the information rate needs, which is the reason
  the layout stops where it does.
* **Not spread spectrum.** This is the part worth being explicit about, because
  97.305(c) authorizes SS emissions **only at 222 MHz and above** — on HF an SS
  emission is unauthorized, not merely unfashionable. Nothing here is SS: the 117
  subcarriers each carry independent coded bits at 23.4 baud, with no spreading
  code, no frequency hopping and no chipping, so there is no processing gain, and
  the information rate is a large fraction of the emission bandwidth (12 889 bps
  peak in 2 729 Hz is 4.7 b/s/Hz). The 2-symbol preamble is a known training
  sequence of the same width as the payload, not a spreading sequence. The LDPC
  code buys coding gain rather than processing gain — it *lowers* the symbol rate
  for a given information rate instead of expanding the bandwidth.
* **Code.** 97.307(f)(3) permits only a *specified* digital code, and 97.309(a)(4)
  covers "any technique whose technical characteristics have been documented
  publicly, such as CLOVER, G-TOR, or PacTOR". This README is that documentation:
  frame layout, header fields, MCS ladder, FEC and ARQ are each specified in the
  files under [Layout](#layout).
* **Where to transmit.** A 2.74 kHz-wide data emission belongs only in segments
  whose band plan allows it — IARU Region 1's 14.101–14.112 MHz, for instance, not
  the CW or narrow-band sub-bands. In Region 1 those wide-data segments cap
  emissions at 2 700 Hz, 29 Hz tighter than the default layout, so use
  `Phy(**NARROW)` there: 111 carriers, 2 601.6 Hz span, measuring 2 588 Hz at
  99 %, about 5 % less throughput on every mode. The `MCS_TABLE` thresholds are
  measured for the default geometry.
* **Attended operation.** The link layer is an ARQ session; running it unattended
  makes it an automatically controlled digital station under 97.221, with its own
  segment restrictions.

None of this is legal advice, and rules differ outside the US: several
administrations impose 2.7 kHz or tighter limits, and some restrict which emission
 types may use which segments. Check your own regulator and band plan.

## What it does

* OFDM: 12 kHz audio baseband, 512-point FFT, 23.4 Hz spacing, **117 active
  carriers (87 data + 30 comb pilots)**, 64-sample cyclic prefix (5.3 ms),
  48 ms symbols.
* Channel estimation by 3-tap averaging across pilots and across adjacent
  symbols, then interpolation. Measured on AWGN the estimate stays within
  **0.5 dB of perfect CSI**; interpolating straight from raw pilot estimates
  costs **2.0 dB**, which is where an earlier version of this modem was losing
  its margin. Covering it is a regression test.
* Systematic LDPC with staircase parity: linear-time encoding, no singular
  parity matrices, sum-product decoding with syndrome early exit. Code graphs
  are grown PEG-style and verified **4-cycle-free**.
* 33 measured modes across seven band geometries — 2.74 kHz, 562 Hz, 492 Hz,
  375 Hz deep, 234 Hz nano, 225 Hz mini and 175 Hz micro — from **8.8 bps to
  12 889 bps** raw (8-FSK at the bottom, 256-QAM 8/9 at the top),
  each with a measured S/N threshold, spaced about 2 dB apart across the
  range so rate control always has a rung in reach. The bottom three are built by
  making the *codeword* longer, not the emission narrower, which is the one trick
  87 parallel carriers allow and a single-carrier ladder does not.
* Every frame carries its own MCS, so a receiver needs no side information:
  2-symbol random-BPSK preamble → robust header (QPSK ¼ LDPC, 60-bit field set
  + CRC-16) → payload blocks of 4 OFDM symbols, all of it repeated in the
  harsh-condition modes.
* Burst ARQ with selective repeat inside the burst, a short probe burst to
  learn the channel, and S/N-driven rate control with 3 dB operating margin.

## Measured thresholds (AWGN, block error rate ≤ 10 %)

From `python -m halcyon.bench --mode thresholds`
(`results/thresholds_awgn.md`):

| MCS | mode | raw info rate | channel S/N | receiver S/N |
|----:|------|--------------:|------------:|-------------:|
| 0 | QPSK 1/4 r4 | 227 bps | −3.0 dB | 8.1 dB |
| 1 | QPSK 1/8 L4 | 453 bps | −2.0 dB | 7.2 dB |
| 2 | QPSK 1/4 L2 | 906 bps | 0.0 dB | 5.9 dB |
| 3 | QPSK 1/3 | 1 208 bps | 2.0 dB | 5.8 dB |
| 4 | QPSK 1/2 | 1 812 bps | 2.6 dB | 5.8 dB |
| 5 | QPSK 3/4 | 2 719 bps | 5.7 dB | 7.1 dB |
| 6 | QPSK 7/8 | 3 172 bps | 7.0 dB | 8.1 dB |
| 7 | 16QAM 1/2 | 3 625 bps | 8.6 dB | 9.3 dB |
| 8 | 16QAM 2/3 | 4 833 bps | 10.3 dB | 10.9 dB |
| 9 | 16QAM 3/4 | 5 438 bps | 11.5 dB | 11.8 dB |
| 10 | 16QAM 7/8 | 6 344 bps | 13.7 dB | 14.0 dB |
| 11 | 64QAM 2/3 | 7 250 bps | 15.6 dB | 15.8 dB |
| 12 | 64QAM 3/4 | 8 156 bps | 17.4 dB | 17.5 dB |
| 13 | 64QAM 5/6 | 9 062 bps | 18.8 dB | 18.7 dB |
| 14 | 64QAM 7/8 | 9 516 bps | 19.6 dB | 19.7 dB |
| 15 | 256QAM 5/6 | 12 083 bps | 24.1 dB | 24.2 dB |
| 16 | 256QAM 8/9 | 12 889 bps | 25.0 dB | 25.1 dB |

"Channel S/N" is the signal-to-noise ratio in the 2 742 Hz passband; "receiver
S/N" is what the pilot estimator reports. Rate control compares estimator with
estimator, which is why the table carries both axes. Both carry about **±1 dB**
of uncertainty: the criterion is the first 1 dB grid point with block error
≤ 10 %, over 6 frames × 6 blocks, and re-running it moves the QPSK rows most
because a single failing frame is 17 % of the sample and pushes the answer up awhole grid step.  Treat anything within 1.5 dB across runs as the same result.

The sweep used to be centred on the *reported*-S/N column instead of the channel
axis. That is invisible for the ordinary modes, where the two agree within a
decibel, and badly wrong for the bottom three, which report 6–8 dB while sitting
near −4 dB: their window began above where they work and recorded the first grid
point as the threshold. The three low rows in `results/thresholds_awgn.md` were
re-measured after that was fixed.

The ladder has been revised five times on measured evidence. **QPSK ⅞
(3 172 bps)** fills what used to be a 2.9 dB climb for +33 % rate above QPSK ¾.
**64QAM ⅚ (9 062 bps)** replaces 256QAM ⅔, which bought +1.6 % for a full decibel.
Then the bottom was rebuilt twice: **repetition rungs** were added (227 bps at
−3.5 dB), and where a long codeword beats repetition it **replaced** it — the
plain 4-symbol QPSK ¼ rung and the 2× repeated one are both gone, dominated by
1 392-bit and 2 784-bit codewords at the same rates, 2.7 and 1.0 dB more
sensitive. The header now carries five bits of MCS, of which 17 of 32 are used.

All 17 rungs come from one sweep with the same ≤ 10 % block-error criterion, so
`results/thresholds_awgn.md` and this table are the same measurement rather than
two. Treat the table as ±1 dB: re-running it moves the QPSK rows most.

### Harsh-condition rungs: long codewords, not short bursts

The bottom of the ladder is where a copied design would show, so it is worth being
explicit about what this is not. Cutting carrier count and baud rate is the obvious
way to go slower, and repeating that move here would just be a narrower profile. What
this modem does instead uses the one thing 87 parallel carriers give it and a
single-carrier ladder cannot: **a long codeword is cheap**. A 1 392-bit rate-¼
codeword is 8 symbols (384 ms) and a 2 784-bit rate-⅛ codeword is 16 symbols
(768 ms); both hold the same payload rate as the short-block rung they replaced
and decode at lower S/N, because the extra symbols buy code distance instead of
being spent on repeats.

| mode | codeword | symbols/block | info rate | channel S/N | against the rung it replaced |
|---|---:|---:|---:|---:|---|
| QPSK 1/4 L2 | 1 392 b, rate ¼ | 8 | 906 bps | 0.0 | 2.7 dB better than 4-symbol QPSK ¼ at the same rate |
| QPSK 1/8 L4 | 2 784 b, rate ⅛ | 16 | 453 bps | −2.0 | 1.0 dB better than 2× repetition at the same rate |
| QPSK 1/4 r4 | 696 b × 4 | 16 | 227 bps | −3.0 | repetition still wins at this rate |

Measured, and worth stating plainly:

* **Longer blocks stop paying past about 2 800 bits, and repetition won at 227 bps.**
  Stretching the rate-⅛ code from 2 784 to 5 568 bits measured the *same* −2.5 dB
  rather than the 3 dB a longer block should buy, so this LDPC saturates at short
  lengths — a code-quality limit, not a bandwidth one, and the clearest target
  left in the design. At 227 bps the crude 4× repeated code is still the most
  sensitive thing in the ladder and keeps that rung. Below −3.5 dB no payload
  trick helps: the 2-symbol random-BPSK preamble stops syncing before decoding
  starts.
* **The header follows the frame's whole span.** With the payload strengthened
  but the header sent once, every such mode walled at 0 dB with 100 % header loss
  — twice, once for repeated payloads and again for long codewords, so it is
  measured rather than assumed. The header now repeats by repetition × block
  length and the receiver tries factors 1, 2, 4 and 8 until its CRC passes, since
  it cannot know either before decoding the header. That is a wire-format change,
  hence header version 2.
* **These rungs are not estimator targets.** The pilot estimator is noise-loaded
  at low S/N and reports about 9 dB while the channel sits at −3.5 to +0.5 dB, so
  `best_mcs` skips both kinds of low rung entirely; the link reaches them by
  stepping down after failures — one rung at a time, and never by falling onto
  one from an estimate. They are a floor, not a rate-control target.
* **What it bought, end to end.** At 0 dB channel S/N a 4 000-byte message now
  completes at 228 bps, and at 5 dB at 812 bps with no retransmissions. Before the
  ladder was extended the 0 dB test completed 0 % of messages. It takes 16
  retransmissions at 0 dB, which is the real cost of sitting on a rung's
  threshold rather than the cost of the rung itself.
* **A failed burst is not thrown away.** The receiver keeps the per-block LLRs and
  the retransmission decodes on the *sum* of both looks, so each extra attempt
  adds about 3 dB of Eb/N0. Measured on the 10.4 bps rung: at −8.5 dB channel S/N
  0 of 9 blocks decoded on the first attempt and 5 of 9 after one combining step;
  at −8.0 dB, 0 of 9 then 8 of 9. The rate stays 10.4 bps — this is how airtime
  buys sensitivity once the ladder is not allowed to go slower. It does not
  rescue a dead control channel: at −8.0 dB with bare-header ACKs the link still
  fails to complete, because the ACK needs the S/N the payload needs.

### Tested and rejected: no FEC at all (CRC only)

At the top of the ladder most of the airtime carries data, and the parity is what
a strong path may not need, so an **uncoded rung** was built (`MCS(...,
uncoded=True)`: payload plus CRC-16, ARQ instead of a code) and measured as its
own mode at both candidate points.

| mode | raw rate | block error where a coded rung already works |
|---|---:|---|
| 64QAM 7/8 (coded) | 9 516 bps | 0 % at 19.6 dB |
| 64QAM CRC-only | 10 875 bps | 94 % at 23 dB, 47 % at 24 dB |
| 256QAM 5/6 (coded) | 12 083 bps | 0 % at 24.1 dB |
| 256QAM CRC-only | 14 500 bps | 100 % at 28 dB, 67 % at 30 dB |

Uncoded did not merely lose, it never closed: at 256QAM the 256QAM CRC-only rung
was still failing two thirds of its blocks at 30 dB, six decibels above where
the coded 256QAM rung is error-free. That is structural rather than a tuning
failure — a block fails if one of its 2 784 bits is wrong, and near threshold an
uncoded 8-bit constellation produces those bit errors in bulk, so the block-error
cliff arrives long after the coded one. ARQ cannot rescue a mode that never
closes, because every retransmission lands in the same place. What the ladder has
instead is **256QAM at rate 8/9**: +6.7 % over 5/6 for about one decibel, keeping
the code and with it the soft-decision gain. The `uncoded` path stays in the code
so the answer remains measurable rather than remembered.

### 500 Hz low-speed ladder: 16-FSK and a coherent top rung (`Phy(**COMPACT)`)

The wide ladder's floor is set by its emission being 2.74 kHz wide. A narrow
emission is the other route down: the receiver collects less noise, and the
modulation can be one that works before there is any channel estimate worth
trusting. That is what this ladder is: 21 carriers over **492 Hz** carrier
span, measuring **541 Hz at 99 % occupied** (designator `0K54J2D` — the span is
not the emission), with the modulation chosen for the job rather than inherited
from the fast modes.

* **16-FSK at every rate below 100 bps, measured against coherent BPSK rather
  than assumed.** 16-FSK 1/2 carries 42 bps at −4.0 dB where BPSK 1/8 needed
  0.0 dB for 39 bps — 4 dB more signal for *less* payload — and 16-FSK 3/4 does
  62 bps at −2.5 dB. Coherent BPSK survives only at the top of the ladder, where
  78 bps for +0.5 dB is still the best rate this bandwidth offers.
* **16-FSK, detected non-coherently**, for the slowest: one active tone per
  symbol out of 16, Gray-labelled, decided on tone energy alone. No channel
  estimate is needed, so a phase that rotates between pilots costs it nothing —
  which is exactly the failure that stops a coherent mode working down here.

| mode | info rate | channel S/N (492 Hz) | Eb/N0 | equivalent at 2.74 kHz |
|---|---:|---:|---:|---:|
| 16FSK 1/4 r2 | 10 bps | **−7.5 dB** | 9.2 dB | **−15.0 dB** |
| 16FSK 1/4 | 21 bps | **−6.0 dB** | 7.7 dB | **−13.5 dB** |
| 16FSK 1/2 | 42 bps | **−4.0 dB** | 6.7 dB | **−11.5 dB** |
| 16FSK 1/2 V9 | 41 bps | **−4.5 dB** | 6.3 dB | **−12.0 dB** |
| 16FSK 3/4 | 62 bps | **−2.5 dB** | 6.5 dB | **−10.0 dB** |
| BPSK 1/4 L2 | 78 bps | +0.5 dB | 8.5 dB | −7.0 dB |

At 600 bytes, with S/N referred to 2.7 kHz as in the rest of this README, the
adaptive compact link measures **53.4 bps at −8 dB** (two frames, the BPSK and
16-FSK 3/4 rungs, no retransmissions), **47.5 bps at −10 dB** (two frames, no
retransmissions), and **3.4 bps at −12 dB** (19 frames, 24 retransmissions);
the −12 dB point now closes the message, where it used to miss it.  The
peak is not the top rung: BPSK 1/4 L2 has the highest nominal rate, but its
108-byte burst carries a 600-byte message in six bursts, each paying a full ACK
exchange, where 16-FSK 3/4 carries it in one.  The 21 bps rung appears only at
−12 dB, and there by stepping down after failures.  These are adaptive-link numbers, one message per point; the
fixed-rung probe figures this paragraph used to quote (55.5, 36.5 and 5.9 bps)
are kept in `results/xara_comparison.md`.  This is still the narrow-geometry
answer to the negative-SNR problem: at a fixed noise density the 492 Hz receiver
sees 7.5 dB more S/N than the 2.74 kHz one, and non-coherent 16-FSK needs no
channel estimate.

A separate 375 Hz test geometry (`Phy(**DEEP)`) narrows the same 16-FSK rungs.
The bandwidth column is explicit because that is the experimental variable:

| geometry | bandwidth | −8 dB | −10 dB | −12 dB |
|---|---:|---:|---:|---:|
| compact | 492 Hz | 53.4 bps | 47.5 bps | 3.4 bps |
| deep test | 375 Hz | **55.2 bps** | **55.2 bps** | 13.8 bps |
| nano test | 234 Hz | 27.0 bps | 27.0 bps | **20.6 bps** |

On the adaptive link deep leads at −8 and −10 dB (55.2 bps at both, its 62 bps
rung closing 600 bytes in one twelve-block frame), and nano leads at −12 dB,
where it closes at 20.6 bps against deep's 13.8 and compact's 3.4: 234 Hz
collects 3.2 dB
more in-band S/N than 492 Hz at the same noise density, which is what keeps its
slower 8-FSK rung alive there.  Nano's −10 dB cell is where the retabulated
threshold shows up: its 31 bps rung now needs +0.3 dB and the receiver estimates
+0.6, so the 3 dB margin clears and the message lands at 27.0 bps, the same as
at −8 dB.  With the previous threshold (−2.1 dB reported) the bar was +0.9 dB,
and that same estimate sent the first window down a rung, costing the cell 6 bps.  The non-coherent rungs no longer report the
header's S/N, which reads 10-14 dB high because the header fills every active
carrier where a tone fills one: they report the payload's own tone energies,
with the per-symbol median — a noise order statistic whose expectation is
ln 2 times the mean — pinning the noise so the signal falls out of the total,
referred to the payload power.  With that estimate on the channel's axis the
ladder thresholds are channel thresholds and the span cap is gone.  The
one-block probe, whose header was three quarters of the frame, is now sent at
the burst window instead, which is what lifted compact's −10 dB cell from 14.6
to 47.5 bps.  All three rows are
adaptive-link runs, 600 bytes and one message per point, with the channel S/N
referred to 2.7 kHz.  The 100–350 Hz sweep then selected two more points:
**micro** (175 Hz, 8-FSK 1/4, 8.8 bps, about −18.5 dB at 2.7 kHz) for maximum
sensitivity, and **mini** (225 Hz, 16-FSK 1/4, 11.7 bps, about −17.4 dB) as the
speed/sensitivity knee.  They remain bench and console profiles, but the host
does not use them: mini's 600-byte link measured 1.4–1.9 bps and micro is
slower still, below the 6 bps floor worth operating.  The host ladder is
compact at normal S/N, 375 Hz from −4 dB and 234 Hz nano from −12 dB; below
that it has no slower fallback, rather than dropping onto a mode under 6 bps.
That is the same default xara uses: 500 Hz-class bandwidth for low speeds,
narrowing further when the path needs it.

| selected geometry | bandwidth | mode | rate | 2.7 kHz threshold |
|---|---:|---|---:|---:|
| mini | 225 Hz | 16FSK 1/4 | 11.7 bps | −17.4 dB |
| micro | 175 Hz | 8FSK 1/4 | 8.8 bps | −18.5 dB |

The thresholds are the measured ≤10 % block-error points; the full sweep and the
4-FSK control are in `results/narrow_sweep.md`.

The V9 rung is the same 16-FSK 1/2 shape with the K=9 convolutional code and
Viterbi decoding instead of the LDPC. Through the full chain it reaches 10 %
block error about 0.5 dB earlier (7.3 % against 11.5 % at −4.5 dB, and 75 %
against 96.9 % at −5.5 dB), which is inside the ±0.5 dB uncertainty of the
96-block points: a rung worth keeping, not a ladder reshuffle.

The right-hand column is the honest axis for a narrow mode: at a fixed noise
density, 492 Hz collects 10·log₁₀(2 742/492) = **7.5 dB less noise** than the
wide layout,so an in-band S/N of −6.0 dB here is the same thing as −13.5 dB referred to
2.74 kHz. That is what puts a 21 bps rung at an absolute signal level
the wide ladder cannot reach; the same payload in coherent BPSK needed about 6 dB
more signal.

The 10 bps rung reaches that floor by repeating the 20.8 bps codeword twice and
summing LLRs, which is what measured clean; a single rate-1/8 codeword at
n = 864 still leaves a 0.33 block-error floor at the modem level even though the
code alone decodes cleanly, so it is not shipped.

Those floors turned out to be a **code-construction bug, now fixed**: at rate 1/8
there are about seven parity checks per information bit, so with a fixed column
weight of 6 entire checks ended up fed only by the staircase parity columns. A
check like that codes nothing — it just forces adjacent parity bits to be equal —
and the effect was a block-error floor that did not move with S/N. `code_for` now
scales the column weight with the check-to-information ratio, and every one of
the previously broken (n, k) pairs decodes with **0 % error** at 5 dB Eb/N0. That
fix also improved the bottom of the wide ladder by half a decibel (227 bps now
reaches −3.0 dB rather than −3.5).

### The slowest rung is 8.8 bps (the 352 Hz profile stays retired)

An earlier build carried a 352 Hz profile with 8-FSK at **7.8 bps, measured at
−7.0 dB** (−15.9 dB referred to 2.74 kHz) — the measurement that proved the
frame-loss floor was an acquisition bug rather than a coding one. It is
deliberately not shipped: the 175 Hz micro profile reaches 8.8 bps with a more
usable frame, while the 352 Hz point is slower without being more sensitive.
At these rates a short message still costs about a minute of airtime, so 8.8 bps
is the floor and the slowest shipped rung is the 175 Hz 8-FSK micro rung.

That earlier note claimed 1.45 dB was recoverable here by narrowing the frame
to the 352 Hz the 16-FSK payload occupies. **It is not, and the measurement
says so.** Thresholds are quoted as in-band S/N, and at a fixed in-band S/N a
narrower band means a higher noise *density*: measured in a 398 Hz frame the
10.4 bps rung went from 0 % to 0.44 block error at −7.0 dB, i.e. it lost about
the 0.9 dB the width change was supposed to win. The two cancel exactly, as
they must, because the required signal power at a fixed noise density is
P = N₀·B·(S/N) and B moves both terms the other way. Narrowing an emission buys
band-plan fit, not sensitivity; the wide-referred column already prices it.

### Narrow, high-noise profile (`Phy(**ROBUST)`)

Not every path deserves 2.7 kHz. `ROBUST` is **24 carriers over 562.5 Hz** (18 of
them data), for a band that is noisy, crowded, or only has half a kilohertz of
clear space in it — and it fits the 500–600 Hz narrow-band segments of the
Region 1 band plan.

| mode | info rate | channel S/N (562 Hz) | Eb/N0 |
|---|---:|---:|---:|
| QPSK 1/3 | 250 bps | 2.0 dB | 5.5 dB |
| QPSK 3/4 | 562 bps | 5.5 dB | 5.5 dB |
| 16QAM 1/2 | 750 bps | 8.5 dB | 7.3 dB |

What it does and does not buy:

* **It is not more sensitive per bit, and the table shows why.** Eb/N0 lands
  within 0.2 dB of the wide profile's at every rate (5.5 against 5.6 dB at
  QPSK ⅓, 5.5 against 5.7 at QPSK ¾, 7.3 against 7.4 at 16QAM ½). Narrowing an
  emission creates no coding gain, and any claim otherwise is a
  bandwidth-reference mix-up.
* **It works at a much lower absolute signal.** For a given noise density the
  receiver collects 10·log10(2 742/562) = **6.9 dB less noise**, so the signal
  level at the antenna can be 6.9 dB lower for the same in-band S/N. On a
  power-limited path that is the entire benefit.
* **It survives QRM.** A 2.74 kHz mode must live with whatever sits anywhere in
  2.74 kHz; 562 Hz only has to find 562 Hz that is clear.
* **It is genuinely slow:** 250–750 bps payload, and the header costs 480 ms per
  burst because 18 data carriers need ten header symbols to reach rate 1/4.

At 600 bytes the adaptive link measures **68 bps at its 2.0 dB threshold**
(S/N in the 562 Hz band, −4.9 dB referred to 2.74 kHz), with 38 block
retransmissions, and 61 bps a decibel below it; at 0 dB and below it no longer
completes.
Above the threshold it climbs to 129 bps at 4 dB, 172 bps at 6 dB and 485 bps
at 8 dB; `results/goodput_awgn_robust.md` holds the sweep
(`--mode goodput --profile robust --snr-step 1`).  The threshold is a usable
operating point rather than a cliff edge, and the retransmission count is what
it costs.

Two honest limits. Blocks here are 12 symbols, not 4: a 4-symbol block at 18
carriers is 144 coded bits, and at that length the code has no distance at all
(a 25 % block error that did not improve with S/N). And the two lowest-rate modes
are absent from the table — at n = 432 with k = 104 and k = 216 the LDPC
construction never reached 10 % block error at any S/N, which is a defect in the
code sizing for those pairs, not in the geometry. They come back when that is
fixed, with measured thresholds rather than guessed ones.

## End-to-end goodput

End-to-end ARQ goodput on AWGN (4 000-byte messages, including preambles, ACKs
and 0.2 s turnaround each way):

| S/N | 0 dB | 5 dB | 10 dB | 15 dB | 20 dB | 25 dB | 30 dB |
|-----|------|------|-------|-------|-------|-------|-------|
| goodput | 221 bps | 812 bps | 1 692 bps | 3 448 bps | 4 728 bps | 5 333 bps | 6 349 bps |

These are lower than modem-level rates by design — they pay for the ARQ round
trips. Short messages do worse; overhead is dominated by the fixed
preamble+header+ACK cost per exchange.

## What is honestly not as good

* **Fading is the weak spot.** Measured on a moderate Watterson path
  (0.5–1 Hz Doppler, 1 ms delay, two taps): 62 bps at 16 dB and 33 bps at
  22 dB, with 0 % of 4 000-byte messages completed. **These figures predate a
  fix to the channel model.** `fractional_delay` used to shift a burst
  *circularly*, wrapping its tail onto its start — a free cyclic prefix for the
  receiver, and a destroyed last block. It is a linear delay now. The two agree
  on AWGN, where there are no delay taps and nothing above changes, but on this
  same moderate path an 800-byte transfer measured 83 bps at 22 dB with the
  wrapping delay against 60 bps with the linear one, so treat the numbers in
  this section as optimistic until they are re-run. The payload interleaver
  spreads every codeword across every block in the burst, so a fade degrades
  all blocks equally instead of failing a few and letting selective repeat fix
  them; frequency-selective nulls also hurt linear pilot interpolation across
  a 4-carrier spacing. Per-block interleaving plus a proper 2D channel
  estimate is the fix, and it is the single highest-value change left.
* **The QPSK rungs are expensive per decibel, and it is still not the header.**
  QPSK ¼ to ½ spans 906 → 1 812 bps for 2.1 dB, and what is left is short-block
  coding length, not the header: measured on its own at 24 channel seeds per
  point, the header decodes 24/24 from 4 dB (22/24 at 2 dB), so an earlier
  version of this file was wrong to blame it. The clear symptom is that stretching
  a codeword past ~2 800 bits buys no further sensitivity, which points at the
  LDPC construction and decoder budget rather than at the frame format. What is
  genuinely uncovered is the 18–175 bps region below −6.6 dB: the preamble
  stops syncing below −4 dB, so that region needs a repeated preamble.
* **Simplified channel model.** AWGN plus a two/three-tap Watterson-style
  fading simulator; no real ionospheric measurements.
* **No audio/radio front end.** The modem works in complex baseband; an SSB
  audio path (48 kHz WAV or sound card) is not included yet.
* **No measurements on air.** Halcyon's numbers come from simulation, not from
  radios.

## Short-block decoder comparison

The narrow ladder's 432–576-bit blocks are where the LDPC decoder is weakest, so
`results/short_block_decoders.md` compares it against two alternatives on the
same 512-bit block (88 payload + 16 CRC), BPSK over AWGN, a block delivered only
when its CRC-16 passes: sum-product LDPC, LDPC with order-2 statistics
reprocessing, and a polar code with CRC-aided list decoding (list of 8).

| Eb/N0 | LDPC | LDPC + OSD-2 | polar CA-SCL L8 |
|------:|-----:|-------------:|----------------:|
| 0 dB | 66.7 % | 66.7 % | **38.3 %** |
| 1 dB | 25.0 % | 25.0 % | **5.0 %** |
| 2 dB | 1.7 % | 1.7 % | **0.0 %** |
| 3 dB | 0.0 % | 0.0 % | 0.0 % |

Polar wins below about 2 dB and costs roughly 7–20× the decode time in Python;
reprocessing changed nothing at any measured point. At the real frame block
(432 bits, a 512-bit code with 80 punctured coordinates) the low-rate rung
shows the same ordering -- 27 % against 83 % at 0 dB and 3 % against 20 % at
1 dB for k = 104 -- while at rate 1/2 the two are level and both close by 3 dB.
Rate matching is implemented, but the LDPC is still what every frame uses.
A tuning sweep over list size and CRC width puts the polar decoder at 8.3 %
against 16.7 % at 0 dB when the list goes from 8 to 64, for 7× the decode time;
a CRC-24 changed nothing because CRC-16 false accepts did not occur, and OSD
stayed flat at every window, flip count and BP budget tried. Reproduce with
`python3 tools/compare_short_block.py --length 432 --k 104` and
`python3 tools/tune_decoders.py`.

## Layout

```
halcyon/bits.py      bit packing, CRC-16/CCITT-FALSE
halcyon/ldpc.py      LDPC construction, encoding, sum-product decoding, OSD
halcyon/polar.py     experimental polar code, CRC-aided list decoding
halcyon/conv.py      convolutional coding with soft-decision Viterbi decoding
halcyon/bch.py       BCH(31,16) over GF(2^5), algebraic decoding
halcyon/product.py   BCH product code with iterative Chase-2
halcyon/qam.py       Gray-labelled BPSK/QAM mapping and max-log LLR demapping
halcyon/ofdm.py      OFDM modulation, pilot channel estimation, sync/CFO
halcyon/frame.py     MCS ladder, header fields, block sizing
halcyon/modem.py     frame assembly, transmit, receive
halcyon/channel.py   AWGN, Watterson fading, CFO, timing error
halcyon/link.py      burst ARQ, selective repeat, rate control
halcyon/bench.py     thresholds, rate, goodput and compliance measurements
halcyon/host.py      host-mode service: the two loopback ports PAT dials
halcyon/switch.py    select Halcyon in PAT, restore the previous modem
tests/               tests: CRC, LDPC, polar, conv, product, QAM, framing, sync, link, host mode
docs/PAT.md          PAT setup guide, shipped with the package
docs/VARAC.md        VarAC settings guide, shipped with the package
CREDITS.md           protocol documents, reference programs and libraries
```

`results/` holds the measured tables, including the decoder comparison above and the
Halcyon/xara/aria mode, speed and S/N comparison in
[`mode_comparison.md`](results/mode_comparison.md).

## Credits

Halcyon is written from published specifications, and no third-party source
code is included. The protocol documents it follows — the TCI specification,
the Hermes-Lite 2 protocol pages and the openHPSDR framing — and the programs
whose behaviour it was checked against, PAT, VarAC, Quisk, deskHPSDR and
Hamlib, are credited in [`CREDITS.md`](CREDITS.md), together with the libraries
actually used (NumPy, SciPy, and `websockets` for the optional TCI client).

## Licence

Halcyon is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.  See `LICENSE` for the full text.  It is distributed in the hope that
it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

The licence is deliberate: anyone may read, modify and redistribute this modem,
and a modified version passed on to others has to stay open as well.
