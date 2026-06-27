#!/usr/bin/env bash
# Install/upgrade docker buildx as a per-user CLI plugin (no sudo).
# Needed because the system buildx (shipped with the Docker package) is older
# than what the freshly-installed `docker compose` requires for `compose build`.
set -euo pipefail

PLUGIN_DIR="${HOME}/.docker/cli-plugins"
DEST="${PLUGIN_DIR}/docker-buildx"

# Map uname arch -> buildx asset arch.
case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

echo "Resolving latest buildx release..."
V="$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest \
     | grep -oP '"tag_name":\s*"\K[^"]+')"
[ -n "$V" ] || { echo "could not resolve latest version" >&2; exit 1; }
echo "Latest: ${V} (linux/${ARCH})"

mkdir -p "$PLUGIN_DIR"
curl -fSL "https://github.com/docker/buildx/releases/download/${V}/buildx-${V}.linux-${ARCH}" \
  -o "$DEST"
chmod +x "$DEST"

echo "Installed -> ${DEST}"
docker buildx version
