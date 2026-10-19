#!/usr/bin/env bash
# Fresh start: wipe all runtime state and regenerate only the environment
# (statute corpus + mock title/lien/CLUE/valuation records). No demo cases,
# no seeded inbox, no tally history — cases are created by the intake watcher
# as real claim emails arrive.
#
# Usage: ./scripts/fresh.sh
# To load the 3 canned demo fixtures instead (eval + scripted demo):
#   ./scripts/seed.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.data.seed --seed 42 --out data --env-only
# runtime state the watcher/pipeline/UI create as they work
rm -f data/blackboard.db data/tally.db data/intake_state.json data/inbox.json
rm -rf data/chroma
echo "Fresh data/ ready. Next:"
echo "  .venv/bin/python scripts/build_index.sh   # build the statute RAG index"
echo "  .venv/bin/python -m settleready.agents.intake watch   # start the intake daemon"
