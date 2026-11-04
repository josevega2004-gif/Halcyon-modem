"""Link layer: burst ARQ with selective repeat inside the burst.

The model is deliberately honest about the cost of a half-duplex HF link:
every burst pays a preamble and header, every burst needs an ACK, and each
direction change costs a turnaround delay.  Goodput therefore reflects
retransmissions and ACK airtime, not just modulation efficiency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .frame import best_mcs, chunk_bits, table_for
from .modem import Modem


@dataclass
class LinkConfig:
    max_blocks: int = 12
    turnaround_s: float = 0.20
    max_attempts: int = 8
    adaptive: bool = True
    start_mcs: int = 4
    # Thresholds are measured at 10 % block error; a multi-block burst needs a
    # few dB of headroom to keep the retransmission rate low.
    margin_db: float = 3.0
    probe_first: bool = True


@dataclass
class LinkStats:
    total_bits: int = 0
    delivered_bits: int = 0
    airtime_s: float = 0.0
    frames: int = 0
    retransmissions: int = 0
    header_failures: int = 0
    snr_reports: list[float] = field(default_factory=list)
    mcs_used: list[int] = field(default_factory=list)
    mcs_changes: int = 0
    complete: bool = False

    @property
    def goodput_bps(self) -> float:
        return self.delivered_bits / self.airtime_s if self.airtime_s > 0 else 0.0

    @property
    def mean_mcs(self) -> float:
        return float(np.mean(self.mcs_used)) if self.mcs_used else 0.0

    @property
    def mean_snr_db(self) -> float:
        return float(np.mean(self.snr_reports)) if self.snr_reports else 0.0

    @property
    def efficiency(self) -> float:
        return self.delivered_bits / self.total_bits if self.total_bits else 0.0


class Link:
    """One end-to-end transfer over a simulated channel."""

    def __init__(self, modem: Modem, cfg: LinkConfig, make_channel, seed: int = 0):
        self.modem = modem
        self.cfg = cfg
        self.make_channel = make_channel
        self.rng = np.random.default_rng(seed)
        self._burst = 0
        self.stats = LinkStats()

    def _channel(self, tx: np.ndarray) -> np.ndarray:
        self._burst += 1
        return self.make_channel(self._burst)(tx)

    def _window(self, total: int, offset: int, mcs_idx: int, probe: bool = False):
        cap = chunk_bits(self.modem.phy, mcs_idx)
        want = 1 if probe else self.cfg.max_blocks
        n = int(min(want, np.ceil((total - offset) / cap))) or 1
        ranges = [
            (min(offset + i * cap, total), min(offset + (i + 1) * cap, total)) for i in range(n)
        ]
        return cap, n, ranges

    def _step_down(self, mcs_idx: int, reported_db: float | None) -> int:
        """Pick a level the receiver's report says should work, never upward."""
        if reported_db is None:
            return max(0, mcs_idx - 1)
        cand = best_mcs(reported_db, self.cfg.margin_db, self.modem.phy)
        return max(0, min(mcs_idx - 1, cand))

    def run(self, data: bytes) -> LinkStats:
        phy = self.modem.phy
        fs = float(phy.fs)
        bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8)) if data else np.zeros(0, np.uint8)
        st = self.stats = LinkStats(total_bits=int(bits.size))
        total = bits.size
        # The profile decides the ladder, so a start index from a wider profile can
        # be out of range here: the narrow ladders have only a few rungs.
        mcs_idx = int(min(self.cfg.start_mcs, len(table_for(phy)) - 1))
        offset = 0
        seq = 0
        probe = self.cfg.probe_first
        prior: dict[int, np.ndarray] = {}   # block index -> LLRs of failed attempts

        while offset < total:
            cap, n, ranges = self._window(total, offset, mcs_idx, probe)
            remaining = list(range(n))
            attempts = 0

            while remaining and attempts < self.cfg.max_attempts:
                attempts += 1
                payload = np.concatenate([bits[ranges[i][0] : ranges[i][1]] for i in remaining])
                tx, _ = self.modem.build_bits(payload, mcs_idx, seq=seq)
                st.mcs_used.append(mcs_idx)
                st.frames += 1
                st.airtime_s += tx.size / fs + self.cfg.turnaround_s
                res = self.modem.receive(
                    self._channel(tx), prior=[prior.get(offset + i) for i in remaining]
                )
                if res.block_llrs is not None:
                    for k, i in enumerate(remaining):
                        if k < len(res.block_llrs):
                            if res.blocks_ok[k]:
                                prior.pop(offset + i, None)
                            else:
                                prior[offset + i] = res.block_llrs[k]

                if not res.header_ok:
                    st.header_failures += 1
                    last = st.snr_reports[-1] if st.snr_reports else None
                    new_mcs = self._step_down(mcs_idx, last)
                    if self.cfg.adaptive and new_mcs != mcs_idx:
                        mcs_idx = new_mcs
                        st.mcs_changes += 1
                        cap, n, ranges = self._window(total, offset, mcs_idx, probe)
                        remaining = list(range(n))
                        attempts = 0
                    continue

                st.snr_reports.append(res.snr_db)
                suggestion = best_mcs(res.snr_db, self.cfg.margin_db, phy)
                ack = self.modem.build_ack(
                    seq, res.blocks_ok, res.snr_db, suggestion if self.cfg.adaptive else mcs_idx
                )
                st.airtime_s += ack.size / fs + self.cfg.turnaround_s
                ack_res = self.modem.receive(self._channel(ack))

                if ack_res.header_ok and ack_res.ftype == 1 and ack_res.seq == seq:
                    bitmap = ack_res.ack_bitmap
                    still = [i for k, i in enumerate(remaining) if not (bitmap >> k) & 1]
                    st.retransmissions += len(still)  # blocks that must go again
                    nothing_decoded = len(still) == len(remaining)
                    remaining = still
                else:
                    nothing_decoded = False  # lost ACK: same blocks go again

                if nothing_decoded and self.cfg.adaptive:
                    # The burst was no good at this level; reframe at a level the
                    # receiver's own S/N report supports.
                    new_mcs = self._step_down(mcs_idx, res.snr_db)
                    if new_mcs != mcs_idx:
                        mcs_idx = new_mcs
                        st.mcs_changes += 1
                        cap, n, ranges = self._window(total, offset, mcs_idx, probe)
                        remaining = list(range(n))
                        attempts = 0

            if not remaining:
                offset += n * cap
                seq = (seq + 1) & 0xFF
                probe = False
                if self.cfg.adaptive and st.snr_reports:
                    new_mcs = best_mcs(st.snr_reports[-1], self.cfg.margin_db, phy)
                    if new_mcs != mcs_idx:
                        mcs_idx = new_mcs
                        st.mcs_changes += 1
            else:
                break  # gave up on this window

        st.delivered_bits = min(offset, total)
        st.complete = offset >= total
        return st
