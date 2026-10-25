#!/usr/bin/env bash
# Operator-side relay server for the shared demo mailbox.
# The relay is the ONLY process holding the Gmail app password; clones talk
# to it with a demo token and never see the secret. See
# src/settleready/tools/relay_server.py for the security model and setup.
# Usage: bash scripts/relay_server.sh [--port 8899]
#   Secrets live in relay.env on this machine (never committed):
#     GMAIL_ADDRESS=...
#     GMAIL_APP_PASSWORD=<redacted>
#     RELAY_TOKEN=public-demo-token
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.tools.relay_server "$@"
