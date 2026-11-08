"""CAT control: the rigctld protocol, plus a real session on Hamlib's Dummy rig."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from halcyon.cat import RigError, Rigctld, RigService, hamlib_version, list_rigs


# --------------------------------------------------------------- fake rigctld
class FakeRig(threading.Thread):
    """A rigctld that answers the handful of commands this client sends."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got: list[str] = []
        self.freq = 7_074_000
        self.mode = "USB"
        self.passband = 2400
        self.ptt = 0
        self.reject = None
        self.silent: str | None = None  # a command prefix to swallow, for timeouts
        self._stop = threading.Event()

    def run(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(1.0)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(1024)
            except (socket.timeout, OSError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                self.got.append(cmd)
                self._answer(conn, cmd)
        conn.close()

    def _answer(self, conn, cmd: str) -> None:
        if self.silent and cmd.startswith(self.silent):
            return
        if self.reject and cmd.startswith(self.reject):
            conn.sendall(b"RPRT -11\n")
            return
        if cmd.startswith("F "):
            self.freq = int(cmd.split()[1])
            conn.sendall(b"RPRT 0\n")
        elif cmd == "f":
            conn.sendall(f"{self.freq}\n".encode())
        elif cmd.startswith("M "):
            parts = cmd.split()
            self.mode = parts[1]
            self.passband = int(parts[2]) if len(parts) > 2 else 0
            conn.sendall(b"RPRT 0\n")
        elif cmd == "m":
            conn.sendall(f"{self.mode}\n{self.passband}\n".encode())
        elif cmd.startswith("T "):
            self.ptt = int(cmd.split()[1])
            conn.sendall(b"RPRT 0\n")
        elif cmd == "t":
            conn.sendall(f"{self.ptt}\n".encode())
        elif cmd.startswith("l "):
            conn.sendall(b"-73.0\n")
        elif cmd.startswith("V "):
            conn.sendall(b"RPRT 0\n")
        elif cmd.startswith("\\chk_vfo"):
            conn.sendall(b"CHKVFO 0\n")
        elif cmd.startswith("\\dump_state"):
            conn.sendall(b"1\n0\nRPRT 0\n")
        else:
            conn.sendall(b"RPRT -1\n")

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def fake() -> FakeRig:
    srv = FakeRig()
    srv.start()
    yield srv
    srv.stop()


# --------------------------------------------------------------------- client
def test_frequency_is_written_and_read_back(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        rig.set_freq(14_074_000)
        assert "F 14074000" in fake.got
        assert rig.get_freq() == 14_074_000
        assert rig.freq_hz == 14_074_000
    finally:
        rig.close()


def test_mode_needs_two_reply_lines(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        rig.set_mode("USB", 2400)
        assert "M USB 2400" in fake.got
        mode, passband = rig.get_mode()
        assert (mode, passband) == ("USB", 2400)
    finally:
        rig.close()


def test_ptt_is_a_rig_command_not_a_modem_one(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        rig.set_ptt(True)
        assert "T 1" in fake.got
        assert rig.get_ptt() is True
        rig.set_ptt(False)
        assert rig.get_ptt() is False
    finally:
        rig.close()


def test_a_rejected_command_is_an_error_with_the_code(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        fake.reject = "F"
        with pytest.raises(RigError) as exc:
            rig.set_freq(1_000_000)
        assert exc.value.code == -11
    finally:
        rig.close()


def test_raw_reaches_commands_the_panel_does_not_surface(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        assert rig.raw("l STRENGTH") == ["-73.0"]
        assert "l STRENGTH" in fake.got
    finally:
        rig.close()


def test_raw_refuses_to_smuggle_a_second_command(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        with pytest.raises(RigError, match="one rigctld command per line"):
            rig.raw("T 1\nT 0")
    finally:
        rig.close()


def test_status_reports_every_control_it_can_read(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        st = rig.status()
        assert st["connected"] is True
        assert st["freq_hz"] == 7_074_000
        assert st["mode"] == "USB"
        assert st["passband"] == 2400
        assert st["strength"] == -73.0
        assert st["ptt"] is False
        assert st["error"] == ""
    finally:
        rig.close()


def test_no_session_reports_disconnected_rather_than_lying():
    assert Rigctld(port=9).status() == {"connected": False}


def test_connecting_to_nothing_raises_with_an_actionable_message():
    with pytest.raises(RigError, match="could not reach rigctld"):
        Rigctld(port=9, timeout=0.2).open()


class ResettingRig(threading.Thread):
    """Resets the first connection, the way a rigctld still starting up can."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.connections = 0

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            if self.connections == 1:
                try:
                    conn.recv(64)  # take the probe, then abort the stream
                    conn.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                except OSError:
                    pass
                conn.close()
                continue
            self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buf = b""
        while True:
            try:
                chunk = conn.recv(1024)
            except (socket.timeout, OSError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                if cmd == "\\chk_vfo":
                    conn.sendall(b"CHKVFO 0\n")
                elif cmd == "f":
                    conn.sendall(b"7074000\n")
                else:
                    conn.sendall(b"RPRT 0\n")
        conn.close()

    def stop(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_a_reset_probe_reconnects_instead_of_failing_the_open():
    """A rigctld that hangs up on the probe must not fail the whole connect."""
    srv = ResettingRig()
    srv.start()
    try:
        rig = Rigctld(port=srv.port, timeout=2.0).open()
        try:
            assert rig.connected is True
            assert rig.vfo_mode == ""
            assert rig.get_freq() == 7_074_000
        finally:
            rig.close()
        assert srv.connections >= 2
    finally:
        srv.stop()


class ChattyRig(FakeRig):
    """Leaves a dump unfinished, then answers the next command normally.

    A dump that ends on silence can stop between lines, so whatever arrived
    after the last newline is still buffered when the dump returns.  If it were
    kept, the next answer would start with it: a frequency of 7_074_000 would
    read back as 37_074_000.
    """

    def _answer(self, conn, cmd: str) -> None:
        if cmd.startswith("\\dump_state"):
            conn.sendall(b"1\n0\n3")  # the last line never gets its newline
        elif cmd == "f":
            conn.sendall(b"7074000\n")
        else:
            super()._answer(conn, cmd)


def test_a_half_read_dump_is_not_answered_as_the_next_command():
    srv = ChattyRig()
    srv.start()
    try:
        rig = Rigctld(port=srv.port, timeout=0.3).open()
        try:
            assert rig.dump_state() == ["1", "0"]
            assert rig.get_freq() == 7_074_000
            mode, passband = rig.get_mode()
            assert (mode, passband) == ("USB", 2400)
        finally:
            rig.close()
    finally:
        srv.stop()


def test_service_without_a_session_is_disconnected():
    svc = RigService()
    st = svc.status()
    assert st["connected"] is False
    assert st["rigctld"] is False


# ------------------------------------------------------------------ rig models
def test_rig_models_come_from_rigctl():
    if "not installed" in hamlib_version():
        pytest.skip("hamlib is not installed")
    models = list_rigs("dummy")
    assert models, "the Dummy rig should always be listed"
    assert any(m["model"] == 1 for m in models)
    assert all({"model", "mfg", "name"} <= set(m) for m in models)


# ------------------------------------------------------- real hamlib round trip
def test_full_cat_session_on_hamlibs_dummy_rig():
    """rigctld -m 1 is Hamlib's in-memory rig: real protocol, no hardware."""
    import shutil

    if not shutil.which("rigctld"):
        pytest.skip("rigctld is not installed")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    svc = RigService()
    try:
        st = svc.connect(port=port, model=1, timeout=3.0)
        assert st["connected"], st
        assert st["rigctld"] is True and st["pid"]

        svc.rig.set_freq(14_074_000)
        assert svc.rig.get_freq() == 14_074_000
        svc.rig.set_mode("USB", 2400)
        mode, passband = svc.rig.get_mode()
        assert mode == "USB" and passband in (0, 2400)

        svc.rig.set_ptt(True)
        assert svc.rig.get_ptt() is True
        svc.rig.set_ptt(False)
        assert svc.rig.get_ptt() is False

        shown = svc.status()
        assert shown["freq_hz"] == 14_074_000
        assert shown["mode"] == "USB"
    finally:
        svc.stop()
    assert svc.rig is None and svc.proc is None


# ------------------------------------------------------------------ console api
def test_console_guards_cat_calls_without_a_session():
    from halcyon.ui import _cat_command, _cat_set, cat_status

    assert cat_status()["connected"] is False
    with pytest.raises(ValueError, match="no CAT session"):
        _cat_set({"ptt": True})
    with pytest.raises(ValueError, match="no CAT session"):
        _cat_command({"line": "T 1"})


def test_the_capability_dump_leaves_no_reply_behind(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        assert rig.dump_state() == ["1", "0"]
        assert rig.get_freq() == 7_074_000
        assert rig.get_mode() == ("USB", 2400)
    finally:
        rig.close()


def test_raw_reads_a_multi_line_reply_to_its_end(fake):
    rig = Rigctld(port=fake.port).open()
    try:
        assert rig.raw("\\dump_state") == ["1", "0"]
        assert rig.get_freq() == 7_074_000
    finally:
        rig.close()


def test_a_command_that_never_answers_closes_the_session(fake):
    rig = Rigctld(port=fake.port, timeout=0.3).open()
    try:
        fake.silent = "f"
        with pytest.raises(RigError, match="did not answer"):
            rig.get_freq()
        assert not rig.connected, "a half-read reply must not be reused"
    finally:
        rig.close()


class SlowRig(threading.Thread):
    """A rigctld that accepts one connection and hangs up before answering.

    That is the startup race seen with a real rigctld: the port is open a moment
    before the daemon is ready, so the first command gets no reply.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.freq = 7_074_000
        self._stop = threading.Event()

    def run(self) -> None:
        try:
            first, _ = self.sock.accept()
        except OSError:
            return
        try:
            first.recv(64)  # read a command, answer nothing, hang up
        except OSError:
            pass
        first.close()
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(0.5)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(1024)
            except (socket.timeout, OSError):
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode().strip()
                if cmd.startswith("\\chk_vfo"):
                    conn.sendall(b"CHKVFO 0\n")
                elif cmd == "f":
                    conn.sendall(f"{self.freq}\n".encode())
                else:
                    conn.sendall(b"RPRT 0\n")
        conn.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def test_a_rigctld_that_drops_the_probe_leaves_a_usable_session():
    srv = SlowRig()
    srv.start()
    try:
        rig = Rigctld(port=srv.port, timeout=0.5).open()
        try:
            assert rig.connected, "the optional probe must not cost the session"
            assert rig.get_freq() == 7_074_000
        finally:
            rig.close()
    finally:
        srv.stop()
