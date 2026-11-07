"""Frame format: MCS ladder, header fields, block sizing.

A frame is

    [ preamble: 2 OFDM symbols, random BPSK, all active carriers ]
    [ header:   2 OFDM symbols, QPSK, rate-1/4 LDPC  ]
    [ payload:  n_blocks x 4 OFDM symbols at the negotiated MCS ]

The header is coded far more heavily than the payload so that a receiver can
read it long before it could decode data, which is what makes one-shot
per-frame rate adaptation possible: the sender picks an MCS, and every frame
carries its own MCS so a burst can be decoded without side information.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bits import crc16, int_to_bits

SYMS_PER_BLOCK = 4
HEADER_SYMS = 2
MAX_BLOCKS = 12
MAX_PAYLOAD_BITS = (1 << 16) - 1

# Channel S/N is quoted in a 2.75 kHz passband, the way operators quote it.  A
# narrow geometry collects proportionally less noise at the same noise density,
# so its own in-band S/N is higher by this ratio -- and the narrow ladders'
# thresholds are measured on that in-band axis.  One constant, so the conversion
# happens in one place instead of separately in the host and the link.
REFERENCE_BW_HZ = 2742.1875

FT_DATA = 0
FT_ACK = 1

# (name, width in bits) -- serialised MSB first
HEADER_FIELDS: tuple[tuple[str, int], ...] = (
    ("version", 2),
    ("ftype", 2),
    ("mcs", 5),
    ("blocks", 6),
    ("payload_bits", 16),
    ("seq", 8),
    ("ack_bitmap", 12),
    ("snr_code", 5),
    ("mcs_sugg", 4),
)
# 2 + 2 + 5 + 6 + 16 + 8 + 12 + 5 + 4 = 60.  This said 59 for a long time in a
# trailing comment; the C++ port copied the comment and read the CRC one bit
# off the end, which is exactly the sort of thing a second implementation is
# good at finding.
HEADER_PAYLOAD_BITS = sum(w for _, w in HEADER_FIELDS)  # 60
HEADER_INFO_BITS = HEADER_PAYLOAD_BITS + 16             # + CRC-16 = 76


def snr_to_code(snr_db: float) -> int:
    return int(np.clip(round(snr_db + 8.0), 0, 31))


def code_to_snr(code: int) -> float:
    return float(code) - 8.0


@dataclass(frozen=True)
class MCS:
    bps: int          # bits per data carrier
    rate: float       # LDPC code rate
    label: str
    # S/N (dB) as the receiver reports it at block FER <= 10 %: the pilot estimate
    # for the QAM rungs, the payload tone-energy estimate (``mfsk.snr_from_tones``)
    # for the non-coherent ones, which is the same in-band axis.  Rate control
    # compares estimator with estimator, so this axis is used there.
    snr_min: float
    # The same threshold expressed as channel S/N in the 2.75 kHz band, which is
    # the axis operators quote.  Measured by
    # ``python -m halcyon.bench --mode thresholds``.
    #
    # Both axes carry about +-1 dB of measurement uncertainty: the criterion is
    # the first 1 dB grid point with block error <= 10 %, over 6 frames x 6
    # blocks.  The QPSK rows wobble most, because 12 % of 36 blocks is one block.
    snr_channel: float
    # Repetition: the codeword is transmitted `rep` times and the decoder sums the
    # per-bit LLRs before decoding.  Each doubling is worth about 3 dB with no new
    # code to trust, which is exactly what a "connect in harsh conditions" rung
    # needs -- inventing a rate-1/16 LDPC at n = 696 would not be.
    rep: int = 1
    # Block-length multiplier: one codeword spanning `block_mult` times the base
    # 4-symbol block, instead of a short code sent several times.  This is where
    # OFDM earns its keep: 87 parallel data carriers make a 2 784-bit codeword only
    # 32 symbols long, so a real rate-1/8 code fits in the airtime that repetition
    # would spend sending a 696-bit code four times -- and it carries twice the
    # payload while it does it.  A single-carrier ladder cannot build codewords
    # like that at any usable symbol rate, which is the structural difference
    # between this modem and a single-carrier one, not a copied ladder.
    block_mult: int = 1
    # No FEC at all: the block is data + its CRC-16 sent at rate 1.0, and the
    # burst ARQ carries the risk instead of parity bits.  At the top of the
    # ladder 1/6 of the airtime is parity a strong path does not need, but a
    # rate-8/9 code has so little redundancy that it may decode worse than no
    # code at all -- so this is offered as its own rung and kept only if it
    # measures onto the frontier.  CRC-16 still rejects a bad block (the ARQ
    # resends it); what disappears is the ability to repair one.
    uncoded: bool = False
    # Modulation family.  "qam" is the OFDM payload: one constellation point per
    # data carrier.  "mfsk" is the low-speed mode: one active tone per symbol out
    # of 16, detected non-coherently, so the payload needs no channel estimate.
    kind: str = "qam"
    # Bits per 16-FSK symbol when kind == "mfsk": 4 for a 16-tone alphabet, 3 for
    # 8 tones.  The smaller alphabet is worth about a decibel and costs a third of
    # the rate, which is the trade worth making at the very bottom of the ladder.
    tone_bits: int = 4
    # Block code: "ldpc" for the ladder's usual code, or "conv9"/"conv14" for a
    # zero-terminated convolutional code with soft-decision Viterbi decoding.
    # The convolutional rate is fixed at 1/2 minus the tail, so ``rate`` above is
    # only a label for those rungs: block_sizes() derives k from the geometry.
    codec: str = "ldpc"


MCS_TABLE: tuple[MCS, ...] = (
    # Harsh-condition rungs, measured against each other rather than assumed.
    #
    # The 453 and 906 bps rungs are single long codewords: 87 parallel data
    # carriers make a 2 784- or 1 392-bit LDPC codeword only 16 or 8 symbols long,
    # which is a structural advantage a single-carrier ladder has no way to copy.
    # Measured at equal payload rate they came out 1.0-2.7 dB more sensitive than
    # the short-block rungs they replace, so those were dropped as dominated.
    #
    # The 227 bps rung is still 4x repetition of the (696, 168) code, because here
    # repetition won: a rate-1/8 codeword stretched to 5 568 bits measured the same
    # -2 dB as the 2 784-bit one rather than 3 dB better, so beyond about 2 800 bits
    # this LDPC stops improving and the short repeated code is the most sensitive
    # thing the ladder has.  What limits the bottom is our short-block code
    # behaviour, not the bandwidth; and below -4 dB the 2-symbol preamble stops
    # syncing whatever the block length.
    #
    # The reported-S/N column reads high here (about 9 dB) because the pilot
    # estimator is noise-loaded at low S/N, so rate control never targets these
    # modes from the estimator (see ``best_mcs``); the link steps down into them
    # after failures instead.
    MCS(2, 1 / 4, "QPSK 1/4 r4", 8.1, -3.0, 4),
    MCS(2, 1 / 8, "QPSK 1/8 L4", 7.2, -2.0, 1, 4),
    MCS(2, 1 / 4, "QPSK 1/4 L2", 5.9, 0.0, 1, 2),
    MCS(2, 1 / 3, "QPSK 1/3", 5.8, 2.0),
    MCS(2, 1 / 2, "QPSK 1/2", 5.8, 2.6),
    MCS(2, 3 / 4, "QPSK 3/4", 7.1, 5.7),
    # Fills the 2.5 dB hole between QPSK 3/4 and 16QAM 1/2: the old ladder
    # charged 2.9 dB for 906 bps there, and part of that was the constellation
    # change rather than the rate.  At 7.0 dB this carries 3 172 bps where the
    # old ladder carried 2 719.
    MCS(2, 7 / 8, "QPSK 7/8", 8.1, 7.0),
    MCS(4, 1 / 2, "16QAM 1/2", 9.3, 8.6),
    MCS(4, 2 / 3, "16QAM 2/3", 10.9, 10.3),
    MCS(4, 3 / 4, "16QAM 3/4", 11.8, 11.5),
    MCS(4, 7 / 8, "16QAM 7/8", 14.0, 13.7),
    MCS(6, 2 / 3, "64QAM 2/3", 15.8, 15.6),
    MCS(6, 3 / 4, "64QAM 3/4", 17.5, 17.4),
    # Replaces 256QAM 2/3, which bought +1.6 % rate over 64QAM 7/8 for a whole
    # decibel -- it was never on the frontier.  This sits in the 2.2 dB gap
    # between 64QAM 3/4 and 64QAM 7/8 instead.
    MCS(6, 5 / 6, "64QAM 5/6", 18.7, 18.8),
    MCS(6, 7 / 8, "64QAM 7/8", 19.7, 19.6),
    MCS(8, 5 / 6, "256QAM 5/6", 24.2, 24.1),
    # Rate 8/9 at 256QAM extends the ceiling itself: +6.7 % rate (12 889 bps)
    # for about a decibel of signal, measured at 6 frames.  It is the only way
    # up from here short of a wider band, and 5/6 stays below it because a link
    # should not need the extra decibel when 5/6 already closes.
    MCS(8, 8 / 9, "256QAM 8/9", 25.1, 25.0),
)


# Narrow, high-noise profile (``Phy(**ROBUST)``): 18 data carriers in 562.5 Hz,
# 12-symbol blocks (432 coded bits at QPSK).  Measured on its own axe -- channel
# S/N is referred to 562.5 Hz here, not to the 2 742 Hz band, so these numbers are
# not comparable with MCS_TABLE row for row (compare Eb/N0 instead).
#
# Only modes that reached 10 % block error are listed.  QPSK 1/4 and QPSK 1/2 were
# dropped: at n = 432 with k = 104 and k = 216 the code never came below 25 %
# block error at any S/N in the search, which is a defect in the LDPC sizing for
# those (n, k) pairs rather than a property of the geometry.  Fix that and they
# can come back with measured thresholds.
ROBUST_TABLE: tuple[MCS, ...] = (
    MCS(2, 1 / 3, "QPSK 1/3", 5.8, 2.0),
    MCS(2, 3 / 4, "QPSK 3/4", 7.1, 5.5),
    MCS(4, 1 / 2, "16QAM 1/2", 9.1, 8.5),
)

def header_repeat(m: MCS, phy) -> int:
    """How many times the header is sent.

    The header has to survive whatever the payload survives, but the payload's
    protection is carried by ``rep`` *and* ``block_mult``, and multiplying the two
    is only sensible while the header is short.  On the wide profile the header is
    2 symbols, so it follows the whole span; on the narrow profiles it is already
    10-12 symbols at rate 1/4, several times stronger than the payload, and
    multiplying that by a block length of 18 would spend ten seconds on a header
    for a 20 bps frame.  So: follow ``rep``, and follow ``block_mult`` only when
    the header is short enough to need it.
    """
    hs = int(getattr(phy, "header_syms", HEADER_SYMS))
    if hs >= 4:
        return max(1, m.rep)
    return max(1, m.rep) * max(1, m.block_mult)


def header_power_db(phy, m: MCS, blocks: int) -> float:
    """How far a non-coherent frame's mean power sits above its payload's, in dB.

    The receiver's noise is set by the frame's mean power, and the header fills
    every active carrier where a tone fills one, so the payload pays
    ``10 log10((h*active + n) / (h + n))`` for the header.  A one-block nano
    probe carries a 22-symbol header against 64 payload symbols and sits 4 dB
    below the burst it is meant to predict; that is a property of the probe's
    shape, not of the channel, so the tone estimate is referred back to the
    payload power before rate control sees it.  The coherent modes put the same
    power on header and payload, so they need no referral.
    """
    if m.kind != "mfsk":
        return 0.0
    spb = int(getattr(phy, "syms_per_block", SYMS_PER_BLOCK))
    h = int(getattr(phy, "header_syms", HEADER_SYMS)) * header_repeat(m, phy)
    n = int(blocks) * spb * max(1, m.rep) * max(1, m.block_mult)
    active = float(max(1, int(np.size(getattr(phy, "active", 1)))))
    if h + n <= 0:
        return 0.0
    return float(10.0 * np.log10((h * active + n) / float(h + n)))


# Narrow, low-speed ladder (``Phy(**COMPACT)``): 21 carriers over 492 Hz.  This is
# the shape a slow level takes -- a narrow emission that fits a 500 Hz gap --
# with the modulation chosen for the job rather than inherited from the fast modes:
# 16-FSK for everything below 100 bps, because at these rates the problem is
# detecting a tone whose phase has rotated, and non-coherent detection solves it
# without a channel estimate.  That was measured rather than assumed: replacing
# the coherent BPSK rungs with 16-FSK bought 4.0 dB at ~40 bps and 3.5 dB at
# ~62 bps, and BPSK 1/8 was dropped for being dominated outright.  Coherent BPSK
# keeps only the top rung, where its rate still wins.
COMPACT_TABLE: tuple[MCS, ...] = (
    # 10 bps: repetition of the working (576, 144) codeword.  A longer rate-1/8
    # code at n = 864 measured a 0.33 block-error floor that did not move with
    # S/N or with the LLR scale, so the rung is built the way that measured
    # clean -- send the codeword twice and sum the LLRs.
    MCS(2, 1 / 4, "16FSK 1/4 r2", -8.6, -7.5, 2, 12, False, "mfsk"),
    # 21 bps.  Every rung here is non-coherent: at these rates the receiver cannot
    # form a channel estimate worth having, and 16-FSK does not need one.
    MCS(2, 1 / 4, "16FSK 1/4", -6.5, -6.0, 1, 12, False, "mfsk"),
    # The two rungs that replaced coherent BPSK, measured against it: 16-FSK 1/2
    # carries *more* than BPSK 1/8 (42 against 39 bps) for 4.0 dB less signal
    # (-4.0 against 0.0 dB), and 16-FSK 3/4 does 62 bps at -2.5 dB.  BPSK 1/8 was
    # dropped as dominated; the coherent modes were paying for a channel estimate
    # that pilot interpolation cannot deliver at -4 dB.
    MCS(2, 1 / 2, "16FSK 1/2", -4.0, -4.0, 1, 12, False, "mfsk"),
    MCS(2, 3 / 4, "16FSK 3/4", -2.2, -2.5, 1, 12, False, "mfsk"),
    # Top rung of the narrow ladder: coherent BPSK still wins on rate, 78 bps, and
    # earns its 3.5 dB over 16-FSK 3/4 by carrying 26 % more.
    MCS(1, 1 / 4, "BPSK 1/4 L2", 5.7, 0.5, 1, 2),
    # Same 16-FSK 1/2 shape as the rung above it, with the block code replaced by
    # the K=9 convolutional code and Viterbi decoding.  Appended at the end
    # rather than inserted beside its sibling because MCS indices travel in the
    # header and address the published threshold table: a new rung goes at the
    # end so no existing index moves.
    #
    # Measured through this chain (AWGN, 16 frames x 6 blocks per point): 7.3 %
    # block error at -4.5 dB against 11.5 % for the LDPC rung, and 75 % against
    # 96.9 % at -5.5 dB.  The 10 % crossing is therefore about 0.5 dB earlier,
    # less than the 0.5-1 dB the ideal non-coherent comparison promised -- the
    # pilot estimate, header and sync eat part of it, and 96 blocks per point
    # leaves about +-0.5 dB.  The threshold below is the measured channel S/N;
    # the reported tone-energy S/N at that point is -4.6 dB.
    MCS(2, 1 / 2, "16FSK 1/2 V9", -4.6, -4.5, 1, 12, False, "mfsk", 4, "conv9"),
)


# 375 Hz narrow test ladder (``Phy(**DEEP)``): 16 carriers, the same three
# non-coherent 16-FSK payloads as COMPACT.  The 62/42/21 bps rungs are reused so
# the only experimental variable is bandwidth; the 10 bps rung is deliberately
# omitted, as requested.  Thresholds are measured on this geometry by
# ``python -m halcyon.bench --mode thresholds --profile deep``.
DEEP_TABLE: tuple[MCS, ...] = (
    MCS(2, 1 / 4, "16FSK 1/4", -6.8, -5.0, 1, 12, False, "mfsk"),
    MCS(2, 1 / 2, "16FSK 1/2", -4.8, -3.5, 1, 12, False, "mfsk"),
    MCS(2, 3 / 4, "16FSK 3/4", -2.4, -1.5, 1, 12, False, "mfsk"),
)


# 250 Hz-class test ladder (``Phy(**NANO)``): 10 carriers over 234 Hz, 8-FSK.
# The 16-tone alphabet does not fit this width at 23.4 Hz spacing, so the lower
# alphabet costs one bit per tone and gives two rungs: 15.6 and 31.25 bps.
NANO_TABLE: tuple[MCS, ...] = (
    MCS(3, 1 / 4, "8FSK 1/4", -5.7, -4.0, 1, 16, False, "mfsk", 3),
    MCS(3, 1 / 2, "8FSK 1/2", -2.7, -2.0, 1, 16, False, "mfsk", 3),
)


# Selected narrow points from the 100-350 Hz sweep (``Phy(**MICRO)`` and
# ``Phy(**MINI)``).  Both use 32-block long codewords.  MICRO's 8-FSK 1/4 rung
# measured <=10 % block error at -6.5 dB in-band, or about -18.5 dB referred to
# 2.7 kHz; MINI's 16-FSK 1/4 rung measured the same -6.5 dB in-band, about
# -17.4 dB at 2.7 kHz, and carries a third more data.
MICRO_TABLE: tuple[MCS, ...] = (
    MCS(3, 1 / 4, "8FSK 1/4", -6.5, -6.5, 1, 32, False, "mfsk", 3),
)

MINI_TABLE: tuple[MCS, ...] = (
    MCS(4, 1 / 4, "16FSK 1/4", -6.5, -6.5, 1, 32, False, "mfsk", 4),
)


# No rung below 10 bps: the 352 Hz 8-FSK geometry reached 7.8 bps at -7.0 dB
# (measured; kept in the git history and the notes), but a mode that slow costs
# a minute of airtime per short message, so the ladder now stops at 10 bps.
# Narrowing the frame instead was measured and does not help: in-band S/N is the
# axis, and a narrower band means a higher noise density at the same in-band
# S/N, so the 10.4 bps rung lost 0.44 block error at -7.0 dB in a 398 Hz frame.
# Narrowing buys band-plan fit, not sensitivity.


# Thresholds are per geometry, so the ladder travels with the profile.
TABLES: dict[str, tuple[MCS, ...]] = {
    "wide": MCS_TABLE,
    "robust": ROBUST_TABLE,
    "compact": COMPACT_TABLE,
    "deep": DEEP_TABLE,
    "nano": NANO_TABLE,
    "micro": MICRO_TABLE,
    "mini": MINI_TABLE,
}


def table_for(phy=None) -> tuple[MCS, ...]:
    return TABLES.get(getattr(phy, "profile", "wide"), MCS_TABLE)


def mcs(idx: int, phy=None) -> MCS:
    table = table_for(phy)
    if not 0 <= idx < len(table):
        raise ValueError(f"MCS index {idx} out of range for this profile")
    return table[idx]


def block_sizes(phy, mcs_idx: int) -> tuple[int, int]:
    """(transmitted bits per block, info length k).

    With ``rep > 1`` the transmitted length is ``rep`` copies of a codeword whose
    own length is ``n // rep``, so the *code* stays the one the thresholds were
    measured with; only the repetition and the airtime change.

    A convolutional rung has a fixed rate of 1/2, so its k is what the geometry
    leaves after the K-1 tail bits, not ``rate`` times the block: n0 coded bits
    carry k = n0/2 - (K-1) information bits.
    """
    m = mcs(mcs_idx, phy)
    per_block = int(getattr(phy, "syms_per_block", SYMS_PER_BLOCK))
    if m.kind == "mfsk":
        # 16-FSK: 4 bits per symbol, one tone per symbol, and no data carriers to
        # multiply by.  block_mult stretches the codeword in symbols, exactly as
        # it does for the long-codeword QPSK rungs.
        n0 = per_block * int(m.tone_bits) * max(1, m.block_mult)
    else:
        n0 = per_block * phy.n_data * m.bps * max(1, m.block_mult)
        if m.uncoded:
            # Every transmitted bit is an information bit; the 16 CRC bits live
            # inside k, so the payload per block is k - 16 as for every other rung.
            k = n0 - n0 % 8
            return k * max(1, m.rep), k
    n = n0 * max(1, m.rep)
    if m.codec.startswith("conv"):
        k = n0 // 2 - (int(m.codec[4:]) - 1)
    else:
        k = int(round(m.rate * n0))
    k = max(32, min(k, n0 - 16))
    k -= k % 8  # keep payload chunks byte-aligned for the link layer
    return n, k


def chunk_bits(phy, mcs_idx: int) -> int:
    """Payload bits carried by one block (CRC-16 is inside the info bits)."""
    _, k = block_sizes(phy, mcs_idx)
    return k - 16


def payload_capacity(phy, mcs_idx: int, blocks: int) -> int:
    return chunk_bits(phy, mcs_idx) * blocks


def info_rate_bps(phy, mcs_idx: int) -> float:
    """Raw payload bits per second this rung carries, no ARQ overhead.

    16-FSK carries ``tone_bits`` bits per symbol and one tone per symbol; a QAM
    payload carries one constellation point per data carrier.  A convolutional
    rung's rate is fixed at 1/2 minus the tail, so it is read back from the block
    geometry rather than from the nominal ``rate`` label.
    """
    m = mcs(mcs_idx, phy)
    per_symbol = (
        int(m.tone_bits) if m.kind == "mfsk" else phy.n_data * m.bps
    )
    if m.codec.startswith("conv"):
        n, k = block_sizes(phy, mcs_idx)
        return k * per_symbol / (n * phy.sym_time)
    return per_symbol * m.rate / (phy.sym_time * max(1, m.rep))


def in_band_snr(snr_db: float, phy) -> float:
    """Channel S/N in the 2.75 kHz reference band -> this geometry's own band."""
    bw = float(getattr(phy, "bandwidth_hz", 0.0) or 0.0)
    if bw <= 0.0:
        return float(snr_db)
    return float(snr_db) + 10.0 * float(np.log10(REFERENCE_BW_HZ / bw))


def _highest_rate(candidates, phy) -> int | None:
    """Pick the candidate rung that carries the most bits per second.

    Table position is *not* rate order: the compact ladder's ``16FSK 1/2 V9``
    rung sits at the end because MCS indices travel in the frame header, and it
    is slower than the BPSK rung before it.  Selecting by index picked the 41 bps
    rung over the 78 bps one whenever both qualified.
    """
    best = None
    best_rate = -1.0
    for i in candidates:
        rate = info_rate_bps(phy, i)
        if rate > best_rate:
            best, best_rate = i, rate
    return best


def best_mcs(snr_db: float, margin_db: float = 1.0, phy=None) -> int:
    """Highest-rate MCS whose measured threshold sits below the reported SNR.

    ``snr_db`` is what the receiver's pilot estimator reports, which is the axis
    of ``MCS.snr_min``.

    Repetition rungs are skipped: their thresholds are measured as *reported*
    S/N, the estimator reads high at low S/N, so targeting one from an estimate
    would buy a slower mode for no reason.  They are a floor the link steps onto
    after failures, so the fallback here is the slowest ordinary mode.

    Long-codeword rungs (``block_mult``) are *not* skipped.  They are ordinary
    estimator targets, and every rung of the narrow ladders is one -- skipping
    them left the fallback list empty and rate control collapsed to rung 0 on
    every narrow profile, including at S/N far above every threshold.
    """
    table = table_for(phy)
    candidates = [i for i, m in enumerate(table) if m.rep == 1 and snr_db >= m.snr_min + margin_db]
    best = _highest_rate(candidates, phy)
    if best is not None:
        return best
    # Fall back to the slowest ordinary mode, never onto a repetition rung.  A
    # profile may have none (every rung repeated), in which case its slowest rung
    # is the answer.  Where the ladder has short-block rungs they stay the floor:
    # that is the choice the wide ladder's goodput tables were measured with.  The
    # narrow ladders are built entirely from long codewords, so there the floor is
    # the slowest non-repetition rung -- their rung 0 is two copies of a
    # 12x-stretched codeword, twice the airtime of the next rung for the same
    # bytes, so being handed it by a low estimate cost 334 s of a 378 s transfer.
    ordinary = [i for i, m in enumerate(table) if m.rep == 1]
    plain = [i for i in ordinary if table[i].block_mult == 1]
    if plain:
        return plain[0]
    if ordinary:
        return min(ordinary, key=lambda i: info_rate_bps(phy, i))
    return 0


def best_mcs_channel(snr_db: float, margin_db: float = 3.0, phy=None) -> int:
    """Highest-rate MCS whose measured *channel* threshold this S/N clears.

    The same selection rule as :func:`best_mcs`, on the other measured axis: this
    one takes a channel S/N in the 2.75 kHz reference band and compares it with
    each rung's in-band threshold, which is the axis the narrow ladders were
    measured on.  The host uses this axis because it knows the simulated channel
    S/N before a frame has been sent.
    """
    table = table_for(phy)
    in_band = in_band_snr(snr_db, phy)
    candidates = [
        i for i, m in enumerate(table) if in_band - float(margin_db) >= m.snr_channel
    ]
    best = _highest_rate(candidates, phy)
    return 0 if best is None else best


def pack_header(**fields) -> np.ndarray:
    out = []
    for name, width in HEADER_FIELDS:
        out.append(int_to_bits(int(fields.get(name, 0)), width))
    return np.concatenate(out)


def unpack_header(bits: np.ndarray) -> dict[str, int]:
    bits = np.asarray(bits, dtype=np.uint8)
    out: dict[str, int] = {}
    pos = 0
    for name, width in HEADER_FIELDS:
        v = 0
        for b in bits[pos : pos + width]:
            v = (v << 1) | int(b)
        out[name] = v
        pos += width
    return out


def header_info(**fields) -> np.ndarray:
    """Header info bits plus their CRC-16."""
    body = pack_header(**fields)
    return np.concatenate([body, crc16(body)])
