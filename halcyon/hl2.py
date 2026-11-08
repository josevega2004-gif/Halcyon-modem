"""Hermes Lite 2 connection: just enough HPSDR Protocol 1 to stream IQ and key TX.

    radio = HermesLite("192.168.1.40", rate=48000, freq_hz=14_105_000)
    radio.open()
    iq = radio.capture(0.5)        # complex baseband at radio.rate
    radio.transmit(burst, fs)      # keys PTT, plays IQ, drops PTT
    radio.close()

Wire format, from the Hermes-Lite 2 wiki (github.com/softerhardware/Hermes-Lite2/
wiki/Protocol) and the gateware parser dsopenhpsdr1.v:

  host -> radio, 1032 bytes
  ---------------------------------------------------------------
  0-1   EF FE        sync
  2     01           data frame
  3     02           endpoint EP2 (host -> radio)
  4-7   seq          big-endian frame counter
  8-10  7F 7F 7F     USB sub-frame 1 sync
  11    C0           [7] request, [6:1] address, [0] MOX
  12-15 C1..C4       data word, MSB first
  16..  samples      2 unused audio words, then I16/Q16 big-endian
  520   ..           sub-frame 2 repeats the same layout

  radio -> host, 1032 bytes: same header, endpoint 0x06, 24-bit big-endian I/Q
  (63 samples per sub-frame with one receiver: 504 bytes / (6*1 + 2)).

Registers this client drives:

  0x00  [25:24] rate (0=48k, 1=96k, 2=192k, 3=384k), [6:3] receivers-1, [2] duplex
  0x01  TX NCO, plain Hz
  0x02  RX1 NCO, plain Hz
  0x09  [31:28] TX drive trim, [19] PA enable
  0x0a  [6] LNA full range, [5:0] gain in dB + 12

Two HL2 traps are handled explicitly: the NCO write must not be swallowed by the
"RX0 follows TX" path (duplex is set in 0x00), and MOX is level-sensitive, so it
rides on every command slot of every sub-frame while transmitting, with a frame
sent every 126 samples to keep the watchdog fed.

No hardware was available in this checkout: the packet encode/parse paths are
unit-tested against the byte layouts above, but discovery, streaming and keying
have not been exercised on a radio.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from fractions import Fraction

import numpy as np

SYNC = b"\xef\xfe"
FRAME_DATA = 0x01
FRAME_DISCOVERY = 0x02
FRAME_STARTSTOP = 0x04
EP_TX = 0x02
EP_RX = 0x06
PORT = 1024
FRAME_BYTES = 1032
SUBBUF = 512
SLOTS_PER_FRAME = 2

BOARDS = {
    0x00: "Metis",
    0x01: "Hermes / HL2 in emulation",
    0x04: "Angelia",
    0x05: "Orion",
    0x06: "Hermes Lite 2",
    0x0A: "HermesC",
}
RATE_BITS = {48_000: 0, 96_000: 1, 192_000: 2, 384_000: 3}
RATES = tuple(RATE_BITS)

# Register addresses used by the control round-robin.
ADDR_CONFIG = 0x00
ADDR_TX_NCO = 0x01
ADDR_RX_NCO = 0x02
ADDR_TX_GAIN = 0x09
ADDR_LNA = 0x0A


# ----------------------------------------------------------------- discovery
def discovery_request() -> bytes:
    """The 63-byte broadcast that makes every Metis-family radio answer."""
    return SYNC + bytes([FRAME_DISCOVERY]) + bytes(60)


def parse_discovery(pkt: bytes, ip: str = "") -> dict | None:
    """Decode a 60-byte discovery reply, or None if it is not one."""
    if len(pkt) < 20 or pkt[0:2] != SYNC:
        return None
    board_id = pkt[10]
    return {
        "ip": ip,
        "mac": ":".join(f"{b:02X}" for b in pkt[3:9]),
        "board_id": board_id,
        "board": BOARDS.get(board_id, f"board 0x{board_id:02X}"),
        "firmware": f"{pkt[9]}.{pkt[15]}",
        # 0x13 is the "Number of Hardware Receivers" in the HL2 reply.  Byte
        # 0x0D is not: a real HL2 on firmware 72.0 answers 10 there, which is
        # not its DDC count (four on stock gateware) and cannot be a stream
        # request either.
        "receivers": max(1, pkt[19]),
        "busy": bool(pkt[2] & 0x01),
    }


def discover(timeout: float = 1.2, port: int = PORT) -> list[dict]:
    """Broadcast a discovery request and collect whatever answers."""
    found: dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", 0))
        sock.settimeout(0.2)
        req = discovery_request()
        for dest in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(req, (dest, port))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                pkt, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            got = parse_discovery(pkt, addr[0])
            if got:
                found[got["mac"]] = got
    finally:
        sock.close()
    return list(found.values())


# ---------------------------------------------------------------- frame build
def cc_slot(frame: bytearray, sub: int, addr: int, data: int, mox: bool = False,
            rqst: bool = False) -> None:
    """Write one command-and-control slot into a sub-frame."""
    base = 8 + sub * SUBBUF
    frame[base : base + 3] = b"\x7f\x7f\x7f"
    frame[base + 3] = (0x80 if rqst else 0) | ((addr & 0x3F) << 1) | (1 if mox else 0)
    frame[base + 4 : base + 8] = (int(data) & 0xFFFFFFFF).to_bytes(4, "big")


def pack_iq(frame: bytearray, iq: np.ndarray, mox: bool = False, seq: int = 0) -> int:
    """Lay 126 samples of TX baseband into the two sub-frames; return count used."""
    iq = np.asarray(iq, dtype=np.complex64).ravel()
    per_sub = (SUBBUF - 8) // 8
    used = 0
    for sub in range(2):
        base = 8 + sub * SUBBUF + 8
        chunk = iq[used : used + per_sub]
        used += chunk.size
        if chunk.size == 0:
            continue
        i = np.clip(np.real(chunk), -1.0, 1.0) * 32767.0
        q = np.clip(np.imag(chunk), -1.0, 1.0) * 32767.0
        words = np.empty((chunk.size, 8), dtype=np.uint8)
        words[:, 0:4] = 0  # audio / EADDR words: the HL2 ignores them
        words[:, 4:6] = (np.rint(i).astype(">i2").view(np.uint8).reshape(-1, 2))
        words[:, 6:8] = (np.rint(q).astype(">i2").view(np.uint8).reshape(-1, 2))
        frame[base : base + chunk.size * 8] = words.tobytes()
    return used


def build_frame(seq: int, slots: list[tuple[int, int]], mox: bool = False,
                tx: np.ndarray | None = None) -> bytes:
    """One 1032-byte host-to-radio frame carrying up to two register writes."""
    frame = bytearray(FRAME_BYTES)
    frame[0:2] = SYNC
    frame[2] = FRAME_DATA
    frame[3] = EP_TX
    frame[4:8] = (seq & 0xFFFFFFFF).to_bytes(4, "big")
    for sub, (addr, data) in enumerate(slots[:SLOTS_PER_FRAME]):
        cc_slot(frame, sub, addr, data, mox=mox)
    # Unused slots still need their USB sync so the radio sees a well-formed frame.
    for sub in range(len(slots), SLOTS_PER_FRAME):
        base = 8 + sub * SUBBUF
        frame[base : base + 3] = b"\x7f\x7f\x7f"
    if tx is not None:
        pack_iq(frame, tx, mox=mox, seq=seq)
    return bytes(frame)


def start_packet(run: bool = True, wideband: bool = False, disable_watchdog: bool = False) -> bytes:
    """The 64-byte start/stop command."""
    cmd = (1 if run else 0) | (2 if wideband else 0) | (0x80 if disable_watchdog else 0)
    return SYNC + bytes([FRAME_STARTSTOP, cmd]) + bytes(60)


def parse_iq(pkt: bytes, receivers: int = 1) -> np.ndarray:
    """24-bit big-endian I/Q out of one 1032-byte receive frame."""
    empty = np.zeros(0, dtype=np.complex64)
    if len(pkt) != FRAME_BYTES or pkt[0:2] != SYNC or pkt[3] != EP_RX:
        return empty
    # Every enabled receiver contributes 6 bytes (24-bit I then 24-bit Q) to a
    # sample, and the two bytes after the last one carry the microphone word, so
    # the stride depends on how many receivers the radio was told to stream.
    # This client uses receiver 0; the rest are skipped rather than misread.
    stride = 6 * max(1, int(receivers)) + 2
    per_sub = (SUBBUF - 8) // stride
    out = []
    for sub in range(2):
        base = 8 + sub * SUBBUF + 8
        seg = pkt[base : base + per_sub * stride]
        if len(seg) < per_sub * stride:
            continue
        w = np.frombuffer(seg, dtype=np.uint8).reshape(-1, stride)[:, :6].astype(np.int32)
        i = (w[:, 0] << 16) | (w[:, 1] << 8) | w[:, 2]
        q = (w[:, 3] << 16) | (w[:, 4] << 8) | w[:, 5]
        i = np.where(i & 0x800000, i - (1 << 24), i)
        q = np.where(q & 0x800000, q - (1 << 24), q)
        out.append(i + 1j * q)
    if not out:
        return empty
    return (np.concatenate(out) / 8388607.0).astype(np.complex64)


# --------------------------------------------------------------------- client
class HermesLite:
    """A connected radio: control round-robin, receive ring buffer, TX keying."""

    def __init__(self, ip: str, port: int = PORT, rate: int = 48_000,
                 freq_hz: float = 14_105_000.0, lna_db: int = 16, drive: int = 8,
                 receivers: int = 1, mac: str = "", board: str = "",
                 firmware: str = ""):
        self.ip = ip
        self.port = port
        self.rate = int(rate) if rate in RATE_BITS else 48_000
        self.freq_hz = float(freq_hz)
        self.lna_db = int(np.clip(lna_db, -12, 48))
        self.drive = int(np.clip(drive, 0, 15))
        self.receivers = max(1, int(receivers))
        self.mac = mac
        self.board = board or "Hermes Lite 2"
        self.firmware = firmware

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

        self._seq = 0
        self._mox = False
        self._tx_iq = np.zeros(0, dtype=np.complex64)
        self._tx_pos = 0
        self._tx_done = threading.Event()
        self._tx_done.set()

        self._rx = np.zeros(0, dtype=np.complex64)
        self._rx_lock = threading.Lock()
        self.rx_frames = 0
        self.tx_frames = 0
        self.started = 0.0
        self.last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "HermesLite":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        bound = False
        for port in (self.port, 0):
            try:
                sock.bind(("", port))
                bound = True
                break
            except OSError:
                continue
        if not bound:
            sock.close()
            raise OSError("could not bind a UDP socket for the radio")
        sock.settimeout(0.2)
        sock.connect((self.ip, self.port))
        self._sock = sock
        self._stop.clear()
        self.started = time.time()

        # Bring the radio up, then keep the control round-robin running: MOX is
        # level-sensitive and the HL2 watchdog expects a frame every few ms.
        sock.send(start_packet(run=True))
        self._threads = [
            threading.Thread(target=self._run_tx, name="hl2-tx", daemon=True),
            threading.Thread(target=self._run_rx, name="hl2-rx", daemon=True),
        ]
        for t in self._threads:
            t.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._mox = False
        if self._sock is not None:
            try:
                self._sock.send(start_packet(run=False))
            except OSError:
                pass
            time.sleep(0.05)
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for t in self._threads:
            t.join(timeout=0.6)
        self._threads = []

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    # -- control values ----------------------------------------------------
    def configure(self, rate: int | None = None, freq_hz: float | None = None,
                  lna_db: int | None = None, drive: int | None = None) -> dict:
        with self._lock:
            if rate in RATE_BITS:
                self.rate = int(rate)
            if freq_hz is not None:
                self.freq_hz = float(np.clip(freq_hz, 0.1e6, 38.4e6))
            if lna_db is not None:
                self.lna_db = int(np.clip(lna_db, -12, 48))
            if drive is not None:
                self.drive = int(np.clip(drive, 0, 15))
        return self.status()

    def _config_word(self) -> int:
        bits = RATE_BITS.get(self.rate, 0)
        return ((bits & 0x3) << 24) | (((self.receivers - 1) & 0xF) << 3) | 0x04

    def _gain_word(self) -> int:
        # [31:28] drive trim, [19] PA enable.  Full-scale headroom is the caller's
        # job: this nibble only trims the AD9866, it is not a volume control.
        return ((self.drive & 0xF) << 28) | (1 << 19)

    def _lna_word(self) -> int:
        return (1 << 6) | int(np.clip(self.lna_db + 12, 0, 60))

    def _round_robin(self, i: int) -> list[tuple[int, int]]:
        regs = [
            (ADDR_CONFIG, self._config_word()),
            (ADDR_LNA, self._lna_word()),
            (ADDR_RX_NCO, int(self.freq_hz)),
            (ADDR_TX_NCO, int(self.freq_hz)),
            (ADDR_TX_GAIN, self._gain_word()),
        ]
        a = regs[i % len(regs)]
        b = regs[(i + 1) % len(regs)]
        return [a, b]

    # -- threads -----------------------------------------------------------
    def _run_tx(self) -> None:
        per_frame = (SUBBUF - 8) // 8 * 2  # 126 samples per 1032-byte frame
        period = per_frame / float(self.rate)
        i = 0
        next_at = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            if now < next_at:
                time.sleep(min(period, next_at - now))
                continue
            next_at = max(next_at + period, now - period)
            with self._lock:
                slots = self._round_robin(i)
                mox = self._mox
                chunk = self._tx_iq[self._tx_pos : self._tx_pos + per_frame]
                if chunk.size:
                    self._tx_pos += chunk.size
                    if self._tx_pos >= self._tx_iq.size:
                        self._tx_done.set()
            i += 2
            try:
                self._sock.send(build_frame(self._seq, slots, mox=mox,
                                            tx=chunk if chunk.size else None))
                self._seq += 1
                self.tx_frames += 1
            except OSError as exc:
                self.last_error = str(exc)
                time.sleep(0.05)

    def _run_rx(self) -> None:
        keep = int(self.rate * 6)  # six seconds of ring buffer
        while not self._stop.is_set():
            try:
                pkt = self._sock.recv(2048)
            except socket.timeout:
                continue
            except OSError as exc:
                self.last_error = str(exc)
                break
            iq = parse_iq(pkt, self.receivers)
            if iq.size:
                with self._rx_lock:
                    self._rx = np.concatenate([self._rx, iq])[-keep:]
                self.rx_frames += 1

    # -- data paths --------------------------------------------------------
    def recent(self, seconds: float = 0.25) -> np.ndarray:
        with self._rx_lock:
            n = int(self.rate * seconds)
            return self._rx[-n:].copy() if self._rx.size else np.zeros(0, dtype=np.complex64)

    def capture(self, seconds: float = 1.0, timeout: float = 3.0) -> np.ndarray:
        """Wait for `seconds` of fresh receive IQ (as much as arrives)."""
        want = int(self.rate * seconds)
        end = time.time() + max(timeout, seconds * 2.0 + 1.0)
        with self._rx_lock:
            self._rx = np.zeros(0, dtype=np.complex64)
        while time.time() < end:
            with self._rx_lock:
                if self._rx.size >= want:
                    return self._rx[-want:].copy()
            time.sleep(0.01)
        return self.recent(seconds)

    def key(self, on: bool) -> None:
        self._mox = bool(on)

    def transmit(self, iq: np.ndarray, rate: float, timeout: float = 10.0) -> float:
        """Key PTT, ship baseband IQ at `rate`, drop PTT.  Returns airtime."""
        iq = np.asarray(iq, dtype=np.complex64).ravel()
        if rate and abs(rate - self.rate) > 1e-6:
            iq = resample(iq, float(rate), float(self.rate))
            if iq.size < 2:
                return 0.0
        if iq.size == 0:
            return 0.0
        peak = float(np.max(np.abs(iq))) or 1.0
        iq = iq / peak * 0.9
        with self._lock:
            self._tx_iq = iq
            self._tx_pos = 0
            self._mox = True
            self._tx_done.clear()
        airtime = iq.size / float(self.rate)
        self._tx_done.wait(timeout=min(timeout, airtime + 2.0))
        time.sleep(0.12)  # let the last samples leave before unkeying
        self._mox = False
        with self._lock:
            self._tx_iq = np.zeros(0, dtype=np.complex64)
        return round(airtime, 3)

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict:
        return {
            "connected": self.connected,
            "ip": self.ip,
            "port": self.port,
            "mac": self.mac,
            "board": self.board,
            "firmware": self.firmware,
            "rate": self.rate,
            "freq_hz": round(self.freq_hz, 1),
            "lna_db": self.lna_db,
            "drive": self.drive,
            "ptt": bool(self._mox),
            "rx_frames": self.rx_frames,
            "tx_frames": self.tx_frames,
            "uptime_s": round(time.time() - self.started, 1) if self.started else 0.0,
            "error": self.last_error,
        }


def resample(iq: np.ndarray, rate_in: float, rate_out: float) -> np.ndarray:
    """Rate-convert TX IQ, filtering before changing the rate.

    Linear interpolation is fine on the way up but has no anti-alias filter on
    the way down, so images of the modem's passband would fold back into it.
    scipy is a declared dependency; the linear path is only a fallback for a
    partial install.
    """
    if not rate_in or not rate_out or int(round(rate_in)) == int(round(rate_out)):
        return iq
    if int(round(rate_in)) <= 0 or int(round(rate_out)) <= 0:
        return iq
    ratio = Fraction(int(round(rate_out)), int(round(rate_in))).limit_denominator(2000)
    try:
        from scipy.signal import resample_poly

        return resample_poly(iq, ratio.numerator, ratio.denominator).astype(np.complex64)
    except ImportError:  # pragma: no cover - scipy is a declared dependency
        n = max(2, int(round(iq.size * float(ratio.numerator) / ratio.denominator)))
        src = np.linspace(0.0, 1.0, iq.size, endpoint=False)
        dst = np.linspace(0.0, 1.0, n, endpoint=False)
        return (np.interp(dst, src, iq.real) + 1j * np.interp(dst, src, iq.imag)).astype(
            np.complex64
        )


def spectrum_row(iq: np.ndarray, width: int = 400) -> list[int]:
    """One quantised spectrum line, 0-255, for a waterfall strip."""
    if iq.size < 64:
        return [0] * width
    n = 1 << int(np.ceil(np.log2(min(iq.size, 4096))))
    seg = iq[-n:]
    win = np.hanning(seg.size)
    p = np.abs(np.fft.fftshift(np.fft.fft(seg * win))) ** 2
    edges = np.linspace(0, p.size, width + 1).astype(int)
    band = np.array([p[edges[i] : edges[i + 1]].mean() for i in range(width)])
    db = 10.0 * np.log10(np.maximum(band, 1e-20))
    lo = float(np.percentile(db, 20))
    hi = float(np.percentile(db, 99.5))
    q = np.clip((db - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    return [int(v * 255) for v in q]
