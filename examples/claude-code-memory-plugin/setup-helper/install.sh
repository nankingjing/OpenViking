#!/usr/bin/env bash
set -euo pipefail

SHARED_REL="../../memory-plugin-shared/install.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
LOCAL_SHARED="$SCRIPT_DIR/$SHARED_REL"

if [ -f "$LOCAL_SHARED" ]; then
  exec bash "$LOCAL_SHARED" --harness claude "$@"
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/ov-memory-install.XXXXXX")" || { echo "mktemp failed" >&2; exit 1; }
trap 'rm -f "$tmp"' EXIT
curl -fsSL -o "$tmp" "https://raw.githubusercontent.com/volcengine/OpenViking/main/examples/memory-plugin-shared/install.sh"
exec bash "$tmp" --harness claude "$@"
