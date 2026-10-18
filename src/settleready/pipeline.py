"""Pipeline: intake -> extract -> verify -> blackboard (modules 1-3 live here;
RAG / compute / review loop arrive in modules 4-5).

Each stage reads and writes the Case Blackboard — the contract between stages.
Run:  PYTHONPATH=src python -m settleready.pipeline --case-id TL-2026-1043 --data data
"""
from __future__ import annotations

import glob
import os

from settleready.agents.extraction import extract_fields
from settleready.agents.verification import verify_case
from settleready.memory.blackboard import CaseBlackboard
from settleready.tools.pdf_parser import parse_document


def _doc_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]  # claim_form.pdf -> claim_form


def run_case(case_id: str, data_dir: str = "data", provider=None) -> dict:
    """Run intake -> extraction -> verification for one case, via the blackboard."""
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.create_case(case_id)
    summary: dict = {"case_id": case_id, "stages": []}

    # -- intake: parse the case's PDFs -------------------------------------
    bb.set_stage(case_id, "intake")
    pdf_dir = os.path.join(data_dir, "pdfs", case_id)
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs for {case_id} in {pdf_dir} — run scripts/seed.sh first.")
    docs = [{"source": os.path.basename(f), "pages": parse_document(f)} for f in pdfs]
    docs_present = [_doc_name(f) for f in pdfs]
    bb.write(case_id, "docs_present", docs_present)
    summary["stages"].append(f"intake: parsed {len(docs)} documents {docs_present}")

    # -- extraction: ReAct loop --------------------------------------------
    bb.set_stage(case_id, "extraction")
    extraction = extract_fields(docs, provider=provider, case_id=case_id)
    bb.write(case_id, "fields", extraction["fields"])
    bb.write(case_id, "extraction_trace", extraction["trace"])
    bb.write(case_id, "extraction_flags", extraction["flags"])
    n_ok = sum(1 for v in extraction["fields"].values() if v["confidence"] >= 0.8)
    summary["stages"].append(
        f"extraction: {n_ok} fields at confidence>=0.8, flags={extraction['flags']}")

    # -- verification: fan-out ----------------------------------------------
    bb.set_stage(case_id, "verification")
    report = verify_case(case_id, extraction["fields"], docs_present, data_dir=data_dir)
    bb.write(case_id, "verification", report)
    summary["stages"].append(
        f"verification: passed={report['passed']}, "
        f"conflicts={[c['reason_code'] for c in report['conflicts']]}, flags={report['flags']}")

    # -- routing ---------------------------------------------------------------
    next_stage = "ready_for_review" if report["passed"] else "needs_info"
    bb.set_stage(case_id, next_stage, event="routed",
                 details={"passed": report["passed"],
                          "conflicts": [c["reason_code"] for c in report["conflicts"]]})
    summary["next"] = next_stage
    summary["audit"] = bb.audit_log(case_id)
    bb.close()
    return summary


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Run the SettleReady pipeline for one case")
    p.add_argument("--case-id", default="TL-2026-1043")
    p.add_argument("--data", default="data")
    a = p.parse_args()

    out = run_case(a.case_id, data_dir=a.data)
    print(f"\nCase {out['case_id']} -> {out['next']}")
    for s in out["stages"]:
        print(f"  {s}")
    print("  audit:")
    for e in out["audit"]:
        print(f"    {e['ts']} [{e['stage']}] {e['event']}")
