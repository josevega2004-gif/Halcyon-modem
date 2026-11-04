# Debian packages

Two binary packages, one per architecture:

```
dist/halcyon_0.1.0-1_amd64.deb
dist/halcyon_0.1.0-1_arm64.deb
```

Build them with:

```bash
packaging/build_deb.sh --arch amd64
packaging/build_deb.sh --arch arm64
```

No root and no `dpkg-buildpackage` is involved: the script stages a tree under
`build/deb/<arch>/root` and calls `dpkg-deb --build --root-owner-group`, which
records `root:root` ownership itself. The version comes from `pyproject.toml`,
so the two packages can never disagree about it.

## What is inside

| Path | What it is |
| --- | --- |
| `/usr/bin/halcyon` | the console, `python3 -m halcyon.ui` |
| `/usr/bin/halcyon-modem` | the C++17 modem core, compiled for this architecture |
| `/usr/lib/python3/dist-packages/halcyon/` | modem, link layer, radio clients, web UI |
| `/usr/share/man/man1/halcyon*.1.gz` | man pages |
| `/usr/share/doc/halcyon/` | copyright, changelog, PAT/VarAC guides, credits |

`Depends: python3, python3-numpy, python3-scipy`; `Recommends: hamlib-utils`
for CAT control, since `rigctld` only matters if you drive a rig through it.

The package is architecture-specific rather than `Architecture: all` only
because of `halcyon-modem`; everything else is pure Python. Building for arm64
therefore needs an aarch64 compiler — install `g++-aarch64-linux-gnu`, or let
`tools/get_cross_toolchain.sh` unpack the cross packages into `.toolchain/`
(no root) and pass `CROSS_ROOT=.toolchain/root`. The script refuses to build a
package whose binary does not match the architecture it claims.

## Licence and maintainer

The package is `GPL-3+`.  `packaging/copyright` carries the DEP-5 stanza and
points at `/usr/share/common-licenses/GPL-3`, so the archive does not repeat the
full text; `LICENSE` in the source tree has it for anyone reading the code.

The maintainer address in `build_deb.sh` is still a placeholder
(`halcyon@example.org` is a documentation domain, so mail to it goes nowhere).
Set `MAINT="Your Name <you@example.org>"` or edit the default before handing the
package to anyone: Debian policy wants a reachable maintainer, and the changelog
trailer records whatever is used.
