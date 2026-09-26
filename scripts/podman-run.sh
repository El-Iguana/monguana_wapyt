#!/usr/bin/env bash
# Rebuild and restart the local *development* Monguana container from the
# working tree and the sibling wapyt checkout. Linux + Podman only.
# To install Monguana, use compose instead: see INSTALL.md.
#
#   scripts/podman-run.sh            rebuild and restart
#   scripts/podman-run.sh --logs     ... and follow the logs
#
# Host networking so profiles can point at MongoDB on this machine
# (127.0.0.1:27017) or the LAN, exactly as from a local run; the app binds to
# 127.0.0.1 so it is not published to the network. Plain podman rather than
# compose, which needs the podman socket running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=monguana
IMAGE=localhost/monguana
PORT="${PORT:-8766}"
VOLUME=monguana_data

cd "$ROOT"

[ -f .env ] || { echo "no .env — copy .env.example and set MONGUANA_ADMIN_PASS" >&2; exit 1; }
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"

# Build against the sibling wapyt checkout, not the commit the Containerfile
# pins, so unmerged widgetset work shows up here. A filtered copy, because the
# checkout carries a test virtualenv of a couple of hundred MB.
WAPYT="$(cd ../wa_pytincture_widgetset && pwd)"
CONTEXT="$(mktemp -d)"
trap 'rm -rf "$CONTEXT"' EXIT
tar -C "$WAPYT" --exclude=.git --exclude=.venv --exclude=__pycache__ \
    --exclude=build --exclude=dist --exclude='*.whl' -cf - . | tar -C "$CONTEXT" -xf -

echo "==> building $IMAGE:$VERSION (wapyt from $WAPYT)"
podman build --build-context "wapyt-src=$CONTEXT" \
  -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile .

echo "==> restarting $NAME"
podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d \
  --name "$NAME" \
  --hostname "$NAME" \
  --restart unless-stopped \
  --label app=Monguana \
  --network host \
  --userns=keep-id:uid=10001,gid=999 \
  --env-file .env \
  -e MONGUANA_DATA_DIR=/data \
  -e MONGUANA_BIND=127.0.0.1 \
  -e "PORT=$PORT" \
  -e "MONGUANA_CANONICAL_ORIGIN=http://127.0.0.1:$PORT" \
  -v "$VOLUME:/data:U" \
  "$IMAGE:latest" >/dev/null

printf '==> waiting for startup'
for _ in $(seq 1 90); do
  if podman logs "$NAME" 2>&1 | grep -q "Application startup complete"; then
    echo; echo "    Monguana $VERSION on http://127.0.0.1:$PORT/monguana"
    echo "    (use 127.0.0.1, not localhost — pytincture requires a literal loopback address)"
    [ "${1:-}" = "--logs" ] && podman logs -f "$NAME"
    exit 0
  fi
  printf '.'; sleep 1
done

echo; echo "did not start; last lines:" >&2
podman logs "$NAME" 2>&1 | tail -20 >&2
exit 1
