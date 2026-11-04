#!/usr/bin/env bash
# Fetch an aarch64 C++ toolchain into .toolchain/ without root, so
#   CROSS_ROOT=.toolchain/root packaging/build_deb.sh --arch arm64
# can produce a real arm64 package on an amd64 machine.
#
# This unpacks the Debian cross packages locally instead of installing them.
# It is only a convenience: if g++-aarch64-linux-gnu is installed properly,
# build_deb.sh uses it and none of this is needed.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
dest="$root/.toolchain"
mkdir -p "$dest/debs" "$dest/root"

pkgs=(
    g++-15-aarch64-linux-gnu
    gcc-15-aarch64-linux-gnu
    libstdc++-15-dev-arm64-cross
    libc6-dev-arm64-cross
    libc6-arm64-cross
    linux-libc-dev-arm64-cross
    libgcc-15-dev-arm64-cross
    binutils-aarch64-linux-gnu
)

echo "==> downloading ${#pkgs[@]} cross packages into $dest/debs"
( cd "$dest/debs" && apt-get download "${pkgs[@]}" )

for d in "$dest"/debs/*.deb; do
    dpkg -x "$d" "$dest/root"
done

# The emulator lets us actually run the arm64 binary to check it.
( cd "$dest/debs" && apt-get download qemu-user ) || echo "note: qemu-user unavailable; skipping" >&2
for d in "$dest"/debs/qemu-user*.deb; do
    [ -e "$d" ] && dpkg -x "$d" "$dest/root"
done

echo "==> toolchain ready: $dest/root"
ls "$dest"/root/usr/bin/ | grep -E 'aarch64-linux-gnu-g\+\+|^qemu-aarch64$' || true
echo
echo "next: CROSS_ROOT=$dest/root $here/build_deb.sh --arch arm64"
