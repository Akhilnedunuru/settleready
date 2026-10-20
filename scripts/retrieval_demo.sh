#!/usr/bin/env bash
# Retrieval A/B demo (checkpoint 3.1): the same legal question answered from
# parametric memory vs grounded in state-filtered retrieval, side by side.
# Requires the RAG index — run bash scripts/demo_reset.sh first if data/ is fresh.
# Usage: bash scripts/retrieval_demo.sh [--state TX] [--question "..."]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.eval.retrieval_demo "$@"
