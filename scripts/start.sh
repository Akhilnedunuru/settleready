#!/usr/bin/env bash
# Single-terminal starter: intake watcher (background) + Streamlit UI (foreground).
# Both logs appear here, prefixed [watcher] / [ui]. Ctrl-C stops both.
#
# Implementation note: the watcher uses process substitution >(sed ...) instead
# of a pipeline, so $! is the *python* PID — `kill` in the EXIT trap actually
# stops the watcher (a `python | sed &` pipeline would capture sed's PID and
# leave the watcher running after Ctrl-C).
#
# Usage: ./scripts/start.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi

cleanup() {
  kill "$WATCHER_PID" 2>/dev/null || true
}
trap cleanup EXIT

export PYTHONPATH="$PWD/src"
# -u: unbuffered, so watcher logs appear immediately through the substitution.
.venv/bin/python -u -m settleready.agents.intake watch --interval 15 \
  > >(sed 's/^/[watcher] /') 2>&1 &
WATCHER_PID=$!
echo "[start] watcher running (pid $WATCHER_PID); starting UI… (Ctrl-C stops both)"

.venv/bin/streamlit run src/settleready/ui/app.py 2>&1 | sed 's/^/[ui] /'
