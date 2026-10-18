"""Module-3 tests: mock DBs, verification fan-out, blackboard. All hermetic."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settleready.agents.verification import verify_case
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore
from settleready.tools.mock_dbs import MockClueReports, MockLienholderAPI, MockStateTitleDB


def _data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "title_db.json").write_text(json.dumps({
        "VIN1": {"state": "TX", "brands": []},
        "VIN2": {"state": "GA", "brands": ["salvage"]},
    }))
    (d / "liens.json").write_text(json.dumps({
        "VIN1": {"holder": "Capital Auto Finance", "amount": 4200},
        "VIN2": None,
    }))
    (d / "clue.json").write_text(json.dumps({"POL1": [], "POL2": [{"claim": "old"}]}))
    # tally.db with no promotions (fresh episodic memory)
    ReturnTallyStore(str(d / "tally.db")).close()
    return str(d)


def _fields(**over):
    base = {
        "vin": {"value": "VIN1", "confidence": 0.95},
        "title_state": {"value": "TX", "confidence": 0.9},
        "policy_number": {"value": "POL1", "confidence": 0.95},
        "lien_declared": {"value": False, "confidence": 0.9},
        "loss_type": {"value": "theft", "confidence": 0.95},
    }
    base.update(over)
    return base


# -- mock DBs ---------------------------------------------------------------
def test_mock_dbs(tmp_path):
    dd = _data_dir(tmp_path)
    assert MockStateTitleDB(dd).lookup("VIN1")["state"] == "TX"
    assert MockStateTitleDB(dd).lookup("NOPE") is None
    assert MockLienholderAPI(dd).lien_for("VIN1")["amount"] == 4200
    assert MockLienholderAPI(dd).lien_for("VIN2") is None
    assert MockClueReports(dd).history("POL1") == []
    assert len(MockClueReports(dd).history("POL2")) == 1


# -- verification -------------------------------------------------------------
def test_undeclared_lien_conflict(tmp_path):
    dd = _data_dir(tmp_path)
    r = verify_case("C1", _fields(), ["claim_form", "police_report", "title"], data_dir=dd)
    assert r["passed"] is False
    assert [c["reason_code"] for c in r["conflicts"]] == ["UNDECLARED_LIEN"]
    assert any(c["check"] == "lien_check" and c["status"] == "conflict" for c in r["checks"])


def test_declared_lien_matching_passes(tmp_path):
    dd = _data_dir(tmp_path)
    f = _fields(**{"lien_declared": {"value": True, "confidence": 0.9}})
    r = verify_case("C1", f, ["claim_form", "police_report", "title"], data_dir=dd)
    assert r["conflicts"] == []
    assert any(c["check"] == "lien_check" and c["status"] == "pass" for c in r["checks"])


def test_missing_title_doc_fails_completeness(tmp_path):
    dd = _data_dir(tmp_path)
    r = verify_case("C1", _fields(), ["claim_form", "police_report"], data_dir=dd)
    assert r["passed"] is False
    assert "missing_documents" in r["flags"]
    assert any(c["check"] == "completeness" and c["status"] == "fail" for c in r["checks"])


def test_title_brand_flagged(tmp_path):
    dd = _data_dir(tmp_path)
    f = _fields(**{"vin": {"value": "VIN2", "confidence": 0.95},
                   "title_state": {"value": "GA", "confidence": 0.9}})
    r = verify_case("C2", f, ["claim_form", "police_report", "title"], data_dir=dd)
    assert "title_brand_present" in r["flags"]


def test_promoted_upfront_check_runs_first(tmp_path):
    dd = _data_dir(tmp_path)
    tally = ReturnTallyStore(os.path.join(dd, "tally.db"))
    for _ in range(3):
        tally.log_return(reason_code="UNDECLARED_LIEN", field="lien_status", title_state="GA")
    tally.close()
    f = _fields(**{"vin": {"value": "VIN2", "confidence": 0.95},
                   "title_state": {"value": "GA", "confidence": 0.9}})
    r = verify_case("C2", f, ["claim_form", "police_report", "title"], data_dir=dd)
    upfront = [c for c in r["checks"] if c["status"] == "upfront"]
    assert upfront and upfront[0]["check"] == "verify_lienholder_first"


# -- blackboard -----------------------------------------------------------------
def test_blackboard_roundtrip(tmp_path):
    bb = CaseBlackboard(str(tmp_path / "bb.db"))
    bb.create_case("C9", loss_type="theft", title_state="TX")
    bb.set_stage("C9", "extraction")
    bb.write("C9", "fields", {"vin": {"value": "VIN1"}})
    assert bb.read("C9", "fields")["vin"]["value"] == "VIN1"
    assert bb.read("C9")["fields"]["vin"]["value"] == "VIN1"
    assert bb.get_case("C9")["stage"] == "extraction"
    events = [e["event"] for e in bb.audit_log("C9")]
    assert "case_created" in events and "stage_transition" in events
    bb.close()
