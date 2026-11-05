"""Benchmark harness: measured thresholds and end-to-end goodput.

    python -m halcyon.bench --mode thresholds
    python -m halcyon.bench --mode goodput --channel moderate

Thresholds are recorded on two axes: the true channel S/N (the honest
comparison axis, used in the README) and the S/N the receiver's pilot
estimator reports (the axis rate control uses).  Results go to ``results/``.
"""

from __future__ import annotations

import argparse
import csv
import os
import time

import numpy as np
from scipy.signal import welch

from .channel import make_channel
from .frame import MCS_TABLE, chunk_bits, mcs, table_for
from .link import Link, LinkConfig
from .modem import Modem
from .ofdm import COMPACT, DEEP, MICRO, MINI, NANO, NARROW, ROBUST, Phy

# Geometry per profile: the thresholds travel with the layout, because a narrow
# emission collects less noise and its S/N axis means something different.
PROFILES = {
    "wide": Phy,
    "robust": lambda: Phy(**ROBUST),
    "compact": lambda: Phy(**COMPACT),
    "deep": lambda: Phy(**DEEP),
    "nano": lambda: Phy(**NANO),
    "micro": lambda: Phy(**MICRO),
    "mini": lambda: Phy(**MINI),
}
_TABLE_NAMES = {
    "wide": "MCS_TABLE",
    "robust": "ROBUST_TABLE",
    "compact": "COMPACT_TABLE",
    "deep": "DEEP_TABLE",
    "nano": "NANO_TABLE",
    "micro": "MICRO_TABLE",
    "mini": "MINI_TABLE",
}

# Occupied bandwidth: 117 carriers at 23.4375 Hz = 2742 Hz, which stays under
# the 2.8 kHz HF limit the FCC applies in place of the old baud-rate caps.
BANDWIDTH_HZ = Phy().bandwidth_hz
RESULTS_DIR = "results"



def shannon_bps(snr_db: float, bw: float = BANDWIDTH_HZ) -> float:
    return bw * np.log2(1.0 + 10.0 ** (snr_db / 10.0))


def info_rate_bps(phy: Phy, idx: int) -> float:
    m = mcs(idx, phy)
    # 16-FSK carries 4 bits per symbol; a QAM payload carries one point per data
    # carrier.  Both are then scaled by the code rate and the repetition.
    # 4 bits for a 16-tone alphabet, 3 for the 8-tone one: reading a fixed 4
    # here over-reported the 8-FSK rung's rate by a third (10 bps against the
    # 7.8 bps the geometry actually carried).
    per_symbol = (
        int(getattr(m, "tone_bits", 4))
        if getattr(m, "kind", "qam") == "mfsk"
        else phy.n_data * m.bps
    )
    if getattr(m, "codec", "ldpc").startswith("conv"):
        # A convolutional rung's rate is fixed at 1/2 minus the tail, not the
        # nominal ``rate`` field, so read it back from the block geometry.
        from .frame import block_sizes

        n, k = block_sizes(phy, idx)
        return k * per_symbol / (n * phy.sym_time)
    return per_symbol * m.rate / (phy.sym_time * max(1, m.rep))


def burst_channel(kind: str, snr_db: float, seed: int, bandwidth_hz: float | None = None):
    """Channel at ``snr_db`` referred to the *signal's own* occupied bandwidth.

    ``Channel`` defaults to the 2.75 kHz passband.  A narrow profile has to pass
    its own bandwidth instead, or "channel S/N" quietly means a different thing
    and every threshold moves by 10*log10(2742/B) -- which is exactly the 6.9 dB
    that made a 562 Hz mode look 7 dB more sensitive than it is.
    """
    kw = {"bandwidth_hz": bandwidth_hz} if bandwidth_hz else {}
    return make_channel(kind, snr_db, seed=seed & 0x7FFFFFFF, **kw)


# --------------------------------------------------------------------- thresholds
def block_fer(
    modem: Modem,
    idx: int,
    snr_db: float,
    kind: str,
    frames: int,
    seed: int = 1,
) -> tuple[float, float, float]:
    """(block error rate, header loss rate, mean reported S/N)."""
    phy = modem.phy
    rng = np.random.default_rng(seed)
    cap = chunk_bits(phy, idx)
    # A full burst (6 blocks) so the measurement matches what the link sends.
    data = rng.integers(0, 256, 6 * cap // 8, dtype=np.uint8).tobytes()
    fails = total = hdr = 0
    reported: list[float] = []
    for f in range(frames):
        tx, nb = modem.build(data, idx, seq=f & 0xFF)
        rx = burst_channel(kind, snr_db, seed=rng.integers(1 << 31), bandwidth_hz=phy.bandwidth_hz)(tx)
        res = modem.receive(rx)
        if not res.header_ok:
            fails += nb
            total += nb
            hdr += 1
            continue
        reported.append(res.snr_db)
        fails += int((~res.blocks_ok).sum())
        total += int(res.blocks_ok.size)
    rep = float(np.mean(reported)) if reported else float("-inf")
    return fails / max(total, 1), hdr / frames, rep


def measure_thresholds(
    kind: str,
    frames: int,
    step: float = 1.0,
    half: float = 3.5,
    phy: Phy | None = None,
) -> list[dict]:
    phy = phy or Phy()
    modem = Modem(phy)
    rows = []
    for idx, m in enumerate(table_for(phy)):
        curve = []
        thr_true = None
        thr_rep = None
        # Sweep around the *channel* threshold, not the reported one.  The bottom
        # rungs report 6-8 dB while sitting at -4 dB, so centring on snr_min put
        # the whole window above where they work and recorded +5 dB for a rung
        # that decodes at -4 dB: the measured value was simply the bottom of a
        # sweep that never reached the interesting region.
        for snr in np.arange(m.snr_channel - half, m.snr_channel + half + 1e-9, step):
            fer, hdr, rep = block_fer(modem, idx, float(snr), kind, frames)
            curve.append((float(snr), fer, hdr, rep))
            if thr_true is None and fer <= 0.10:
                thr_true = float(snr)
                thr_rep = rep
        rows.append(
            {
                "mcs": idx,
                "label": m.label,
                "bps": round(info_rate_bps(phy, idx)),
                "threshold_db": thr_true,
                "threshold_reported": thr_rep,
                # Energy per information bit over noise density: the only axis on
                # which profiles of different width can be compared honestly.
                "ebn0_db": None
                if thr_true is None
                else thr_true
                + 10.0 * float(np.log10(phy.bandwidth_hz / max(info_rate_bps(phy, idx), 1.0))),
                "curve": curve,
            }
        )
        print(
            f"  MCS {idx:2d} {m.label:<12s} {info_rate_bps(phy, idx):6.0f} bps  "
            f"true S/N {'--' if thr_true is None else f'{thr_true:5.1f}'} dB  "
            f"reported {'--' if thr_rep is None else f'{thr_rep:5.1f}'} dB  |  "
            + " ".join(f"{s:5.1f}:{f:.2f}" for s, f, _, _ in curve)
        )
    return rows


# ----------------------------------------------------------------------- goodput
def measure_goodput(
    kind: str,
    snrs: np.ndarray,
    message_bytes: int,
    messages: int,
) -> list[dict]:
    phy = Phy()
    modem = Modem(phy)
    rows = []
    for snr in snrs:
        gps, mcses, retx, complete, reps = [], [], [], [], []
        for i in range(messages):
            rng = np.random.default_rng(5000 + i)
            data = rng.integers(0, 256, message_bytes, dtype=np.uint8).tobytes()
            link = Link(
                modem,
                LinkConfig(),
                make_channel=lambda s, k=kind: make_channel(
                    k, float(snr), seed=int(s), bandwidth_hz=phy.bandwidth_hz
                ),
                seed=i,
            )
            st = link.run(data)
            gps.append(st.goodput_bps)
            mcses.append(st.mean_mcs)
            retx.append(st.retransmissions)
            complete.append(st.complete)
            reps.append(st.mean_snr_db)
        gp = float(np.mean(gps))
        rows.append(
            {
                "snr_db": float(snr),
                "goodput_bps": gp,
                "shannon_bps": shannon_bps(float(snr)),
                "mean_mcs": float(np.mean(mcses)),
                "retx": float(np.mean(retx)),
                "complete": float(np.mean(complete)),
                "reported_snr_db": float(np.mean(reps)),
            }
        )
        print(
            f"  S/N {snr:5.1f} dB  goodput {gp:7.0f} bps  "
            f"({100 * gp / shannon_bps(float(snr)):4.1f}% of Shannon)  "
            f"mcs {np.mean(mcses):4.1f}  retx {np.mean(retx):3.1f}  "
            f"ok {100 * np.mean(complete):3.0f}%  (rx sees {np.mean(reps):4.1f} dB)"
        )
    return rows


# ------------------------------------------------------------------------- output
def write_markdown(path: str, header: str, table: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(header + "\n\n" + table + "\n")


# --------------------------------------------------------------------- rate

def rate_table(snrs: np.ndarray, margin_db: float = 0.0, phy: Phy | None = None) -> list[dict]:
    """Best MCS that closes at each channel S/N, and what it carries.

    This is the modem-level comparison axis (raw information rate per Hz of
    channel), independent of ARQ overhead.
    """
    phy = phy or Phy()
    rows = []
    for snr in snrs:
        idx = -1
        for i, m in enumerate(table_for(phy)):
            if snr >= m.snr_channel + margin_db:
                idx = i
        rows.append(
            {
                "snr_db": float(snr),
                "mcs": idx,
                "label": mcs(idx, phy).label if idx >= 0 else "--",
                "rate": info_rate_bps(phy, idx) if idx >= 0 else 0.0,
                "shannon": shannon_bps(float(snr)),
            }
        )
    return rows


def snr_for_rate(bps_target: float, margin_db: float = 0.0, phy: Phy | None = None) -> float:
    """Lowest channel S/N at which some mode reaches ``bps_target``.

    The sweep starts below the ladder's own floor: with a negative threshold in the
    table, starting at 0 dB silently clamped every low target to 0 dB and reported
    margins against a requirement that no mode actually has.
    """
    lo = min(m.snr_channel for m in table_for(phy)) - 6.0
    snrs = np.arange(lo, 40.0, 0.05)
    rates = np.array([r["rate"] for r in rate_table(snrs, margin_db, phy)])
    hit = np.nonzero(rates >= bps_target)[0]
    return float(snrs[hit[0]]) if hit.size else float("nan")


def rate_at_snr(snr_db: float, margin_db: float = 0.0, phy: Phy | None = None) -> float:
    """Information rate the current ladder closes at this channel S/N."""
    return float(rate_table(np.array([snr_db]), margin_db, phy)[0]["rate"])


def slowest_rate() -> float:
    """Slowest payload rate the ladder can carry at all."""
    return rate_at_snr(min(m.snr_channel for m in MCS_TABLE))









# ---------------------------------------------------------------- regulatory
# 47 CFR 97.307(f)(3) sets the authorized bandwidth for data on 160-10 m at
# 2.8 kHz, replacing the old 300-baud cap.  IARU Region 1 band plans are tighter
# in their wide-data segments: 2.7 kHz.  Occupied bandwidth is measured as the
# 99 % power containment bandwidth of the real waveform, not the carrier span.
FCC_BW_HZ = 2800.0
IARU_R1_BW_HZ = 2700.0


def occupied_bw(tx: np.ndarray, fs: int, frac: float = 0.99) -> tuple[float, float, float]:
    """(width, low edge, high edge) holding ``frac`` of the emission's power."""
    f, psd = welch(
        tx, fs=fs, nperseg=8192, return_onesided=False, detrend=False, window="blackman"
    )
    psd = np.fft.fftshift(psd)
    f = np.fft.fftshift(f)
    cum = np.cumsum(psd / psd.sum())
    lo = f[np.searchsorted(cum, (1 - frac) / 2)]
    hi = f[np.searchsorted(cum, 1 - (1 - frac) / 2)]
    return float(hi - lo), float(lo), float(hi)


def compliance_row(phy: Phy, label: str) -> dict:
    rng = np.random.default_rng(7)
    qpsk = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j]) / np.sqrt(2)
    tx = phy.modulate(rng.choice(qpsk, size=(120, phy.n_data)))
    bw, lo, hi = occupied_bw(tx, phy.fs, 0.99)
    bw999, _, _ = occupied_bw(tx, phy.fs, 0.999)
    return {
        "label": label,
        "carriers": phy.active.size,
        "data": phy.n_data,
        "pilots": phy.n_pilots,
        "span_hz": phy.bandwidth_hz,
        "ocbw_hz": bw,
        "ocbw999_hz": bw999,
        "lo_hz": lo,
        "hi_hz": hi,
        "peak_bps": max(info_rate_bps(phy, i) for i in range(len(table_for(phy)))),
        "fcc_ok": bw <= FCC_BW_HZ,
        "iaru_ok": bw <= IARU_R1_BW_HZ,
    }


def compliance_table(rows: list[dict]) -> str:
    out = [
        "Measured 99 % occupied bandwidth of the transmitted waveform (Welch PSD,",
        "Blackman window), against 47 CFR 97.307(f)(3) 2.8 kHz and the 2.7 kHz that",
        "IARU Region 1 band plans allow in their wide data segments.",
        "",
        "| profile | carriers | data / pilots | carrier span | 99 % occupied | 99.9 % | FCC 2.8 kHz | IARU 2.7 kHz |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        out.append(
            f'| {r["label"]} | {r["carriers"]} | {r["data"]} / {r["pilots"]} | '
            f'{r["span_hz"]:.0f} Hz | {r["ocbw_hz"]:.0f} Hz | {r["ocbw999_hz"]:.0f} Hz | '
            f'{"pass" if r["fcc_ok"] else "FAIL"} | {"pass" if r["iaru_ok"] else "over"} |'
        )
    r0 = rows[0]
    out += [
        "",
        f"**Not a spread-spectrum emission.** The {r0['carriers']} subcarriers each carry",
        "independent coded bits at 23.4 baud, with no spreading code, no frequency",
        "hopping and no chipping, so there is no processing gain and the information",
        f"rate ({r0['peak_bps']:.0f} bps peak, {r0['peak_bps'] / r0['ocbw_hz']:.1f} b/s/Hz in",
        "the occupied bandwidth) is a large fraction of the emission bandwidth -- the",
        "opposite of a spread signal.  47 CFR 97.305(c) authorizes SS emissions only at",
        "222 MHz and above, so on HF an SS emission is unauthorized rather than merely",
        "discouraged.",
        "",
        f"The default layout fits the FCC limit with {FCC_BW_HZ - r0['ocbw_hz']:.0f} Hz to",
        "spare.  HF data under 97.307(f)(3) must use a specified digital code; 97.309(a)(4)",
        "covers any technique whose characteristics are documented publicly (the clause",
        "written for CLOVER, G-TOR and Pactor), which this repository is.",
    ]
    return "\n".join(out)


def rate_html(rows: list[dict]) -> str:
    out = [
        f"| S/N ({BANDWIDTH_HZ:.0f} Hz) | mode | modem rate | Shannon | % of Shannon |",
        "|--------------:|------|-----------:|--------:|-------------:|",
    ]
    for r in rows:
        label = r.get("label", "--")
        out.append(
            f'| {r["snr_db"]:5.1f} dB | {label:<11s} | {r["rate"]:7.0f} bps | '
            f'{r["shannon"]:6.0f} bps | {100 * r["rate"] / r["shannon"]:4.1f} % |'
        )
    return "\n".join(out)


def thresholds_table(rows: list[dict], kind: str, phy: Phy | None = None) -> str:
    bw = "--" if phy is None else f"{phy.bandwidth_hz:.0f} Hz"
    out = [
        f"Measured payload thresholds, `{kind}` channel, block error rate <= 10 %.",
        "",
        f"Geometry bandwidth: **{bw}**.",
        "",
        "| MCS | mode | info rate | bandwidth | channel S/N | Eb/N0 | receiver S/N |",
        "|----:|------|----------:|----------:|------------:|------:|-------------:|",
    ]
    for r in rows:
        t = "--" if r["threshold_db"] is None else f'{r["threshold_db"]:.1f} dB'
        e = "--" if r.get("ebn0_db") is None else f'{r["ebn0_db"]:.1f} dB'
        rep = "--" if r["threshold_reported"] is None else f'{r["threshold_reported"]:.1f} dB'
        out.append(
            f'| {r["mcs"]} | {r["label"]} | {r["bps"]} bps | {bw} | {t} | {e} | {rep} |'
        )
    return "\n".join(out)


def goodput_table(rows: list[dict]) -> str:
    out = [
        f"| S/N ({BANDWIDTH_HZ:.0f} Hz) | goodput | % of Shannon | mean MCS | retx | complete |",
        "|--------------:|--------:|-------------:|---------:|-----:|---------:|",
    ]
    for r in rows:
        out.append(
            f'| {r["snr_db"]:5.1f} dB | {r["goodput_bps"]:7.0f} bps | '
            f'{100 * r["goodput_bps"] / r["shannon_bps"]:4.1f} % | '
            f'{r["mean_mcs"]:4.1f} | {r["retx"]:3.1f} | {100 * r["complete"]:3.0f} % |'
        )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Halcyon modem benchmarks")
    ap.add_argument(
        "--mode",
        choices=["thresholds", "goodput", "rate", "compliance", "all"],
        default="all",
    )
    ap.add_argument("--channel", default="awgn", choices=["awgn", "good", "moderate", "poor"])
    ap.add_argument(
        "--profile",
        choices=["wide", "robust", "compact", "deep", "nano", "micro", "mini"],
        default="wide",
    )
    ap.add_argument("--half", type=float, default=3.5, help="threshold search half-width, dB")
    ap.add_argument("--frames", type=int, default=4, help="frames per threshold point")
    ap.add_argument("--messages", type=int, default=4, help="messages per goodput point")
    ap.add_argument("--message-bytes", type=int, default=600)
    ap.add_argument("--snr-min", type=float, default=-2.0)
    ap.add_argument("--snr-max", type=float, default=30.0)
    ap.add_argument("--snr-step", type=float, default=2.0)
    args = ap.parse_args()

    t0 = time.time()
    if args.mode in ("thresholds", "all"):
        phy = PROFILES[args.profile]()
        print(
            f"Measuring thresholds on '{args.channel}' channel, {args.profile} profile "
            f"({phy.active.size} carriers, {phy.bandwidth_hz:.0f} Hz)"
        )
        rows = measure_thresholds(args.channel, args.frames, half=args.half, phy=phy)
        table = thresholds_table(rows, args.channel, phy)
        # Keep the wide profile's historical filenames; a second profile must not
        # overwrite them.
        stem = f"thresholds_{args.channel}" + (
            "" if args.profile == "wide" else f"_{args.profile}"
        )
        path = os.path.join(RESULTS_DIR, f"{stem}.md")
        write_markdown(path, "# Halcyon measured thresholds", table)
        with open(os.path.join(RESULTS_DIR, f"{stem}.csv"), "w") as fh:
            w = csv.writer(fh)
            w.writerow(["mcs", "label", "info_bps", "snr_db", "block_fer", "header_loss", "reported_db"])
            for r in rows:
                for snr, fer, hdr, rep in r["curve"]:
                    w.writerow([r["mcs"], r["label"], r["bps"], snr, f"{fer:.4f}", f"{hdr:.4f}", f"{rep:.2f}"])
        print(table)
        print(f"written {path}")
        name = _TABLE_NAMES[args.profile]
        print(f"\n# paste into frame.py: {args.profile} profile, both axes")
        print(f"{name}: tuple[MCS, ...] = (")
        for r in rows:
            m = mcs(r["mcs"], phy)
            rep = "None" if r["threshold_reported"] is None else f'{r["threshold_reported"]:.1f}'
            ch = "None" if r["threshold_db"] is None else f'{r["threshold_db"]:.1f}'
            print(f'    MCS({m.bps}, {m.rate:.4f}, "{r["label"]}", {rep}, {ch}),')
        print(")")

    if args.mode in ("rate", "all"):
        print("\nModem-level rate vs channel S/N (from measured thresholds)")
        rows = rate_table(np.arange(args.snr_min, args.snr_max + 1e-9, args.snr_step / 2.5))
        table = rate_html(rows)
        path = os.path.join(RESULTS_DIR, "rate_vs_snr.md")
        write_markdown(path, "# Halcyon modem rate vs S/N", table)
        print(table)
        print(f"written {path}")


    if args.mode in ("compliance", "all"):
        rows = [
            compliance_row(Phy(), "default (FCC 2.8 kHz)"),
            compliance_row(Phy(**NARROW), "IARU R1 narrow (2.7 kHz)"),
            compliance_row(Phy(**ROBUST), "robust (562 Hz)"),
            compliance_row(Phy(**COMPACT), "compact (492 Hz)"),
        ]
        table = compliance_table(rows)
        print("\nRegulatory check on the transmitted waveform")
        print(table)
        path = os.path.join(RESULTS_DIR, "compliance.md")
        write_markdown(path, "# Halcyon emission bandwidth and emission type", table)
        print(f"written {path}")

    if args.mode in ("goodput", "all"):
        print(f"\nMeasuring end-to-end goodput on '{args.channel}' channel")
        snrs = np.arange(args.snr_min, args.snr_max + 1e-9, args.snr_step)
        rows = measure_goodput(args.channel, snrs, args.message_bytes, args.messages)
        table = goodput_table(rows)
        path = os.path.join(RESULTS_DIR, f"goodput_{args.channel}.md")
        write_markdown(path, f"# Halcyon goodput, `{args.channel}` channel", table)
        print(table)
        print(f"written {path}")
        gp = np.array([r["goodput_bps"] for r in rows])
        print(
            f"peak end-to-end goodput {gp.max():.0f} bps.  This axis includes "
            "ARQ ACKs and turnaround, so it is not directly comparable with the "
            "modem-level figures; use --mode rate for those."
        )
    print(f"total wall time {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
