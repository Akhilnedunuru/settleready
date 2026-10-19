#!/usr/bin/env bash
# Generate a single missing document for a case (customer follow-up demo).
# The customer was asked for a document by email; this builds the synthetic
# PDF they "send back" -- attach it to your reply email.
#
# Usage: bash scripts/gen_doc.sh TL-2026-1048 title
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi
if [ $# -ne 2 ]; then
  echo "Usage: bash scripts/gen_doc.sh <CASE_ID> <claim_form|police_report|title|repair_estimate>"
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.data.seed --out data --gen-doc "$1" "$2"
echo "Attach data/pdfs/$1/$2.pdf to your reply email (keep the Re: subject)."
