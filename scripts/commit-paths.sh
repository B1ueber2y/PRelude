#!/usr/bin/env bash
# Commit explicitly listed files in this repository only.
# Usage: scripts/commit-paths.sh "<message>" <path>...
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
here="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "$here" != "$root" ]]; then
  echo "commit-paths: run this from inside $root" >&2
  exit 1
fi

if [[ $# -lt 2 ]]; then
  echo "usage: $0 \"<message>\" <path>..." >&2
  exit 1
fi

message="$1"
shift

for p in "$@"; do
  case "$p" in
    .|./|-*|*'*'*|*'?'*|*'['*)
      echo "commit-paths: list files explicitly (got '$p')" >&2
      exit 1
      ;;
  esac
done

git -C "$root" add -- "$@"
git -C "$root" commit -m "$message" -- "$@"
