"""Local web console for the modem.

    python -m halcyon.ui --port 8730

Binds loopback only.  The page is one static file, the API is a handful of JSON
endpoints, and nothing leaves the machine: the browser posts a message, this
process frames it, runs it through the simulated channel, decodes it and sends
back what the receiver saw.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import threading
import time
import wave as wavemod
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from . import config as settings
from .channel import make_channel
from .frame import MAX_BLOCKS, chunk_bits, header_repeat, info_rate_bps, mcs, table_for
from .hl2 import HermesLite, discover, spectrum_row
from .link import Link, LinkConfig
from .tci import TciClient
from .host import HostProtocol, HostServer
from .modem import Modem
from .ofdm import COMPACT, DEEP, MICRO, MINI, NANO, ROBUST, Phy

ROOT = Path(__file__).resolve().parent
STATION = ROOT / "ui" / "station.html"   # the terminal: waterfall, speed levels, transcript
INDEX = ROOT / "ui" / "index.html"  # the bench console: frame anatomy, ladder chart
MAX_BODY = 256 * 1024
CHANNELS = ("awgn", "good", "moderate", "poor")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str | None) -> str:
    """Host part of an Origin or Host header, without the port."""
    if not value:
        return ""
    text = value if "//" in value else "//" + value
    return (urlparse(text).hostname or "").lower()


def _origin_allowed(origin: str | None, host_header: str | None, bound: str = "127.0.0.1") -> bool:
    """True when a POST may act on this console.

    This API can key a transmitter, so a page the operator happens to have open
    must not reach it.  Two headers are checked, because neither can be forged by
    a browser: ``Origin`` refuses a page served from anywhere but this machine,
    and ``Host`` refuses the DNS-rebinding form of the same trick.  The caller
    also requires ``Content-Type: application/json``, which makes a cross-origin
    request need a preflight that this server never answers.
    """
    allowed = _LOOPBACK_HOSTS | {str(bound).lower()}
    if origin not in (None, "", "null"):
        host = _hostname(origin)
        if host and host not in allowed:
            return False
    host = _hostname(host_header)
    if host and host not in allowed:
        return False
    return True

# (label, band caption, geometry factory)
PROFILES: dict[str, tuple[str, str, object]] = {
    "wide": ("Wide", "2.75 kHz", Phy),
    "robust": ("Robust", "562 Hz", lambda: Phy(**ROBUST)),
    "compact": ("Compact", "492 Hz", lambda: Phy(**COMPACT)),
    "deep": ("Deep test", "375 Hz", lambda: Phy(**DEEP)),
    "nano": ("Nano test", "234 Hz", lambda: Phy(**NANO)),
    "mini": ("Mini test", "225 Hz", lambda: Phy(**MINI)),
    "micro": ("Micro test", "175 Hz", lambda: Phy(**MICRO)),
}

_MODEMS: dict[str, Modem] = {}
_LOCK = threading.Lock()


def modem_for(profile: str) -> Modem:
    """One modem (and so one set of code tables) per geometry, built once."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    got = _MODEMS.get(profile)
    if got is None:
        got = Modem(PROFILES[profile][2]())
        _MODEMS[profile] = got
    return got


# --------------------------------------------------------------------- payloads
def modes_payload() -> dict:
    profiles = []
    for pid, (label, band, _) in PROFILES.items():
        phy = modem_for(pid).phy
        rows = []
        for i, m in enumerate(table_for(phy)):
            cap = chunk_bits(phy, i)
            rows.append(
                {
                    "i": i,
                    "label": m.label,
                    "bps": round(info_rate_bps(phy, i)),
                    "thr": m.snr_channel,
                    "rep": m.rep,
                    "bm": m.block_mult,
                    "kind": m.kind,
                    "tones": int(getattr(m, "tone_bits", 4)),
                    "uncoded": bool(m.uncoded),
                    "cap_bytes": cap // 8,
                    "max_bytes": cap * MAX_BLOCKS // 8,
                }
            )
        profiles.append(
            {
                "id": pid,
                "label": label,
                "band": band,
                "bw_hz": round(phy.bandwidth_hz, 1),
                "carriers": int(phy.active.size),
                "data_carriers": int(phy.n_data),
                "sym_ms": round(phy.sym_time * 1e3, 2),
                "modes": rows,
            }
        )
    return {
        "profiles": profiles,
        "channels": list(CHANNELS),
        "fcc_hz": 2800.0,
        "max_blocks": MAX_BLOCKS,
    }


def _waterfall(rx: np.ndarray, fs: float, cols: int, rows: int, span_hz: float = 3200.0) -> dict | None:
    """Time-frequency picture of the capture, 8-bit quantised, time across x."""
    win = 512
    n = int(rx.size)
    if n < win:
        return None
    hop = max(1, (n - win) // rows)
    starts = np.arange(0, n - win + 1, hop)[:rows]
    seg = np.stack([rx[i : i + win] for i in starts])
    P = np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2
    centre = win // 2
    half = int(round(span_hz / 2.0 / (fs / win)))
    band = P[:, max(0, centre - half) : centre + half]
    edges = np.linspace(0, band.shape[1], cols + 1).astype(int)
    cols_db = np.stack(
        [10.0 * np.log10(np.maximum(band[:, edges[i] : edges[i + 1]].mean(axis=1), 1e-20))
         for i in range(cols)],
        axis=1,
    )  # (time, freq)
    lo, hi = np.percentile(cols_db, 15), np.percentile(cols_db, 99.5)
    q = np.clip((cols_db - lo) / max(float(hi - lo), 1e-9), 0.0, 1.0)
    img = (q.T[::-1] * 255).astype(np.uint8)  # rows: frequency, high at the top
    return {
        "w": int(img.shape[1]),
        "h": int(img.shape[0]),
        "span_hz": span_hz,
        "data": base64.b64encode(img.tobytes()).decode(),
    }


def _attempt(res, data: bytes) -> dict:
    payload = bytes(res.payload or b"")
    return {
        "header_ok": bool(res.header_ok),
        "blocks_ok": [bool(b) for b in getattr(res, "blocks_ok", [])],
        "decoded": payload.decode("utf-8", "replace"),
        "match": payload == data,
        "ok": bool(res.ok),
        "bytes": len(payload),
        "reported_snr": round(float(res.snr_db), 2),
        "cfo_hz": round(float(res.cfo_hz), 1),
        "iterations": int(res.header_iterations),
    }


def _decode(req: dict) -> dict:
    profile = str(req.get("profile", "wide"))
    modem = modem_for(profile)
    phy = modem.phy
    table = table_for(phy)
    idx = int(np.clip(int(req.get("mcs", 0)), 0, len(table) - 1))
    text = str(req.get("text", ""))[:4096]
    snr = float(req.get("snr", 8.0))
    kind = str(req.get("channel", "awgn"))
    if kind not in CHANNELS:
        kind = "awgn"
    cfo = float(np.clip(float(req.get("cfo", 0.0)), -90.0, 90.0))
    looks = 2 if req.get("looks") else 1

    cap_bytes = chunk_bits(phy, idx) * MAX_BLOCKS // 8
    data = text.encode("utf-8")[:cap_bytes]
    if not data:
        data = b"halcyon"
    tx, blocks = modem.build(data, idx, seq=1)
    seed = int(req.get("seed", 7)) & 0x7FFFFFFF
    ch = make_channel(kind, snr, seed=seed, bandwidth_hz=phy.bandwidth_hz, cfo_hz=cfo)
    rx = ch(tx)

    t0 = time.perf_counter()
    first = modem.receive(rx)
    attempts = [{"label": "first look", **_attempt(first, data)}]
    # Only spend the second look when the first one actually failed: re-decoding
    # a block that already passed cannot add anything.  It is not reliably better
    # either -- measured at 7.0 dB on QPSK 7/8 (2.5 dB under its threshold) the
    # stored LLRs converged at 1x but not at 0.5x or 2x, while 8 and 9 dB
    # converged at every scale.  That is marginal-decision sensitivity in the
    # sum-product stage, so the combined look is offered only where it is the
    # last chance, exactly as the link layer uses it.
    if looks > 1 and not first.ok and getattr(first, "block_llrs", None):
        second = modem.receive(rx, prior=first.block_llrs)
        attempts.append({"label": "summed with the stored look", **_attempt(second, data)})
    elapsed = time.perf_counter() - t0

    m = table[idx]
    fs = float(phy.fs)
    hdr_rep = header_repeat(m, phy)
    pre_s = phy.preamble.size / fs
    hdr_s = phy.header_syms * hdr_rep * phy.sym_len / fs
    pay_s = max(0.0, tx.size / fs - pre_s - hdr_s)
    return {
        "profile": profile,
        "channel": kind,
        "snr": snr,
        "cfo_set": cfo,
        "seed": seed,
        "trimmed": len(text.encode("utf-8")) > len(data),
        "sent": {
            "bytes": len(data),
            "text": data.decode("utf-8", "replace"),
            "mcs": idx,
            "label": m.label,
            "bps": round(info_rate_bps(phy, idx)),
            "thr": m.snr_channel,
            "rep": m.rep,
            "block_mult": m.block_mult,
            "kind": m.kind,
            "uncoded": bool(m.uncoded),
            "header_repeat": hdr_rep,
            "blocks": blocks,
            "block_bits": chunk_bits(phy, idx),
        },
        "timing": {
            "total_s": round(tx.size / fs, 3),
            "preamble_s": round(pre_s, 3),
            "header_s": round(hdr_s, 3),
            "payload_s": round(pay_s, 3),
            "samples": int(tx.size),
        },
        "attempts": attempts,
        "elapsed_s": round(elapsed, 2),
        "waterfall": _waterfall(rx, fs, cols=64, rows=420),
    }


def _link(req: dict) -> dict:
    profile = str(req.get("profile", "wide"))
    modem = modem_for(profile)
    phy = modem.phy
    table = table_for(phy)
    idx = int(np.clip(int(req.get("mcs", 4)), 0, len(table) - 1))
    text = str(req.get("text", ""))[:24]
    data = text.encode("utf-8") or b"halcyon"
    snr = float(req.get("snr", 10.0))
    kind = str(req.get("channel", "awgn"))
    if kind not in CHANNELS:
        kind = "awgn"
    cfg = LinkConfig(
        start_mcs=idx,
        max_blocks=2,
        max_attempts=4,
        adaptive=bool(req.get("adaptive", True)),
        probe_first=False,
    )
    link = Link(
        modem,
        cfg,
        lambda s: make_channel(
            kind, snr, seed=(int(s) * 7919 + 13) & 0x7FFFFFFF, bandwidth_hz=phy.bandwidth_hz
        ),
        seed=3,
    )
    t0 = time.perf_counter()
    st = link.run(data)
    return {
        "profile": profile,
        "channel": kind,
        "snr": snr,
        "bytes": len(data),
        "text": data.decode("utf-8", "replace"),
        "complete": bool(st.complete),
        "delivered_bits": int(st.delivered_bits),
        "total_bits": int(st.total_bits),
        "frames": int(st.frames),
        "retransmissions": int(st.retransmissions),
        "header_failures": int(st.header_failures),
        "airtime_s": round(st.airtime_s, 2),
        "goodput_bps": round(st.goodput_bps, 1),
        "mean_mcs": round(st.mean_mcs, 2),
        "mean_snr_db": round(st.mean_snr_db, 2),
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


def _audio(params: dict) -> bytes:
    """The burst as a playable WAV, shifted up to 1.5 kHz so it is audible."""
    profile = str(params.get("profile", "wide"))
    modem = modem_for(profile)
    phy = modem.phy
    table = table_for(phy)
    idx = int(np.clip(int(params.get("mcs", 0)), 0, len(table) - 1))
    text = str(params.get("text", ""))[:4096] or "halcyon"
    data = text.encode("utf-8")[: chunk_bits(phy, idx) * MAX_BLOCKS // 8]
    tx, _ = modem.build(data, idx, seq=1)
    fs = int(phy.fs)
    n = np.arange(tx.size)
    audio = np.real(tx * np.exp(2j * np.pi * 1500.0 * n / fs))
    peak = float(np.max(np.abs(audio))) or 1.0
    pcm = np.clip(audio / peak * 0.89, -1.0, 1.0)
    buf = io.BytesIO()
    with wavemod.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


# ----------------------------------------------------------------------- radio
# One Hermes Lite 2 at a time.  The radio has its own lock and its own threads;
# the modem lock is deliberately not held across radio calls, because capturing
# three seconds of air would otherwise block every decode in the console.
_RADIO: HermesLite | None = None
_RLOCK = threading.Lock()


def radio() -> HermesLite | None:
    with _RLOCK:
        r = _RADIO
    return r if (r is not None and r.connected) else None


def radio_status() -> dict:
    r = radio()
    if r is None:
        return {"connected": False}
    return r.status()


def radio_spectrum() -> dict:
    r = radio()
    if r is None:
        raise ValueError("no radio connected")
    row = spectrum_row(r.recent(0.08), 400)
    return {
        "width": len(row),
        "row": base64.b64encode(bytes(row)).decode(),
        "rate": r.rate,
        "freq_hz": r.freq_hz,
        "ptt": bool(r.status()["ptt"]),
    }


def _resample(x: np.ndarray, rate_in: float, rate_out: float) -> np.ndarray:
    """Radio IQ at rate_in -> the modem's own sample rate."""
    x = np.asarray(x, dtype=np.complex64)
    if x.size == 0 or int(rate_in) == int(rate_out):
        return x
    frac = Fraction(int(rate_out), int(rate_in)).limit_denominator(2000)
    try:
        from scipy.signal import resample_poly

        return resample_poly(x, frac.numerator, frac.denominator).astype(np.complex64)
    except Exception:  # scipy absent: linear interpolation, adequate for a display
        n = max(2, int(round(x.size * float(frac.numerator) / frac.denominator)))
        t = np.linspace(0.0, 1.0, x.size, endpoint=False)
        u = np.linspace(0.0, 1.0, n, endpoint=False)
        return (np.interp(u, t, x.real) + 1j * np.interp(u, t, x.imag)).astype(np.complex64)


def _radio_discover(req: dict) -> dict:
    return {"radios": discover(float(req.get("timeout", 1.2)))}


def _radio_connect(req: dict) -> dict:
    global _RADIO
    ip = str(req.get("ip", "")).strip()
    if not ip:
        raise ValueError("a radio address is required")
    with _RLOCK:
        if _RADIO is not None:
            _RADIO.close()
            _RADIO = None
        r = HermesLite(
            ip,
            rate=int(req.get("rate", 48_000)),
            freq_hz=float(req.get("freq_hz", 14_105_000.0)),
            lna_db=int(req.get("lna_db", 16)),
            drive=int(req.get("drive", 8)),
            receivers=int(req.get("receivers", 1)),
            mac=str(req.get("mac", "")),
            board=str(req.get("board", "")),
            firmware=str(req.get("firmware", "")),
        )
        try:
            r.open()
        except OSError as exc:
            raise ValueError(f"could not open the radio at {ip}: {exc}") from exc
        # A UDP socket opens against any address, so prove the radio is really
        # there: wait for it to start streaming IQ before calling this connected.
        deadline = time.time() + 2.5
        while time.time() < deadline and not r.rx_frames:
            time.sleep(0.05)
        if not r.rx_frames:
            r.close()
            raise ValueError(
                f"no frames from {ip}:1024 — check the address, the power and the subnet"
            )
        _RADIO = r
    return radio_status()


def _radio_disconnect(req: dict) -> dict:
    global _RADIO
    with _RLOCK:
        if _RADIO is not None:
            _RADIO.close()
            _RADIO = None
    return {"connected": False}


def _radio_set(req: dict) -> dict:
    r = radio()
    if r is None:
        raise ValueError("no radio connected")
    if "ptt" in req:
        r.key(bool(req["ptt"]))
        return r.status()
    r.configure(
        rate=req.get("rate"),
        freq_hz=req.get("freq_hz"),
        lna_db=req.get("lna_db"),
        drive=req.get("drive"),
    )
    return r.status()


def _radio_decode(req: dict) -> dict:
    r = radio()
    if r is None:
        raise ValueError("no radio connected")
    profile = str(req.get("profile", "wide"))
    modem = modem_for(profile)
    phy = modem.phy
    table = table_for(phy)
    idx = int(np.clip(int(req.get("mcs", 4)), 0, len(table) - 1))
    seconds = float(np.clip(float(req.get("seconds", 3.0)), 0.5, 8.0))
    raw = r.capture(seconds, timeout=seconds + 3.0)
    fs = float(phy.fs)
    rx = _resample(raw, r.rate, fs)
    t0 = time.perf_counter()
    res = modem.receive(rx) if rx.size else None
    elapsed = time.perf_counter() - t0
    payload = bytes(getattr(res, "payload", b"") or b"") if res is not None else b""
    return {
        "source": "radio",
        "profile": profile,
        "mcs": idx,
        "label": table[idx].label,
        "captured_s": round(raw.size / float(r.rate), 2),
        "attempts": [
            {
                "label": "on air",
                "header_ok": bool(getattr(res, "header_ok", False)),
                "blocks_ok": [bool(b) for b in getattr(res, "blocks_ok", [])],
                "decoded": payload.decode("utf-8", "replace"),
                "ok": bool(getattr(res, "ok", False)),
                "bytes": len(payload),
                "reported_snr": round(float(getattr(res, "snr_db", 0.0)), 2),
                "cfo_hz": round(float(getattr(res, "cfo_hz", 0.0)), 1),
            }
        ],
        "elapsed_s": round(elapsed, 2),
        "waterfall": _waterfall(rx.real, fs, cols=64, rows=420) if rx.size else None,
        "status": radio_status(),
    }


def _radio_transmit(req: dict) -> dict:
    r = radio()
    if r is None:
        raise ValueError("no radio connected")
    profile = str(req.get("profile", "wide"))
    modem = modem_for(profile)
    phy = modem.phy
    table = table_for(phy)
    idx = int(np.clip(int(req.get("mcs", 0)), 0, len(table) - 1))
    text = str(req.get("text", ""))[:4096]
    cap_bytes = chunk_bits(phy, idx) * MAX_BLOCKS // 8
    data = text.encode("utf-8")[:cap_bytes] or b"halcyon"
    if req.get("drive") is not None:
        r.configure(drive=int(req["drive"]))
    tx, blocks = modem.build(data, idx, seq=1)
    air = r.transmit(tx.astype(np.complex64), float(phy.fs))
    return {
        "source": "radio",
        "profile": profile,
        "airtime_s": air,
        "drive": r.drive,
        "sent": {
            "bytes": len(data),
            "text": data.decode("utf-8", "replace"),
            "mcs": idx,
            "label": table[idx].label,
            "bps": round(info_rate_bps(phy, idx)),
            "blocks": blocks,
        },
        "status": radio_status(),
    }


# -------------------------------------------------------------------- services
# The modem host is what PAT and VarAC attach to; TCI is the rig side.  Both own
# their sockets and threads, so neither is run under the modem lock.
_HOST: HostServer | None = None
_TCI: TciClient | None = None
_SLOCK = threading.Lock()


def services_status() -> dict:
    with _SLOCK:
        v = _HOST
    host = v.status() if v is not None else {"running": False, "protocol": {"linked": False}}
    return {"host": host}


def _services_start(req: dict) -> dict:
    global _HOST
    with _SLOCK:
        if _HOST is not None and _HOST.running:
            return services_status()
        proto = HostProtocol(
            mycall=str(req.get("mycall", "N0CALL")).upper() or "N0CALL",
            bandwidth=int(req.get("bandwidth", 2300)),
            snr=float(req.get("snr", 10.0)),
            channel=str(req.get("channel", "awgn")),
        )
        srv = HostServer(
            cmd_port=int(req.get("cmd_port", 8300)),
            data_port=int(req.get("data_port", 8301)),
            protocol=proto,
        )
        try:
            srv.start()
        except OSError as exc:
            raise ValueError(
                f"could not open the host ports {srv.cmd_port}/{srv.data_port}: {exc} "
                f"— is another modem already listening?"
            ) from exc
        _HOST = srv
    return services_status()


def _services_stop(req: dict) -> dict:
    global _HOST
    with _SLOCK:
        if _HOST is not None:
            _HOST.stop()
            _HOST = None
    return services_status()


def _services_command(req: dict) -> dict:
    with _SLOCK:
        v = _HOST
    if v is None or not v.running:
        raise ValueError("the modem host is not running")
    line = str(req.get("line", ""))[:200]
    if line:
        v.protocol.handle(line)
    cur = str(req.get("snr", ""))
    if cur:
        try:
            v.protocol.snr = float(cur)
        except ValueError:
            pass
    return services_status()


def tci_status() -> dict:
    with _SLOCK:
        c = _TCI
    if c is None:
        return {"connected": False}
    return c.status()


def _tci_connect(req: dict) -> dict:
    global _TCI
    url = str(req.get("url", "ws://127.0.0.1:50001"))
    with _SLOCK:
        if _TCI is not None:
            _TCI.close()
            _TCI = None
        client = TciClient(url=url)
        try:
            client.open()
        except Exception as exc:
            raise ValueError(f"could not open TCI at {url}: {exc}") from exc
        if req.get("freq_hz"):
            client.set_vfo(float(req["freq_hz"]))
        if req.get("mode"):
            client.set_mode(str(req["mode"]))
        _TCI = client
    time.sleep(0.2)
    return tci_status()


def _tci_set(req: dict) -> dict:
    with _SLOCK:
        c = _TCI
    if c is None or not c.connected:
        raise ValueError("no TCI session")
    if "ptt" in req:
        c.set_ptt(bool(req["ptt"]))
    if "freq_hz" in req:
        c.set_vfo(float(req["freq_hz"]))
    if "mode" in req:
        c.set_mode(str(req["mode"]))
    return c.status()


def _tci_disconnect(req: dict) -> dict:
    global _TCI
    with _SLOCK:
        if _TCI is not None:
            _TCI.close()
            _TCI = None
    return {"connected": False}


# ------------------------------------------------------------------------ cat
# Hamlib owns the rig; the modem only ever reads audio.  rigctld holds the
# serial port, so CAT must not sit behind the decode lock -- a frequency read
# is a network round trip, not a modem operation.
from .cat import RigError, RigService, hamlib_version, list_rigs  # noqa: E402

_CAT: RigService | None = None
_CATLOCK = threading.Lock()


def cat_status() -> dict:
    with _CATLOCK:
        svc = _CAT
    st = svc.status() if svc is not None else {"connected": False, "rigctld": False}
    st["hamlib"] = hamlib_version()
    return st


def _cat_connect(req: dict) -> dict:
    global _CAT
    host = str(req.get("host") or "127.0.0.1")
    port = int(req.get("port") or 4532)
    model = req.get("model")
    device = str(req["device"]) if req.get("device") else None
    baud = int(req["baud"]) if req.get("baud") else None
    with _CATLOCK:
        if _CAT is not None:
            _CAT.stop()
            _CAT = None
        svc = RigService()
        try:
            svc.connect(host=host, port=port, model=model, device=device, baud=baud)
        except Exception as exc:
            svc.stop()
            raise ValueError(str(exc)) from exc
        _CAT = svc
    # A CAT session is usually opened in order to move the rig, so apply the
    # requested frequency and mode before reporting success.
    try:
        if req.get("freq_hz"):
            svc.rig.set_freq(float(req["freq_hz"]))
        if req.get("mode"):
            svc.rig.set_mode(str(req["mode"]))
    except RigError as exc:
        raise ValueError(str(exc)) from exc
    return cat_status()


def _cat_set(req: dict) -> dict:
    with _CATLOCK:
        svc = _CAT
    if svc is None or not svc.connected:
        raise ValueError("no CAT session")
    try:
        if "freq_hz" in req:
            svc.rig.set_freq(float(req["freq_hz"]))
        if "mode" in req:
            svc.rig.set_mode(str(req["mode"]), int(req.get("passband") or 0))
        if "vfo" in req:
            svc.rig.set_vfo(str(req["vfo"]))
        if "ptt" in req:
            svc.rig.set_ptt(bool(req["ptt"]))
    except RigError as exc:
        raise ValueError(str(exc)) from exc
    return svc.status()


def _cat_command(req: dict) -> dict:
    with _CATLOCK:
        svc = _CAT
    if svc is None or not svc.connected or svc.rig is None:
        raise ValueError("no CAT session")
    line = str(req.get("line") or "").strip()
    if not line:
        raise ValueError("empty command")
    if len(line) > 120:
        raise ValueError("command too long")
    try:
        reply = svc.rig.raw(line)
    except RigError as exc:
        raise ValueError(f"{exc} (sent {line!r})") from exc
    return {"reply": reply, "status": svc.status()}


def _cat_disconnect(req: dict) -> dict:
    global _CAT
    with _CATLOCK:
        if _CAT is not None:
            _CAT.stop()
            _CAT = None
    return {"connected": False}


# ----------------------------------------------------------------------- server
class Api(BaseHTTPRequestHandler):
    server_version = "HalcyonUI/0.1"
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _fail(self, msg, code: int = 400) -> None:
        self._json({"error": str(msg)}, code)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        # Reads are checked too: a name that resolves to loopback (DNS
        # rebinding) must not be able to read the console's state.
        if not _origin_allowed(
            None, self.headers.get("Host"), str(self.server.server_address[0])
        ):
            self.close_connection = True
            return self._fail("cross-origin requests are refused", 403)
        pages = {"/": STATION, "/station": STATION}
        if settings.developer():
            # The lab console is an instrument, not part of the terminal: it is
            # served only when the config file asks for it, so that a visitor
            # cannot reach a page of knobs that looks like the product.
            pages.update({"/console": INDEX, "/index.html": INDEX})
        page = pages.get(u.path)
        if page is not None:
            try:
                body = page.read_bytes()
            except OSError:
                return self._fail(f"{page.name} is missing", 500)
            return self._send(body, "text/html; charset=utf-8")
        if u.path == "/api/config":
            return self._json({"developer": settings.developer(),
                               "config": str(settings.path())})
        if u.path == "/api/modes":
            with _LOCK:
                return self._json(modes_payload())
        if u.path == "/api/audio":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                with _LOCK:
                    wav = _audio(q)
            except Exception as exc:  # pragma: no cover - surfaced to the page
                return self._fail(exc)
            return self._send(wav, "audio/wav")
        if u.path == "/api/radio":
            return self._json(radio_status())
        if u.path == "/api/radio/spectrum":
            try:
                return self._json(radio_spectrum())
            except ValueError:
                # Asking for spectrum with no radio is a state, not a failure.  The
                # panel polls this feed, so a 400 every 180 ms would only fill the
                # console; /api/radio carries the real connection status.
                return self._json({"row": None, "connected": False, "width": 0})
        if u.path == "/api/services":
            return self._json(services_status())
        if u.path == "/api/tci":
            return self._json(tci_status())
        if u.path == "/api/cat":
            return self._json(cat_status())
        if u.path == "/api/cat/rigs":
            q = (parse_qs(u.query).get("q") or [""])[0]
            try:
                return self._json({"rigs": list_rigs(q)})
            except Exception as exc:  # pragma: no cover - surfaced to the page
                return self._fail(exc, 500)
        return self._fail("not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # A simple cross-origin request cannot set this header, so refusing
            # anything else is what stops a visited page from driving the API.
            self.close_connection = True
            return self._fail("Content-Type must be application/json", 415)
        if not _origin_allowed(
            self.headers.get("Origin"),
            self.headers.get("Host"),
            str(self.server.server_address[0]),
        ):
            self.close_connection = True
            return self._fail("cross-origin requests are refused", 403)
        if self.headers.get("Transfer-Encoding"):
            # A chunked body has no length this handler can bound, and reading
            # it as if it did would leave framing bytes in the socket.
            self.close_connection = True
            return self._fail("chunked bodies are not supported", 411)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail("bad Content-Length")
        if length < 0:
            self.close_connection = True
            return self._fail("bad Content-Length")
        if length > MAX_BODY:
            self.close_connection = True
            return self._fail("request too large", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._fail("body is not JSON")
        if not isinstance(req, dict):
            return self._fail("body must be a JSON object")
        route = {
            "/api/decode": _decode,
            "/api/link": _link,
            "/api/radio/discover": _radio_discover,
            "/api/radio/connect": _radio_connect,
            "/api/radio/disconnect": _radio_disconnect,
            "/api/radio/set": _radio_set,
            "/api/radio/decode": _radio_decode,
            "/api/radio/transmit": _radio_transmit,
            "/api/services/start": _services_start,
            "/api/services/stop": _services_stop,
            "/api/services/command": _services_command,
            "/api/tci/connect": _tci_connect,
            "/api/tci/set": _tci_set,
            "/api/tci/disconnect": _tci_disconnect,
            "/api/cat/connect": _cat_connect,
            "/api/cat/set": _cat_set,
            "/api/cat/command": _cat_command,
            "/api/cat/disconnect": _cat_disconnect,
        }.get(u.path)
        if route is None:
            return self._fail("not found", 404)
        try:
            # Radio calls get the radio's own lock: a three-second capture must
            # not block every decode in the console.
            if u.path.startswith(
                ("/api/radio", "/api/services", "/api/tci", "/api/cat")
            ):
                out = route(req)
            else:
                with _LOCK:
                    out = route(req)
        except ValueError as exc:
            return self._fail(exc)
        except Exception as exc:  # pragma: no cover - reported in the page
            return self._fail(f"{type(exc).__name__}: {exc}", 500)
        return self._json(out)

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Halcyon modem console (loopback only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8730)
    args = ap.parse_args(argv)
    if args.host not in _LOOPBACK_HOSTS:
        # The API can key a transmitter, and the origin checks refuse every POST
        # from a page not served on the loopback address: binding a network
        # interface would only produce a page that half works.
        ap.error(
            "the console binds loopback only; it is a local control surface, "
            "not a network service"
        )
    srv = ThreadingHTTPServer((args.host, args.port), Api)
    srv.daemon_threads = True
    print(f"Halcyon console: http://{args.host}:{args.port}/   (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
