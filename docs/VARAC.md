# Using Halcyon with VarAC

VarAC is an HF chat client. It does not do modulation itself: it drives a modem
over two loopback TCP ports, 8300 for commands and events and 8301 for data.
Halcyon's host mode speaks that command set on exactly those ports, so VarAC
can open a link through it.

## The short version

```
halcyon-switch varac --mycall N0CALL
```

That starts the host service if it is not running, sends `VERSION` to the
command port, and prints what the client needs:

```
Host: started (pid 41231), log /home/you/.cache/halcyon/host.log
Modem answered: VERSION Halcyon 0.1 host mode

In VarAC's modem settings use:
  modem      the HF modem entry (VARA HF)
  address    127.0.0.1
  port       8300
  bandwidth  2300

VarAC opens the data port itself; it is 8301 here.
Host mode carries the link's bytes only: PTT and audio stay on the radio side.
```

In VarAC, open the modem or radio settings, choose the HF modem entry, and
check the three values: the address `127.0.0.1` and port `8300` are VarAC's own
defaults, and the bandwidth should match the selector you passed
(`500`, `2300` or `2750`). The labels around those fields move between VarAC
versions; the values do not.

When you are done, stop the service:

```
halcyon-switch off
```

VarAC keeps its connection settings in its own interface, so there is nothing
of its configuration for `halcyon-switch` to back up or restore. `off` only
stops the host that the tool started.

## Doing it by hand

```
halcyon-host --mycall N0CALL --bandwidth 2300
```

Then enter the same address, port and bandwidth in VarAC. `halcyon-host` stays
in the foreground and reports what the client sends, which is the quickest way
to see where a session stalls.

## What VarAC sends

VarAC uses the same command set as the other host-mode clients. The host
accepts `MYCALL`, `BW500`/`BW2300`/`BW2750`, `LISTEN`, `CONNECT`,
`DISCONNECT`, `ABORT`, `VERSION`, `COMPRESSION`, `PTT`, `PUBLIC`, `WINLINK`,
`CHAT`, `CQFRAME`, `IGNOREKISSDCD`, `CLEANTXBUFFER`, `QUIT` and `CWID`, and
answers `VERSION`, `PENDING`, `CONNECTED`, `DISCONNECTED`, `PTT ON|OFF`,
`BUFFER`, `SN`, `BUSY` and `IAMALIVE`.

Commands it does not implement are logged with a leading `?` and otherwise
ignored: the protocol has no error channel, so a client cannot be told that an
extension is missing.

## Checking the connection

```
halcyon-switch varac --no-host      # probe only, start nothing
ss -ltnp | grep -E '830[01]'        # are both ports listening?
```

A probe that prints `VERSION Halcyon 0.1 host mode` means the command port is
answering. If it prints nothing, nothing is listening: start the host.

The host's log is `~/.cache/halcyon/host.log` when the tool started it, or the
terminal running `halcyon-host`.

## Troubleshooting

**VarAC reports that the modem is not responding.** The host is not running, or
it is on a different port. Check with `halcyon-switch varac --no-host` and
`ss -ltnp | grep 8300`.

**`Address already in use`.** Another modem service or an earlier Halcyon host
holds the port. `halcyon-switch status` says whether the running process is a
Halcyon host, and `halcyon-switch off` stops the one it started.

**The link opens but no data moves.** The far end has to be speaking the same
frame format, and host mode only carries the link's bytes; the audio path is a
separate problem (see the limits below).

**Wrong bandwidth.** VarAC sends its own `BW...` command when it dials, and that
wins over the host's `--bandwidth` default. Match the two so the client and the
modem agree on the geometry.

## Limits

- Host mode is the network side of the modem. Audio, PTT into a rig and the
  sound-card path are not part of it: this project has no audio front end yet,
  so an over-the-air VarAC chat needs that path from somewhere else. What the
  host provides is the modem service VarAC dials, on loopback.
- A session that carries chat needs a station at the other end running Halcyon.
  This modem does not interoperate with other HF data modems.
- The connection was exercised here against PAT, which drives the same two
  ports and command set. A VarAC installation was not available for a live
  test, so treat the settings above as the configuration to try and check the
  host log if VarAC will not attach.

## See also

- `man halcyon-switch`, `man halcyon-host`
- [`PAT.md`](PAT.md) for the PAT side of the same service
