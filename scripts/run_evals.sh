#!/usr/bin/env bash
# Run the Module 6 eval suite: deterministic metrics + eval report.
# Usage: bash scripts/run_evals.sh [data_dir]
# The LLM-judge is run per-case separately (it needs an API key):
#   PYTHONPATH=src python -m settleready.eval.judge --data data --case TL-2026-1044
set -euo pipefail
cd "$(dirname "$0")/.."
DATA_DIR="${1:-data}"
export PYTHONPATH="$PWD/src"

echo "[evals] deterministic metrics…"
.venv/bin/python -m settleready.eval.metrics --data "$DATA_DIR" || true
echo ""
echo "[evals] building report…"
.venv/bin/python -m settleready.eval.report --data "$DATA_DIR" --out eval-report.md
echo "[evals] done — see eval-report.md"
