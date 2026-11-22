#!/usr/bin/env bash
# Makes an offline update package for a customer: a tarball of one commit, its SHA-256, and an
# SSH signature (ssh-keygen -Y; OpenSSH 8 or newer, no other tools).
#   deploy/make-release.sh ~/.ssh/eol_release_key [commit]
# Out: build/release/eol-<commit>.tar.gz, .sha256, .tar.gz.sig
# The customer verifies with deploy/verify-release.sh, against deploy/allowed_signers.
set -euo pipefail
key=${1:?usage: make-release.sh <private signing key> [commit]}
commit=$(git rev-parse --short "${2:-HEAD}")
out=$(git rev-parse --show-toplevel)/build/release
mkdir -p "$out"
pkg=$out/eol-$commit.tar.gz
git archive --format=tar.gz --prefix="eol-$commit/" -o "$pkg" "$commit"
(cd "$out" && sha256sum "$(basename "$pkg")" > "eol-$commit.sha256")
ssh-keygen -Y sign -f "$key" -n eol-release "$pkg"
echo "made: $pkg (+ .sha256, + .sig)"
