"""The console's server-side helpers, and the HTTP surface they sit behind."""

from __future__ import annotations

import json
import socket
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from halcyon.ofdm import Phy
from halcyon.ui import MAX_BODY, Api, _audio, _decode, _waterfall, modes_payload


def test_cross_origin_posts_are_refused():
    from halcyon.ui import _origin_allowed

    assert _origin_allowed(None, "127.0.0.1:8730")
    assert _origin_allowed("http://localhost:8730", "localhost:8730")
    # a page served from elsewhere, and the DNS-rebinding variant of it
    assert not _origin_allowed("https://evil.example", "127.0.0.1:8730")
    assert not _origin_allowed(None, "evil.example:8730")


def test_modes_payload_covers_every_profile_and_rung():
    d = modes_payload()
    assert [p["id"] for p in d["profiles"]] == [
        "wide", "robust", "compact", "deep", "nano", "mini", "micro"
    ]
    for p in d["profiles"]:
        assert p["modes"], p["id"]
        for m in p["modes"]:
            assert m["bps"] > 0
            assert m["cap_bytes"] > 0
            assert m["max_bytes"] >= m["cap_bytes"]
    # 8.8 bps is the one deliberate close-to-9 rung; every other is >=10.
    assert min(m["bps"] for p in d["profiles"] for m in p["modes"]) >= 8.8
    for p in d["profiles"]:
        for m in p["modes"]:
            if m["bps"] < 10:
                assert p["id"] == "micro"


def test_waterfall_is_a_time_by_frequency_image():
    phy = Phy()
    rng = np.random.default_rng(0)
    rx = rng.standard_normal(20000) + 1j * rng.standard_normal(20000)
    wf = _waterfall(rx, phy.fs, cols=32, rows=120)
    assert wf is not None
    assert wf["w"] == 120 and wf["h"] == 32
    raw = np.frombuffer(__import__("base64").b64decode(wf["data"]), dtype=np.uint8)
    assert raw.size == wf["w"] * wf["h"]


def test_audio_is_a_playable_wav():
    wav = _audio({"profile": "wide", "mcs": 4, "text": "hello"})
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) > 44


def test_decode_reports_the_frame_and_recovers_the_text():
    out = _decode(
        {
            "profile": "wide",
            "mcs": 4,
            "text": "cq de halcyon",
            "snr": 12.0,
            "channel": "awgn",
            "cfo": 0.0,
            "looks": 0,
        }
    )
    assert out["sent"]["label"] == "QPSK 1/2"
    assert out["timing"]["total_s"] > 0
    first = out["attempts"][0]
    assert first["header_ok"] and first["match"]
    assert first["decoded"] == "cq de halcyon"
    assert out["waterfall"]["w"] > 0


def test_second_look_only_runs_when_the_first_look_fails():
    clean = _decode(
        {"profile": "wide", "mcs": 4, "text": "cq de halcyon", "snr": 14.0, "looks": 1}
    )
    assert clean["attempts"][0]["ok"]
    assert len(clean["attempts"]) == 1

    weak = _decode(
        {"profile": "wide", "mcs": 6, "text": "second look demo", "snr": 5.0, "looks": 1}
    )
    assert not weak["attempts"][0]["ok"]
    assert len(weak["attempts"]) == 2
    assert weak["attempts"][1]["label"] == "summed with the stored look"


def test_decode_caps_the_message_at_the_burst_capacity():
    out = _decode({"profile": "wide", "mcs": 0, "text": "x" * 5000, "snr": 30.0, "looks": 0})
    assert out["trimmed"] is True
    assert out["sent"]["bytes"] == out["sent"]["block_bits"] // 8 * 12


def test_the_console_refuses_to_bind_a_network_interface():
    from halcyon.ui import main

    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0", "--port", "0"])


# --------------------------------------------------------------- over HTTP
# The checks above test the helpers directly.  These start the real handler on
# an ephemeral loopback port and speak HTTP to it, because the guarantees that
# matter -- the Host and Origin checks, and a body that is refused before it is
# read -- only exist in the request path.

@pytest.fixture()
def console():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _http(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        reply = conn.getresponse()
        return (
            reply.status,
            {k.lower(): v for k, v in reply.getheaders()},
            reply.read(),
        )
    finally:
        conn.close()


def test_the_terminal_is_served_on_a_loopback_host_name(console):
    status, headers, body = _http(console, "GET", "/", headers={"Host": "localhost:8730"})

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b"<html" in body.lower()
    assert headers["cache-control"] == "no-store"


def test_a_rebound_host_name_cannot_read_the_console(console):
    """DNS rebinding: a name that resolves to loopback must not read the API."""
    status, _, body = _http(
        console, "GET", "/api/config", headers={"Host": "evil.example:8730"}
    )
    assert status == 403
    assert "cross-origin" in json.loads(body)["error"]

    page, _, _ = _http(console, "GET", "/", headers={"Host": "evil.example:8730"})
    assert page == 403


def test_a_post_from_a_foreign_page_is_refused(console):
    status, _, body = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        body=b"{}",
        headers={
            "Host": "127.0.0.1:8730",
            "Origin": "https://evil.example",
            "Content-Type": "application/json",
        },
    )

    assert status == 403
    assert "cross-origin" in json.loads(body)["error"]


def test_a_post_must_be_json(console):
    """A simple cross-origin request cannot set this header."""
    status, headers, _ = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        body=b"x",
        headers={"Host": "127.0.0.1:8730", "Content-Type": "text/plain"},
    )

    assert status == 415


def test_a_chunked_body_is_refused_and_the_connection_closed(console):
    status, _, _ = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        body=b"{}",
        headers={
            "Host": "127.0.0.1:8730",
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
    )

    assert status == 411


def test_a_negative_content_length_is_refused_and_the_console_stays_up(console):
    """A negative length would make the handler read to end of stream."""
    status, _, _ = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        headers={
            "Host": "127.0.0.1:8730",
            "Content-Type": "application/json",
            "Content-Length": "-5",
        },
    )

    assert status == 400

    ok, _, _ = _http(console, "GET", "/api/config", headers={"Host": "127.0.0.1:8730"})
    assert ok == 200


def test_an_oversized_body_is_refused_before_it_is_read(console):
    status, _, body = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        headers={
            "Host": "127.0.0.1:8730",
            "Content-Type": "application/json",
            "Content-Length": str(MAX_BODY + 1),
        },
    )

    assert status == 413
    assert json.loads(body)["error"] == "request too large"


@pytest.mark.parametrize(
    ("framing", "expected"),
    [
        ("Transfer-Encoding: chunked\r\n", b"411"),
        ("Content-Length: -5\r\n", b"400"),
        (f"Content-Length: {MAX_BODY + 1}\r\n", b"413"),
    ],
)
def test_a_refused_body_is_not_left_in_the_socket(console, framing, expected):
    """The handler closes the connection so unread framing cannot be reused.

    A refused request still has bytes in flight when the reply goes out.  If the
    connection stayed open, the next request on it would be read starting from
    the middle of that body -- so the server must hang up, which the client sees
    as an empty read rather than a timeout.
    """
    payload = (
        "POST /api/radio/disconnect HTTP/1.1\r\n"
        "Host: 127.0.0.1:8730\r\n"
        "Content-Type: application/json\r\n"
        f"{framing}\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", console), timeout=3) as sock:
        sock.sendall(payload)
        sock.settimeout(3)
        data = b""
        closed = False
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                closed = True
                break
            data += chunk

    status_line = data.split(b"\r\n", 1)[0]
    assert status_line.startswith(b"HTTP/1.1"), status_line
    assert expected in status_line, status_line
    assert closed, "an unread body must not leave a usable connection"


def test_a_same_origin_post_reaches_its_route(console):
    status, headers, body = _http(
        console,
        "POST",
        "/api/radio/disconnect",
        body=b"{}",
        headers={
            "Host": "127.0.0.1:8730",
            "Origin": "http://127.0.0.1:8730",
            "Content-Type": "application/json",
        },
    )

    assert status == 200
    assert json.loads(body) == {"connected": False}
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"


def test_the_radio_connect_payload_carries_no_receiver_count(console):
    """The server always opens one stream, so the page must not send a count."""
    status, _, body = _http(console, "GET", "/", headers={"Host": "127.0.0.1:8730"})
    assert status == 200
    page = body.decode()
    connect = page[
        page.index("async function connectRadio"):
        page.index("async function disconnectRadio")
    ]
    assert "radioPost('/api/radio/connect'" in connect
    assert "receivers" not in connect

    # The single-file build duplicates this function, so it has to agree.
    standalone = (
        Path(__file__).resolve().parents[1] / "halcyon" / "ui" / "halcyon-standalone.html"
    ).read_text()
    built = standalone[
        standalone.index("async function connectRadio"):
        standalone.index("async function disconnectRadio")
    ]
    assert "receivers" not in built
