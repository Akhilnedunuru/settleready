#!/usr/bin/env bash
# Generate deterministic synthetic fixtures: claim PDFs, mock DB records,
# statute corpus, seeded return-tally history.
# Usage: ./scripts/seed.sh [num_cases]   (default 3; first 3 are hand-designed)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.data.seed --seed 42 --out data --num-cases "${1:-3}"
echo "Fixtures written to ./data/"
