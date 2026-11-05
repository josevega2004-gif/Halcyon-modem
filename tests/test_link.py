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
