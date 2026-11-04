"""The config file decides which instruments the console shows."""

from __future__ import annotations

import json

import pytest

from halcyon import config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never read the developer's real ~/.config/halcyon/config.json."""
    monkeypatch.setenv("HALCYON_CONFIG", str(tmp_path / "halcyon.json"))
    return tmp_path / "halcyon.json"


def test_missing_file_means_defaults(isolated):
    assert config.load() == config.DEFAULTS
    assert config.developer() is False


def test_developer_switch_comes_from_the_file(isolated):
    isolated.write_text(json.dumps({"ui": {"developer": True}}))
    assert config.developer() is True


def test_a_broken_file_is_treated_as_absent(isolated):
    isolated.write_text("{ this is not json")
    assert config.load() == config.DEFAULTS
    assert config.developer() is False


def test_partial_file_keeps_the_other_defaults(isolated):
    isolated.write_text(json.dumps({"ui": {"developer": True}}))
    assert config.load()["ui"] == {"developer": True}


def test_non_boolean_developer_value_is_coerced(isolated):
    isolated.write_text(json.dumps({"ui": {"developer": 1}}))
    assert config.developer() is True


def test_path_follows_the_environment(isolated):
    assert config.path() == isolated


def test_example_is_valid_json_that_enables_developer():
    assert json.loads(config.example())["ui"]["developer"] is True
