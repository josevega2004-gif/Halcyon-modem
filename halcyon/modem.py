"""The modem: bits -> waveform and waveform -> bits, all in one place.

One object owns the PHY, the code caches and the interleaver, so a transmitter
and a receiver in the same process share exactly the same tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .bits import bits_to_bytes, bytes_to_bits, crc16
from .frame import (
    FT_ACK,
    FT_DATA,
    HEADER_INFO_BITS,
    HEADER_PAYLOAD_BITS,
    MAX_BLOCKS,
    MAX_PAYLOAD_BITS,
    MCS_TABLE,
    SYMS_PER_BLOCK,
    block_sizes,
    header_info,
    header_repeat,
    code_to_snr,
    mcs,
    snr_to_code,
    table_for,
    unpack_header,
)
from .ldpc import code_for
from .ofdm import Phy


@dataclass
class RxResult:
    """Everything the link layer and the operator might want to know."""

    payload: bytes = b""
    blocks_ok: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    block_bits: list[np.ndarray] = field(default_factory=list)
    payload_bits: int = 0
    mcs: int = 0
    blocks: int = 0
    ftype: int = -1
    seq: int = -1
    ack_bitmap: int = 0
    snr_reported: float = -99.0
    mcs_sugg: int = -1
    snr_db: float = -99.0
    header_ok: bool = False
    cfo_hz: float = 0.0
    sync_ok: bool = False
    header_iterations: int = 0
    # Per-block LLRs from the last receive, so the link layer can sum a
    # retransmission onto the first attempt instead of starting cold.
    block_llrs: object = None

    @property
    def ok(self) -> bool:
        return bool(self.header_ok and np.all(self.blocks_ok))


class Modem:
    def __init__(self, phy: Phy | None = None, max_iter: int = 40):
        self.phy = phy or Phy()
        self.max_iter = max_iter
        n_h = self.phy.header_syms * self.phy.n_data * 2
        self.header_n = n_h
        self.header_k = n_h // 4
        if self.header_k < HEADER_INFO_BITS:  # pragma: no cover
            raise RuntimeError("header code too small")
        self._interleavers: dict[tuple[int, int], np.ndarray] = {}
        # LLR scale for the non-coherent 16-FSK modes.  The tones are normalised
        # by their per-symbol median energy, so this is a dimensionless constant;
        # 2.0 measured best of 1/2/4 on the compact profile.
        self.mfsk_scale = 2.0

    # ------------------------------------------------------------------ helpers
    def block_code(self, mcs_idx: int):
        """The block code, or ``None`` for an uncoded (CRC-only) rung."""
        n, k = block_sizes(self.phy, mcs_idx)
        mode = mcs(mcs_idx, self.phy)
        if mode.uncoded:
            return None
        if mode.codec.startswith("conv"):
            from .conv import ConvolutionalBlockCodec

            return ConvolutionalBlockCodec(
                int(mode.codec[4:]), n // max(1, mode.rep)
            )
        return code_for(n // max(1, mode.rep), k)

    def header_code(self):
        return code_for(self.header_n, self.header_k)

    def interleaver(self, blocks: int, n: int) -> np.ndarray:
        key = (blocks, n)
        perm = self._interleavers.get(key)
        if perm is None:
            rng = np.random.default_rng(blocks * 100003 + n)
            perm = rng.permutation(blocks * n)
            self._interleavers[key] = perm
        return perm

    # ----------------------------------------------------------------- transmit
    def _header_symbols(self, info: np.ndarray) -> np.ndarray:
        code = self.header_code()
        info = np.asarray(info, dtype=np.uint8).ravel()
        if info.size > code.k:  # pragma: no cover
            raise ValueError("header fields do not fit the header code")
        padded = np.zeros(code.k, dtype=np.uint8)
        padded[: info.size] = info
        cw = code.encode(padded)[0]
        return self.phy.bits_to_symbols(cw, 2)

    def build(self, payload: bytes, mcs_idx: int, **kw) -> tuple[np.ndarray, int]:
        """Frame a byte payload; returns (waveform, number_of_blocks)."""
        return self.build_bits(bytes_to_bits(payload), mcs_idx, **kw)

    def build_bits(
        self,
        bits: np.ndarray,
        mcs_idx: int,
        seq: int = 0,
        ftype: int = FT_DATA,
        ack_bitmap: int = 0,
        snr_code: int = 0,
        mcs_sugg: int = 0,
    ) -> tuple[np.ndarray, int]:
        """Frame an arbitrary bit payload; returns (waveform, number_of_blocks)."""
        bits = np.asarray(bits, dtype=np.uint8).ravel()
        if bits.size > MAX_PAYLOAD_BITS:
            raise ValueError(f"payload too large ({bits.size} > {MAX_PAYLOAD_BITS} bits)")
        n, k = block_sizes(self.phy, mcs_idx)
        cap = k - 16
        _codec = mcs(mcs_idx, self.phy).codec
        if _codec.startswith("conv"):
            constraint = int(_codec[4:])
            per_copy = n // max(1, mcs(mcs_idx, self.phy).rep)
            if 2 * (k + constraint - 1) > per_copy:
                raise ValueError("convolutional rung geometry does not fit the block")
        blocks = max(1, int(np.ceil(bits.size / cap))) if bits.size else 0
        if blocks > MAX_BLOCKS:
            raise ValueError(f"payload needs {blocks} blocks, max is {MAX_BLOCKS}")
        blocks_n = blocks

        info = np.zeros((max(blocks, 1), k), dtype=np.uint8)
        for b in range(blocks):
            part = bits[b * cap : (b + 1) * cap]
            info[b, : part.size] = part
            info[b, cap :] = crc16(info[b, :cap])
        if blocks:
            rep = max(1, mcs(mcs_idx, self.phy).rep)
            code = self.block_code(mcs_idx)
            if code is None:
                # Uncoded rung: the info bits (payload + CRC-16) are transmitted
                # as they stand.  Nothing to encode and nothing to repair.
                cw = info[:blocks].astype(np.uint8)
            else:
                cw = code.encode(info[:blocks]).astype(np.uint8)
            if rep > 1:
                cw = np.tile(cw, (1, rep))
            flat = cw.reshape(-1)
            flat = flat[self.interleaver(blocks, n)]
            _mode = mcs(mcs_idx, self.phy)
            if _mode.kind == "mfsk":
                from .mfsk import mfsk as _mfsk

                tones_n = 1 << int(_mode.tone_bits)
                pay_wave = self.phy.modulate_mfsk(_mfsk(tones_n).map_bits(flat), tones_n)
            else:
                pay_wave = self.phy.modulate(self.phy.bits_to_symbols(flat, _mode.bps))
        else:
            pay_wave = np.zeros(0, dtype=complex)

        hdr = header_info(
            version=2,
            ftype=ftype,
            mcs=mcs_idx,
            blocks=blocks_n,
            payload_bits=int(bits.size),
            seq=seq & 0xFF,
            ack_bitmap=ack_bitmap & 0xFFF,
            snr_code=snr_code & 0x1F,
            mcs_sugg=mcs_sugg & 0x0F,
        )
        hdr_syms = self._header_symbols(hdr)
        hdr_wave = self.phy.modulate(hdr_syms)
        # The header sets the frame's floor, not the payload: measured, frames with a
        # strengthened payload walled at ~0 dB because the header was still sent
        # once.  It must be repeated by the frame's whole span -- repetition *and*
        # block length -- and the receiver tries each factor until the CRC passes,
        # since it cannot know either before decoding the header.
        _m = mcs(mcs_idx, self.phy)
        hdr_rep = header_repeat(_m, self.phy)
        if hdr_rep > 1:
            hdr_wave = np.tile(hdr_wave, hdr_rep)
        pre = self.phy.preamble
        waveform = np.concatenate([pre, hdr_wave, pay_wave])
        return waveform, blocks_n

    def build_ack(
        self,
        seq: int,
        blocks_ok: np.ndarray,
        snr_db: float,
        mcs_sugg: int = 0,
        data_mcs: int = 0,
    ) -> np.ndarray:
        bitmap = 0
        for i, ok in enumerate(blocks_ok[:MAX_BLOCKS]):
            if ok:
                bitmap |= 1 << i
        hdr = header_info(
            version=2,
            ftype=FT_ACK,
            mcs=0,
            blocks=int(len(blocks_ok)),
            payload_bits=0,
            seq=seq & 0xFF,
            ack_bitmap=bitmap,
            snr_code=snr_to_code(snr_db),
            mcs_sugg=mcs_sugg & 0x0F,
        )
        hdr_syms = self._header_symbols(hdr)
        hdr_wave = self.phy.modulate(hdr_syms)
        # An ACK has no payload whose repetition can protect its header, so it
        # used to go out with one copy even when the data frame's header was
        # repeated four times.  At -2 dB the data blocks decoded on every
        # attempt but the ACK header was lost 8/8, and the link discarded good
        # data until it gave up.  Match the ACK header to the data rung's own
        # header protection; the receiver already tries 1/2/4/8 copies.
        # Narrow profiles already carry 10-12 header symbols, but the ACK still
        # has no payload repetition behind it: one and two copies both measured
        # 0/2 ACK headers at -4 dB on the compact profile while data headers
        # decoded.  Four copies is the control-frame floor there; wide high-rate
        # frames keep their single copy.
        min_rep = 4 if int(getattr(self.phy, "header_syms", 2)) >= 4 else 1
        rep = max(min_rep, header_repeat(mcs(data_mcs, self.phy), self.phy))
        if rep > 1:
            hdr_wave = np.tile(hdr_wave, rep)
        return np.concatenate([self.phy.preamble, hdr_wave])

    # -------------------------------------------------------------------- receive
    def receive(self, rx: np.ndarray, prior: list | None = None) -> RxResult:
        rx = np.asarray(rx, dtype=complex)
        res = RxResult()
        acq = self.phy.acquire(rx)
        if acq is None:
            return res
        start, cfo = acq
        res.cfo_hz = cfo
        res.sync_ok = True
        rx = self.phy.derotate(rx, cfo)
        sym_len = self.phy.sym_len
        h_off = start + self.phy.preamble_syms * sym_len
        hs = self.phy.header_syms
        hdr_bits = HEADER_PAYLOAD_BITS + 16
        # A frame acquired a sample or two late overruns the capture by exactly
        # that much at the end, and this used to abandon the whole burst for it --
        # deterministically, at every seed: start +1, +2 and +3 measured 0/6
        # blocks where start 0 and -1 measured 6/6, with the *same* 5 % raw bit
        # error rate and the same noise estimate.  Nothing in the waveform was
        # wrong; the receiver refused frames it could have decoded.  Padding the
        # tail costs nothing, and a couple of samples of guard-interval slack are
        # harmless (see ``Ofdm.acquire``).
        if h_off + hs * sym_len > rx.size:
            rx = np.pad(rx, (0, h_off + hs * sym_len - rx.size))
        # The sender repeats the header by the payload's repetition factor, and the
        # receiver cannot know that factor before it has decoded the header.  Try
        # each one in turn and let the header CRC settle it; ascending order means
        # ordinary single-copy frames pay for one attempt only.
        #
        # A doubled preamble was implemented and measured as an escape from the
        # -4 dB sync wall and dropped: a repeated preamble makes the correlation
        # peak ambiguous among its copies, and at -4 dB acquisition locked on the
        # wrong one more often than with the short preamble (100 % header loss
        # against 25 %, 4 frames).
        hdr = None
        for hr in (1, 2, 4, 8):
            span = hs * hr * sym_len
            if h_off + span > rx.size:
                if h_off + span - rx.size > 4 * sym_len:
                    break
                rx = np.pad(rx, (0, h_off + span - rx.size))
            Xh, var_h, snr_h = self.phy.demodulate(rx[h_off : h_off + span], hs * hr)
            llr_h = self.phy.symbols_to_llr(Xh, 2, var_h)
            if hr > 1:
                llr_h = llr_h.reshape(hr, llr_h.size // hr).sum(axis=0)
            bits_h, conv, iters = self.header_code().decode(
                llr_h, max_iter=max(self.max_iter, 60)
            )
            res.header_iterations = iters
            if not conv:
                continue
            body = bits_h[:hdr_bits]
            if not np.array_equal(
                crc16(body[:HEADER_PAYLOAD_BITS].copy()), body[HEADER_PAYLOAD_BITS:]
            ):
                continue
            cand = unpack_header(body[:HEADER_PAYLOAD_BITS])
            if cand["version"] != 2 or cand["mcs"] >= len(table_for(self.phy)):
                continue
            hdr = cand
            break
        if hdr is None:
            return res
        res.header_ok = True
        res.ftype = hdr["ftype"]
        res.mcs = hdr["mcs"]
        res.blocks = hdr["blocks"]
        res.payload_bits = hdr["payload_bits"]
        res.seq = hdr["seq"]
        res.ack_bitmap = hdr["ack_bitmap"]
        res.snr_reported = code_to_snr(hdr["snr_code"])
        res.mcs_sugg = hdr["mcs_sugg"]
        res.snr_db = snr_h
        if hdr["ftype"] == FT_ACK or res.blocks == 0:
            res.blocks_ok = np.zeros(0, dtype=bool)
            return res

        blocks = res.blocks
        _m = mcs(res.mcs, self.phy)
        rep = max(1, _m.rep)
        # Payload starts after every copy of the header, however many the sender
        # used -- the factor comes from the decoded MCS, not from how we got here.
        p_off = h_off + hs * header_repeat(_m, self.phy) * sym_len
        block_mult = max(1, mcs(res.mcs, self.phy).block_mult)
        n_syms = blocks * self.phy.syms_per_block * rep * block_mult
        need = p_off + n_syms * sym_len
        if need > rx.size:
            # Only a genuinely truncated capture is fatal; a late acquisition is
            # not -- it overruns by the offset and decodes once the tail is padded.
            if need - rx.size > sym_len:
                res.blocks_ok = np.zeros(blocks, dtype=bool)
                res.block_bits = [np.zeros(0, dtype=np.uint8) for _ in range(blocks)]
                return res
            rx = np.pad(rx, (0, need - rx.size))
        _mode = mcs(res.mcs, self.phy)
        if _mode.kind == "mfsk":
            from .frame import header_power_db
            from .mfsk import mfsk as _mfsk, snr_from_tones

            tones_n = 1 << int(_mode.tone_bits)
            E = self.phy.demodulate_mfsk(
                rx[p_off : p_off + n_syms * sym_len], n_syms, tones_n
            )
            llr = _mfsk(tones_n).demap(E, self.mfsk_scale).ravel()
            # The header estimate is the *header's* S/N, and the QPSK header puts
            # power on every data carrier where a tone puts it on one: measured
            # 10-14 dB high on these profiles.  The payload's own tone energies
            # give the S/N the data actually saw.
            res.snr_db = snr_from_tones(E, tones_n) + header_power_db(
                self.phy, _mode, blocks
            )
        else:
            X, var, snr = self.phy.demodulate(rx[p_off : p_off + n_syms * sym_len], n_syms)
            res.snr_db = snr if np.isfinite(snr) else snr_h
            llr = self.phy.symbols_to_llr(X, _mode.bps, var)
        n, k = block_sizes(self.phy, res.mcs)
        perm = self.interleaver(blocks, n)
        flat = np.empty_like(llr)
        flat[perm] = llr  # undo the transmitter-side permutation
        info = flat.reshape(blocks, n)
        if rep > 1:
            info = info.reshape(blocks, rep, n // rep).sum(axis=1)
        if prior is not None:
            # Soft combining: add the stored LLRs from earlier attempts of the
            # same block. Independent noise looks, so each retransmission adds
            # about 3 dB of Eb/N0 -- that is how airtime buys sensitivity when
            # the rate itself is not allowed to fall any further.
            for b_ in range(min(len(prior), info.shape[0])):
                p_ = prior[b_]
                if p_ is not None and getattr(p_, 'size', 0) == info.shape[1]:
                    info[b_] = info[b_] + p_
        res.block_llrs = [info[b_].copy() for b_ in range(info.shape[0])]
        code = self.block_code(res.mcs)
        ok = np.zeros(blocks, dtype=bool)
        parts: list[np.ndarray] = []
        cap = k - 16
        for b in range(blocks):
            if code is None:
                bits_b = (info[b] < 0).astype(np.uint8)
                conv_b = True
            else:
                bits_b, conv_b, _ = code.decode(info[b], max_iter=self.max_iter)
            chunk = bits_b[:cap]
            crc_bits = bits_b[cap:k]  # CRC-16 sits at the end of the info part
            if np.array_equal(crc16(chunk.copy()), crc_bits):
                ok[b] = True
                parts.append(chunk)
            else:
                parts.append(np.zeros(0, dtype=np.uint8))
        res.blocks_ok = ok
        res.block_bits = parts
        if parts and any(ok):
            allbits = np.concatenate([p for p, o in zip(parts, ok) if o])
            if res.payload_bits:
                allbits = allbits[: res.payload_bits]
            res.payload = bits_to_bytes(allbits) if allbits.size else b""
        return res
