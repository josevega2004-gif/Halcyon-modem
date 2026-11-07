"""Host-mode modem service: PAT and VarAC attach through this command set.

The service is a pair of TCP ports: 8300 carries CR-terminated ASCII commands
and events, 8301 carries the raw byte stream of the link.  PAT drives its modem
transport through exactly that pair, and VarAC attaches to the same two ports,
so speaking this protocol is what lets those programs drive Halcyon.

Commands (host -> modem), each line ending in a bare CR:

    MYCALL <call> [alias...]      BW500 / BW2300 / BW2750
    LISTEN ON|OFF                 CONNECT <src> <dst>
    DISCONNECT                    ABORT
    VERSION                       COMPRESSION ON|OFF

Events (modem -> host):

    VERSION <text>    PENDING    CONNECTED <src> <dst>    DISCONNECTED
    PTT ON|OFF    BUFFER <n>    SN <n>    BUSY ON|OFF    IAMALIVE

The limit worth stating plainly: a link this server forms runs between two
Halcyon stations over our own OFDM/LDPC frames.  PAT and VarAC can select this
modem and open a connection, and their bytes move through the real modem; an
on-air session needs Halcyon at the far end, and nothing here pretends
otherwise.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .channel import make_channel
from .frame import (
    MAX_BLOCKS,
    REFERENCE_BW_HZ,
    best_mcs_channel,
    chunk_bits,
    in_band_snr,
    info_rate_bps,
    table_for,
)
from .modem import Modem
from .ofdm import COMPACT, DEEP, NANO, ROBUST, Phy

VERSION = "Halcyon 0.1 host mode"
CMD_PORT = 8300
DATA_PORT = 8301
HEARTBEAT_S = 15.0
# Rate control keeps this much margin over a rung's measured threshold; below
# it a rung is only reached when the ladder has no faster option left.
MCS_MARGIN_DB = 3.0
# Bytes a client writes while unlinked are held for the next link; a client that
# never links must not be able to grow this without bound.
MAX_BUFFERED = 256 * 1024

# The modulation ladder, widest first.  A move to a lower rank is a fallback and
# a move to a higher rank a recovery; both are written to the host log while a
# link is up, so an operator can see the link adapt in either direction.
_PROFILE_RANK = {"wide": 3, "compact": 2, "deep": 1, "nano": 0}


def geometry_for(bandwidth: int, snr_db: float | None = None) -> tuple[str, Phy]:
    """The client's bandwidth selector -> a Halcyon geometry.

    500 Hz uses compact, the 492 Hz low-speed ladder, matching the 500 Hz-class
    default xara uses.  At -4 dB and below, use the 375 Hz deep test geometry:
    it measured 55.2 bps against 36.5 bps at -8 dB and still fits the 500 Hz
    class.  At -12 dB use the 234 Hz nano geometry instead; its 16 bps rung
    measured 11.4 bps against 6.6 bps for the 21 bps deep rung.  A 500 Hz start
    is a ceiling: the host never selects the wide profile, even if a later
    BW2300 or BW2750 arrives, and may still fall back to deep/nano.  Nano is the
    floor: the 225 Hz mini and 175 Hz micro test profiles are not host fallbacks,
    because mini's 600-byte link measured 1.4-1.9 bps and micro is slower still,
    below the 6 bps worth operating.  At
    ordinary S/N 2300 and 2750 map to the wide profile, whose occupied bandwidth
    is 2.74 kHz -- slightly wider than the 2300 Hz setting the selector asks for,
    which is the one honest mismatch in this mapping.
    """
    if snr_db is not None and snr_db <= -12.0:
        return "nano", Phy(**NANO)
    if snr_db is not None and snr_db <= -4.0:
        return "deep", Phy(**DEEP)
    if int(bandwidth) <= 500:
        return "compact", Phy(**COMPACT)
    return "wide", Phy()


def pick_mcs(phy: Phy, snr: float) -> int:
    """Highest rung with 3 dB of margin at this channel S/N.

    ``snr`` is referred to the 2.75 kHz channel, but each ladder's thresholds are
    measured in its own band; :func:`frame.best_mcs_channel` does the conversion
    and the selection, so the host and the link cannot drift apart on either.
    """
    return best_mcs_channel(snr, MCS_MARGIN_DB, phy)


@dataclass
class HostState:
    mycall: str = "N0CALL"
    aliases: list[str] = field(default_factory=list)
    bandwidth: int = 2300
    listening: bool = False
    linked: bool = False
    peer: str = ""
    compression: bool = False
    public: bool = False
    bytes_tx: int = 0
    bytes_rx: int = 0
    last_sn: float | None = None
    connected_at: float = 0.0


class HostProtocol:
    """The host-mode state machine, with no sockets in it.

    Feed it CR-split lines and collect the events it emits; the server class
    below only carries those bytes to a client.
    """

    def __init__(self, mycall: str = "N0CALL", bandwidth: int = 2300,
                 sink=None, snr: float = 10.0, channel: str = "awgn"):
        bw = int(bandwidth)
        # A 500 Hz selection is a ceiling, not just a starting point: the host
        # may fall back to 375/234 Hz at low S/N, but a later BW2300/BW2750
        # from the client must not widen it.
        self.max_bandwidth = 500 if bw <= 500 else None
        self.state = HostState(mycall=mycall, bandwidth=self._capped(bw))
        self.snr = float(snr)
        self.channel = channel
        self.log: deque[tuple[str, str]] = deque(maxlen=400)
        self._sink = sink
        self._modem: Modem | None = None
        # One warning per low-margin episode: a weak path should not fill the
        # log with a line per burst, and recovery closes the episode.
        self._low_margin_warned = False
        self._low_margin_since: float | None = None
        # PAT dials two command connections and two data connections and we serve
        # every one of them on its own thread, so the state machine is shared.
        # ``_lock`` guards the state; ``_air`` serialises the modem, which is the
        # only work here worth several seconds of CPU.  RLock because the command
        # handlers call helpers that take the same lock.
        self._lock = threading.RLock()
        self._air = threading.Lock()

    # -- plumbing ----------------------------------------------------------
    def emit(self, line: str) -> None:
        with self._lock:
            self.log.append((time.strftime("%H:%M:%S"), line))
        if self._sink is not None:
            try:
                self._sink(line)
            except Exception:  # a dead client must not kill the state machine
                pass

    def _capped(self, bandwidth: int) -> int:
        value = int(bandwidth)
        if self.max_bandwidth is not None and value > self.max_bandwidth:
            return self.max_bandwidth
        return value

    def _set_bandwidth(self, bandwidth: int) -> None:
        """Record a client bandwidth request without ever widening a 500 cap."""
        value = int(bandwidth)
        if value <= 0:
            return
        if value <= 500:
            self.max_bandwidth = 500
            value = 500
        self.state.bandwidth = self._capped(value)

    def modem(self) -> Modem:
        with self._lock:
            _, phy = geometry_for(self.state.bandwidth, self.snr)
            previous = self._modem.phy.profile if self._modem is not None else None
            if self._modem is None or previous != phy.profile:
                self._modem = Modem(phy)
                if (previous in _PROFILE_RANK and phy.profile in _PROFILE_RANK
                        and _PROFILE_RANK[phy.profile] != _PROFILE_RANK[previous]
                        and self.state.linked):
                    self._note_transition(previous, phy)
            return self._modem

    def _note_transition(self, previous: str, phy: Phy) -> None:
        """Record a profile change in the host log, once per change."""
        wider = _PROFILE_RANK[phy.profile] > _PROFILE_RANK[previous]
        self.log.append((
            time.strftime("%H:%M:%S"),
            f"({'recovery' if wider else 'fallback'} {previous} -> {phy.profile} "
            f"{phy.bandwidth_hz:.0f} Hz at S/N {self.snr:+.1f} dB)",
        ))

    def _check_margin(self, phy: Phy, idx: int) -> None:
        """Log the start and the end of a below-target margin episode."""
        with self._lock:
            if not self.state.linked:
                return
            mode = table_for(phy)[idx]
            margin = in_band_snr(self.snr, phy) - mode.snr_channel
            low = margin < MCS_MARGIN_DB
            detail = ""
            if low and not self._low_margin_warned:
                self._low_margin_warned = True
                self._low_margin_since = time.monotonic()
                kind = "low margin"
            elif not low and self._low_margin_warned:
                self._low_margin_warned = False
                since = self._low_margin_since
                self._low_margin_since = None
                kind = "margin recovered"
                if since is not None:
                    detail = f", after {time.monotonic() - since:.1f} s"
            else:
                return
            self.log.append((
                time.strftime("%H:%M:%S"),
                f"({kind}: L{idx} {mode.label} at S/N {self.snr:+.1f} dB, "
                f"{margin:+.1f} dB vs {MCS_MARGIN_DB:g} dB target{detail})",
            ))

    def linked(self) -> bool:
        with self._lock:
            return bool(self.state.linked)

    # -- command handling --------------------------------------------------
    def handle(self, line: str) -> None:
        """Feed one CR-terminated command line, from any client thread."""
        with self._lock:
            self._handle(line)

    def _handle(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        parts = text.split()
        head = parts[0].upper()
        arg = text[len(parts[0]):].strip()

        if head == "MYCALL":
            calls = [c for c in arg.split() if c]
            if calls:
                self.state.mycall = calls[0].upper()
                self.state.aliases = [c.upper() for c in calls[1:]]
            return
        if head in ("BW500", "BW2300", "BW2750"):
            self._set_bandwidth(int(head[2:]))
            return
        if head == "BW" and arg.isdigit():
            self._set_bandwidth(int(arg))
            return
        if head == "LISTEN":
            on = arg.upper().startswith("ON") or arg.upper() == "CQ"
            self.state.listening = on
            # BUSY is the client's carrier-sense report, not a listen flag: emitting it
            # here would tell the client the channel is occupied when it is not.
            return
        if head == "CONNECT":
            self._connect(arg)
            return
        if head in ("DISCONNECT", "ABORT"):
            self._disconnect()
            return
        if head == "VERSION":
            self.emit(f"VERSION {VERSION}")
            return
        if head == "COMPRESSION":
            # ON, OFF and TEXT are accepted.  TEXT is what PAT sends for a Winlink
            # session, and it means the B2F stream is compressed -- compression
            # on, not off, which a startswith("ON") test would get backwards.
            mode = arg.upper()
            self.state.compression = mode.startswith("ON") or mode.startswith("TEXT")
            return
        if head == "PTT":
            self.emit("PTT ON" if arg.upper().startswith("ON") else "PTT OFF")
            return
        if head == "PUBLIC":
            # Winlink's public-gateway flag.  PAT sets it on every dial; keep it
            # so the panel can report what the client asked for.
            self.state.public = arg.upper().startswith("ON")
            return
        if head == "WINLINK":
            # PAT announces WINLINK SESSION just before CONNECT.  It lets a client
            # ask for a robust mode; there is no reply, so accepting it silently
            # is the whole of the handshake.
            return
        if head in ("CHAT", "IGNOREKISSDCD", "CQFRAME", "CLEANTXBUFFER", "QUIT", "CWID"):
            return
        # Anything else is an extension we do not implement; the protocol
        # has no error channel, so it is simply noted and ignored.
        self.log.append((time.strftime("%H:%M:%S"), f"? {text}"))

    def _connect(self, arg: str) -> None:
        fields = arg.split()
        if len(fields) >= 2 and fields[0].isdigit():
            # CONNECT <bandwidth> <target>: other hosts also accept the
            # selector first, so treat a leading number as bandwidth rather than
            # as our own callsign.
            self._set_bandwidth(int(fields[0]))
            src, dst = self.state.mycall, fields[1].upper()
        elif len(fields) >= 2:
            src, dst = fields[0].upper(), fields[1].upper()
            if not self.state.mycall or self.state.mycall == "N0CALL":
                self.state.mycall = src
        elif fields:
            src, dst = self.state.mycall, fields[0].upper()
        else:
            src, dst = self.state.mycall, "UNKNOWN"
        self.emit("PENDING")
        self.emit("PTT ON")
        self.emit(f"CONNECTED {src} {dst}")
        self.emit("PTT OFF")
        self.state.linked = True
        self.state.peer = dst
        self.state.connected_at = time.time()

    def _disconnect(self) -> None:
        was = self.state.linked
        self.state.linked = False
        self.state.peer = ""
        self._low_margin_warned = False
        self._low_margin_since = None
        self.emit("DISCONNECTED")
        if not was:
            self.log.append((time.strftime("%H:%M:%S"), "(disconnect with no link)"))

    # -- data plane --------------------------------------------------------
    def carry(self, data: bytes) -> bytes:
        """Push bytes through the modem over the simulated channel, return what
        the far end decoded.  This is the link the host-mode ports ride on.

        ``_air`` is held for the whole transfer so two data connections cannot
        drive the same modem at once; the state lock is taken only around the
        reads and writes of it, so commands (ABORT, VERSION) still get served
        while a burst is on the air.
        """
        with self._air:
            with self._lock:
                if not self.state.linked or not data:
                    return b""
                modem = self.modem()
            phy = modem.phy
            idx = pick_mcs(phy, self.snr)
            self._check_margin(phy, idx)
            cap = chunk_bits(phy, idx) * MAX_BLOCKS // 8
            out = bytearray()
            for start in range(0, len(data), max(1, cap)):
                chunk = data[start : start + cap]
                tx, _ = modem.build(chunk, idx, seq=0)
                ch = make_channel(
                    self.channel, self.snr, seed=(len(chunk) * 31 + start) & 0x7FFFFFFF,
                    bandwidth_hz=REFERENCE_BW_HZ,
                )
                res = modem.receive(ch(tx))
                with self._lock:
                    self.state.last_sn = round(float(res.snr_db), 1) if res.header_ok else None
                    self.state.bytes_tx += len(chunk)
                    if res.ok:
                        payload = bytes(res.payload or b"")
                        out.extend(payload)
                        self.state.bytes_rx += len(payload)
                if res.header_ok:
                    self.emit(f"SN {int(round(res.snr_db))}")
                self.emit("PTT ON")
                self.emit("PTT OFF")
                self.emit("BUFFER 0")
            return bytes(out)

    def status(self) -> dict:
        with self._lock:
            return self._status()

    def _next_rung(self, phy: Phy, idx: int) -> tuple[int | None, float | None]:
        """The rung rate control moves to next, and the channel S/N that does it.

        Rate order, not table order: the compact ladder's 41 bps rung sits last.
        The first higher rate to become eligible is the switch point; sampling
        just above it reports whichever rung the selector actually lands on,
        because a faster rung with the same threshold can be chosen instead.
        """
        table = table_for(phy)
        rate = info_rate_bps(phy, idx)
        offset = in_band_snr(0.0, phy)
        needs = [
            m.snr_channel + MCS_MARGIN_DB - offset
            for i, m in enumerate(table)
            if info_rate_bps(phy, i) > rate
        ]
        if not needs:
            return None, None
        need = float(min(needs))
        nxt = pick_mcs(phy, need + 0.01)
        return nxt, need

    def _status(self) -> dict:
        # The selector (``bandwidth``) is what the client asked for; the profile
        # and its occupied width are what the modem will actually put on the air
        # at the current S/N.  Computing the geometry here is pure, so status
        # never builds a modem or writes a transition line as a side effect.
        profile, phy = geometry_for(self.state.bandwidth, self.snr)
        idx = pick_mcs(phy, self.snr)
        table = table_for(phy)
        mode = table[idx]
        # Margin over the rung's measured in-band threshold.  Rate control asks
        # for 3 dB, so this is normally >= 3 and falls only at a ladder's floor.
        margin = in_band_snr(self.snr, phy) - mode.snr_channel
        nxt, nxt_snr = self._next_rung(phy, idx)
        return {
            "mycall": self.state.mycall,
            "aliases": list(self.state.aliases),
            "bandwidth": self.state.bandwidth,
            "profile": profile,
            "occupied_hz": round(float(phy.bandwidth_hz), 1),
            "mcs": idx,
            "mcs_label": mode.label,
            "mcs_margin_db": round(float(margin), 1),
            "mcs_margin_low": margin < MCS_MARGIN_DB,
            "mcs_margin_low_s": (
                round(time.monotonic() - self._low_margin_since, 1)
                if self._low_margin_warned and self._low_margin_since is not None
                else None
            ),
            "next_mcs": nxt,
            "next_mcs_label": table[nxt].label if nxt is not None else None,
            "next_mcs_snr_db": round(nxt_snr, 1) if nxt_snr is not None else None,
            "max_bandwidth": self.max_bandwidth,
            "listening": self.state.listening,
            "linked": self.state.linked,
            "peer": self.state.peer,
            "compression": self.state.compression,
            "public": self.state.public,
            "bytes_tx": self.state.bytes_tx,
            "bytes_rx": self.state.bytes_rx,
            "last_sn": self.state.last_sn,
            "link_s": round(time.time() - self.state.connected_at, 1)
            if self.state.linked else 0.0,
        }


class HostServer:
    """The two sockets around a :class:`HostProtocol`.

    The data port echoes through the modem: bytes a client writes come back
    decoded from the far end of the simulated link, which is what makes the pipe
    testable end to end without a second station.
    """

    def __init__(self, host: str = "127.0.0.1", cmd_port: int = CMD_PORT,
                 data_port: int = DATA_PORT, protocol: HostProtocol | None = None,
                 snr: float = 10.0, channel: str = "awgn", echo: bool = True):
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port
        self.protocol = protocol or HostProtocol(snr=snr, channel=channel, sink=self._send_line)
        self.protocol._sink = self._send_line
        self.echo = echo
        self._cmd_sock: socket.socket | None = None
        self._data_sock: socket.socket | None = None
        # PAT opens more than one command and data connection while it dials,
        # and treats the close of any of them as the modem disappearing.  So
        # clients are served, not evicted: every event goes to all of them and
        # each socket is closed only when its own peer goes away.
        self._clients: list[socket.socket] = []
        self._data_conns: list[socket.socket] = []
        self._client_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.error = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "HostServer":
        self._stop.clear()
        self._cmd_sock = self._listen(self.cmd_port)
        try:
            self._data_sock = self._listen(self.data_port)
        except OSError:
            # Leave no closed socket behind: ``running`` keys off this attribute,
            # and a server that failed to start must not claim to be up.
            self._cmd_sock.close()
            self._cmd_sock = None
            raise
        self.cmd_port = self._cmd_sock.getsockname()[1]
        self.data_port = self._data_sock.getsockname()[1]
        self._threads = [
            threading.Thread(target=self._accept_cmds, name="host-cmd", daemon=True),
            threading.Thread(target=self._accept_data, name="host-data", daemon=True),
            threading.Thread(target=self._heartbeat, name="host-beat", daemon=True),
        ]
        for t in self._threads:
            t.start()
        return self

    def _listen(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, port))
        s.listen(2)
        s.settimeout(0.3)
        return s

    def stop(self) -> None:
        self._stop.set()
        with self._client_lock:
            for s in self._clients + self._data_conns:
                try:
                    s.close()
                except OSError:
                    pass
            self._clients.clear()
            self._data_conns.clear()
        for s in (self._cmd_sock, self._data_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._cmd_sock = self._data_sock = None
        for t in self._threads:
            t.join(timeout=0.5)
        self._threads = []

    @property
    def running(self) -> bool:
        return self._cmd_sock is not None and not self._stop.is_set()

    # -- sockets -----------------------------------------------------------
    def _send_line(self, line: str) -> None:
        payload = (line + "\r").encode("ascii", "replace")
        with self._client_lock:
            dead = []
            for conn in self._clients:
                try:
                    conn.sendall(payload)
                except OSError:
                    dead.append(conn)
            for conn in dead:
                self._clients.remove(conn)
                try:
                    conn.close()
                except OSError:
                    pass

    def _accept_cmds(self) -> None:
        while not self._stop.is_set() and self._cmd_sock is not None:
            try:
                conn, _ = self._cmd_sock.accept()
            except (socket.timeout, OSError):
                continue
            with self._client_lock:
                self._clients.append(conn)
            threading.Thread(target=self._serve_cmds, args=(conn,), daemon=True).start()

    def _serve_cmds(self, conn: socket.socket) -> None:
        conn.settimeout(0.3)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                self.protocol.handle(line.decode("ascii", "replace"))
        try:
            conn.close()
        except OSError:
            pass
        with self._client_lock:
            if conn in self._clients:
                self._clients.remove(conn)

    def _accept_data(self) -> None:
        while not self._stop.is_set() and self._data_sock is not None:
            try:
                conn, _ = self._data_sock.accept()
            except (socket.timeout, OSError):
                continue
            with self._client_lock:
                self._data_conns.append(conn)
            threading.Thread(target=self._serve_data, args=(conn,), daemon=True).start()

    def _serve_data(self, conn: socket.socket) -> None:
        conn.settimeout(0.5)
        # Bytes written while unlinked are buffered and sent on the next link, as
        # the protocol says.  Buffering them here rather than spinning on the link
        # flag means the thread keeps reading: a client that disconnects while it
        # waits is noticed, instead of holding a thread and a socket forever.
        buffered = bytearray()
        while not self._stop.is_set():
            pending = b""
            try:
                data = conn.recv(1024)
            except socket.timeout:
                data = None
            except OSError:
                break
            if data == b"":
                break
            if data:
                if self.protocol.linked():
                    pending = bytes(buffered) + data
                    buffered.clear()
                elif len(buffered) < MAX_BUFFERED:
                    buffered.extend(data)
            elif self.protocol.linked() and buffered:
                # The link came up between reads: flush what was queued for it.
                pending = bytes(buffered)
                buffered.clear()
            if not pending:
                continue
            back = self.protocol.carry(pending)
            if self.echo and back:
                try:
                    conn.sendall(back)
                except OSError:
                    break
        try:
            conn.close()
        except OSError:
            pass
        with self._client_lock:
            if conn in self._data_conns:
                self._data_conns.remove(conn)

    def _heartbeat(self) -> None:
        # Waiting on the stop event rather than sleeping lets ``stop`` join this
        # thread instead of timing out on it.
        while not self._stop.wait(HEARTBEAT_S):
            if self._clients:
                self._send_line("IAMALIVE")

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict:
        return {
            "running": self.running,
            "host": self.host,
            "cmd_port": self.cmd_port,
            "data_port": self.data_port,
            "protocol": self.protocol.status(),
            "log": [f"{t} {m}" for t, m in list(self.protocol.log)[-40:]],
            "error": self.error,
        }


def main(argv: list[str] | None = None) -> int:
    """Run the host-mode service in the foreground.

    PAT and other host-mode clients dial 8300/8301 on the loopback interface;
    this is the process that has to be listening there.
    """
    ap = argparse.ArgumentParser(
        prog="halcyon-host",
        description="Halcyon host-mode modem service (two loopback TCP ports)",
    )
    ap.add_argument("--host", default="127.0.0.1", help="address to bind")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT, help="command port")
    ap.add_argument("--data-port", type=int, default=DATA_PORT, help="data port")
    ap.add_argument("--mycall", default="N0CALL", help="callsign before the client sends MYCALL")
    ap.add_argument(
        "--bandwidth", type=int, default=2300, choices=(500, 2300, 2750),
        help="default bandwidth selector; 500 is a hard ceiling",
    )
    ap.add_argument("--snr", type=float, default=10.0, help="simulated link S/N, dB")
    ap.add_argument(
        "--channel", default="awgn", choices=("awgn", "good", "moderate", "poor"),
        help="simulated channel",
    )
    ap.add_argument(
        "--no-echo", action="store_true",
        help="do not return decoded data to the client that wrote it",
    )
    args = ap.parse_args(argv)

    protocol = HostProtocol(
        mycall=args.mycall, bandwidth=args.bandwidth, snr=args.snr, channel=args.channel
    )
    try:
        server = HostServer(
            host=args.host,
            cmd_port=args.cmd_port,
            data_port=args.data_port,
            protocol=protocol,
            echo=not args.no_echo,
        ).start()
    except OSError as exc:
        print(
            f"halcyon-host: cannot listen on {args.host}:{args.cmd_port} and "
            f"{args.data_port}: {exc}",
            file=sys.stderr,
        )
        return 1

    stop = threading.Event()

    def _stop(signum: int, frame) -> None:  # noqa: ARG001 - signal signature
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(
        f"{VERSION} on {args.host}, commands :{server.cmd_port}, "
        f"data :{server.data_port}; {protocol.state.mycall}, "
        f"{protocol.state.bandwidth} Hz, S/N {args.snr:g} dB (Ctrl-C to stop)",
        flush=True,
    )
    try:
        while not stop.wait(0.5):
            pass
    finally:
        server.stop()
        print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
