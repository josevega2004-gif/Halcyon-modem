"""The C++ terminal front end, driven against a stub console."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "cpp" / "build" / "halcyon-tui"

MODES = {
    "profiles": [
        {
            "id": "wide",
            "label": "Wide",
            "band": "2.75 kHz",
            "bw_hz": 2742.0,
            "modes": [
                {"i": 0, "label": "QPSK 1/4 r4", "bps": 227, "thr": -3.0},
                {"i": 1, "label": "QPSK 1/2", "bps": 1812, "thr": 2.6},
            ],
        }
    ],
    "channels": ["awgn"],
}

RADIO = {
    "connected": True,
    "board": "HL-2",
    "firmware": "73",
    "mac": "00:1c:0d:00:00:01",
    "freq_hz": 14_105_000,
    "rate": 48_000,
    "lna_db": 16,
    "drive": 8,
    "ptt": False,
}

SPECTRUM = bytes(range(8, 200, 24))


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _json(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/modes":
            return self._json(MODES)
        if path == "/api/radio":
            return self._json(RADIO)
        if path == "/api/services":
            return self._json({"host": {"running": True, "protocol": {"linked": False}}})
        if path == "/api/cat":
            return self._json({"connected": False, "rigctld": False})
        if path == "/api/tci":
            return self._json({"connected": False})
        if path == "/api/radio/spectrum":
            return self._json(
                {
                    "width": len(SPECTRUM),
                    "row": base64.b64encode(SPECTRUM).decode(),
                    "connected": True,
                    "freq_hz": RADIO["freq_hz"],
                    "ptt": False,
                }
            )
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.seen.append(
            {
                "path": path,
                "content_type": self.headers.get("Content-Type"),
                "body": json.loads(raw or b"{}"),
            }
        )
        request = json.loads(raw or b"{}")
        if path == "/api/decode":
            text = str(request.get("text", ""))
            blocks = bytes((i * 7) % 256 for i in range(32))
            return self._json(
                {
                    "profile": request.get("profile", "wide"),
                    "sent": {
                        "bytes": len(text),
                        "text": text,
                        "mcs": request.get("mcs", 0),
                        "label": "QPSK 1/2",
                        "bps": 1812,
                        "blocks": 1,
                    },
                    "attempts": [
                        {
                            "label": "first look",
                            "header_ok": True,
                            "blocks_ok": [True],
                            "decoded": text.upper(),
                            "match": True,
                            "ok": True,
                            "bytes": len(text),
                            "reported_snr": 12.5,
                            "cfo_hz": 0.0,
                            "iterations": 3,
                        }
                    ],
                    "timing": {"total_s": 0.4},
                    "elapsed_s": 0.1,
                    "trimmed": False,
                    "waterfall": {
                        "w": 8,
                        "h": 4,
                        "data": base64.b64encode(blocks).decode(),
                    },
                }
            )
        if path == "/api/link":
            return self._json(
                {
                    "complete": True,
                    "goodput_bps": 812.3,
                    "frames": 4,
                    "retransmissions": 0,
                    "bytes": 24,
                }
            )
        if path == "/api/radio/discover":
            return self._json(
                {
                    "radios": [
                        {
                            "ip": "10.0.0.2",
                            "board": "HL-2",
                            "mac": RADIO["mac"],
                            "firmware": RADIO["firmware"],
                        }
                    ]
                }
            )
        if path == "/api/radio/connect":
            return self._json(RADIO)
        if path == "/api/radio/disconnect":
            return self._json({"connected": False})
        return self._json({"error": "not found"}, 404)


@pytest.fixture(scope="module")
def tui():
    if shutil.which("make") is None or (
        shutil.which("g++") is None and shutil.which("clang++") is None
    ):
        pytest.skip("no C++ toolchain to build the terminal front end")
    built = subprocess.run(
        ["make", "-C", str(ROOT / "cpp"), "tui"],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assert BINARY.is_file()
    return BINARY


@pytest.fixture()
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.daemon_threads = True
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run(tui, stub, *args, url=None):
    base = url or f"http://127.0.0.1:{stub.server_address[1]}"
    return subprocess.run(
        [str(tui), "--url", base, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_once_draws_the_station(tui, stub):
    result = run(tui, stub, "--once")

    assert result.returncode == 0, result.stderr
    assert "HL-2" in result.stdout
    assert "14.105000 MHz" in result.stdout
    assert "QPSK 1/2" in result.stdout
    assert "host   running" in result.stdout
    assert "CAT    no session" in result.stdout
    assert "spectrum" in result.stdout


def test_send_goes_through_the_console_api(tui, stub):
    result = run(tui, stub, "--once", "--send", "cq test")

    assert result.returncode == 0, result.stderr
    assert "tx  cq test" in result.stdout
    # The stub decodes to upper case, so seeing this proves the reply is shown
    # rather than the text the user typed.
    assert "rx  CQ TEST" in result.stdout
    assert "12.5 dB" in result.stdout

    posts = [entry for entry in stub.seen if entry["path"] == "/api/decode"]
    assert len(posts) == 1
    assert posts[0]["content_type"] == "application/json"
    assert posts[0]["body"]["text"] == "cq test"
    assert posts[0]["body"]["profile"] == "wide"
    assert posts[0]["body"]["mcs"] == 1  # level 4 clamps to the stub's two rungs


def test_link_runs_and_reports_goodput(tui, stub):
    result = run(tui, stub, "--once", "--link", "cq de halcyon")

    assert result.returncode == 0, result.stderr
    assert "link complete" in result.stdout
    assert "812.3 bps" in result.stdout


def test_an_unreachable_console_is_a_clear_failure(tui, stub):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = run(tui, stub, "--once", url=f"http://127.0.0.1:{port}")

    assert result.returncode == 1
    assert "cannot reach the console" in result.stderr
    assert "python -m halcyon.ui" in result.stderr


def test_help_names_the_keys(tui, stub):
    result = subprocess.run([str(tui), "--help"], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert "usage: halcyon-tui" in result.stdout
    assert "m message" in result.stdout
