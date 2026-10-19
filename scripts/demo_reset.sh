#!/usr/bin/env bash
# Full morning reset: wipe ALL case state, reload the synthetic world +
# N demo cases, rebuild the RAG index. One command before you start demoing.
#
# Usage: bash scripts/demo_reset.sh [N] [--no-tally-seed]
#   (default N=15; --no-tally-seed leaves the episodic return-tally empty
#   instead of pre-seeding 2x GA UNDECLARED_LIEN — useful when testing the
#   promotion behavior from scratch)
# Then:  bash scripts/start.sh              (watcher + UI)
set -euo pipefail
cd "$(dirname "$0")/.."
N="${1:-15}"
TALLY_FLAG="${2:-}"

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

bash scripts/fresh.sh
bash scripts/seed.sh "$N" "$TALLY_FLAG"
bash scripts/build_index.sh
echo ""
echo "Reset complete: $N cases seeded, RAG index built, all case state wiped."
echo "Start with:  bash scripts/start.sh"
