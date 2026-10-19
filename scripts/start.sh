#!/usr/bin/env bash
# Single-terminal starter: intake watcher (background) + Streamlit UI (foreground).
# Both logs appear here, prefixed [watcher] / [ui]. Ctrl-C stops both.
#
# Usage: ./scripts/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.agents.intake watch --interval 15 2>&1 \
  | sed 's/^/[watcher] /' &
WATCHER_PID=$!
trap "kill $WATCHER_PID 2>/dev/null" EXIT INT TERM
echo "[start] watcher running (pid $WATCHER_PID); starting UI…"
.venv/bin/streamlit run src/settleready/ui/app.py 2>&1 | sed 's/^/[ui] /'
