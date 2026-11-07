import numpy as np

from halcyon.channel import make_channel
from halcyon.frame import MCS_TABLE, chunk_bits
from halcyon.link import Link, LinkConfig
from halcyon.modem import Modem
from halcyon.ofdm import COMPACT, Phy

PHY = Phy()
MODEM = Modem(PHY)


def run_link(snr_db: float, data: bytes, kind: str = "awgn", start_mcs: int = 2):
    link = Link(
        MODEM,
        LinkConfig(start_mcs=start_mcs),
        make_channel=lambda seed: make_channel(kind, snr_db, seed=int(seed)),
        seed=1,
    )
    return link.run(data)


def test_delivers_payload_at_good_snr():
    data = np.random.default_rng(0).integers(0, 256, 1200, dtype=np.uint8).tobytes()
    st = run_link(30.0, data)
    assert st.complete
    assert st.delivered_bits == st.total_bits
    assert st.goodput_bps > 1500
    assert st.mean_mcs > 5


def test_adapts_down_and_still_delivers():
    data = np.random.default_rng(1).integers(0, 256, 240, dtype=np.uint8).tobytes()
    st = run_link(8.0, data, start_mcs=9)
    assert st.complete
    assert st.mean_mcs < 9


def test_bad_mcs_start_recovers():
    data = bytes(range(120))
    st = run_link(6.0, data, start_mcs=11)
    assert st.complete
    assert st.mean_mcs < 11


def test_capacity_matches_blocks():
    """A mid-ladder block must carry a useful chunk, whatever index it sits at."""
    idx = next(i for i, m in enumerate(MCS_TABLE) if m.label == "16QAM 2/3")
    cap_bytes = chunk_bits(PHY, idx) // 8
    assert cap_bytes > 50


def test_narrow_ladder_rate_control_keeps_a_fast_rung():
    """Every compact rung is a long codeword, so this ladder is where skipping
    ``block_mult`` rungs left ``best_mcs`` with nothing to choose."""
    from halcyon.frame import best_mcs, table_for

    phy = Phy(**COMPACT)
    table = table_for(phy)
    idx = best_mcs(20.0, 3.0, phy)
    assert idx != 0
    # the highest-rate rung that qualifies, not merely the last qualifying index
    assert table[idx].label == "BPSK 1/4 L2"


def test_compact_adaptive_link_does_not_fall_to_the_slowest_rung():
    phy = Phy(**COMPACT)
    link = Link(
        Modem(phy),
        LinkConfig(start_mcs=3, max_blocks=2, max_attempts=4, probe_first=False),
        make_channel=lambda seed: make_channel(
            "awgn", 12.0, seed=int(seed), bandwidth_hz=phy.bandwidth_hz
        ),
        seed=1,
    )
    st = link.run(b"x" * 120)
    assert st.complete
    assert min(st.mcs_used) > 0, st.mcs_used


def test_compact_600_bytes_closes_at_minus_8_db():
    """Narrow 16-FSK is the low-SNR path; 2.7 kHz S/N is the reference axis."""
    phy = Phy(**COMPACT)
    data = np.random.default_rng(9102).integers(0, 256, 600, dtype=np.uint8).tobytes()
    link = Link(
        Modem(phy),
        LinkConfig(start_mcs=2, max_attempts=8, adaptive=False, probe_first=False),
        make_channel=lambda seed: make_channel(
            "awgn", -8.0, seed=int(seed), bandwidth_hz=2742.1875
        ),
        seed=0,
    )
    st = link.run(data)
    assert st.complete
    assert st.delivered_bits == 4800
    assert st.goodput_bps > 30.0


def test_best_mcs_fallback_never_lands_on_a_repetition_rung():
    """A report below every threshold must not summon the repetition floor.

    Compact's rung 0 is two copies of a 12x-stretched codeword: twice the airtime
    of rung 1 for the same payload.  Landing there on an estimate cost 334 s of a
    378 s transfer at 0 dB in-band.
    """
    from halcyon.frame import best_mcs, info_rate_bps, table_for

    phy = Phy(**COMPACT)
    table = table_for(phy)
    idx = best_mcs(-20.0, 3.0, phy)
    assert table[idx].rep == 1, table[idx].label
    ordinary = [i for i, m in enumerate(table) if m.rep == 1]
    assert idx == min(ordinary, key=lambda i: info_rate_bps(phy, i))


def test_compact_link_steps_down_one_rung_from_a_low_report():
    """Between the probe's start rung and the floor there are four rungs; a low
    estimator reading takes the next one down, not the slowest one."""
    phy = Phy(**COMPACT)
    data = np.random.default_rng(7).integers(0, 256, 600, dtype=np.uint8).tobytes()
    link = Link(
        Modem(phy),
        LinkConfig(start_mcs=4),
        make_channel=lambda seed: make_channel(
            "awgn", 0.0, seed=int(seed), bandwidth_hz=phy.bandwidth_hz
        ),
        seed=0,
    )
    st = link.run(data)
    assert st.complete
    assert st.mcs_used[0] == 4
    assert st.mcs_used[1] == 3, st.mcs_used
    assert 0 not in st.mcs_used, st.mcs_used
    assert st.goodput_bps > 35.0, st.goodput_bps


def test_margin_is_applied_on_the_reported_axis():
    """The tone estimate reports the payload's own S/N, so the non-coherent
    rungs' thresholds are channel thresholds and a 3 dB margin is a real 3 dB
    again -- no cap needed to make a two-rung ladder usable."""
    from halcyon.frame import best_mcs, table_for
    from halcyon.ofdm import NANO

    phy = Phy(**NANO)
    # Measured top-rung threshold is -2.7 dB reported, so the bar is 0.3 dB.
    # Take it from the table rather than repeating the number: a re-measurement
    # must move the boundary, not break the test.
    bar = table_for(phy)[1].snr_min + 3.0
    assert best_mcs(bar + 0.1, 3.0, phy) == 1
    assert best_mcs(bar - 0.1, 3.0, phy) == 0
    assert best_mcs(6.0, 3.0, Phy()) == 3  # unchanged on the wide ladder


def test_nano_stays_on_its_top_rung_where_it_decodes():
    from halcyon.ofdm import NANO

    phy = Phy(**NANO)
    data = np.random.default_rng(3).integers(0, 256, 200, dtype=np.uint8).tobytes()
    link = Link(
        Modem(phy),
        LinkConfig(),
        make_channel=lambda seed: make_channel(
            "awgn", -8.0, seed=int(seed), bandwidth_hz=2742.1875
        ),
        seed=0,
    )
    st = link.run(data)
    assert st.complete
    assert set(st.mcs_used) == {1}, st.mcs_used
