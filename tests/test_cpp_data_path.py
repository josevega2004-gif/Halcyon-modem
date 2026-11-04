"""The C++ modem must find its rate table wherever it is run from.

halcyon-modem used to open cpp/data/wide.bin relative to the working directory,
so a clone build run from cpp/ and an installed /usr/bin/halcyon-modem both
failed at startup.  These tests invoke the binary from a directory that holds
no tables at all.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CPP = ROOT / "cpp"
BIN = CPP / "build" / "halcyon-modem"
DATA = CPP / "data" / "wide.bin"


def _build() -> None:
    """Compile the core, or skip on a machine with no toolchain."""
    if shutil.which("make") is None or shutil.which("g++") is None:
        pytest.skip("no make/g++ here, cannot exercise the C++ binary")
    built = subprocess.run(["make", "-s", "-C", str(CPP)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert BIN.is_file(), "make reported success but produced no binary"


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    _build()
    return subprocess.run(
        [str(BIN), *args], cwd=str(cwd), capture_output=True, text=True, env=dict(os.environ, **(env or {}))
    )


def test_selftest_from_a_foreign_directory(tmp_path: Path) -> None:
    out = _run("selftest", cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert "17/17" in out.stdout


def test_rungs_from_a_foreign_directory(tmp_path: Path) -> None:
    out = _run("rungs", cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert "QPSK" in out.stdout


def test_data_flag_is_honoured(tmp_path: Path) -> None:
    out = _run("rungs", "--data", str(DATA), cwd=tmp_path)
    assert out.returncode == 0, out.stderr


def test_data_environment_variable_is_honoured(tmp_path: Path) -> None:
    out = _run("rungs", cwd=tmp_path, env={"HALCYON_DATA": str(DATA)})
    assert out.returncode == 0, out.stderr


def test_missing_table_says_how_to_fix_it(tmp_path: Path) -> None:
    out = _run("rungs", "--data", str(tmp_path / "absent.bin"), cwd=tmp_path)
    assert out.returncode != 0
    assert "--data" in out.stderr and "HALCYON_DATA" in out.stderr
