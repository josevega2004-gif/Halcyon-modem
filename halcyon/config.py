"""User configuration, read from a small JSON file.

The modem needs no configuration to run: every default in the console is the
one a station should start with.  What this file controls is narrower -- which
*instruments* the console shows.  Some of the UI exists to exercise the modem
rather than to operate it: the propagation model with its S/N slider, fading
profiles and QSB switch, and the lab console at /console.  Those are hidden
unless they are explicitly asked for, because a first-time user should not be
handed sliders that look like they tune a receiver when they do not.

    {"ui": {"developer": true}}

Path: `$HALCYON_CONFIG`, otherwise `~/.config/halcyon/config.json`.  A missing,
empty or malformed file is not an error -- the defaults below apply, so a user
who never creates one still gets a working terminal.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {"ui": {"developer": False}}


def path() -> Path:
    """Where the file is looked for.  Overridable for tests and for packagers."""
    env = os.environ.get("HALCYON_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "halcyon" / "config.json"


def _merge(base: dict, over: dict) -> dict:
    """Recursive merge, so a file that sets one key keeps the other defaults."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    """The effective configuration.  Never raises: bad input means defaults.

    A console that refuses to start because of a typo in a file that only
    controls which panel is visible would be a poor trade, so a broken file is
    treated exactly like an absent one.
    """
    try:
        text = path().read_text(encoding="utf-8")
    except OSError:
        return copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(text)
    except ValueError:
        return copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULTS)
    return _merge(DEFAULTS, data)


def developer() -> bool:
    """True when the file asks for the test instruments to be visible."""
    ui = load().get("ui")
    return bool(isinstance(ui, dict) and ui.get("developer"))


def example() -> str:
    """The file to paste into place to turn the instruments on."""
    return json.dumps({"ui": {"developer": True}}, indent=2) + "\n"
