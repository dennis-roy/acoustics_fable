#!/usr/bin/env bash
# Checks an offline update package before it is installed, then unpacks it.
#   deploy/verify-release.sh eol-<commit>.tar.gz
# Needs eol-<commit>.tar.gz.sig beside it, and deploy/allowed_signers (the integrator's public key).
# Then: cd eol-<commit> && sudo deploy/install.sh
set -euo pipefail
pkg=${1:?usage: verify-release.sh <package.tar.gz>}
signers=$(dirname "$0")/allowed_signers
[ -f "$signers" ] || { echo "no $signers: ask the integrator for it" >&2; exit 1; }
ssh-keygen -Y verify -f "$signers" -I eol-release@zynith -n eol-release -s "$pkg.sig" < "$pkg"
tar -xzf "$pkg"
echo "verified and unpacked: $(tar -tzf "$pkg" | head -1)"
