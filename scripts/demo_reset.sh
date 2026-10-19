#!/usr/bin/env bash
# Full morning reset: wipe ALL case state, reload the synthetic world +
# N demo cases, rebuild the RAG index. One command before you start demoing.
#
# Usage: bash scripts/demo_reset.sh [N]     (default N=15)
# Then:  bash scripts/start.sh              (watcher + UI)
set -euo pipefail
cd "$(dirname "$0")/.."
N="${1:-15}"

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

bash scripts/fresh.sh
bash scripts/seed.sh "$N"
bash scripts/build_index.sh
echo ""
echo "Reset complete: $N cases seeded, RAG index built, all case state wiped."
echo "Start with:  bash scripts/start.sh"
