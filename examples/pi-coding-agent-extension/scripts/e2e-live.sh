#!/usr/bin/env bash
# Live e2e acceptance gate — real pi + real OpenViking + real LLM.
# Requires: OPENVIKING_URL, OPENVIKING_API_KEY, SUPER_RELAY_API_KEY (see e2e-live.mjs).
set -euo pipefail
cd "$(dirname "$0")/.."
exec node scripts/e2e-live.mjs "$@"
