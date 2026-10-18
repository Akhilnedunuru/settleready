#!/usr/bin/env bash
# Build the local Chroma RAG index from the synthetic statute corpus.
# Usage: ./scripts/build_index.sh   (run ./scripts/seed.sh first)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi
if [ ! -f .env ]; then
  echo "Copy .env.example to .env and add your OPENAI_API_KEY first."
  exit 1
fi

.venv/bin/python -m settleready.tools.rag build-index --corpus data/statutes --persist data/chroma
echo "Index built at ./data/chroma"
