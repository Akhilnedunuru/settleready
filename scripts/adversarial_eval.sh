#!/usr/bin/env bash
# Adversarial eval (Module 3): hostile inputs the agent must fail SAFELY on —
# conflicts flagged with both values preserved, missing docs caught, decision
# boundaries exact, retrieval bait resisted, uncovered questions abstained.
# Runs against an isolated temp world (never touches ./data) + the real
# ./data/chroma index. Build the index first: bash scripts/demo_reset.sh
# Usage: bash scripts/adversarial_eval.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi
if [ ! -d data/chroma ]; then
  echo "No RAG index at data/chroma — run bash scripts/demo_reset.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.eval.adversarial "$@"
