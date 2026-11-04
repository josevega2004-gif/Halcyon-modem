import json
import os

import pytest

from halcyon import switch


@pytest.fixture(autouse=True)
def isolated_default(tmp_path, monkeypatch):
    """Keep the parser's default away from the real PAT configuration."""
    monkeypatch.setenv("HALCYON_PAT_CONFIG", str(tmp_path / "no-such-config.json"))


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, indent=4) + "\n")
    return path


def read_config(path):
    return json.loads(path.read_text())


def test_on_adds_transport_and_keeps_other_keys(tmp_path):
    cfg = write_config(tmp_path, {"http_addr": "127.0.0.1:8080", "varahf": None})

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block())

    data = read_config(cfg)
    assert data["http_addr"] == "127.0.0.1:8080"
    assert data["varahf"] == {"addr": "127.0.0.1:8300", "bandwidth": "2300"}
    assert switch.points_at_halcyon(cfg)
    assert switch.backup_path(cfg).exists()
    assert read_config(switch.backup_path(cfg))["varahf"] is None


def test_off_restores_the_previous_block(tmp_path):
    original = {"varahf": {"addr": "127.0.0.1:9999", "bandwidth": "500"}}
    cfg = write_config(tmp_path, dict(original))

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block())
    assert switch.points_at_halcyon(cfg)
    assert switch.restore(cfg)

    assert read_config(cfg) == original
    assert not switch.state_path(cfg).exists()


def test_off_removes_a_transport_that_was_not_there(tmp_path):
    cfg = write_config(tmp_path, {"http_addr": "127.0.0.1:8080"})

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block())
    switch.restore(cfg)

    assert read_config(cfg) == {"http_addr": "127.0.0.1:8080"}
    assert not switch.points_at_halcyon(cfg)


def test_repeated_on_keeps_the_original_value(tmp_path):
    cfg = write_config(tmp_path, {"varahf": {"addr": "127.0.0.1:9999", "bandwidth": "500"}})

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block())
    switch.apply_transport(
        cfg, switch.TRANSPORT, switch.halcyon_block("127.0.0.1:8300", "500")
    )
    switch.restore(cfg)

    assert read_config(cfg)["varahf"] == {"addr": "127.0.0.1:9999", "bandwidth": "500"}


def test_restore_without_a_record_changes_nothing(tmp_path):
    cfg = write_config(tmp_path, {"varahf": {"addr": "127.0.0.1:8300"}})

    assert switch.restore(cfg) is False
    assert read_config(cfg)["varahf"] == {"addr": "127.0.0.1:8300"}


def test_dry_run_writes_nothing(tmp_path):
    cfg = write_config(tmp_path, {"http_addr": "127.0.0.1:8080"})
    before = cfg.read_text()

    switch.apply_transport(
        cfg, switch.TRANSPORT, switch.halcyon_block(), dry_run=True
    )

    assert cfg.read_text() == before
    assert not switch.state_path(cfg).exists()


def test_default_profile_targets_halcyon(tmp_path):
    profiles = switch.load_profiles(tmp_path / "missing.json")

    assert profiles[switch.PROFILE_HALCYON]["transport"] == switch.TRANSPORT
    assert profiles[switch.PROFILE_HALCYON]["settings"]["addr"].endswith(":8300")


def test_profiles_file_overrides_the_builtin(tmp_path):
    path = tmp_path / "modems.json"
    path.write_text(json.dumps({"halcyon": {"transport": "varahf", "settings": {"addr": "127.0.0.1:8300", "bandwidth": "500"}}}))

    profiles = switch.load_profiles(path)

    assert profiles["halcyon"]["settings"]["bandwidth"] == "500"


def test_status_reports_a_foreign_transport(tmp_path, capsys):
    cfg = write_config(tmp_path, {"varahf": {"addr": "127.0.0.1:9999"}})

    assert switch.main(["--config", str(cfg), "status"]) == 0
    out = capsys.readouterr().out
    assert "not selected" in out
    assert str(cfg) in out


def test_on_and_off_through_the_command_line(tmp_path):
    cfg = write_config(tmp_path, {"varahf": {"addr": "127.0.0.1:9999"}})
    original = read_config(cfg)

    assert switch.main(["--config", str(cfg), "on", "--no-host", "--mycall", "N0CALL"]) == 0
    assert switch.points_at_halcyon(cfg)

    assert switch.main(["--config", str(cfg), "off"]) == 0
    assert read_config(cfg) == original


def test_config_may_come_after_the_command(tmp_path):
    cfg = write_config(tmp_path, {"varahf": {"addr": "127.0.0.1:9999"}})

    assert switch.main(["on", "--no-host", "--config", str(cfg)]) == 0
    assert switch.points_at_halcyon(cfg)

    assert switch.main(["off", "--config", str(cfg)]) == 0
    assert read_config(cfg)["varahf"] == {"addr": "127.0.0.1:9999"}


def test_selected_after_a_switch_to_an_unusual_port(tmp_path):
    cfg = write_config(tmp_path, {})

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block("127.0.0.1:18300"))
    assert switch.selected_port(cfg) == 18300
    assert switch.points_at_halcyon(cfg)

    switch.restore(cfg)
    assert not switch.points_at_halcyon(cfg)


def test_probe_reads_the_version_line():
    from halcyon.host import HostServer

    server = HostServer(cmd_port=0, data_port=0).start()
    try:
        assert switch.probe(server.cmd_port) == "VERSION Halcyon 0.1 host mode"
    finally:
        server.stop()


def test_probe_reports_nothing_on_a_closed_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]

    assert switch.probe(free, timeout=0.3) is None


def test_varac_command_fails_when_nothing_answers(tmp_path, capsys):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]

    assert switch.main(["varac", "--no-host", "--cmd-port", str(free)]) == 1
    assert "No modem answered" in capsys.readouterr().err


def test_transport_file_is_private(tmp_path):
    cfg = write_config(tmp_path, {})
    os.chmod(cfg, 0o644)

    switch.apply_transport(cfg, switch.TRANSPORT, switch.halcyon_block())

    assert cfg.stat().st_mode & 0o077 == 0
