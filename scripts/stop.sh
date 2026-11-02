#!/usr/bin/env bash
# Stop any running SettleReady UI + watcher, cleanly.
# Usage: ./scripts/stop.sh
# Use this instead of Ctrl-Z (which orphans the watcher) when Ctrl-C is slow.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "[stop] stopping watcher…"
pkill -f "settlesready.agents.intake watch" 2>/dev/null || true
pkill -f "settleready.agents.intake watch" 2>/dev/null || true

echo "[stop] stopping Streamlit UI…"
if command -v lsof >/dev/null 2>&1; then
  lsof -ti:8501 | xargs kill 2>/dev/null || true
fi
pkill -f "streamlit run src/settleready/ui/app.py" 2>/dev/null || true

sleep 3

# Force-kill anything still hanging around.
pkill -9 -f "settlesready.agents.intake watch" 2>/dev/null || true
pkill -9 -f "settleready.agents.intake watch" 2>/dev/null || true
if command -v lsof >/dev/null 2>&1; then
  lsof -ti:8501 | xargs kill -9 2>/dev/null || true
fi

left_w=$(pgrep -f "intake watch" | wc -l | tr -d ' ')
if command -v lsof >/dev/null 2>&1; then
  left_u=$(lsof -ti:8501 | wc -l | tr -d ' ')
else
  left_u=0
fi
if [ "$left_w" = "0" ] && [ "$left_u" = "0" ]; then
  echo "[stop] clean — no watcher or UI processes left."
else
  echo "[stop] WARNING: $left_w watcher(s), $left_u process(es) on :8501 still alive."
fi
