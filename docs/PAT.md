# Using Halcyon with PAT

This guide points [PAT](https://getpat.io/) at Halcyon, checks that the two are
talking, and puts PAT back on whatever it used before when you are done.

Halcyon's host mode is a pair of loopback TCP ports: **8300** carries
CR-terminated commands and events, **8301** carries the link's byte stream.
PAT hardwires those two ports, so the modem has to be listening there before
you dial. In PAT's configuration file the transport that uses this command set
is called `varahf`.

## The short version

With the package installed:

```
halcyon-switch on --mycall N0CALL
pat connect varahf:///N0CALL
halcyon-switch off
```

`on` writes the `varahf` block PAT needs, saves the value that was there
before, and starts the host service. `off` stops the service and restores the
previous configuration. `halcyon-switch status` reports what PAT is pointed at
and whether the host is up:

```
$ halcyon-switch status
PAT config : /home/you/.config/pat/config.json
Transport  : varahf -> 127.0.0.1:8300, bandwidth 2300
Halcyon    : selected
Host       : running (pid 41231), listening on 8300 and 8301
Backup     : /home/you/.config/pat/config.json.halcyon-backup
Log        : /home/you/.cache/halcyon/host.log
```

Nothing above needs root, and nothing leaves the loopback interface.

## Doing it by hand

### 1. Start the host service

```
halcyon-host --mycall N0CALL --bandwidth 2300
```

It prints one line and then waits; `Ctrl-C` stops it.

```
Halcyon 0.1 host mode on 127.0.0.1, commands :8300, data :8301; N0CALL, 2300 Hz, S/N 10 dB (Ctrl-C to stop)
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--mycall CALL` | Callsign used until the client sends `MYCALL`. PAT sends it on every dial, so this is only a default. |
| `--bandwidth 500\|2300\|2750` | Default geometry selector. `500` is a hard ceiling: the host starts on the compact profile (492 Hz low-speed ladder), may narrow to 375 Hz below −4 dB and 234 Hz below −12 dB, and clamps a later `BW2300`/`BW2750` to 500. `2300` and `2750` use the wide profile (2.74 kHz occupied). |
| `--host ADDRESS` | Bind address, default `127.0.0.1`. Leave it on loopback. |
| `--cmd-port`, `--data-port` | Port numbers, default 8300 and 8301. Change these only if you also change PAT. |
| `--snr DB`, `--channel NAME` | Simulated link conditions (`awgn`, `good`, `moderate`, `poor`). |
| `--no-echo` | Do not return decoded data to the client that wrote it. |

To have it start at login instead, save this as
`~/.config/systemd/user/halcyon-host.service`:

```ini
[Unit]
Description=Halcyon host-mode modem service
After=network.target

[Service]
ExecStart=/usr/bin/halcyon-host --mycall N0CALL --bandwidth 2300
Restart=on-failure

[Install]
WantedBy=default.target
```

then `systemctl --user daemon-reload && systemctl --user enable --now halcyon-host`.

### 2. Tell PAT where the modem is

Either let the switch tool edit the file (`halcyon-switch on --no-host`), or
edit `~/.config/pat/config.json` yourself. The block PAT needs is small:

```json
{
    "varahf": {
        "addr": "127.0.0.1:8300",
        "bandwidth": "2300"
    }
}
```

Keep every other key in the file as it is. PAT rewrites this file when you use
`pat configure`, so if you edit it by hand, do it while PAT is not running.
The configuration is private to your user; `halcyon-switch` writes it mode 0600.

The bandwidth value is what PAT announces with `BW2300`. It also sends its own
`BW...` command on the air, and the host takes the value PAT sends, except that a
host started at 500 keeps it as a ceiling and clamps a wider announcement.

### 3. Dial

```
pat connect varahf:///N0CALL
```

The part after the third slash is the station you are calling. A Winlink
gateway or a second station running Halcyon goes there; for a two-station test
use the other station's callsign.

## What actually crosses the ports

PAT opens **two** command connections and **two** data connections while it
dials, and treats the close of any one of them as the modem disappearing. The
host serves every client rather than evicting the previous one, which is why
PAT can dial more than once without restarting anything.

On the command port PAT sends `PUBLIC ON`, `CWID ON`, `COMPRESSION TEXT`,
`MYCALL`, `LISTEN OFF`, `VERSION`, `BW2300`, `WINLINK SESSION` and `CONNECT`;
the host answers `VERSION`, `PENDING`, `PTT ON`, `CONNECTED <src> <dst>`,
`PTT OFF`, plus `IAMALIVE` heartbeats and `SN`/`BUFFER` while data moves.

Two details are easy to get wrong, and the host does not:

- `COMPRESSION TEXT` means compression is **on**, not off.
- `CONNECT` may arrive as `CONNECT <bandwidth> <target>` or
  `CONNECT <src> <dst>`; a leading number is read as bandwidth.

## Checking the link

```
ss -ltnp | grep -E '830[01]'        # both ports listening?
halcyon-switch status               # same question, plus PAT's setting
curl -s http://127.0.0.1:8730/api/services   # console view, if it is running
```

A bare socket test moves real bytes through the modem without PAT:

```
python3 - <<'PY'
import socket
c = socket.create_connection(("127.0.0.1", 8300))
c.sendall(b"MYCALL N0CALL\rLISTEN ON\rCONNECT N0CALL N0CALL\r")
print(c.recv(4096))
d = socket.create_connection(("127.0.0.1", 8301))
d.sendall(b"hello")
print(d.recv(1024))
PY
```

The data port echoes the bytes back decoded from the far end of the simulated
link, so `hello` coming back means the whole chain works.

## Troubleshooting

**PAT says it cannot reach the modem, or the connection is refused.** Nothing
is listening on 8300. Start `halcyon-host` (or `halcyon-switch on`) and check
`ss -ltnp | grep 8300`.

**`Address already in use` when starting.** Another modem service or an earlier
Halcyon host holds the port. Stop the old one; `halcyon-switch status` says
whether the running process is a Halcyon host, and `halcyon-switch off` stops
the one it started.

**PAT connects but the session does nothing.** That is the B2F exchange waiting
for the far end. PAT writes nothing until the other station speaks first, so
with no second station there is no mail to move. The transport is working; the
mail needs a peer.

**Everything worked, then PAT lost the modem.** Look at the host's log
(`~/.cache/halcyon/host.log` for a host started by `halcyon-switch`, or the
terminal that is running `halcyon-host`). Unknown commands are logged with a
leading `?` and otherwise ignored, because the protocol has no error channel.

**Wrong bandwidth.** PAT's `BW...` command selects the bandwidth, but a host
started at 500 treats it as a hard ceiling: a later `BW2300`/`BW2750` is
clamped to 500, while deep/nano remain available at low S/N. Start the host at
2300 or 2750 if you want the wide profile. `halcyon-switch on --bandwidth 500`
writes that value into PAT's configuration as well. Each step down or back up
the ladder is recorded in the host's log while a link is up, for example
`(fallback compact -> deep 375 Hz at S/N -8.0 dB)` and
`(recovery deep -> compact 492 Hz at S/N -2.0 dB)`. A rung below the 3 dB
margin target starts a warning and a return above it closes the episode, for
example `(low margin: L0 8FSK 1/4 at S/N -13.0 dB, +1.7 dB vs 3 dB target)`
and `(margin recovered: L4 BPSK 1/4 L2 at S/N +20.0 dB, +27.0 dB vs 3 dB target,
after 1.4 s)`. The recovery line includes how long the margin stayed below
the target since the warning; while the episode is active, the host status
reports the running count in `mcs_margin_low_s`.

## Switching between modems

`halcyon-switch` keeps one remembered value: whatever the transport block held
the first time you ran `on`. Repeating `on` does not overwrite it, and `off`
always restores that first value. If the block did not exist before, `off`
removes it.

The originals are kept next to PAT's configuration:

- `config.json.halcyon-backup` — a copy of the file before the first switch
- `config.json.halcyon-state.json` — the one block to restore, removed by `off`

To switch by name instead of by hand, put profiles in
`~/.config/halcyon/modems.json`:

```json
{
    "halcyon": {
        "transport": "varahf",
        "settings": {"addr": "127.0.0.1:8300", "bandwidth": "2300"}
    }
}
```

`halcyon-switch list` shows them and `halcyon-switch use halcyon` applies one.
Profiles only write the block named by their `transport` key; running `off`
afterwards still restores what was there before.

## Limits

- PAT can select Halcyon, dial, and move bytes; a session that carries mail
  needs a station at the other end running Halcyon. This modem does not
  interoperate with other HF data modems.
- No audio, PTT or rig control is involved: host mode is the network side. The
  HL2, TCI and CAT panels cover the radio side.
- The ports are for this machine only. Do not bind them to a public interface
  or forward them; any local process that reaches 8300 can drive the modem.

## See also

- `man halcyon-switch`, `man halcyon-host`
- `README.md`, section *PAT and Winlink*
- PAT's own documentation for `pat connect` and `pat configure`
