#!/usr/bin/env bash
# Restart PRelude so other machines on this network (e.g. a phone on the same VPN) can reach it.
# Usage: scripts/serve-lan.sh [port]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
port="${1:-8787}"
host="$(hostname -I | awk '{print $1}')"

pid="$(ss -ltnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [[ -n "$pid" ]]; then
  kill "$pid"
  sleep 1
fi

mkdir -p "$root/.prelude-data"
nohup "$root/.venv/bin/prelude" serve --host "$host" --port "$port" >"$root/.prelude-data/server.log" 2>&1 &
sleep 2

echo "PRelude is listening on $host:$port"
echo "Check from your phone: http://$(hostname -f):$port/healthz"
