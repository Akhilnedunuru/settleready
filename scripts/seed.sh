#!/usr/bin/env bash
# Generate deterministic synthetic fixtures: claim PDFs, mock DB records,
# statute corpus, seeded return-tally history.
# Usage: ./scripts/seed.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.data.seed --seed 42 --out data
echo "Fixtures written to ./data/"
