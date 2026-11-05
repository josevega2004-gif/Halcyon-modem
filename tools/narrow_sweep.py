"""Measure narrow test geometries at exact 12.5 Hz carrier spacing.

This is an experiment harness, not a production ladder.  Every geometry uses a
960-point FFT and 64-sample CP, so the carrier spacing is 12.5 Hz and each
requested width is an exact number of carriers:

    350, 300, 250, 225, 200, 175, 150, 125, 100 Hz

The table can carry 4-, 8- and 16-FSK rungs.  A mode is named by its alphabet
and code rate: ``4q`` is 4-FSK 1/4, ``4h`` is 4-FSK 1/2, ``16h`` is 16-FSK
1/2, and so on.  The 16-tone rungs need at least 16 carriers; all geometries
can carry 4- and 8-tone rungs.  Every rung uses the same 32-block payload
geometry, so differences come from modulation and rate rather than block size.

S/N is reported in the geometry's own band and referred back to the 2742 Hz
reference used by the rest of the project.

    PYTHONPATH=. python3 -m tools.narrow_sweep --targets 100 125 150 \\
        --modes 4q 4h 8q 8h --snr -6.5 -5.5 -4.5
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import time

from halcyon.frame import MCS, TABLES, block_sizes
from halcyon.modem import Modem
from halcyon.ofdm import Phy

REFERENCE_BW_HZ = 2742.1875
TARGETS = (350, 300, 250, 225, 200, 175, 150, 125, 100)
MODES = ("4q", "4h", "8q", "8h", "16q", "16h")
TONE_BITS = {"4": 2, "8": 3, "16": 4}
RATES = {"q": 1 / 4, "h": 1 / 2}


def geometry(target: int, block_mult: int = 32) -> Phy:
    """Build a test PHY for ``target`` Hz with all modulation rungs."""
    active = int(round(target / 12.5))
    if not 8 <= active <= 32:
        raise ValueError("target must be between 100 and 400 Hz")
    # A pilot every three bins gives the 8-10 carrier geometries three or four
    # pilots, enough for the pilot-curvature noise estimate.  Wider layouts can
    # keep the normal comb.
    pilot_step = 3 if active <= 14 else 4
    phy = Phy(
        n_fft=960,
        bin_lo=11,
        bin_hi=10 + active,
        pilot_step=pilot_step,
        header_syms=1,
        syms_per_block=12,
        profile=f"narrow{target}",
    )
    # Enough header symbols for the 76 header info bits at rate 1/4.
    phy.header_syms = max(2, math.ceil(152 / phy.n_data))

    table = []
    for modes in ("4", "8", "16"):
        if phy.active.size < int(modes):
            continue
        for suffix, rate in RATES.items():
            table.append(
                MCS(
                    TONE_BITS[modes],
                    rate,
                    f"{modes}FSK 1/{round(1 / rate)}",
                    8.0,
                    -4.0,
                    1,
                    block_mult,
                    False,
                    "mfsk",
                    TONE_BITS[modes],
                )
            )
    TABLES[phy.profile] = tuple(table)
    return phy


def mode_index(phy: Phy, name: str) -> int:
    tones, suffix = name[:-1], name[-1]
    label = f"{tones}FSK 1/{round(1 / RATES[suffix])}"
    for i, m in enumerate(TABLES[phy.profile]):
        if m.label == label:
            return i
    raise ValueError(f"{name} does not fit a {phy.bandwidth_hz:.0f} Hz geometry")


def measure(
    target: int,
    names: list[str],
    snrs: list[float],
    frames: int,
    block_mult: int,
) -> list[dict]:
    from halcyon.bench import block_fer, info_rate_bps

    phy = geometry(target, block_mult)
    modem = Modem(phy)
    gain = 10.0 * math.log10(REFERENCE_BW_HZ / phy.bandwidth_hz)
    print(
        f"\n{target:3d} Hz: {phy.active.size} carriers, {phy.n_data} data, "
        f"{phy.n_pilots} pilots, header {phy.header_syms} sym, "
        f"{phy.sym_time * 1000:.1f} ms/sym, 2.7k gain {gain:.1f} dB",
        flush=True,
    )
    out = []
    for name in names:
        idx = mode_index(phy, name)
        mode = TABLES[phy.profile][idx]
        n, k = block_sizes(phy, idx)
        print(
            f"  {mode.label:<11s} {info_rate_bps(phy, idx):5.1f} bps  "
            f"n={n} k={k}",
            flush=True,
        )
        for snr in snrs:
            t0 = time.time()
            fer, hdr, rep = block_fer(modem, idx, float(snr), "awgn", frames)
            row = {
                "target_hz": target,
                "bandwidth_hz": phy.bandwidth_hz,
                "mode": mode.label,
                "info_bps": round(info_rate_bps(phy, idx), 2),
                "coded_bits": n,
                "info_bits": k,
                "snr_own_db": float(snr),
                "snr_2k7_db": float(snr) - gain,
                "block_fer": fer,
                "header_loss": hdr,
                "reported_db": rep,
                "wall_s": time.time() - t0,
            }
            out.append(row)
            print(
                f"    {snr:5.1f} dB own / {row['snr_2k7_db']:6.1f} dB 2.7k  "
                f"block-FER {fer:4.2f}  header {hdr:4.2f}  rx {rep:5.1f}  "
                f"{row['wall_s']:5.1f}s",
                flush=True,
            )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Narrow-bandwidth sensitivity sweep")
    ap.add_argument("--targets", type=int, nargs="+", default=list(TARGETS))
    ap.add_argument("--modes", nargs="+", choices=MODES, default=["8q", "16q"])
    ap.add_argument("--snr", type=float, nargs="+", default=[-5.5, -4.5, -3.5])
    ap.add_argument("--frames", type=int, default=2)
    ap.add_argument("--block-mult", type=int, default=32)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows = []
    for target in args.targets:
        rows.extend(measure(target, args.modes, args.snr, args.frames, args.block_mult))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
