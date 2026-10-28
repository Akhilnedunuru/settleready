"""Disposition Planner tests (Checkpoint 4.1 ToT): beam search, pruning,
budgets, dedupe, escalation, and the LangChain/LangGraph wiring.

All hermetic: no provider (heuristic mode) unless a FakeProvider is passed,
no network, no real inbox. The planner must never raise into the pipeline.
"""
import json
import os

import pytest

from settleready.agents.disposition import (
    BEAM_WIDTH,
    MAX_DEPTH,
    _initial_state,
    _run_graph,
    _run_loop,
    gaps_from_report,
    plan_disposition,
)
from settleready.memory.blackboard import CaseBlackboard


def _world(tmp_path, case_id="C1", followups=0, asked_needs=None):
    dd = str(tmp_path)
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    bb.create_case(case_id)
    if followups:
        bb.write(case_id, "followup_count", followups)
    if asked_needs is not None:
        bb.write(case_id, "info_requested", json.dumps(sorted(asked_needs)))
    bb.close()
    return dd


def _report(conflicts=(), failed_checks=()):
    checks = [{"check": c, "status": "fail", "detail": d}
              for c, d in failed_checks]
    return {"case_id": "C1", "checks": checks,
            "conflicts": list(conflicts), "flags": [], "passed": False}


_MISMATCH = {"reason_code": "LIENHOLDER_MISMATCH", "field": "lien_holder",
             "detail": "form names Capital Auto Finance; registry shows First Community Bank"}
_UNDECLARED = {"reason_code": "UNDECLARED_LIEN", "field": "lien_status",
               "detail": "registry shows Lone Star $7,417"}
_TITLE_FAIL = ("completeness", "Missing required documents: ['title']")


def test_combined_email_beats_single_asks(tmp_path):
    dd = _world(tmp_path)
    out = plan_disposition("C1", _report(conflicts=[_MISMATCH],
                                         failed_checks=[_TITLE_FAIL]),
                           data_dir=dd)
    assert out["outcome"] == "email"
    assert len(out["needs"]) == 2  # lien question + title request, one email
    assert any("lienholder" in n for n in out["needs"])
    assert any("title" in n for n in out["needs"])
    assert len(out["selected_plan"]["emails"]) == 1  # one send, both gaps


def test_analyst_gap_never_emailed(tmp_path):
    dd = _world(tmp_path)
    out = plan_disposition(
        "C1",
        _report(conflicts=[_MISMATCH],
                failed_checks=[("valuation_data",
                                "No comparable listings found for valuation")]),
        data_dir=dd)
    assert out["outcome"] == "email"
    assert len(out["needs"]) == 1  # only the lien question
    assert all("comparable" not in n for n in out["needs"])
    assert any(g["id"] == "check:valuation_data"
               for g in out["analyst_flags"])


def test_analyst_only_case_routes_rework(tmp_path):
    dd = _world(tmp_path)
    out = plan_disposition(
        "C1",
        _report(failed_checks=[("valuation_data",
                                "No comparable listings found for valuation")]),
        data_dir=dd)
    assert out["outcome"] == "analyst_rework"
    assert out["needs"] == []
    assert out["analyst_flags"]


def test_budget_exhausted_escalates(tmp_path):
    dd = _world(tmp_path, followups=3)
    out = plan_disposition("C1", _report(conflicts=[_MISMATCH]),
                           data_dir=dd)
    assert out["outcome"] == "escalate"


def test_already_asked_gap_is_not_reasked(tmp_path):
    # Seed the ask first so the planner can dedupe against it.
    gaps, _ = gaps_from_report(_report(conflicts=[_MISMATCH]))
    dd = _world(tmp_path, asked_needs=[gaps[0]["ask_text"]])
    out = plan_disposition("C1", _report(conflicts=[_MISMATCH]),
                           data_dir=dd)
    assert out["outcome"] == "no_action"


def test_beam_width_and_depth_caps(tmp_path):
    dd = _world(tmp_path)
    report = _report(
        conflicts=[_MISMATCH, _UNDECLARED,
                   {"reason_code": "PHANTOM_LIEN", "field": "lien_status",
                    "detail": "declared a lien; registry shows none"}],
        failed_checks=[("completeness",
                        "Missing required documents: ['title', 'police_report']")])
    out = plan_disposition("C1", report, data_dir=dd)
    assert out["outcome"] == "email"
    for level in out["trail"]:
        assert len(level["kept"]) <= BEAM_WIDTH
    assert len(out["trail"]) <= MAX_DEPTH


class FakeProvider:
    """Routes on the chain markers; records calls for assertions."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def complete(self, *, system, user, max_tokens=1024, temperature=0.0,
                 json_mode=False):
        if self.fail:
            raise RuntimeError("LLM down")
        self.calls.append(system)
        if "DISPOSITION_GENERATOR" in system:
            import re
            ids = re.findall(r'"id": "([^"]+)"', user)
            return json.dumps({"actions": [
                {"subset": ids, "rationale": "LLM grouping: all remaining"}]})
        if "DISPOSITION_CRITIC" in system:
            return json.dumps({"coverage": 4, "budget": 3, "actor": 2,
                               "grounding": 1})
        return "{}"


def test_llm_chains_drive_the_plan(tmp_path):
    dd = _world(tmp_path)
    fake = FakeProvider()
    out = plan_disposition("C1", _report(conflicts=[_MISMATCH],
                                         failed_checks=[_TITLE_FAIL]),
                           data_dir=dd, provider=fake)
    assert out["outcome"] == "email"
    assert len(out["needs"]) == 2
    assert any("DISPOSITION_GENERATOR" in c for c in fake.calls)
    assert any("DISPOSITION_CRITIC" in c for c in fake.calls)


def test_llm_failure_falls_back_to_heuristics(tmp_path):
    dd = _world(tmp_path)
    out = plan_disposition("C1", _report(conflicts=[_MISMATCH],
                                         failed_checks=[_TITLE_FAIL]),
                           data_dir=dd, provider=FakeProvider(fail=True))
    assert out["outcome"] == "email"
    assert len(out["needs"]) == 2


def test_graph_runner_matches_loop_runner():
    pytest.importorskip("langgraph")
    gaps, analyst = gaps_from_report(_report(conflicts=[_MISMATCH],
                                             failed_checks=[_TITLE_FAIL]))
    ctx = {"gaps": gaps, "gen_chain": None, "crit_chain": None}
    via_graph = _run_graph(_initial_state(gaps, analyst, 0), ctx)
    via_loop = _run_loop(_initial_state(gaps, analyst, 0), ctx)
    assert via_graph["outcome"] == via_loop["outcome"] == "email"
    assert via_graph["selected"]["emails"] == via_loop["selected"]["emails"]


def test_pipeline_routes_through_planner(tmp_path):
    """End-to-end wiring: a failing seeded case gets a planner disposition."""
    from settleready.data.seed import main as seed_main
    from settleready.pipeline import run_case

    dd = str(tmp_path)
    seed_main(seed=42, out=dd, num_cases=1)
    # 1043 is the failing fixture (missing title); sorted, not listdir
    # order, which is filesystem-dependent.
    case_id = sorted(os.listdir(os.path.join(dd, "pdfs")))[0]
    assert case_id == "TL-2026-1043"

    class ExtractionFake:
        """Extraction fields normally; planner-chain JSON when asked."""

        def complete(self, *, system, user, max_tokens=1024,
                     temperature=0.0, json_mode=False):
            if "DISPOSITION_GENERATOR" in system:
                import re
                ids = re.findall(r'"id": "([^"]+)"', user)
                return json.dumps({"actions": [
                    {"subset": ids, "rationale": "all remaining"}]})
            if "DISPOSITION_CRITIC" in system:
                return json.dumps({"coverage": 4, "budget": 3, "actor": 2,
                                   "grounding": 1})
            fields = ["vin", "mileage", "title_state", "policy_number",
                      "holder_name", "loss_type", "loss_date", "lien_declared"]
            return json.dumps({"fields": {
                f: {"value": "X", "confidence": 0.95, "span": "x"}
                for f in fields}})

    out = run_case(case_id, data_dir=dd, provider=ExtractionFake())
    assert out["next"] == "needs_info"
    assert out["disposition"]["outcome"] == "email"
    assert out["disposition"]["needs"]  # the planner chose the ask(s)
