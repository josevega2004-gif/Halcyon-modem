"""Point PAT at Halcyon, or put it back the way it was.

PAT reaches a modem over two loopback ports, 8300 for commands and 8301 for
data.  In PAT's configuration file the transport that carries the Winlink
packet protocol over those ports is called ``varahf``, so switching modems
mostly means replacing that one block.  This module edits it, remembers the
value that was there before so ``off`` can restore it, and starts or stops the
host service.

The functions below are usable on their own; :func:`main` is the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

TRANSPORT = "varahf"  # PAT's name for the command set Halcyon speaks.
HOST = "127.0.0.1"
CMD_PORT = 8300
DATA_PORT = 8301
DEFAULT_ADDR = f"{HOST}:{CMD_PORT}"
DEFAULT_BANDWIDTH = "2300"
PROFILE_HALCYON = "halcyon"


# ---------------------------------------------------------------- paths


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base)


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "halcyon"


def pat_config() -> Path:
    override = os.environ.get("HALCYON_PAT_CONFIG")
    if override:
        return Path(override)
    return config_dir() / "pat" / "config.json"


def profiles_path() -> Path:
    return config_dir() / "halcyon" / "modems.json"


def state_path(cfg: Path) -> Path:
    return cfg.with_name(cfg.name + ".halcyon-state.json")


def backup_path(cfg: Path) -> Path:
    return cfg.with_name(cfg.name + ".halcyon-backup")


def pid_path() -> Path:
    return cache_dir() / "host.pid"


def log_path() -> Path:
    return cache_dir() / "host.log"


# ---------------------------------------------------------------- json


def read_json(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object at the top level")
    return data


def write_json(path: Path, data: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=4) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- PAT config


def halcyon_block(addr: str = DEFAULT_ADDR, bandwidth: str = DEFAULT_BANDWIDTH) -> dict:
    return {"addr": addr, "bandwidth": str(bandwidth)}


def addr_parts(addr: str) -> tuple[str, int]:
    host, _, port = str(addr or "").rpartition(":")
    try:
        return host or HOST, int(port)
    except ValueError:
        return str(addr or HOST), CMD_PORT


def selected_port(cfg: Path | None = None) -> int:
    """The command port PAT is configured to dial, or the default."""
    block = read_json(cfg or pat_config()).get(TRANSPORT)
    if isinstance(block, dict):
        return addr_parts(block.get("addr", ""))[1]
    return CMD_PORT


def points_at_halcyon(cfg: Path | None = None) -> bool:
    cfg = cfg or pat_config()
    block = read_json(cfg).get(TRANSPORT)
    if not isinstance(block, dict):
        return False
    if state_path(cfg).exists():
        # This tool put the block there; trust its record over the address.
        return True
    host, port = addr_parts(block.get("addr", ""))
    return port == CMD_PORT and host in (HOST, "localhost", "::1")


def remember(cfg: Path, transport: str, data: dict, dry_run: bool = False) -> None:
    """Save what the transport looked like before we touched it, once.

    Repeated switches keep the first remembered value: that is the one ``off``
    should restore, not a value this tool wrote on an earlier run.
    """
    marker = state_path(cfg)
    if marker.exists() or dry_run:
        return
    if cfg.exists():
        shutil.copy2(cfg, backup_path(cfg))
    write_json(
        marker,
        {"transport": transport, "had": transport in data, "previous": data.get(transport)},
    )


def apply_transport(
    cfg: Path, transport: str, settings: dict, dry_run: bool = False
) -> dict:
    """Write ``transport`` into PAT's config, remembering the previous value."""
    data = read_json(cfg)
    remember(cfg, transport, data, dry_run=dry_run)
    data[transport] = settings
    write_json(cfg, data, dry_run=dry_run)
    return data


def restore(cfg: Path, dry_run: bool = False) -> bool:
    """Undo the last switch.  Returns True if there was anything to undo."""
    marker = state_path(cfg)
    if not marker.exists():
        return False
    state = read_json(marker)
    data = read_json(cfg)
    transport = state.get("transport", TRANSPORT)
    if state.get("had") and state.get("previous") is not None:
        data[transport] = state["previous"]
    else:
        data.pop(transport, None)
    write_json(cfg, data, dry_run=dry_run)
    if not dry_run:
        marker.unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------- profiles


def default_profiles() -> dict:
    return {
        PROFILE_HALCYON: {
            "transport": TRANSPORT,
            "settings": halcyon_block(),
        }
    }


def load_profiles(path: Path | None = None) -> dict:
    path = path or profiles_path()
    profiles = default_profiles()
    profiles.update(read_json(path))
    return profiles


# ---------------------------------------------------------------- host process


def port_open(port: int, host: str = HOST, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def probe(port: int = CMD_PORT, host: str = HOST, timeout: float = 2.0) -> str | None:
    """Ask the service to identify itself, or return None if nothing answers."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"VERSION\r")
            sock.settimeout(timeout)
            buf = b""
            while b"\r" not in buf:
                chunk = sock.recv(256)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    line = buf.split(b"\r", 1)[0].decode("ascii", "replace").strip()
    return line or None


def running_pid() -> int | None:
    try:
        pid = int(pid_path().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if is_host_process(pid) else None


def is_host_process(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return b"halcyon.host" in cmdline or b"halcyon-host" in cmdline


def start_host(
    mycall: str = "N0CALL",
    bandwidth: str = DEFAULT_BANDWIDTH,
    host: str = HOST,
    cmd_port: int = CMD_PORT,
    data_port: int = DATA_PORT,
) -> str:
    pid = running_pid()
    if pid is not None:
        return f"already running (pid {pid})"
    if port_open(cmd_port, host):
        return f"{host}:{cmd_port} is already listening; not starting a second host"

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "halcyon.host",
        "--host",
        host,
        "--cmd-port",
        str(cmd_port),
        "--data-port",
        str(data_port),
        "--mycall",
        mycall,
        "--bandwidth",
        str(bandwidth),
    ]
    with open(log, "ab") as handle:
        proc = subprocess.Popen(  # noqa: S603 - fixed argument list
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    pid_path().write_text(f"{proc.pid}\n")
    for _ in range(50):
        if port_open(cmd_port, host):
            return f"started (pid {proc.pid}), log {log}"
        if proc.poll() is not None:
            pid_path().unlink(missing_ok=True)
            return f"failed to start (exit {proc.returncode}), see {log}"
        time.sleep(0.1)
    return f"started (pid {proc.pid}) but {host}:{cmd_port} is not answering yet, see {log}"


def stop_host() -> str:
    pid = running_pid()
    if pid is None:
        pid_path().unlink(missing_ok=True)
        return "not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path().unlink(missing_ok=True)
        return "not running"
    for _ in range(30):
        if not is_host_process(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_path().unlink(missing_ok=True)
    return f"stopped (pid {pid})"


def host_state(cfg: Path | None = None) -> str:
    pid = running_pid()
    port = selected_port(cfg) if cfg is not None else CMD_PORT
    listening = port_open(port)
    if pid and listening:
        return f"running (pid {pid}), listening on {port}"
    if listening:
        return f"listening on {port}, started outside this tool"
    return "not running"


# ---------------------------------------------------------------- command line


def _describe_block(block) -> str:
    if not isinstance(block, dict):
        return "not set"
    addr = block.get("addr", "?")
    bandwidth = block.get("bandwidth", "?")
    return f"{addr}, bandwidth {bandwidth}"


def cmd_status(args) -> int:
    cfg = Path(args.config)
    data = read_json(cfg)
    block = data.get(TRANSPORT)
    print(f"PAT config : {cfg}")
    print(f"Transport  : {TRANSPORT} -> {_describe_block(block)}")
    if points_at_halcyon(cfg):
        print("Halcyon    : selected")
    elif TRANSPORT in data:
        print("Halcyon    : not selected (the transport points elsewhere)")
    else:
        print(f"Halcyon    : not selected (no {TRANSPORT} block)")
    print(f"Host       : {host_state(cfg)}")
    print(f"Backup     : {backup_path(cfg)}" + ("" if backup_path(cfg).exists() else " (none yet)"))
    print(f"Log        : {log_path()}")
    return 0


def cmd_on(args) -> int:
    cfg = Path(args.config)
    settings = halcyon_block(args.addr, args.bandwidth)
    existing = read_json(cfg)
    apply_transport(cfg, TRANSPORT, settings, dry_run=args.dry_run)
    if args.dry_run:
        print(f"would set {TRANSPORT} in {cfg} to {settings}")
    else:
        had = "replaced" if TRANSPORT in existing else "added"
        print(f"{had} {TRANSPORT} in {cfg}: {_describe_block(settings)}")
        if state_path(cfg).exists():
            print(f"previous value saved in {backup_path(cfg)}")
    if args.no_host:
        return 0
    if args.dry_run:
        print("would start the host service on " + str(args.addr))
        return 0
    print("Host: " + start_host(mycall=args.mycall, bandwidth=args.bandwidth, host=args.host, cmd_port=args.cmd_port))
    print(f"Next: pat connect {TRANSPORT}:///CALL")
    return 0


def cmd_off(args) -> int:
    cfg = Path(args.config)
    if not args.keep_host:
        print("Host: " + stop_host())
    if restore(cfg, dry_run=args.dry_run):
        print(f"Restored {TRANSPORT} in {cfg}" if not args.dry_run else f"would restore {TRANSPORT} in {cfg}")
    else:
        print(f"Nothing recorded for {cfg}; PAT config left alone")
    return 0


def cmd_use(args) -> int:
    cfg = Path(args.config)
    profiles = load_profiles(Path(args.profiles) if args.profiles else None)
    profile = profiles.get(args.name)
    if not isinstance(profile, dict) or "transport" not in profile:
        names = ", ".join(sorted(profiles))
        raise SystemExit(f"unknown profile {args.name!r}; known: {names}")
    settings = dict(profile.get("settings") or {})
    apply_transport(cfg, profile["transport"], settings, dry_run=args.dry_run)
    if args.dry_run:
        print(f"would set {profile['transport']} in {cfg} to {settings}")
    else:
        print(f"{profile['transport']} in {cfg}: {_describe_block(settings)}")
    return 0


def cmd_varac(args) -> int:
    if not args.no_host:
        print(
            "Host: "
            + start_host(
                mycall=args.mycall,
                bandwidth=args.bandwidth,
                host=args.host,
                cmd_port=args.cmd_port,
                data_port=args.data_port,
            )
        )
    answer = probe(args.cmd_port, args.host)
    if answer is None:
        print(
            f"No modem answered on {args.host}:{args.cmd_port}.  Start the host "
            "with 'halcyon-switch varac' or 'halcyon-host'.",
            file=sys.stderr,
        )
        return 1
    print(f"Modem answered: {answer}")
    print()
    print("In VarAC's modem settings use:")
    print(f"  modem      the HF modem entry (VARA HF)")
    print(f"  address    {args.host}")
    print(f"  port       {args.cmd_port}")
    print(f"  bandwidth  {args.bandwidth}")
    print()
    print(f"VarAC opens the data port itself; it is {args.data_port} here.")
    print("Host mode carries the link's bytes only: PTT and audio stay on the radio side.")
    return 0


def cmd_list(args) -> int:
    for name, profile in sorted(load_profiles(Path(args.profiles) if args.profiles else None).items()):
        profile = profile if isinstance(profile, dict) else {}
        print(
            f"{name:12s} {profile.get('transport', '?'):10s} "
            f"{_describe_block(profile.get('settings'))}"
        )
    return 0


def cmd_host(args) -> int:
    from . import host as host_module

    return host_module.main(
        [
            "--host", args.host,
            "--cmd-port", str(args.cmd_port),
            "--data-port", str(args.data_port),
            "--mycall", args.mycall,
            "--bandwidth", args.bandwidth,
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS matters: a sub-command parser that carried the default would
    # overwrite the --config given before the sub-command.  main() fills in the
    # default once, after parsing.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="PAT config file (default: ~/.config/pat/config.json)",
    )

    ap = argparse.ArgumentParser(
        prog="halcyon-switch",
        description="Point PAT at Halcyon, or restore the previous modem.",
        parents=[common],
    )
    sub = ap.add_subparsers(dest="command")

    status = sub.add_parser("status", parents=[common], help="show the current selection")
    status.set_defaults(func=cmd_status)

    on = sub.add_parser("on", parents=[common], help="select Halcyon and start the host")
    on.add_argument("--mycall", default="N0CALL", help="station callsign reported to PAT")
    on.add_argument("--bandwidth", default=DEFAULT_BANDWIDTH, choices=["500", "2300", "2750"])
    on.add_argument(
        "--addr", default=DEFAULT_ADDR,
        help="address written for PAT; PAT itself dials 8300 (default: %(default)s)",
    )
    on.add_argument("--host", default=HOST, help="address the host service binds")
    on.add_argument("--cmd-port", type=int, default=CMD_PORT)
    on.add_argument("--data-port", type=int, default=DATA_PORT)
    on.add_argument("--no-host", action="store_true", help="edit PAT only, start nothing")
    on.add_argument("--dry-run", action="store_true", help="show what would change")
    on.set_defaults(func=cmd_on)

    off = sub.add_parser("off", parents=[common], help="restore PAT and stop the host")
    off.add_argument("--keep-host", action="store_true", help="leave the host running")
    off.add_argument("--dry-run", action="store_true")
    off.set_defaults(func=cmd_off)

    use = sub.add_parser("use", parents=[common], help="apply a named modem profile")
    use.add_argument("name")
    use.add_argument(
        "--profiles", default=None, help="profile file (default: ~/.config/halcyon/modems.json)"
    )
    use.add_argument("--dry-run", action="store_true")
    use.set_defaults(func=cmd_use)

    varac = sub.add_parser(
        "varac", parents=[common],
        help="start the host, check it answers, and print VarAC's settings",
    )
    varac.add_argument("--mycall", default="N0CALL", help="station callsign")
    varac.add_argument("--bandwidth", default=DEFAULT_BANDWIDTH, choices=["500", "2300", "2750"])
    varac.add_argument("--host", default=HOST, help="address the host binds")
    varac.add_argument("--cmd-port", type=int, default=CMD_PORT)
    varac.add_argument("--data-port", type=int, default=DATA_PORT)
    varac.add_argument("--no-host", action="store_true", help="only check, start nothing")
    varac.set_defaults(func=cmd_varac)

    names = sub.add_parser("list", parents=[common], help="list modem profiles")
    names.add_argument("--profiles", default=None)
    names.set_defaults(func=cmd_list)

    host = sub.add_parser("host", parents=[common], help="run the host service in the foreground")
    host.add_argument("--mycall", default="N0CALL")
    host.add_argument("--bandwidth", default=DEFAULT_BANDWIDTH, choices=["500", "2300", "2750"])
    host.add_argument("--host", default=HOST)
    host.add_argument("--cmd-port", type=int, default=CMD_PORT)
    host.add_argument("--data-port", type=int, default=DATA_PORT)
    host.set_defaults(func=cmd_host)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "config"):
        args.config = str(pat_config())
    if not getattr(args, "command", None):
        # No subcommand: behave like `status`, which is what people check first.
        return cmd_status(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
