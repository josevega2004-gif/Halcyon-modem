"""TCI (Transceiver Control Interface) client.

TCI is Expert Electronics' WebSocket replacement for CAT plus audio cables: the
radio software (ExpertSDR3, Thetis, deskHPSDR, Quisk) is the server, we are the
client, and every TCI server listens on port 50001 by default.

Two message kinds share the socket:

    text   ``command:arg1,arg2;``  -- several commands may arrive in one frame,
           and ``ready``/``start``/``stop`` arrive bare, with no colon
    binary a 64-byte little-endian header (16 uint32) followed by payload:
             [0]  receiver        [1]  sample rate      [2]  format
             [3]  codec (0)       [4]  crc (0)          [5]  length (in floats)
             [6]  stream type     [7]  channels
           stream type 0 = IQ, 1 = RX audio, 2 = TX audio, 3 = TX chrono.
           format 0 = int16, 1 = int24, 2 = int32, 3 = float32.

``length`` is counted in *floats*, not frames, which is the reading the two
working implementations agree on (Quisk and deskHPSDR); the parser here is
tolerant of the frames reading by bounding to the actual payload size.

PTT is ``trx:0,true;``.  VFO is ``vfo:0,0,<hz>;``, modulation is
``modulation:0,<mode>;``.  Receive audio needs ``audio_start:0;`` and arrives as
type-1 frames; there is no spectrum stream in TCI, so a panadapter is computed
from ``iq_start:0;`` type-0 frames client-side.
"""

from __future__ import annotations

import json
import struct
import threading
import time
from collections import deque
from fractions import Fraction

import numpy as np

DEFAULT_URL = "ws://127.0.0.1:50001"
HEADER = struct.Struct("<16I")
HEADER_BYTES = HEADER.size  # 64
STREAM_IQ, STREAM_RX_AUDIO, STREAM_TX_AUDIO, STREAM_TX_CHRONO = 0, 1, 2, 3
FMT_INT16, FMT_INT24, FMT_INT32, FMT_FLOAT32 = 0, 1, 2, 3
MODES = ("LSB", "USB", "DSB", "CWL", "CWU", "NFM", "AM", "SAM", "DIGL", "DIGU", "SPEC", "DRM")


# ------------------------------------------------------------------ pure parts
def parse_text(frame: str) -> list[tuple[str, str]]:
    """Split one text frame into (command, args) pairs.

    Several commands can share a frame, separated by semicolons, and ``ready`` /
    ``start`` / ``stop`` carry no colon at all.
    """
    out: list[tuple[str, str]] = []
    for part in frame.replace("\r", "").split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, args = part.partition(":")
            out.append((name.strip().lower(), args.strip()))
        else:
            out.append((part.lower(), ""))
    return out


def parse_header(data: bytes) -> dict | None:
    """Decode a binary frame header; None if the frame is too short."""
    if len(data) < HEADER_BYTES:
        return None
    vals = HEADER.unpack_from(data, 0)
    return {
        "receiver": vals[0],
        "rate": vals[1],
        "format": vals[2],
        "codec": vals[3],
        "crc": vals[4],
        "length": vals[5],
        "stream": vals[6],
        "channels": vals[7],
    }


def decode_payload(data: bytes, hdr: dict) -> np.ndarray:
    """Payload as float32 samples, whatever integer format the server chose."""
    body = data[HEADER_BYTES:]
    if not body:
        return np.zeros(0, dtype=np.float32)
    fmt = hdr["format"]
    if fmt == FMT_FLOAT32:
        arr = np.frombuffer(body[: len(body) // 4 * 4], dtype="<f4")
    elif fmt == FMT_INT16:
        arr = np.frombuffer(body[: len(body) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif fmt == FMT_INT32:
        arr = np.frombuffer(body[: len(body) // 4 * 4], dtype="<i4").astype(np.float32) / 2147483648.0
    elif fmt == FMT_INT24:
        raw = np.frombuffer(body[: len(body) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
        v = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8)
             | (raw[:, 2].astype(np.int32) << 16))
        v = np.where(v & 0x800000, v - (1 << 24), v)
        arr = v.astype(np.float32) / 8388608.0
    else:
        return np.zeros(0, dtype=np.float32)
    return np.asarray(arr, dtype=np.float32)


def build_frame(samples: np.ndarray, rate: int = 48_000, stream: int = STREAM_TX_AUDIO,
                channels: int = 2, receiver: int = 0) -> bytes:
    """One TCI binary frame with float32 payload.

    ``length`` and the payload both count floats (interleaved channels), which is
    what Quisk and deskHPSDR expect on the wire.
    """
    flat = np.asarray(samples, dtype="<f4").ravel()
    header = HEADER.pack(receiver, int(rate), FMT_FLOAT32, 0, 0, int(flat.size),
                         int(stream), int(channels), 0, 0, 0, 0, 0, 0, 0, 0)
    return header + flat.tobytes()


def resample(audio: np.ndarray, rate_in: float, rate_out: float) -> np.ndarray:
    """Rate-convert TX audio, filtering before decimating.

    The server plays what it is sent, so an unfiltered decimation would fold
    the audio band's images back into it; scipy is a declared dependency, and
    the linear path is only a fallback for a partial install.
    """
    if not rate_in or not rate_out or int(round(rate_in)) == int(round(rate_out)):
        return audio
    if int(round(rate_in)) <= 0 or int(round(rate_out)) <= 0:
        return audio
    ratio = Fraction(int(round(rate_out)), int(round(rate_in))).limit_denominator(2000)
    try:
        from scipy.signal import resample_poly

        return resample_poly(audio, ratio.numerator, ratio.denominator).astype(np.float32)
    except ImportError:  # pragma: no cover - scipy is a declared dependency
        n = max(2, int(round(audio.size * float(ratio.numerator) / ratio.denominator)))
        src = np.linspace(0.0, 1.0, audio.size, endpoint=False)
        dst = np.linspace(0.0, 1.0, n, endpoint=False)
        return np.interp(dst, src, audio).astype(np.float32)


def mono(samples: np.ndarray, channels: int) -> np.ndarray:
    """Interleaved stereo -> mono (Quisk sends L=R, other servers may differ)."""
    if channels <= 1:
        return samples
    usable = samples[: samples.size // channels * channels].reshape(-1, channels)
    return usable.mean(axis=1)


# --------------------------------------------------------------------- client
class TciClient:
    """A TCI session: handshake, control, receive audio and transmit audio."""

    def __init__(self, url: str = DEFAULT_URL, audio_rate: int = 48_000,
                 samples_per_frame: int = 2400, timeout: float = 4.0):
        self.url = url
        self.audio_rate = int(audio_rate)
        self.samples_per_frame = int(samples_per_frame)
        self.timeout = float(timeout)
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tx_lock = threading.Lock()

        self.handshake: dict[str, str] = {}
        self.log: deque[tuple[str, str]] = deque(maxlen=300)
        self.rx: deque[np.ndarray] = deque(maxlen=64)
        self.iq: deque[np.ndarray] = deque(maxlen=32)
        self.vfo_hz = 0
        self.mode = "USB"
        self.ptt = False
        self.rx_frames = 0
        self.tx_frames = 0
        self.error = ""
        self.started = 0.0

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "TciClient":
        try:
            from websockets.sync.client import connect
        except Exception as exc:  # pragma: no cover - environment problem
            raise RuntimeError(
                "the TCI client needs the 'websockets' package (pip install websockets)"
            ) from exc
        self._ws = connect(self.url, open_timeout=self.timeout, close_timeout=1.0,
                           max_size=None)
        self._stop.clear()
        self.started = time.time()
        # Declare 2.0 audio parameters before asking for the stream: servers use
        # these two commands to recognise a 2.0 client.
        for cmd in (
            "audio_stream_sample_type:float32",
            "audio_stream_channels:2",
            f"audio_stream_samples:{self.samples_per_frame}",
            f"audio_samplerate:{self.audio_rate}",
            "audio_start:0",
        ):
            self.send(cmd)
        self._thread = threading.Thread(target=self._reader, name="tci-rx", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.send("audio_stop:0")
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._stop.is_set()

    # -- control -----------------------------------------------------------
    def send(self, cmd: str) -> None:
        ws = self._ws
        if ws is None:
            return
        line = cmd if cmd.endswith(";") else cmd + ";"
        # One writer at a time: the TX thread streams binary frames into the same
        # websocket, and a websocket client is not safe to write from two threads.
        with self._tx_lock:
            try:
                ws.send(line)
            except Exception as exc:
                self.error = str(exc)

    def set_vfo(self, hz: float) -> None:
        self.vfo_hz = int(hz)
        self.send(f"vfo:0,0,{int(hz)}")

    def set_mode(self, mode: str) -> None:
        self.mode = mode.upper()
        self.send(f"modulation:0,{mode.lower()}")

    def set_ptt(self, on: bool) -> None:
        self.ptt = bool(on)
        self.send(f"trx:0,{'true' if on else 'false'}")

    # -- receiving ---------------------------------------------------------
    def _reader(self) -> None:
        ws = self._ws
        while not self._stop.is_set() and ws is not None:
            try:
                msg = ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._stop.is_set():
                    self.error = str(exc)
                break
            if isinstance(msg, bytes):
                self._binary(msg)
            else:
                self._text(msg)

    def _text(self, frame: str) -> None:
        for name, args in parse_text(frame):
            self.log.append((time.strftime("%H:%M:%S"), f"< {name}:{args}" if args else f"< {name}"))
            if name in ("protocol", "device", "trx_count", "channels_count",
                        "audio_samplerate", "iq_samplerate", "if_limits",
                        "modulations_list", "receive_only"):
                self.handshake[name] = args
            elif name in ("vfo", "dds"):
                parts = args.split(",")
                if len(parts) >= 3 and name == "vfo":
                    self.vfo_hz = int(float(parts[2]))
                elif len(parts) >= 2:
                    self.vfo_hz = int(float(parts[1]))
            elif name == "modulation":
                parts = args.split(",")
                if len(parts) >= 2:
                    self.mode = parts[1].upper()
            elif name == "trx":
                parts = args.split(",")
                if len(parts) >= 2:
                    self.ptt = parts[1].strip().lower() == "true"
            elif name == "ready":
                self.handshake["ready"] = "1"

    def _binary(self, msg: bytes) -> None:
        hdr = parse_header(msg)
        if hdr is None:
            return
        samples = decode_payload(msg, hdr)
        if hdr["stream"] == STREAM_RX_AUDIO and samples.size:
            self.rx.append(mono(samples, max(1, hdr["channels"])))
            self.rx_frames += 1
        elif hdr["stream"] == STREAM_IQ and samples.size:
            pairs = samples.size // 2 * 2
            pairs_arr = samples[:pairs].reshape(-1, 2)
            self.iq.append((pairs_arr[:, 0] + 1j * pairs_arr[:, 1]).astype(np.complex64))
            self.rx_frames += 1
        elif hdr["stream"] == STREAM_TX_CHRONO:
            self.log.append((time.strftime("%H:%M:%S"), f"< chrono {hdr['length']}"))

    def drain_audio(self, seconds: float) -> np.ndarray:
        """Everything received so far for the requested duration (mono, 48 kHz)."""
        want = int(self.audio_rate * seconds)
        chunks = list(self.rx)
        self.rx.clear()
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[-want:]

    def capture(self, seconds: float = 1.0) -> np.ndarray:
        """The most recent `seconds` of receive audio, waiting if it is short.

        The buffer is deliberately not cleared here: audio that arrived between
        the operator pressing a button and this call is exactly what they want
        to decode, and discarding it would wait forever for a stream that has
        already been sent.
        """
        end = time.time() + seconds + self.timeout
        while time.time() < end:
            got = sum(c.size for c in self.rx)
            if got >= self.audio_rate * seconds:
                break
            time.sleep(0.01)
        return self.drain_audio(seconds)

    # -- transmitting ------------------------------------------------------
    def transmit(self, audio: np.ndarray, rate: float) -> float:
        """Key, stream audio as float32 stereo TX frames, unkey.  Returns airtime."""
        audio = np.asarray(audio, dtype=np.float32).ravel()
        if audio.size == 0:
            return 0.0
        if rate and int(rate) != self.audio_rate:
            audio = resample(audio, float(rate), float(self.audio_rate))
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = (audio / peak * 0.6).astype(np.float32)  # TCI servers drop quiet TX
        airtime = audio.size / float(self.audio_rate)
        self.set_ptt(True)
        try:
            step = self.samples_per_frame
            t0 = time.perf_counter()
            for i in range(0, audio.size, step):
                chunk = audio[i : i + step]
                stereo = np.repeat(chunk, 2)  # interleaved L=R
                with self._tx_lock:
                    if self._ws is None:
                        break
                    try:
                        self._ws.send(build_frame(stereo, self.audio_rate))
                    except Exception as exc:
                        self.error = str(exc)
                        break
                self.tx_frames += 1
                # pace the stream to real time so the server's buffer stays fed
                target = t0 + (i + chunk.size) / float(self.audio_rate)
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(min(delay, 0.25))
        finally:
            time.sleep(0.15)
            self.set_ptt(False)
        return round(airtime, 3)

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict:
        return {
            "connected": self.connected,
            "url": self.url,
            "device": self.handshake.get("device", ""),
            "protocol": self.handshake.get("protocol", ""),
            "ready": self.handshake.get("ready", "") == "1",
            "vfo_hz": self.vfo_hz,
            "mode": self.mode,
            "ptt": self.ptt,
            "audio_rate": self.audio_rate,
            "rx_frames": self.rx_frames,
            "tx_frames": self.tx_frames,
            "uptime_s": round(time.time() - self.started, 1) if self.started else 0.0,
            "error": self.error,
            "log": [f"{t} {m}" for t, m in list(self.log)[-30:]],
        }


def status_json(client: TciClient | None) -> dict:
    if client is None:
        return {"connected": False}
    return client.status()
