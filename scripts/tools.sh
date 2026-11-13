#!/usr/bin/env bash
# Pinned per-project CLIs in .cache/bin (kind, kagent), checksum-verified.
# Leaves any global kind alone: other clusters on this machine may depend on it.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env
BIN=.cache/bin; mkdir -p "$BIN"
OS=$(uname -s | tr '[:upper:]' '[:lower:]'); ARCH=$(uname -m); [ "$ARCH" = x86_64 ] && ARCH=amd64; [ "$ARCH" = aarch64 ] && ARCH=arm64

fetch() { # name version url sha_url
  local name=$1 ver=$2 url=$3 sha_url=$4 stamp="$BIN/.$1-$2"
  [ -x "$BIN/$name" ] && [ -f "$stamp" ] && return 0
  echo "downloading $name $ver"
  curl -fsSL -o "$BIN/$name.tmp" "$url"
  local want; want=$(curl -fsSL "$sha_url" | awk '{print $1}')
  local got; got=$(shasum -a 256 "$BIN/$name.tmp" | awk '{print $1}')
  [ "$want" = "$got" ] || { echo "checksum mismatch for $name: want $want got $got"; rm -f "$BIN/$name.tmp"; exit 1; }
  chmod +x "$BIN/$name.tmp"; mv "$BIN/$name.tmp" "$BIN/$name"; rm -f "$BIN/.$name-"*; touch "$stamp"
}

fetch kind "$KIND_VERSION" \
  "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-${OS}-${ARCH}" \
  "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-${OS}-${ARCH}.sha256sum"
fetch kagent "$KAGENT_VERSION" \
  "https://github.com/kagent-dev/kagent/releases/download/${KAGENT_VERSION}/kagent-${OS}-${ARCH}" \
  "https://github.com/kagent-dev/kagent/releases/download/${KAGENT_VERSION}/kagent-${OS}-${ARCH}.sha256"
"$BIN/kind" version; "$BIN/kagent" version 2>/dev/null | head -3 || true
