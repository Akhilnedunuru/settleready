"""Pipeline: intake -> extract -> verify -> research -> compute -> blackboard
(modules 1-4 live here; the human review loop arrives in module 5).

Each stage reads and writes the Case Blackboard — the contract between stages.
Run:  PYTHONPATH=src python -m settleready.pipeline --case-id TL-2026-1043 --data data
"""
from __future__ import annotations

import glob
import os

from settleready.agents.extraction import extract_fields
from settleready.agents.outreach import maybe_send_info_request, needs_from_report
from settleready.agents.researcher import research
from settleready.agents.verification import verify_case
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.events import emit as _emit_event
from settleready.tools.acv import compute_acv
from settleready.tools.mock_dbs import MockValuationService
from settleready.tools.pdf_parser import parse_document


def _doc_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]  # claim_form.pdf -> claim_form


def _running_marker(data_dir: str, case_id: str) -> str:
    return os.path.join(data_dir, "running", case_id)


def is_running(data_dir: str, case_id: str, stale_after: int = 180) -> bool:
    """Is the pipeline actively processing this case? A marker file written
    at pipeline start (and removed at the end) says so; markers older than
    `stale_after` seconds are treated as crashed, not running."""
    import time
    try:
        age = time.time() - os.path.getmtime(_running_marker(data_dir, case_id))
        return age < stale_after
    except OSError:
        return False


def run_case(case_id: str, data_dir: str = "data", provider=None) -> dict:
    """Run intake -> extraction -> verification for one case, via the blackboard."""
    import time
    os.makedirs(os.path.join(data_dir, "running"), exist_ok=True)
    with open(_running_marker(data_dir, case_id), "w") as f:
        f.write(str(time.time()))
    _emit_event(data_dir, case_id, "pipeline_started")
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        return _run_case_inner(case_id, data_dir, provider, bb)
    except Exception as e:
        _emit_event(data_dir, case_id, "pipeline_failed", {"error": str(e)})
        raise
    finally:
        try:
            os.remove(_running_marker(data_dir, case_id))
        except OSError:
            pass
        _emit_event(data_dir, case_id, "pipeline_finished")
        bb.close()


def _run_case_inner(case_id: str, data_dir: str, provider, bb: CaseBlackboard) -> dict:
    summary: dict = {"case_id": case_id, "stages": []}
    bb.create_case(case_id)

    # -- intake: parse the case's PDFs -------------------------------------
    bb.set_stage(case_id, "intake")
    pdf_dir = os.path.join(data_dir, "pdfs", case_id)
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs for {case_id} in {pdf_dir} — run scripts/seed.sh first.")
    docs = []
    for f in pdfs:
        name = os.path.basename(f)
        pages = parse_document(f)
        docs.append({"source": name, "pages": pages})
        _emit_event(data_dir, case_id, "tool_call",
                    {"tool": "pdf_parser.parse_document",
                     "args": {"file": name},
                     "result": f"{len(pages)} page(s) extracted"})
    docs_present = [_doc_name(f) for f in pdfs]
    bb.write(case_id, "docs_present", docs_present)
    _emit_event(data_dir, case_id, "documents_parsed",
                {"count": len(docs), "docs": docs_present})
    # Customer follow-up statements (text replies) join the extraction corpus
    # as their own document, so a confirmation in words ("yes, the lien is
    # with Lone Star") can resolve a conflict without new PDFs.
    for i, stmt in enumerate(bb.read(case_id, "customer_statements") or []):
        docs.append({"source": f"customer_reply_{i + 1}.txt", "override": True,
                     "pages": [{"page": 1, "text": stmt["body"]}]})
    summary["stages"].append(f"intake: parsed {len(docs)} documents {docs_present}")

    # -- extraction: ReAct loop --------------------------------------------
    bb.set_stage(case_id, "extraction")
    extraction = extract_fields(docs, provider=provider, case_id=case_id,
                                data_dir=data_dir)
    bb.write(case_id, "fields", extraction["fields"])
    bb.write(case_id, "extraction_trace", extraction["trace"])
    bb.write(case_id, "extraction_flags", extraction["flags"])
    n_ok = sum(1 for v in extraction["fields"].values() if v["confidence"] >= 0.8)
    summary["stages"].append(
        f"extraction: {n_ok} fields at confidence>=0.8, flags={extraction['flags']}")
    summary["trace"] = extraction["trace"]  # ReAct Thought->Action->Observation steps

    # -- verification: fan-out ----------------------------------------------
    bb.set_stage(case_id, "verification")
    report = verify_case(case_id, extraction["fields"], docs_present, data_dir=data_dir)
    bb.write(case_id, "verification", report)
    _emit_event(data_dir, case_id, "verification_checks",
                {"passed": report["passed"],
                 "checks": [{"check": c["check"], "status": c["status"]}
                            for c in report["checks"]]})
    summary["stages"].append(
        f"verification: passed={report['passed']}, "
        f"conflicts={[c['reason_code'] for c in report['conflicts']]}, flags={report['flags']}")

    # -- routing ---------------------------------------------------------------
    if not report["passed"]:
        next_stage = "needs_info"
        bb.set_stage(case_id, next_stage, event="routed",
                     details={"passed": False,
                              "conflicts": [c["reason_code"] for c in report["conflicts"]]})
        # Analyst agent's targeted follow-up (checkpoint-1 workflow): ask the
        # claimant for exactly what's missing. Deduplicated per need-set, so
        # re-runs don't re-send. The customer's reply re-triggers the pipeline
        # via the intake watcher, closing the loop.
        holder = (extraction["fields"].get("holder_name") or {}).get("value", "")
        outreach = maybe_send_info_request(
            data_dir, case_id, needs_from_report(report), holder_name=holder or "")
        summary["outreach"] = outreach
        if outreach.get("escalated"):
            next_stage = "escalated"
        summary["next"] = next_stage
        summary["audit"] = bb.audit_log(case_id)
        return summary

    # -- research: state law for this title state --------------------------------
    bb.set_stage(case_id, "research")
    title_state = (extraction["fields"].get("title_state") or {}).get("value", "")
    if not title_state:
        # Fallback: our policy numbers are prefixed with the state (TX-A-...).
        # An empty title_state would make state-filtered retrieval return
        # nothing; deriving it keeps legal research working instead of
        # silently producing an empty section.
        policy_no = (extraction["fields"].get("policy_number") or {}).get("value", "")
        prefix = (policy_no.split("-")[0] or "") if policy_no else ""
        if len(prefix) == 2 and prefix.isalpha():
            title_state = prefix.upper()
            _emit_event(data_dir, case_id, "state_fallback",
                        {"from": "policy_number", "state": title_state})
    legal = research("What is the total loss threshold and valuation method?",
                     title_state, data_dir=data_dir, provider=provider,
                     case_id=case_id)
    bb.write(case_id, "legal_research", legal)
    _emit_event(data_dir, case_id, "legal_research",
                {"citations": legal["citations"]})
    summary["stages"].append(
        f"research: {title_state} threshold answered with citations {legal['citations']}")

    # -- compute: deterministic ACV (money math never via LLM) -------------------
    bb.set_stage(case_id, "compute")
    fields = extraction["fields"]
    comps = MockValuationService(data_dir).comparables(case_id)
    mileage = (fields.get("mileage") or {}).get("value") or 0
    acv, worksheet = compute_acv(comparables=comps, mileage=mileage)
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "acv.compute_acv",
                 "args": {"comparables": len(comps), "mileage": mileage},
                 "result": f"ACV=${acv:,}"})
    bb.write(case_id, "acv", {"acv": acv, "worksheet": worksheet})
    summary["stages"].append(f"compute: ACV=${acv:,} ({worksheet['formula']})")

    next_stage = "ready_for_review"
    bb.set_stage(case_id, next_stage, event="routed", details={"acv": acv})
    summary["next"] = next_stage
    summary["audit"] = bb.audit_log(case_id)
    return summary


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Run the SettleReady pipeline for one case")
    p.add_argument("--case-id", default="TL-2026-1043")
    p.add_argument("--data", default="data")
    p.add_argument("--show-trace", action="store_true",
                   help="print the ReAct Thought->Action->Observation trace")
    a = p.parse_args()

    out = run_case(a.case_id, data_dir=a.data)
    print(f"\nCase {out['case_id']} -> {out['next']}")
    for s in out["stages"]:
        print(f"  {s}")
    if a.show_trace:
        print("  ReAct trace:")
        for t in out.get("trace", []):
            print(f"    THOUGHT: {t['thought']}")
            print(f"    ACTION : {t['action']}")
            print(f"    OBS    : {t['observation']}\n")
    print("  audit:")
    for e in out["audit"]:
        print(f"    {e['ts']} [{e['stage']}] {e['event']}")
