"""Audit rows carry stage outcomes (not just {} details).

set_stage logs the transition at stage entry, before the outcome exists, so
the pipeline fills the row in once the stage finishes (blackboard
set_audit_details). These tests pin that contract at both levels: the
blackboard round-trip, and a stubbed end-to-end pipeline run for the clean
and non-clean branches.
"""
import os

import pytest

from settleready.memory.blackboard import CaseBlackboard
from settleready import pipeline


def _bb(tmp_path):
    return CaseBlackboard(str(tmp_path / "bb.db"))


def test_set_audit_details_roundtrip(tmp_path):
    bb = _bb(tmp_path)
    bb.create_case("C1")
    row = bb.set_stage("C1", "extraction")
    assert isinstance(row, int)
    bb.set_audit_details(row, {"fields_at_confidence": 8, "fields_total": 8})
    rows = bb.audit_log("C1")
    by_event = {r["event"]: r for r in rows}
    assert by_event["stage_transition"]["details"] == {
        "fields_at_confidence": 8, "fields_total": 8}
    # sibling rows untouched: case_created keeps its own details
    assert by_event["case_created"]["details"] == {
        "loss_type": "", "title_state": ""}
    # unknown row id is a no-op, not an error
    bb.set_audit_details(999999, {"x": 1})
    assert len(bb.audit_log("C1")) == len(rows)
    bb.close()


def _fields():
    def f(v):
        return {"value": v, "confidence": 1.0, "source": "claim_form.pdf p.1"}
    return {"vin": f("VIN1"), "mileage": f(41000), "title_state": f("GA"),
            "policy_number": f("GA-B-1"), "holder_name": f("Aisha Webb"),
            "loss_type": f("FIRE"), "loss_date": f("2026-09-20"),
            "lien_declared": f(False)}


def _run(tmp_path, monkeypatch, report):
    dd = str(tmp_path)
    case_id = "TL-2026-1099"
    os.makedirs(os.path.join(dd, "pdfs", case_id))
    open(os.path.join(dd, "pdfs", case_id, "claim_form.pdf"), "wb").close()
    monkeypatch.setattr(pipeline, "parse_document",
                        lambda f: [{"page": 1, "text": "x"}])
    monkeypatch.setattr(pipeline, "extract_fields",
                        lambda docs, **kw: {"fields": _fields(), "trace": [],
                                            "flags": []})
    monkeypatch.setattr(pipeline, "verify_case", lambda *a, **k: report)
    monkeypatch.setattr(pipeline, "research",
                        lambda *a, **k: {"citations": ["GA.md"], "answer": "a",
                                         "chunks": []})
    monkeypatch.setattr(pipeline, "compute_acv",
                        lambda **k: (22300, {"formula": "f"}))

    class _Val:
        def __init__(self, data_dir):
            pass

        def comparables(self, case_id):
            return [{}, {}, {}, {}, {}, {}]

    monkeypatch.setattr(pipeline, "MockValuationService", _Val)
    return pipeline.run_case(case_id, data_dir=dd)


def test_clean_run_audit_rows_carry_outcomes(tmp_path, monkeypatch):
    report = {"passed": True,
              "checks": [{"check": f"c{i}", "status": "pass"} for i in range(5)],
              "conflicts": [], "flags": []}
    out = _run(tmp_path, monkeypatch, report)
    assert out["next"] == "ready_for_review"
    rows = {(r["stage"], r["event"]): r["details"] for r in out["audit"]}
    assert rows[("intake", "stage_transition")] == {"documents": ["claim_form"]}
    assert rows[("extraction", "stage_transition")] == {
        "fields_at_confidence": 8, "fields_total": 8, "below_threshold": []}
    assert rows[("verification", "stage_transition")] == {
        "passed": True, "pass": 5, "fail": 0, "conflict": 0, "conflicts": []}
    assert rows[("research", "stage_transition")] == {
        "title_state": "GA", "citations": ["GA.md"]}
    assert rows[("compute", "stage_transition")] == {
        "acv": 22300, "comparables": 6, "mileage": 41000}
    assert rows[("ready_for_review", "routed")] == {"acv": 22300}


def test_nonclean_run_verification_row_carries_conflict(tmp_path, monkeypatch):
    import settleready.agents.disposition as disp
    monkeypatch.setattr(disp, "plan_disposition",
                        lambda *a, **k: {"outcome": "escalate"})
    report = {"passed": False,
              "checks": [{"check": "lien_check", "status": "conflict"}],
              "conflicts": [{"reason_code": "UNDECLARED_LIEN"}], "flags": []}
    out = _run(tmp_path, monkeypatch, report)
    assert out["next"] == "escalated"
    rows = {(r["stage"], r["event"]): r["details"] for r in out["audit"]}
    assert rows[("verification", "stage_transition")] == {
        "passed": False, "pass": 0, "fail": 0, "conflict": 1,
        "conflicts": ["UNDECLARED_LIEN"]}
    assert rows[("needs_info", "routed")] == {
        "passed": False, "conflicts": ["UNDECLARED_LIEN"]}
