"""Suspicion score tests (Lab 6.2, lightweight)."""
import os

from settleready.memory.blackboard import CaseBlackboard
from settleready.obs.suspicion import compute_suspicion


def _seed(tmp_path, stage="needs_info", fields=None, conflicts=None,
          ai_returns=0, returns=0):
    bb = CaseBlackboard(os.path.join(str(tmp_path), "blackboard.db"))
    bb.create_case("TL-2026-1043", loss_type="theft", title_state="TX")
    bb.set_stage("TL-2026-1043", stage)
    if fields is not None:
        bb.write("TL-2026-1043", "fields", fields)
    if conflicts is not None:
        bb.write("TL-2026-1043", "conflicts", conflicts)
    if ai_returns:
        bb.write("TL-2026-1043", "ai_return_count", ai_returns)
    if returns:
        bb.write("TL-2026-1043", "return_count", returns)
    bb.close()


def _full_fields(conf=0.95):
    return {f: {"value": "x", "confidence": conf, "source": "t"}
            for f in ["vin", "mileage", "title_state", "policy_number",
                      "holder_name", "loss_type", "loss_date", "lien_declared"]}


def test_clean_case_scores_zero(tmp_path):
    _seed(tmp_path, stage="ready_for_review", fields=_full_fields())
    out = compute_suspicion("TL-2026-1043", str(tmp_path))
    assert out["score"] == 0
    assert out["level"] == "routine"
    assert out["signals"] == []


def test_unresolved_lien_conflict_adds_signals(tmp_path):
    _seed(tmp_path, fields=_full_fields(), conflicts=[
        {"reason_code": "UNDECLARED_LIEN", "field": "lien_declared",
         "status": "unresolved"}])
    out = compute_suspicion("TL-2026-1043", str(tmp_path))
    names = [s["signal"] for s in out["signals"]]
    assert "unresolved_conflict" in names
    assert "high_stakes_conflict" in names
    assert out["score"] == 40  # 25 + 15
    assert out["level"] == "routine"


def test_low_confidence_fields_add_signal(tmp_path):
    _seed(tmp_path, fields=_full_fields(conf=0.7))
    out = compute_suspicion("TL-2026-1043", str(tmp_path))
    assert "low_confidence_fields" in [s["signal"] for s in out["signals"]]
    assert out["score"] == 15


def test_score_caps_at_100_and_escalates(tmp_path):
    _seed(tmp_path, fields=_full_fields(conf=0.5), conflicts=[
        {"reason_code": "UNDECLARED_LIEN", "field": "lien_declared",
         "status": "unresolved"}],
        ai_returns=2, returns=2)
    out = compute_suspicion("TL-2026-1043", str(tmp_path))
    # 15 (low conf) + 25 + 15 (conflicts) + 20 (ai returns) + 20 (analyst) = 95
    assert out["score"] == 95
    assert out["level"] == "escalate"
