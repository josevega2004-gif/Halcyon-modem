"""The host-mode protocol PAT and VarAC speak: commands, events, and the data pipe."""

from __future__ import annotations

import socket
import time

import pytest

from halcyon.frame import in_band_snr, table_for
from halcyon.host import HostProtocol, HostServer, geometry_for, pick_mcs


def make(snr: float = 20.0) -> tuple[HostProtocol, list[str]]:
    events: list[str] = []
    return HostProtocol(mycall="N0CALL", sink=events.append, snr=snr), events


# ------------------------------------------------------------------ commands
def test_mycall_and_aliases():
    p, ev = make()
    p.handle("MYCALL k1abc K1ABC-T")
    assert p.state.mycall == "K1ABC"
    assert p.state.aliases == ["K1ABC-T"]
    assert ev == []


def test_bandwidth_selects_a_geometry_and_rebuilds_the_modem():
    p, _ = make()
    p.handle("BW2750")
    assert p.state.bandwidth == 2750
    assert p.modem().phy.bandwidth_hz > 2000
    p.handle("BW500")
    assert p.state.bandwidth == 500
    assert p.modem().phy.bandwidth_hz < 1000, "the 500 Hz setting must be a narrow geometry"
    assert p.modem().phy.profile == "compact", "500 Hz is the low-speed 492 Hz ladder"


def test_500_hz_at_low_snr_switches_to_deep_and_carries():
    p, _ = make(snr=-8.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.handle("BW500")
    assert p.modem().phy.profile == "deep"
    data = b"low snr narrow path"
    assert p.carry(data) == data


def test_low_snr_uses_375_hz_even_when_the_client_asked_for_wide():
    p, _ = make(snr=-8.0)
    p.handle("BW2750")
    assert p.modem().phy.profile == "deep"
    assert p.modem().phy.bandwidth_hz < 400.0


def test_very_low_snr_uses_234_hz():
    p, _ = make(snr=-12.0)
    p.handle("BW500")
    assert p.modem().phy.profile == "nano"
    assert 200.0 < p.modem().phy.bandwidth_hz < 250.0


def test_below_nano_the_host_has_no_slower_fallback():
    for snr in (-15.5, -16.5):
        p, _ = make(snr=snr)
        p.handle("BW500")
        assert p.modem().phy.profile == "nano"
        assert 200.0 < p.modem().phy.bandwidth_hz < 250.0


def test_500_hz_is_a_session_ceiling_against_wider_commands():
    p = HostProtocol(bandwidth=500, sink=[].append, snr=10.0)
    for command in ("BW2300", "BW2750", "CONNECT 2300 K1ABC W7ABC"):
        p.handle(command)
        assert p.state.bandwidth == 500, command
        assert p.status()["max_bandwidth"] == 500
    assert p.modem().phy.bandwidth_hz <= 500.0


def test_500_hz_ceiling_still_uses_lower_bandwidth_modes():
    for snr, profile in ((-8.0, "deep"), (-12.0, "nano")):
        p = HostProtocol(bandwidth=500, sink=[].append, snr=snr)
        p.handle("BW2300")
        assert p.state.bandwidth == 500
        assert p.modem().phy.profile == profile
        assert p.modem().phy.bandwidth_hz < 500.0


def test_500_hz_never_selects_the_wide_profile():
    for snr in (20.0, 0.0, -4.0, -8.0, -12.0, -16.0):
        p = HostProtocol(bandwidth=500, sink=[].append, snr=snr)
        p.handle("BW2750")
        phy = p.modem().phy
        assert phy.bandwidth_hz <= 500.0, (snr, phy.profile)
        assert phy.profile in ("compact", "deep", "nano")


def test_status_exposes_the_active_profile_and_occupied_bandwidth():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    st = p.status()
    assert st["bandwidth"] == 500, "the selector is still what the client asked for"
    assert st["max_bandwidth"] == 500
    assert st["profile"] == "compact"
    assert 480.0 < st["occupied_hz"] < 500.0
    p.snr = -8.0
    st = p.status()
    assert st["bandwidth"] == 500, "the geometry must not rewrite the selector"
    assert st["profile"] == "deep"
    assert 300.0 < st["occupied_hz"] < 400.0
    assert not any("fallback" in m for _, m in p.log), p.log


def test_status_exposes_the_selected_mcs_and_its_margin():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    st = p.status()
    _, phy = geometry_for(st["bandwidth"], p.snr)
    idx = pick_mcs(phy, p.snr)
    assert st["mcs"] == idx
    assert st["mcs_label"] == table_for(phy)[idx].label
    expected = in_band_snr(p.snr, phy) - table_for(phy)[idx].snr_channel
    assert st["mcs_margin_db"] == pytest.approx(round(expected, 1))
    assert st["mcs_margin_db"] >= 3.0, "rate control keeps 3 dB at this S/N"
    p.snr = -8.0
    st = p.status()
    assert st["profile"] == "deep"
    assert st["mcs"] == pick_mcs(geometry_for(500, p.snr)[1], p.snr)
    assert st["mcs_margin_db"] >= 3.0


def test_status_flags_a_margin_below_the_target():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    assert p.status()["mcs_margin_low"] is False
    p.snr = -8.0
    st = p.status()
    assert st["mcs_margin_db"] >= 3.0
    assert st["mcs_margin_low"] is False
    p.snr = -13.0
    st = p.status()
    assert st["profile"] == "nano"
    assert st["mcs_margin_db"] < 3.0, "the nano ladder has no rung that clears 3 dB"
    assert st["mcs_margin_low"] is True


def test_low_margin_is_logged_once_while_it_stays_low():
    p, _ = make(snr=20.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.handle("BW500")
    p.carry(b"ok")
    assert not any("low margin" in m for _, m in p.log)

    p.snr = -13.0
    p.carry(b"x")
    notes = [m for _, m in p.log if "low margin" in m]
    assert len(notes) == 1, notes
    assert "L0 8FSK 1/4" in notes[0], notes[0]
    assert "-13.0 dB" in notes[0], notes[0]
    p.carry(b"y")
    assert len([m for _, m in p.log if "low margin" in m]) == 1, "one line per episode"

    p.handle("DISCONNECT")
    p.handle("CONNECT K1ABC W7ABC")
    p.carry(b"z")
    assert len([m for _, m in p.log if "low margin" in m]) == 2, "a new link warns again"


def test_margin_recovery_is_logged_and_a_second_drop_warns_again():
    p, _ = make(snr=20.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.handle("BW500")
    p.snr = -13.0
    p.carry(b"x")
    assert sum("low margin" in m for _, m in p.log) == 1

    p.snr = 20.0
    p.carry(b"ok")
    recovered = [m for _, m in p.log if "margin recovered" in m]
    assert len(recovered) == 1, recovered
    assert "L4 BPSK 1/4 L2" in recovered[0], recovered[0]
    assert "+20.0 dB" in recovered[0], recovered[0]
    assert "+27.0 dB" in recovered[0], recovered[0]
    assert ", after " in recovered[0], recovered[0]
    assert recovered[0].endswith(" s)"), recovered[0]
    seconds = float(recovered[0].split(", after ", 1)[1].split(" s)", 1)[0])
    assert seconds >= 0.0
    assert ", after " not in [m for _, m in p.log if "low margin" in m][0]

    p.snr = -13.0
    p.carry(b"y")
    assert sum("low margin" in m for _, m in p.log) == 2, "a second episode warns"
    assert sum("margin recovered" in m for _, m in p.log) == 1


def test_status_reports_the_running_low_margin_duration():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    assert p.status()["mcs_margin_low_s"] is None
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.snr = -13.0
    p.carry(b"x")
    st = p.status()
    assert st["mcs_margin_low"] is True
    first = st["mcs_margin_low_s"]
    assert first is not None and first >= 0.0
    time.sleep(0.05)
    second = p.status()["mcs_margin_low_s"]
    assert second is not None and second >= first

    p.snr = 20.0
    p.carry(b"ok")
    st = p.status()
    assert st["mcs_margin_low"] is False
    assert st["mcs_margin_low_s"] is None, "the duration ends with the episode"


def test_status_exposes_the_next_rung_and_its_switch_snr():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    st = p.status()
    assert st["next_mcs"] is None, "the top compact rung has nothing above it"
    assert st["next_mcs_label"] is None and st["next_mcs_snr_db"] is None

    p.snr = -8.0
    st = p.status()
    _, phy = geometry_for(500, p.snr)
    assert st["mcs"] == pick_mcs(phy, p.snr)
    assert st["next_mcs"] is not None
    assert st["next_mcs_label"] == table_for(phy)[st["next_mcs"]].label
    need = st["next_mcs_snr_db"]
    assert need > p.snr, "a future switch must need a better channel"
    # ``need`` is rounded to 0.1 dB, so probe clear of the rounding error.
    assert pick_mcs(phy, need + 0.1) == st["next_mcs"]
    assert pick_mcs(phy, need - 0.1) == st["mcs"], "below the boundary nothing moved"


def test_fallback_from_compact_to_a_narrower_profile_is_logged_once():
    for snr, profile in ((-8.0, "deep"), (-13.0, "nano")):
        p, _ = make(snr=20.0)
        p.handle("MYCALL K1ABC")
        p.handle("CONNECT K1ABC W7ABC")
        p.handle("BW500")
        assert p.modem().phy.profile == "compact"
        p.snr = snr
        assert p.modem().phy.profile == profile
        assert p.modem().phy.profile == profile, "the second selection must not relog"
        notes = [m for _, m in p.log if "fallback" in m]
        assert len(notes) == 1, notes
        assert f"compact -> {profile}" in notes[0], notes[0]
        assert f"{snr:+.1f} dB" in notes[0], notes[0]


def test_deep_to_nano_is_logged_as_another_fallback():
    p, _ = make(snr=20.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.handle("BW500")
    assert p.modem().phy.profile == "compact"
    p.snr = -8.0
    assert p.modem().phy.profile == "deep"
    p.snr = -13.0
    assert p.modem().phy.profile == "nano"
    assert p.modem().phy.profile == "nano"
    notes = [m for _, m in p.log if "fallback" in m]
    assert len(notes) == 2, notes
    assert "compact -> deep" in notes[0] and "-8.0 dB" in notes[0], notes[0]
    assert "deep -> nano" in notes[1] and "-13.0 dB" in notes[1], notes[1]


def test_fallback_is_not_logged_before_a_link_exists():
    p, _ = make(snr=20.0)
    p.handle("BW500")
    assert p.modem().phy.profile == "compact"
    p.snr = -8.0
    assert p.modem().phy.profile == "deep"
    assert not any("fallback" in m for _, m in p.log), p.log


def test_recovery_to_a_wider_profile_is_logged_once():
    p, _ = make(snr=20.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    p.handle("BW500")
    p.snr = -8.0
    assert p.modem().phy.profile == "deep"
    p.snr = -2.0
    assert p.modem().phy.profile == "compact"
    assert p.modem().phy.profile == "compact", "the second selection must not relog"
    notes = [m for _, m in p.log if "recovery" in m]
    assert len(notes) == 1, notes
    assert "deep -> compact" in notes[0], notes[0]
    assert "-2.0 dB" in notes[0], notes[0]


def test_wide_session_recovery_from_deep_is_logged():
    p, _ = make(snr=10.0)
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    assert p.modem().phy.profile == "wide"
    p.snr = -8.0
    assert p.modem().phy.profile == "deep"
    p.snr = 4.0
    assert p.modem().phy.profile == "wide"
    notes = [m for _, m in p.log if "recovery" in m]
    assert len(notes) == 1, notes
    assert "deep -> wide" in notes[0], notes[0]
    assert "4.0 dB" in notes[0], notes[0]


def test_version_identifies_halcyon_rather_than_another_modem():
    p, ev = make()
    p.handle("VERSION")
    assert len(ev) == 1 and ev[0].startswith("VERSION ")
    assert "Halcyon" in ev[0]


def test_listen_on_off():
    p, _ = make()
    p.handle("LISTEN ON")
    assert p.state.listening
    p.handle("LISTEN OFF")
    assert not p.state.listening


def test_connect_emits_pending_then_connected_and_links():
    p, ev = make()
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT K1ABC W7ABC")
    assert p.state.linked and p.state.peer == "W7ABC"
    assert "PENDING" in ev
    assert "CONNECTED K1ABC W7ABC" in ev
    assert ev.index("PENDING") < ev.index("CONNECTED K1ABC W7ABC")


def test_connect_with_only_a_target_uses_mycall():
    p, ev = make()
    p.handle("MYCALL K1ABC")
    p.handle("CONNECT W7ABC")
    assert "CONNECTED K1ABC W7ABC" in ev


def test_abort_and_disconnect_both_unlink():
    for cmd in ("DISCONNECT", "ABORT"):
        p, ev = make()
        p.handle("CONNECT K1ABC W7ABC")
        ev.clear()
        p.handle(cmd)
        assert not p.state.linked
        assert ev == ["DISCONNECTED"]


def test_unknown_commands_are_ignored_without_events():
    p, ev = make()
    p.handle("SOMETHINGNEW 1 2 3")
    assert ev == []
    assert any("?" in m for _, m in p.log), "it should still be visible in the log"


def test_host_init_sequence_from_varac_is_accepted():
    p, _ = make()
    for line in ("BW2300", "CHAT ON", "LISTEN ON", "IGNOREKISSDCD ON",
                 "LISTEN CQ", "VERSION", "MYCALL K1ABC K1ABC-T"):
        p.handle(line)
    assert p.state.bandwidth == 2300
    assert p.state.mycall == "K1ABC"
    assert p.state.aliases == ["K1ABC-T"]


# ---------------------------------------------------------------- the link
def test_bytes_cross_the_modem_and_come_back():
    p, _ = make(snr=22.0)
    p.handle("CONNECT K1ABC W7ABC")
    back = p.carry(b"hello world")
    assert back == b"hello world"
    assert p.state.bytes_tx >= 11 and p.state.bytes_rx >= 11
    assert p.state.last_sn is not None


def test_nothing_moves_without_a_link():
    p, _ = make()
    assert p.carry(b"hello") == b""


def test_a_level_is_chosen_with_margin_for_the_channel():
    p, _ = make(snr=12.0)
    _, phy = geometry_for(2300)
    idx = pick_mcs(phy, 12.0)
    from halcyon.frame import table_for

    assert table_for(phy)[idx].snr_channel <= 9.0


# ----------------------------------------------------------------- the server
def _free_ports() -> tuple[int, int]:
    a = socket.socket()
    b = socket.socket()
    a.bind(("127.0.0.1", 0))
    b.bind(("127.0.0.1", 0))
    pa, pb = a.getsockname()[1], b.getsockname()[1]
    a.close()
    b.close()
    return pa, pb


def test_sockets_speak_the_cr_terminated_protocol():
    cp, dp = _free_ports()
    srv = HostServer(cmd_port=cp, data_port=dp, snr=22.0).start()
    try:
        cmd = socket.create_connection(("127.0.0.1", srv.cmd_port), timeout=5)
        cmd.settimeout(5)
        cmd.sendall(b"VERSION\r")
        first = cmd.recv(4096).decode()
        assert first.startswith("VERSION ") and "\r" in first

        cmd.sendall(b"MYCALL K1ABC\rCONNECT K1ABC W7ABC\r")
        time.sleep(0.2)
        cmd.sendall(b"VERSION\r")
        deadline = time.time() + 3
        seen = ""
        while time.time() < deadline and "CONNECTED" not in seen:
            seen += cmd.recv(4096).decode()
        assert "CONNECTED K1ABC W7ABC" in seen

        data = socket.create_connection(("127.0.0.1", srv.data_port), timeout=5)
        data.settimeout(15)
        data.sendall(b"ping through the modem")
        echoed = b""
        deadline = time.time() + 15
        while time.time() < deadline and len(echoed) < 22:
            echoed += data.recv(4096)
        assert echoed == b"ping through the modem"

        st = srv.status()
        assert st["running"] and st["protocol"]["linked"]
        assert st["protocol"]["bytes_rx"] >= 22
        cmd.close()
        data.close()
    finally:
        srv.stop()
    assert not srv.running


def test_data_written_before_the_link_is_sent_once_it_opens():
    cp, dp = _free_ports()
    srv = HostServer(cmd_port=cp, data_port=dp, snr=22.0).start()
    try:
        data = socket.create_connection(("127.0.0.1", srv.data_port), timeout=5)
        data.settimeout(20)
        data.sendall(b"queued before connect")
        time.sleep(0.3)  # written while unlinked: it must be held, not dropped
        cmd = socket.create_connection(("127.0.0.1", srv.cmd_port), timeout=5)
        cmd.sendall(b"MYCALL K1ABC\rCONNECT K1ABC W7ABC\r")
        echoed = b""
        deadline = time.time() + 25
        while time.time() < deadline and len(echoed) < 21:
            echoed += data.recv(4096)
        assert echoed == b"queued before connect"
        cmd.close()
        data.close()
    finally:
        srv.stop()


def test_stopping_closes_both_ports():
    cp, dp = _free_ports()
    srv = HostServer(cmd_port=cp, data_port=dp).start()
    cp_used, dp_used = srv.cmd_port, srv.data_port
    srv.stop()
    # the ports must be free again
    a = socket.socket()
    b = socket.socket()
    try:
        a.bind(("127.0.0.1", cp_used))
        b.bind(("127.0.0.1", dp_used))
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------------ console api
def test_console_reports_services_off_and_refuses_commands():
    from halcyon.ui import _services_command, services_status

    st = services_status()
    assert st["host"]["running"] is False
    with pytest.raises(ValueError, match="not running"):
        _services_command({"line": "VERSION"})
