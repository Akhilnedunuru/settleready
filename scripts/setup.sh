#!/usr/bin/env bash
# One-command environment setup for SettleReady.
# Usage: ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Checking Python version..."
PY=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "    found Python $PY"
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"' \
  || { echo "ERROR: Python 3.11+ required (brew install python@3.11)"; exit 1; }

if ! command -v tesseract >/dev/null 2>&1; then
  echo "WARNING: tesseract not found — install with: brew install tesseract"
  echo "         (only needed for the scanned-PDF fallback in the Doc Parser)"
fi

echo "==> Creating virtual environment (.venv)..."
python3 -m venv .venv

echo "==> Installing dependencies (this takes a few minutes)..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo "==> Creating runtime data directories..."
mkdir -p data

echo ""
echo "Done. Next steps:"
echo "  1. cp .env.example .env   # then paste your OPENAI_API_KEY"
echo "  2. .venv/bin/python scripts/smoke_test.py"
echo "  3. ./scripts/seed.sh && ./scripts/build_index.sh"
