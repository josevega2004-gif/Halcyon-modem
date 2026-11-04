"""The host-mode protocol PAT and VarAC speak: commands, events, and the data pipe."""

from __future__ import annotations

import socket
import time

import pytest

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
