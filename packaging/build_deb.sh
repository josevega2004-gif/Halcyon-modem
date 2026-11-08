#!/usr/bin/env bash
# Build a Debian binary package of Halcyon for one architecture.
#
#   packaging/build_deb.sh --arch amd64
#   packaging/build_deb.sh --arch arm64
#
# Nothing is installed and no root is needed: the tree is staged under
# build/deb/<arch>/root and handed to dpkg-deb, which records root:root
# ownership itself.  The result lands in dist/.
#
# arm64 needs an aarch64 compiler.  Either install it (g++-aarch64-linux-gnu)
# and let the script find aarch64-linux-gnu-g++, or point CROSS_ROOT at an
# unpacked copy of the Debian cross packages, as tools/get_cross_toolchain.sh
# does; the script then uses that compiler with --sysroot and a private
# LD_LIBRARY_PATH for binutils.
set -euo pipefail
umask 022

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
out=${OUT:-"$root/dist"}
maint=${MAINT:-"Halcyon Modem Project <halcyon@example.org>"}

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
}

arch=""
while [ $# -gt 0 ]; do
    case "$1" in
        --arch) arch=${2:?--arch needs a value}; shift 2 ;;
        --arch=*) arch=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "build_deb.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$arch" ] || arch=$(dpkg --print-architecture)

case "$arch" in
    amd64|arm64) ;;
    *) echo "build_deb.sh: supported architectures are amd64 and arm64, not '$arch'" >&2; exit 2 ;;
esac

version=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)
[ -n "$version" ] || { echo "build_deb.sh: no version in pyproject.toml" >&2; exit 1; }
deb_version="$version-1"

# --- compiler -------------------------------------------------------------
extra_flags=()
case "$arch" in
    amd64)
        cxx=${CXX_AMD64:-g++}
        ;;
    arm64)
        if [ -n "${CROSS_ROOT:-}" ]; then
            cxx="$CROSS_ROOT/usr/bin/aarch64-linux-gnu-g++-15"
            [ -x "$cxx" ] || cxx=$(find "$CROSS_ROOT/usr/bin" -maxdepth 1 -name 'aarch64-linux-gnu-g++*' -print -quit 2>/dev/null)
            [ -n "$cxx" ] || { echo "build_deb.sh: no compiler under CROSS_ROOT=$CROSS_ROOT" >&2; exit 1; }
            extra_flags+=("--sysroot=$CROSS_ROOT")
            shlibs=$(dirname "$(find "$CROSS_ROOT" -name 'libopcodes-*.so' | head -1)")
            export LD_LIBRARY_PATH="$shlibs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        else
            cxx=${CXX_ARM64:-aarch64-linux-gnu-g++}
            command -v "$cxx" >/dev/null 2>&1 || {
                cat >&2 <<'EOF'
build_deb.sh: no aarch64 compiler found.

Install one:            sudo apt-get install g++-aarch64-linux-gnu
or fetch a private one: tools/get_cross_toolchain.sh && \
                        CROSS_ROOT=.toolchain/root packaging/build_deb.sh --arch arm64
EOF
                exit 1
            }
        fi
        ;;
esac

echo "==> halcyon $deb_version for $arch (compiler: $cxx)"

# --- stage the package tree ----------------------------------------------
build="$root/build/deb/$arch"
pkgroot="$build/root"
rm -rf "$build"
install -d "$pkgroot/DEBIAN" "$pkgroot/usr/bin" \
           "$pkgroot/usr/lib/python3/dist-packages" \
           "$pkgroot/usr/share/doc/halcyon" "$pkgroot/usr/share/man/man1"

cp -a "$root/halcyon" "$pkgroot/usr/lib/python3/dist-packages/"
find "$pkgroot/usr/lib/python3/dist-packages" -type d -name __pycache__ -prune -print0 \
    | xargs -0 -r rm -rf
find "$pkgroot/usr/lib/python3/dist-packages" -name '*.pyc' -delete

cat > "$pkgroot/usr/bin/halcyon" <<'LAUNCHER'
#!/bin/sh
# Halcyon console: serves the modem UI on the loopback interface.
# See halcyon(1); it takes no configuration beyond --host and --port.
exec python3 -m halcyon.ui "$@"
LAUNCHER
chmod 0755 "$pkgroot/usr/bin/halcyon"

cat > "$pkgroot/usr/bin/halcyon-host" <<'LAUNCHER'
#!/bin/sh
# Halcyon host-mode modem service: the two loopback ports PAT dials.
# See halcyon-host(1) and /usr/share/doc/halcyon/PAT.md.
exec python3 -m halcyon.host "$@"
LAUNCHER
chmod 0755 "$pkgroot/usr/bin/halcyon-host"

cat > "$pkgroot/usr/bin/halcyon-switch" <<'LAUNCHER'
#!/bin/sh
# Point PAT at Halcyon, or restore the previous modem.
# See halcyon-switch(1) and /usr/share/doc/halcyon/PAT.md.
exec python3 -m halcyon.switch "$@"
LAUNCHER
chmod 0755 "$pkgroot/usr/bin/halcyon-switch"

echo "==> building halcyon-modem and halcyon-tui"
make -s -C "$root/cpp" BUILD="$build/cpp" CXX="$cxx" \
     CXXFLAGS="-O2 -std=c++17 -Wall -Wextra -I$root/cpp/include ${extra_flags[*]:-}" \
     >/dev/null
install -m 0755 "$build/cpp/halcyon-modem" "$pkgroot/usr/bin/halcyon-modem"
install -m 0755 "$build/cpp/halcyon-tui" "$pkgroot/usr/bin/halcyon-tui"
# halcyon-modem reads its rate tables from this file at run time.
install -d "$pkgroot/usr/share/halcyon"
install -m 0644 "$root/cpp/data/wide.bin" "$pkgroot/usr/share/halcyon/wide.bin"

# The Qt window needs Qt for the target, which a cross build does not have:
# pkg-config would hand it the host's Qt.  Ship it on a native build instead,
# and let the terminal front end cover the architectures without it.
elfs=("$pkgroot/usr/bin/halcyon-modem" "$pkgroot/usr/bin/halcyon-tui")
gui_built=0
gui_depends=""
if [ "$arch" = "$(dpkg --print-architecture)" ] \
        && command -v pkg-config >/dev/null 2>&1 \
        && pkg-config --exists Qt6Widgets Qt6Network 2>/dev/null; then
    echo "==> building halcyon-gui"
    make -s -C "$root/cpp" BUILD="$build/cpp" CXX="$cxx" \
         CXXFLAGS="-O2 -std=c++17 -Wall -Wextra -I$root/cpp/include" \
         gui >/dev/null
    install -m 0755 "$build/cpp/halcyon-gui" "$pkgroot/usr/bin/halcyon-gui"
    elfs+=("$pkgroot/usr/bin/halcyon-gui")
    gui_built=1
    gui_depends=", libqt6widgets6, libqt6network6, qt6-qpa-plugins"
else
    echo "==> halcyon-gui skipped: Qt 6 for $arch is not available on this build host"
fi

# Never ship a binary built for the wrong machine.
want=x86-64; [ "$arch" = arm64 ] && want=aarch64
for binary in "${elfs[@]}"; do
    got=$(file -b "$binary")
    case "$got" in
        *"$want"*) ;;
        *) echo "build_deb.sh: ${binary##*/} is not $want: $got" >&2; exit 1 ;;
    esac
done

# We ship ELFs, so policy wants a versioned libc6 dependency, and the version
# has to be the one the binaries actually reference rather than a guess.  Read
# it before stripping, while every symbol version is still present.
command -v strings >/dev/null 2>&1 || { echo "build_deb.sh: need strings to read the glibc requirement" >&2; exit 1; }
glibc=$(
    for binary in "${elfs[@]}"; do
        strings "$binary" | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)*' | sed 's/GLIBC_//'
    done | sort -V | tail -1
)
[ -n "$glibc" ] || { echo "build_deb.sh: cannot read the glibc requirement of the C++ binaries" >&2; exit 1; }
echo "==> C++ binaries need libc6 >= $glibc"

strip_tool=$(command -v strip)
[ "$arch" = arm64 ] && [ -x "${CROSS_ROOT:-}/usr/bin/aarch64-linux-gnu-strip" ] \
    && strip_tool="$CROSS_ROOT/usr/bin/aarch64-linux-gnu-strip"
for binary in "${elfs[@]}"; do
    "$strip_tool" --strip-unneeded "$binary" 2>/dev/null || true
done

install -m 0644 "$here/copyright" "$pkgroot/usr/share/doc/halcyon/copyright"
install -m 0644 "$here/config.example.json" \
    "$pkgroot/usr/share/doc/halcyon/config.example.json"
install -m 0644 "$root/docs/PAT.md" \
    "$pkgroot/usr/share/doc/halcyon/PAT.md"
install -m 0644 "$root/docs/VARAC.md" \
    "$pkgroot/usr/share/doc/halcyon/VARAC.md"
install -m 0644 "$root/CREDITS.md" \
    "$pkgroot/usr/share/doc/halcyon/CREDITS.md"
gzip -9n -c "$here/halcyon.1"        > "$pkgroot/usr/share/man/man1/halcyon.1.gz"
gzip -9n -c "$here/halcyon-modem.1"  > "$pkgroot/usr/share/man/man1/halcyon-modem.1.gz"
gzip -9n -c "$here/halcyon-tui.1"    > "$pkgroot/usr/share/man/man1/halcyon-tui.1.gz"
gzip -9n -c "$here/halcyon-host.1"   > "$pkgroot/usr/share/man/man1/halcyon-host.1.gz"
gzip -9n -c "$here/halcyon-switch.1" > "$pkgroot/usr/share/man/man1/halcyon-switch.1.gz"
[ "$gui_built" = 1 ] && gzip -9n -c "$here/halcyon-gui.1" \
    > "$pkgroot/usr/share/man/man1/halcyon-gui.1.gz"

cat > "$pkgroot/usr/share/doc/halcyon/changelog.Debian" <<EOF
halcyon ($deb_version) unstable; urgency=medium

  * Local build from the Halcyon source tree.

 -- $maint  $(date -R)
EOF
gzip -9n "$pkgroot/usr/share/doc/halcyon/changelog.Debian"
chmod 0644 "$pkgroot/usr/share/doc/halcyon/changelog.Debian.gz"

# cp -a keeps the author's 0664/0775 modes, and group-writable files in a
# package are a policy violation, so normalise the whole tree.
find "$pkgroot" -type d -exec chmod 0755 {} +
find "$pkgroot" -type f -exec chmod 0644 {} +
chmod 0755 "$pkgroot/usr/bin/halcyon" "$pkgroot/usr/bin/halcyon-modem" \
           "$pkgroot/usr/bin/halcyon-tui" "$pkgroot/usr/bin/halcyon-host" \
           "$pkgroot/usr/bin/halcyon-switch"
[ "$gui_built" = 1 ] && chmod 0755 "$pkgroot/usr/bin/halcyon-gui"

size=$(du -sk --exclude=DEBIAN "$pkgroot" | cut -f1)
sed -e "s|@VERSION@|$deb_version|" -e "s|@ARCH@|$arch|" \
    -e "s|@MAINT@|$maint|" -e "s|@SIZE@|$size|" -e "s|@LIBC@|$glibc|" \
    -e "s|@GUI_DEPENDS@|$gui_depends|" \
    "$here/control.in" > "$pkgroot/DEBIAN/control"
chmod 0644 "$pkgroot/DEBIAN/control"

( cd "$pkgroot" && find . -path ./DEBIAN -prune -o -type f -print \
    | sed 's|^\./||' | LC_ALL=C sort | xargs -d '\n' md5sum > DEBIAN/md5sums )
chmod 0644 "$pkgroot/DEBIAN/md5sums"

# --- build the archive ----------------------------------------------------
mkdir -p "$out"
deb="$out/halcyon_${deb_version}_${arch}.deb"
rm -f "$deb"
dpkg-deb --build --root-owner-group "$pkgroot" "$deb" >/dev/null
echo "==> $deb"
dpkg-deb -f "$deb" Package Version Architecture Installed-Size Depends
