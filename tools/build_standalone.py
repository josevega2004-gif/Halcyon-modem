"""Build the single-file terminal: one HTML file, no server, no network.

    PYTHONPATH=. python3 tools/build_standalone.py

The result keeps every drawing and interaction of the served terminal, and
replaces its two API calls with a bundle of bursts that the Python modem
measured while building: for each rung, 3 dB above and 3 dB below its own
threshold, for a couple of recorded phrases.

The bundle is real measured data, but it is not a modem: the page cannot encode
or decode anything new, which is why it carries a fixed phrase list instead of a
free-text box, and says so in its radio panel.  The served terminal
(``python3 -m halcyon.ui``) is the one with the engine behind it.
"""

from __future__ import annotations

import base64
import json
import pathlib
import time

import numpy as np

from halcyon.frame import table_for
from halcyon.ui import PROFILES, _decode, modem_for, modes_payload

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "halcyon" / "ui" / "station.html"
OUT = ROOT / "halcyon" / "ui" / "halcyon-standalone.html"
PHRASES = ("cq cq de halcyon", "test de halcyon k")
STRIP_W, STRIP_H = 40, 120


def strip(wf: dict, w: int = STRIP_W, h: int = STRIP_H) -> dict:
    """Shrink a time-by-frequency waterfall to an embeddable strip."""
    img = np.frombuffer(base64.b64decode(wf["data"]), dtype=np.uint8)
    img = img.reshape(wf["h"], wf["w"])  # rows: frequency, columns: time
    rows = np.linspace(0, img.shape[0] - 1, h).astype(int)
    cols = np.linspace(0, img.shape[1] - 1, w).astype(int)
    small = img[np.ix_(rows, cols)]
    return {
        "w": w,
        "h": h,
        "span_hz": wf.get("span_hz", 3200.0),
        "data": base64.b64encode(small.tobytes()).decode(),
    }


def build() -> dict:
    modes = modes_payload()
    bursts: dict[str, dict[str, list]] = {}
    count = 0
    for prof in modes["profiles"]:
        pid = prof["id"]
        table = table_for(modem_for(pid).phy)
        bursts[pid] = {}
        for m in prof["modes"]:
            idx = m["i"]
            thr = table[idx].snr_channel
            points = sorted({round(thr + 3.0, 1), round(thr - 3.0, 1)})
            runs_per_point = []
            for snr in points:
                runs = {}
                for phrase in PHRASES:
                    d = _decode(
                        {
                            "profile": pid,
                            "mcs": idx,
                            "text": phrase,
                            "snr": snr,
                            "channel": "awgn",
                            "cfo": 0.0,
                            "looks": 1,
                        }
                    )
                    a = d["attempts"][0]
                    runs[phrase] = {
                        "attempt": {
                            "header_ok": a["header_ok"],
                            "blocks_ok": a["blocks_ok"],
                            "decoded": a["decoded"],
                            "match": a["match"],
                            "ok": a["ok"],
                            "bytes": a["bytes"],
                            "reported_snr": a["reported_snr"],
                            "cfo_hz": a["cfo_hz"],
                        },
                        "sent": d["sent"],
                        "timing": d["timing"],
                        "elapsed_s": d["elapsed_s"],
                        "waterfall": strip(d["waterfall"]) if d["waterfall"] else None,
                    }
                    count += 1
                runs_per_point.append({"snr": snr, "runs": runs})
            bursts[pid][str(idx)] = runs_per_point
            print(f"  {pid:8s} L{idx + 1:02d} {m['label']:<14s} {m['bps']:>6} bps "
                  f"@ {points}  decoded "
                  f"{runs_per_point[0]['runs'][PHRASES[0]]['attempt']['match']}/"
                  f"{runs_per_point[1]['runs'][PHRASES[0]]['attempt']['match']}", flush=True)
    return {
        "count": count,
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "phrases": list(PHRASES),
        "modes": modes,
        "bursts": bursts,
    }


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"{SRC} is missing")
    print(f"measuring {len(PROFILES)} geometries, {len(PHRASES)} phrases, 2 S/N points each")
    bundle = build()
    html = SRC.read_text()
    marker = "<script>\nconst $ = (s, r = document)"
    if marker not in html:
        raise SystemExit("could not find the page's script block to inject into")
    inject = ("<script>window.HALCYON_BUNDLE = " + json.dumps(bundle, separators=(",", ":"))
              + ";</script>\n")
    html = html.replace(marker, inject + marker, 1)
    OUT.write_text(html)
    kb = OUT.stat().st_size / 1024
    print(f"\nwrote {OUT.relative_to(ROOT)}: {kb:.0f} KB, {bundle['count']} measured bursts, "
          f"no external references")


if __name__ == "__main__":
    main()
