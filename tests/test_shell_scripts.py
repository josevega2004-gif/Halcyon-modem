"""Lint the shell helpers that sit beside the Python tree."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
SCRIPTS = sorted(TOOLS.glob("*.sh"))


@pytest.mark.skipif(
    shutil.which("shellcheck") is None,
    reason="shellcheck is not installed",
)
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_shellcheck_is_clean(script):
    proc = subprocess.run(
        ["shellcheck", "-x", str(script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
