"""The Qt 6 window front end, built and run against a stub console."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
from pathlib import Path

import pytest

from test_tui_cpp import Stub  # noqa: F401  (the same stub console as the TUI)

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "cpp" / "build" / "halcyon-gui"


def qt_available() -> bool:
    if shutil.which("pkg-config") is None:
        return False
    return (
        subprocess.run(
            ["pkg-config", "--exists", "Qt6Widgets", "Qt6Network"],
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.fixture(scope="module")
def gui():
    if not qt_available() or shutil.which("make") is None or (
        shutil.which("g++") is None and shutil.which("clang++") is None
    ):
        pytest.skip("Qt 6 Widgets/Network or a C++ toolchain is not available")
    built = subprocess.run(
        ["make", "-C", str(ROOT / "cpp"), "gui"],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assert BINARY.is_file()
    return BINARY


@pytest.fixture()
def stub():
    import threading
    from http.server import ThreadingHTTPServer

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


def run(gui, stub, *args, url=None):
    base = url or f"http://127.0.0.1:{stub.server_address[1]}"
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [str(gui), "--url", base, *args],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )


def test_check_reports_the_console_state(gui, stub):
    result = run(gui, stub, "--check")

    assert result.returncode == 0, result.stderr
    assert "modes: 1 profiles" in result.stdout
    assert "QPSK 1/2" in result.stdout
    assert "HL-2" in result.stdout
    assert "14.105000 MHz" in result.stdout
    assert "host  running" in result.stdout
    assert "CAT  no session" in result.stdout


def test_send_goes_through_the_console_api(gui, stub):
    result = run(gui, stub, "--check", "--send", "cq test")

    assert result.returncode == 0, result.stderr
    assert "tx  cq test" in result.stdout
    # The stub decodes to upper case, proving the reply is displayed.
    assert "rx  CQ TEST" in result.stdout

    posts = [entry for entry in stub.seen if entry["path"] == "/api/decode"]
    assert len(posts) == 1
    assert posts[0]["content_type"] == "application/json"
    assert posts[0]["body"]["text"] == "cq test"
    assert posts[0]["body"]["profile"] == "wide"


def test_an_unreachable_console_is_a_clear_failure(gui, stub):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = run(gui, stub, "--check", url=f"http://127.0.0.1:{port}")

    assert result.returncode == 1
    assert "cannot reach the console" in result.stderr


def test_the_window_renders_and_saves_a_png(gui, stub, tmp_path):
    shot = tmp_path / "window.png"
    result = run(gui, stub, "--screenshot", str(shot))

    assert result.returncode == 0, result.stderr
    assert shot.is_file()
    raw = shot.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", raw[16:24])
    assert width >= 800 and height >= 500


def test_help_mentions_the_terminal_alternative(gui, stub):
    result = subprocess.run([str(gui), "--help"], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert "usage: halcyon-gui" in result.stdout
    assert "halcyon-tui" in result.stdout
