#!/usr/bin/env bash
# Explicitly mark this checkout as installed or development.
set -euo pipefail
PROFILE="${1:-}"
case "$PROFILE" in
  installed|development) ;;
  *) echo "Usage: $0 installed|development" >&2; exit 2 ;;
esac
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
printf '%s\n' "$PROFILE" > "$HERE/valvebench-build-profile.tmp"
mv "$HERE/valvebench-build-profile.tmp" "$HERE/valvebench-build-profile"
printf 'ValveBench build profile set to %s in %s\n' "$PROFILE" "$HERE"
