#!/usr/bin/env bash
# Generate the "missing" documents for each case into data/customer_docs/<case_id>/.
# These are the files the customer (you, in testing) attaches when replying to
# the follow-up email. E.g. 1043 is missing the title -> data/customer_docs/TL-2026-1043/title.pdf
#
# Usage: bash scripts/gen_customer_docs.sh
# Then: attach the PDF from data/customer_docs/<case_id>/ to your reply email.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Run bash scripts/setup.sh first."
  exit 1
fi

export PYTHONPATH="$PWD/src"
.venv/bin/python - <<'EOF'
import json, os
from settleready.data.seed import _DOC_BUILDERS

OUT = "data"
with open(os.path.join(OUT, "cases.json")) as f:
    cases = json.load(f)

# All possible doc types
ALL_DOCS = set(_DOC_BUILDERS.keys())

for case in cases:
    case_id = case["case_id"]
    have = set(case.get("documents", []))
    missing = ALL_DOCS - have
    # Only generate docs that make sense as customer replies (title, repair_estimate)
    # claim_form and police_report are intake-only.
    # repair_estimate only for collision cases (they have repair_estimate_total).
    customer_docs = []
    if "title" in missing:
        customer_docs.append("title")
    if "repair_estimate" in missing and "repair_estimate_total" in case:
        customer_docs.append("repair_estimate")
    if not customer_docs:
        continue
    dest = os.path.join(OUT, "customer_docs", case_id)
    os.makedirs(dest, exist_ok=True)
    for doc in customer_docs:
        _DOC_BUILDERS[doc](case, dest)
        print(f"Generated {dest}/{doc}.pdf")
EOF

echo ""
echo "Done. Attach files from data/customer_docs/<case_id>/ to your reply emails."
