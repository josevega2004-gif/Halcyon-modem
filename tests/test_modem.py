import numpy as np

from halcyon.channel import make_channel
from halcyon.frame import MCS_TABLE, block_sizes, chunk_bits
from halcyon.modem import Modem
from halcyon.ofdm import Phy

PHY = Phy()
MODEM = Modem(PHY)


def fast_channel(tx, snr_db=40.0, cfo=0.0, seed=1):
    return make_channel("awgn", snr_db, seed=seed, cfo_hz=cfo)(tx)


def test_phy_geometry():
    assert PHY.n_data == 87
    assert PHY.n_pilots == 30
    assert abs(PHY.sym_time - 576 / 12000) < 1e-12
    # 2.8 kHz is the FCC limit on 160-10 m; stay just inside it.
    assert 2700 < PHY.bandwidth_hz < 2800


def test_loopback_every_mcs():
    rng = np.random.default_rng(0)
    flat = rng.integers(0, 256, 400, dtype=np.uint8).tobytes()
    for idx in range(len(MCS_TABLE)):
        cap_bytes = chunk_bits(PHY, idx) // 8
        data = flat[: min(cap_bytes, 200)]
        tx, nb = MODEM.build(data, idx, seq=7)
        res = MODEM.receive(fast_channel(tx))
        assert res.header_ok, f"header failed at mcs {idx}"
        assert res.seq == 7
        assert np.all(res.blocks_ok), f"payload failed at mcs {idx}"
        assert res.payload == data, f"payload mismatch at mcs {idx}"


def test_acquires_carrier_offset():
    data = bytes(range(60))
    tx, _ = MODEM.build(data, 2, seq=1)
    for cfo in (-61.0, -12.0, 0.0, 33.0, 74.0):
        res = MODEM.receive(fast_channel(tx, snr_db=25.0, cfo=cfo))
        assert res.sync_ok
        assert abs(res.cfo_hz - cfo) < 6.0, (cfo, res.cfo_hz)
        assert res.header_ok and res.payload == data


def test_survives_timing_offset():
    data = b"timing"
    tx, _ = MODEM.build(data, 3, seq=2)
    shifted = np.concatenate([np.zeros(37, dtype=complex), tx, np.zeros(11, dtype=complex)])
    res = MODEM.receive(fast_channel(shifted, snr_db=30.0))
    assert res.header_ok and res.payload == data


def test_ack_roundtrip():
    ok = np.array([True, False, True, True, False, True])
    ack = MODEM.build_ack(seq=42, blocks_ok=ok, snr_db=7.4, mcs_sugg=5)
    res = MODEM.receive(fast_channel(ack, snr_db=20.0))
    assert res.header_ok and res.ftype == 1 and res.seq == 42
    assert res.blocks == 6
    assert res.ack_bitmap == 0b101101
    assert res.mcs_sugg == 5
    assert abs(res.snr_reported - 7.4) < 1.1
    assert res.snr_db > 15.0  # measured, ~20 dB channel


def test_garbage_is_rejected():
    rng = np.random.default_rng(5)
    noise = rng.standard_normal(9000) + 1j * rng.standard_normal(9000)
    res = MODEM.receive(noise * 0.1)
    assert not res.header_ok


def test_narrow_profile_loopback():
    """The high-noise profile must frame, sync and decode on its own ladder."""
    from halcyon.frame import ROBUST_TABLE
    from halcyon.ofdm import ROBUST

    phy = Phy(**ROBUST)
    modem = Modem(phy)
    assert phy.n_data == 18
    assert 500 < phy.bandwidth_hz < 600
    assert phy.header_syms == 10
    for idx in range(len(ROBUST_TABLE)):
        cap = chunk_bits(phy, idx) // 8
        data = bytes(range(min(2 * cap, 24)))
        tx, _ = modem.build(data, idx, seq=5)
        res = modem.receive(fast_channel(tx))
        assert res.header_ok, f"narrow header failed at mode {idx}"
        assert res.payload == data, f"narrow payload failed at mode {idx}"


def test_nano_234_hz_loopback():
    """The 250 Hz-class geometry must frame and decode its 8-FSK payload."""
    from halcyon.frame import NANO_TABLE
    from halcyon.ofdm import NANO

    phy = Phy(**NANO)
    modem = Modem(phy)
    assert 200.0 < phy.bandwidth_hz < 250.0
    data = bytes(range(min(chunk_bits(phy, 1) // 8, 24)))
    tx, _ = modem.build(data, 1, seq=12)
    res = modem.receive(fast_channel(tx, snr_db=40.0, seed=7))
    assert res.header_ok
    assert res.seq == 12
    assert res.payload == data
    assert len(NANO_TABLE) == 2


def test_selected_micro_and_mini_loopback():
    """The two selected sweep geometries must frame, sync and decode."""
    from halcyon.frame import MICRO_TABLE, MINI_TABLE
    from halcyon.ofdm import MICRO, MINI

    for cfg, table, seq in ((MICRO, MICRO_TABLE, 13), (MINI, MINI_TABLE, 14)):
        phy = Phy(**cfg)
        modem = Modem(phy)
        assert phy.bandwidth_hz in (175.0, 225.0)
        data = bytes(range(min(chunk_bits(phy, 0) // 8, 24)))
        tx, _ = modem.build(data, 0, seq=seq)
        res = modem.receive(fast_channel(tx, snr_db=40.0, seed=8))
        assert res.header_ok
        assert res.seq == seq
        assert res.payload == data
        assert len(table) == 1


def test_deep_375_hz_loopback():
    """The 375 Hz test geometry must frame and decode its 16-FSK payload."""
    from halcyon.frame import DEEP_TABLE
    from halcyon.ofdm import DEEP

    phy = Phy(**DEEP)
    modem = Modem(phy)
    assert phy.bandwidth_hz == 375.0
    data = bytes(range(min(chunk_bits(phy, 1) // 8, 24)))
    tx, _ = modem.build(data, 1, seq=11)
    res = modem.receive(fast_channel(tx, snr_db=40.0, seed=6))
    assert res.header_ok
    assert res.seq == 11
    assert res.payload == data
    assert len(DEEP_TABLE) == 3


def test_compact_convolutional_rung_loopback():
    """The Viterbi rung must frame, sync and decode through the real chain."""
    from halcyon.frame import COMPACT_TABLE, block_sizes, chunk_bits
    from halcyon.ofdm import COMPACT

    phy = Phy(**COMPACT)
    modem = Modem(phy)
    idx = next(i for i, m in enumerate(COMPACT_TABLE) if m.codec == "conv9")
    n, k = block_sizes(phy, idx)
    assert n == 576
    assert k == 280
    assert chunk_bits(phy, idx) == k - 16
    data = bytes(range(chunk_bits(phy, idx) // 8))
    tx, blocks = modem.build(data, idx, seq=9)
    res = modem.receive(fast_channel(tx, snr_db=40.0, seed=4))
    assert res.header_ok
    assert res.seq == 9
    assert blocks == 1 and res.blocks_ok.all()
    assert res.payload == data


def test_narrow_profile_geometry_and_rate():
    """Narrow modes must actually be slower and narrower than the wide ones."""
    from halcyon.frame import MCS_TABLE, ROBUST_TABLE, chunk_bits
    from halcyon.ofdm import ROBUST

    narrow = Phy(**ROBUST)
    assert narrow.bandwidth_hz < PHY.bandwidth_hz / 4
    # Slowest narrow mode still carries a usable payload per block.
    assert chunk_bits(narrow, 0) // 8 >= 8
    assert ROBUST_TABLE[0].rate <= 1 / 3 + 1e-9
    # Narrower must mean slower: even the fastest narrow mode is far below the
    # wide profile's slowest.
    from halcyon.bench import info_rate_bps

    # Compare with the wide profile's slowest mode at equal coding effort.  The
    # repeated wide modes below it are a different trade: they spend the full band
    # and 8x the airtime to buy sensitivity, which the narrow profile does not do.
    widest_slow = min(
        info_rate_bps(PHY, i)
        for i, m in enumerate(MCS_TABLE)
        if m.rep == 1 and m.block_mult == 1
    )
    assert info_rate_bps(narrow, len(ROBUST_TABLE) - 1) < widest_slow


def test_channel_estimator_cost():
    """Pilot interpolation should stay within 1 dB of perfect channel knowledge.

    Linear interpolation from raw pilots costs about 2 dB of EVM; the 3-tap
    frequency and time average in ``Phy._smooth_pilots`` should hold it near
    0.5 dB at this S/N.  Without the smoothing this test reads ~2.1 dB.
    """
    rng = np.random.default_rng(3)
    qpsk = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j]) / np.sqrt(2)
    syms = rng.choice(qpsk, size=(120, PHY.n_data))
    tx = PHY.modulate(syms)
    noise_var = 10 ** (-15 / 10)  # 15 dB per-carrier S/N
    noise = (rng.standard_normal(tx.size) + 1j * rng.standard_normal(tx.size)) * np.sqrt(
        noise_var / 2
    )
    X, _, _ = PHY.demodulate(tx + noise, syms.shape[0])
    excess = 10 * np.log10(float(np.mean(np.abs(X - syms) ** 2)) / noise_var)
    assert excess < 1.0, f"pilot interpolation costs {excess:.2f} dB"


def test_mcs_geometry():
    for idx, m in enumerate(MCS_TABLE):
        n, k = block_sizes(PHY, idx)
        assert n % (PHY.n_data * m.bps) == 0
        assert k % 8 == 0 and 0 < k < n


def test_soft_combining_sums_prior_llrs():
    """receive(prior=...) must expose and accept the per-block LLRs."""
    import numpy as np

    from halcyon.bench import burst_channel
    from halcyon.frame import chunk_bits
    from halcyon.modem import Modem
    from halcyon.ofdm import COMPACT, Phy

    phy = Phy(**COMPACT)
    modem = Modem(phy)
    data = bytes(range(chunk_bits(phy, 0) // 8))
    tx, blocks = modem.build(data, 0, seq=1)
    rx = burst_channel("awgn", 6.0, seed=3, bandwidth_hz=phy.bandwidth_hz)(tx)

    first = modem.receive(rx)
    assert first.block_llrs is not None and len(first.block_llrs) == blocks

    # Adding zero-mean prior LLRs must not change the decode.
    zeros = [np.zeros_like(x) for x in first.block_llrs]
    again = modem.receive(rx, prior=zeros)
    assert again.blocks_ok.all()
    assert again.payload == data


def test_a_header_claiming_more_blocks_than_the_format_allows_is_refused(monkeypatch):
    """`blocks` is a 6-bit field, so a sender could claim 63; the format allows
    12, and decoding against a claim no sender may make is work for nothing."""
    from halcyon import modem as modem_module
    from halcyon.frame import MAX_BLOCKS
    from halcyon.modem import Modem
    from halcyon.ofdm import Phy

    m = Modem(Phy())
    tx, _ = m.build(b"cq de halcyon", 4, seq=1)
    real = modem_module.unpack_header
    monkeypatch.setattr(
        modem_module,
        "unpack_header",
        lambda bits: {**real(bits), "blocks": MAX_BLOCKS + 1},
    )
    assert not m.receive(tx).header_ok
