#!/usr/bin/env bash
# Repo-scoped commit helper for PRelude.
#
# The user's global Claude Code settings deny `git add` everywhere. For this repo
# only, they allow Claude to commit through this script, which:
#   - refuses to run outside the PRelude repository,
#   - stages only the explicitly listed paths (no `.`, `-A`, `-u`, or globs),
#   - then commits with the given message.
#
# Usage: scripts/commit-paths.sh "<commit message>" <path> [<path>...]
set -euo pipefail

EXPECTED_ROOT="/local/home/shaoliu/myProjects/PRelude"

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "$root" != "$EXPECTED_ROOT" ]]; then
  echo "commit-paths: refusing to run outside $EXPECTED_ROOT (got '${root:-not a git repo}')" >&2
  exit 1
fi

if [[ $# -lt 2 ]]; then
  echo "usage: $0 \"<commit message>\" <path> [<path>...]" >&2
  exit 1
fi

message="$1"
shift

for p in "$@"; do
  case "$p" in
    .|./|-*|*'*'*|*'?'*|*'['*)
      echo "commit-paths: refusing broad or option-like path '$p'; list files explicitly" >&2
      exit 1
      ;;
  esac
done

git -C "$root" add -- "$@"
git -C "$root" commit -m "$message" -- "$@"
