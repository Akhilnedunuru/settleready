"""Module-5 tests: reviewer loop, packet builder, eval metrics. All hermetic."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settleready.agents.builder import build_packet
from settleready.agents.reviewer import MAX_RETURNS, REASON_CODES, submit_review
from settleready.eval.metrics import evaluate_case, run_eval
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore


def _case_with_fields(tmp_path, case_id="C1", title_state="GA"):
    dd = str(tmp_path)
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    bb.create_case(case_id, loss_type="fire", title_state=title_state)
    bb.write(case_id, "fields", {
        "vin": {"value": "V1", "confidence": 0.95, "source": "claim_form.pdf p.1"},
        "title_state": {"value": title_state, "confidence": 0.9, "source": "title.pdf p.1"},
    })
    bb.write(case_id, "verification", {"checks": [], "conflicts": [], "flags": [], "passed": True})
    bb.close()
    ReturnTallyStore(os.path.join(dd, "tally.db")).close()
    return dd


# -- reviewer ------------------------------------------------------------------
def test_approve(tmp_path):
    dd = _case_with_fields(tmp_path)
    out = submit_review("C1", "approve", note="looks good", data_dir=dd)
    assert out["stage"] == "approved"
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    assert bb.get_case("C1")["stage"] == "approved"
    bb.close()


def test_return_logs_tally_and_reworks(tmp_path):
    dd = _case_with_fields(tmp_path)
    out = submit_review("C1", "return", reason_code="WEAK_COMPS", data_dir=dd)
    assert out["stage"] == "analyst_rework"
    assert out["return_count"] == 1
    tally = ReturnTallyStore(os.path.join(dd, "tally.db"))
    rows = {(r["reason_code"], r["field"], r["title_state"]): r["count"] for r in tally.tallies()}
    assert rows[("WEAK_COMPS", "comps", "GA")] == 1
    tally.close()


def test_second_return_escalates(tmp_path):
    dd = _case_with_fields(tmp_path)
    submit_review("C1", "return", reason_code="WEAK_COMPS", data_dir=dd)
    out = submit_review("C1", "return", reason_code="WEAK_COMPS", data_dir=dd)
    assert out["stage"] == "escalated", f"bounded loop: {MAX_RETURNS} returns then human"
    assert out["return_count"] == 2


def test_third_matching_return_promotes(tmp_path):
    dd = _case_with_fields(tmp_path)
    tally = ReturnTallyStore(os.path.join(dd, "tally.db"))
    for _ in range(2):  # seeded history, like scripts/seed.sh does
        tally.log_return(reason_code="UNDECLARED_LIEN", field="lien_status", title_state="GA")
    tally.close()
    out = submit_review("C1", "return", reason_code="UNDECLARED_LIEN", data_dir=dd)
    assert out["promoted"] is True
    assert "verify_lienholder_first" in out["promotion_detail"]


def test_return_requires_valid_reason(tmp_path):
    dd = _case_with_fields(tmp_path)
    try:
        submit_review("C1", "return", reason_code="BOGUS", data_dir=dd)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown reason code")
    assert set(REASON_CODES) >= {"UNDECLARED_LIEN", "WEAK_COMPS", "MISSING_TITLE_DOC"}


# -- builder ----------------------------------------------------------------------
def test_build_packet(tmp_path):
    dd = _case_with_fields(tmp_path)
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    bb.write("C1", "legal_research", {
        "answer": "100% of ACV [TX.md]", "citations": ["TX.md"],
        "chunks": [{"text": "threshold chunk", "citation": "TX.md",
                    "state": "TX", "score": 0.1}]})
    bb.close()
    p = build_packet("C1", data_dir=dd)
    assert p["case_id"] == "C1"
    assert p["fields"]["vin"]["value"] == "V1"
    assert any("claim_form.pdf" in c for c in p["citations"])
    assert p["return_count"] == 0
    assert p["legal"]["chunks"][0]["citation"] == "TX.md"


# -- eval ----------------------------------------------------------------------------
def _spec():
    return {
        "case_id": "C1", "loss_type": "theft", "title_state": "TX", "expected_acv": 17800,
        "vehicle": {"vin": "V1", "mileage": 92000},
        "policy": {"number": "P1", "holder": "Jane Doe"},
        "loss": {"date": "2026-09-14"}, "declared_lien": False,
    }


def test_evaluate_case_perfect():
    state = {"fields": {
        "vin": {"value": "V1", "confidence": 0.95}, "mileage": {"value": 92000, "confidence": 0.9},
        "title_state": {"value": "TX", "confidence": 0.9}, "policy_number": {"value": "P1", "confidence": 0.95},
        "holder_name": {"value": "Jane Doe", "confidence": 0.9}, "loss_type": {"value": "THEFT", "confidence": 0.9},
        "loss_date": {"value": "2026-09-14", "confidence": 0.9}, "lien_declared": {"value": False, "confidence": 0.9}},
        "acv": {"acv": 17800}, "legal_research": {"citations": ["TX.md"]}}
    r = evaluate_case(_spec(), state)
    assert r["extraction_accuracy"] == 1.0
    assert r["acv_error_pct"] == 0.0
    assert r["citation_ok"] is True


def test_evaluate_case_partial():
    state = {"fields": {"vin": {"value": "WRONG", "confidence": 0.5}},
             "acv": {"acv": 18000}, "legal_research": {"citations": ["GA.md"]}}
    r = evaluate_case(_spec(), state)
    assert r["extraction_accuracy"] < 1.0
    assert r["extraction_coverage"] == round(1 / 8, 3)
    assert r["low_confidence_rate"] == 1.0
    assert r["citation_ok"] is False
    assert r["acv_error_pct"] == round(abs(18000 - 17800) / 17800 * 100, 2)


def test_run_eval_skips_unrun_cases(tmp_path):
    dd = str(tmp_path)
    (tmp_path / "cases.json").write_text(json.dumps([_spec(), dict(_spec(), case_id="C2")]))
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    bb.create_case("C1")
    bb.write("C1", "fields", {"vin": {"value": "V1", "confidence": 0.9}})
    bb.close()
    out = run_eval(dd)
    assert out["summary"]["n_scored"] == 1
    assert out["summary"]["skipped"] == ["C2"]
