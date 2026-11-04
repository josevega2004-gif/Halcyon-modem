"""CAT control through Hamlib's rigctld.

Hamlib talks to roughly 320 rigs; rather than link libhamlib into this process
the way the Python bindings do, this speaks to its standard ``rigctld`` daemon
over TCP (port 4532 by default) -- which is how the rest of the ham software
world drives CAT, so an already-running rigctld just works.  It can also start
rigctld for you from a rig model, serial device and baud rate.

The protocol is line-oriented ASCII:

    F <hz>            set frequency             -> RPRT 0
    f                 get frequency             -> <hz>
    M <mode> <hz>     set mode and passband     -> RPRT 0
    m                 get mode                  -> <mode> newline <passband>
    V <vfo>           select VFO (VFOA, VFOB)   -> RPRT 0
    T <0|1>           set PTT                   -> RPRT 0
    t                 get PTT                   -> <0|1>
    l STRENGTH        read the S-meter          -> <value>
    \\chk_vfo          is VFO mode on            -> CHKVFO <0|1>
    \\dump_state       capability dump           -> many lines

A negative reply is ``RPRT -<errno>``; the client turns that into a
:class:`RigError` carrying the code, so a rejected frequency is never silently
treated as success.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4532
MODES = ("USB", "LSB", "CWU", "CWL", "AM", "FM", "NFM", "WFM", "RTTY", "DIGU", "DIGL", "PKTUSB")


class RigError(RuntimeError):
    """A rigctld rejection or transport failure."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def parse_reply(lines: list[str], command: str) -> list[str]:
    """Raise if any line is an error reply; otherwise hand back the payload."""
    for line in lines:
        if line.startswith("RPRT"):
            parts = line.split()
            code = int(parts[1]) if len(parts) > 1 else -1
            if code != 0:
                raise RigError(f"rigctld rejected {command!r} with {line}", code)
    return [l for l in lines if not l.startswith("RPRT")]


class Rigctld:
    """One rigctld session: every command is serialised on a lock."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 2.5):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: socket.socket | None = None
        self._io = None
        self._lock = threading.Lock()
        self.error = ""
        self.vfo_mode = ""
        self.freq_hz = 0
        self.mode = ""
        self.passband = 0
        self.ptt = False
        self.strength: float | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "Rigctld":
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RigError(
                f"could not reach rigctld at {self.host}:{self.port} ({exc})"
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._io = sock.makefile("rb")
        try:
            reply = self.command("\\chk_vfo")
            self.vfo_mode = reply[0] if reply else ""
        except RigError:
            self.vfo_mode = ""
        return self

    def close(self) -> None:
        io, self._io = self._io, None
        sock, self._sock = self._sock, None
        for obj in (io, sock):
            if obj is not None:
                try:
                    obj.close()
                except OSError:
                    pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- transport ---------------------------------------------------------
    def command(self, line: str, lines: int = 1) -> list[str]:
        with self._lock:
            if self._sock is None or self._io is None:
                raise RigError("not connected to rigctld")
            try:
                self._sock.sendall((line + "\n").encode("ascii"))
            except OSError as exc:
                self.close()
                raise RigError(f"send to rigctld failed: {exc}") from exc
            out: list[str] = []
            try:
                while len(out) < lines:
                    raw = self._io.readline()
                    if not raw:
                        raise RigError("rigctld closed the connection")
                    text = raw.decode("ascii", "replace").strip()
                    if text.startswith("RPRT"):
                        parts = text.split()
                        code = int(parts[1]) if len(parts) > 1 else -1
                        if code != 0:
                            raise RigError(f"rigctld rejected {line!r}: {text}", code)
                        return out
                    out.append(text)
            except socket.timeout as exc:
                raise RigError(
                    f"rigctld did not answer {line!r} within {self.timeout}s"
                ) from exc
            return out

    # -- reading -----------------------------------------------------------
    def get_freq(self) -> int:
        self.freq_hz = int(float(self.command("f")[0]))
        return self.freq_hz

    def get_mode(self) -> tuple[str, int]:
        got = self.command("m", lines=2)
        self.mode = got[0] if got else ""
        self.passband = int(float(got[1])) if len(got) > 1 and got[1] else 0
        return self.mode, self.passband

    def get_ptt(self) -> bool:
        self.ptt = (self.command("t")[0] or "0").strip() == "1"
        return self.ptt

    def get_strength(self) -> float | None:
        try:
            self.strength = float(self.command("l STRENGTH")[0])
        except (RigError, IndexError, ValueError):
            self.strength = None
        return self.strength

    def dump_state(self) -> list[str]:
        return self.command("\\dump_state", lines=1)

    # -- writing -----------------------------------------------------------
    def set_freq(self, hz: float) -> None:
        self.command(f"F {int(round(hz))}")
        self.freq_hz = int(round(hz))

    def set_mode(self, mode: str, passband: int = 0) -> None:
        self.command(f"M {mode.upper()} {int(passband)}")
        self.mode = mode.upper()
        self.passband = int(passband)

    def set_vfo(self, vfo: str) -> None:
        self.command(f"V {vfo.upper()}")

    def set_ptt(self, on: bool) -> None:
        self.command(f"T {1 if on else 0}")
        self.ptt = bool(on)

    def set_split(self, on: bool, tx_vfo: str = "") -> None:
        self.command(f"S {1 if on else 0} {tx_vfo}".strip())

    def set_rit(self, hz: float) -> None:
        self.command(f"J {int(round(hz))}")

    def set_xit(self, hz: float) -> None:
        self.command(f"Z {int(round(hz))}")

    def set_level(self, name: str, value: float) -> None:
        self.command(f"L {name.upper()} {value}")

    def raw(self, line: str) -> list[str]:
        """Any rigctld command, for the controls this panel does not surface.

        One command per call: a second line would be read as the answer to the
        first, so it is rejected here rather than silently desynchronising the
        socket.  Multi-line replies are truncated to the first line.
        """
        text = line.strip()
        if not text or "\n" in text or "\r" in text:
            raise RigError("one rigctld command per line")
        return self.command(text, lines=1)

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict:
        if not self.connected:
            return {"connected": False}
        err = ""
        for reader in (self.get_freq, self.get_mode, self.get_ptt, self.get_strength):
            try:
                reader()
            except RigError as exc:  # keep the fields that did work
                err = err or str(exc)
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "vfo_mode": self.vfo_mode,
            "freq_hz": self.freq_hz,
            "mode": self.mode,
            "passband": self.passband,
            "ptt": self.ptt,
            "strength": self.strength,
            "error": err,
        }


class RigService:
    """A rigctld we may have started ourselves, plus the client attached to it."""

    def __init__(self) -> None:
        self.rig: Rigctld | None = None
        self.proc: subprocess.Popen | None = None
        self.model: int | None = None
        self.device: str = ""
        self.baud: int | None = None
        self.error = ""

    @property
    def connected(self) -> bool:
        return self.rig is not None and self.rig.connected

    def spawn(self, model: int, device: str | None, baud: int | None, port: int) -> None:
        cmd = ["rigctld", "-m", str(int(model)), "-T", DEFAULT_HOST, "-t", str(int(port))]
        if device:
            cmd += ["-r", device]
        if baud:
            cmd += ["-s", str(int(baud))]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
        except FileNotFoundError as exc:
            raise RigError("rigctld is not installed; install Hamlib") from exc
        self.model = int(model)
        self.device = device or ""
        self.baud = int(baud) if baud else None
        # rigctld needs a moment before it starts answering
        for _ in range(40):
            time.sleep(0.05)
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                self.proc = None
                raise RigError(f"rigctld exited on start: {err[:200] or 'no output'}")
            try:
                with socket.create_connection((DEFAULT_HOST, port), timeout=0.2):
                    return
            except OSError:
                continue
        raise RigError(f"rigctld did not start listening on {port}")

    def connect(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                model: int | None = None, device: str | None = None,
                baud: int | None = None, timeout: float = 2.5) -> dict:
        self.error = ""
        if model:
            self.spawn(int(model), device, baud, int(port))
        rig = Rigctld(host, int(port), timeout)
        try:
            rig.open()
        except RigError:
            self.stop()
            raise
        self.rig = rig
        return self.status()

    def stop(self) -> None:
        if self.rig is not None:
            self.rig.close()
            self.rig = None
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        self.model = None
        self.device = ""
        self.baud = None

    def status(self) -> dict:
        base = self.rig.status() if self.rig is not None else {"connected": False}
        base.update(
            {
                "model": self.model,
                "device": self.device,
                "baud": self.baud,
                "rigctld": bool(self.proc is not None),
                "pid": self.proc.pid if self.proc is not None else None,
            }
        )
        return base


# ------------------------------------------------------------------ rig models
_RIGS: list[dict] | None = None


def list_rigs(query: str = "", limit: int = 250) -> list[dict]:
    """Hamlib's supported rigs, straight out of ``rigctl -l``."""
    global _RIGS
    if _RIGS is None:
        try:
            done = subprocess.run(
                ["rigctl", "-l"], capture_output=True, text=True, timeout=20
            )
            text = done.stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        rows: list[dict] = []
        for line in text.splitlines():
            cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            rows.append(
                {
                    "model": int(cells[0]),
                    "mfg": cells[1],
                    "name": cells[2],
                    "status": cells[4] if len(cells) > 4 else "",
                }
            )
        _RIGS = rows
    needle = (query or "").strip().lower()
    hits = [r for r in _RIGS if needle in f"{r['mfg']} {r['name']}".lower()]
    return hits[:limit]


_VERSION: str | None = None


def hamlib_version() -> str:
    """Cached: the console asks on every poll and this shells out."""
    global _VERSION
    if _VERSION is not None:
        return _VERSION
    for binary in ("rigctld", "rigctl"):
        try:
            done = subprocess.run([binary, "--version"], capture_output=True,
                                  text=True, timeout=10)
            first = (done.stdout or done.stderr).splitlines()
            if first:
                _VERSION = first[0].strip()
                return _VERSION
        except (OSError, subprocess.SubprocessError, IndexError):
            continue
    _VERSION = "not installed"
    return _VERSION
