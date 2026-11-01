"""Checkpoint 5.1 multi-agent graph: routers, reviewer agent, fallback runner.

The graph wires the four agents (ingestion, grounding, outreach, reviewer)
plus the 4.1 disposition planner. These tests pin the orchestration
contracts: who runs next, the reviewer's vetoes and budgets, and the
plain-loop fallback that mirrors the LangGraph path.
"""
import json
import os

import pytest

from settleready.agents import graph
from settleready.agents.builder import build_packet
from settleready.agents.reviewer import (
    MAX_AI_RETURNS,
    ai_review_case,
)
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fields(**over):
    def f(v):
        return {"value": v, "confidence": 1.0, "source": "claim_form.pdf p.1"}
    d = {"vin": f("VIN1"), "mileage": f(41000), "title_state": f("TX"),
         "policy_number": f("TX-A-1"), "holder_name": f("Priya Nair"),
         "loss_type": f("THEFT"), "loss_date": f("2026-09-20"),
         "lien_declared": f(False)}
    d.update(over)
    return d


def _seed_case(data_dir, case_id="TL-2026-1099", verification=None, legal=None,
               acv=17800):
    """Write a complete, reviewable case straight to the blackboard."""
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.create_case(case_id)
    bb.write(case_id, "fields", _fields())
    bb.write(case_id, "verification", verification or {
        "passed": True, "checks": [{"check": "c", "status": "pass"}],
        "conflicts": [], "flags": []})
    bb.write(case_id, "legal_research", legal or {
        "answer": "Total loss when repairs+salvage >= 100% of ACV.",
        "citations": ["TX.md"], "chunks": [],
        "citation_check": {"passed": True, "reason": ""}})
    bb.write(case_id, "acv", {"acv": acv, "worksheet": {"steps": ["s1"]}})
    bb.close()


class _NoCallProvider:
    """Fails loudly if the LLM is touched (veto paths must not call it)."""
    def complete(self, **kw):
        raise AssertionError("LLM must not be called on a veto path")


class _VerdictProvider:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def complete(self, **kw):
        self.calls += 1
        return json.dumps(self.verdict)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

def test_route_after_ground():
    assert graph.route_after_ground({"grounding_passed": True}) == "reviewer"
    assert graph.route_after_ground({"grounding_passed": False}) == "disposition"
    assert graph.route_after_ground({}) == "disposition"  # fail closed


def test_route_after_disposition():
    assert graph.route_after_disposition({"plan_outcome": "email"}) == "outreach"
    assert graph.route_after_disposition({"plan_outcome": "no_action"}) == "outreach"
    assert graph.route_after_disposition({"plan_outcome": "analyst_rework"}) == "end"
    assert graph.route_after_disposition({"plan_outcome": "escalate"}) == "end"
    # Unknown outcomes fail toward action (outreach is idempotent and
    # budget-bounded), never toward a stranded needs_info with no email.
    assert graph.route_after_disposition({"plan_outcome": "weird"}) == "outreach"
    assert graph.route_after_disposition({}) == "outreach"


def test_route_after_reviewer():
    assert graph.route_after_reviewer(
        {"review": {"decision": "approve"}}) == "end"
    assert graph.route_after_reviewer(
        {"review": {"decision": "return", "stage": "escalated"}}) == "end"
    assert graph.route_after_reviewer(
        {"review": {"decision": "return", "stage": "ground",
                    "target": "ground"}}) == "ground"
    assert graph.route_after_reviewer(
        {"review": {"decision": "return", "stage": "ingest",
                    "target": "ingest"}}) == "ingest"


# ---------------------------------------------------------------------------
# Reviewer: deterministic vetoes (no LLM cost, cannot be overruled)
# ---------------------------------------------------------------------------

def test_reviewer_veto_failed_verification_no_llm_call(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd, verification={
        "passed": False,
        "checks": [{"check": "lien_check", "status": "conflict"}],
        "conflicts": [{"reason_code": "UNDECLARED_LIEN",
                       "detail": "registry shows a lien"}],
        "flags": []})
    out = ai_review_case("TL-2026-1099", data_dir=dd,
                         provider=_NoCallProvider())
    assert out["decision"] == "return"
    assert out["reason_codes"] == ["UNDECLARED_LIEN"]
    assert out["degraded"] is False


def test_reviewer_veto_declined_legal(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd, legal={"answer": "The retrieved statutes do not cover this.",
                          "citations": [], "chunks": [],
                          "citation_check": {"passed": False,
                                             "reason": "no chunks retrieved"}})
    out = ai_review_case("TL-2026-1099", data_dir=dd,
                         provider=_NoCallProvider())
    assert out["decision"] == "return"
    assert out["reason_codes"] == ["WRONG_STATE_LAW"]


def test_reviewer_approve_clean_packet(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    provider = _VerdictProvider({"decision": "approve", "reason_codes": [],
                                 "note": "all checks out"})
    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=provider)
    assert provider.calls == 1
    assert out["decision"] == "approve"
    assert out["stage"] == "ready_for_review"
    assert out["degraded"] is False
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    assert bb.get_case("TL-2026-1099")["stage"] == "ready_for_review"
    bb.close()


def test_reviewer_degraded_on_garbage_llm_output(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)

    class _Garbage:
        def complete(self, **kw):
            return "not json at all"

    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=_Garbage())
    # Fail open with a recorded degradation: the vetoes are the hard safety
    # net; a broken model must not silently become a blocker.
    assert out["decision"] == "approve"
    assert out["degraded"] is True
    assert out["stage"] == "ready_for_review"


def test_reviewer_degraded_on_invalid_reason_codes(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    provider = _VerdictProvider({"decision": "return",
                                 "reason_codes": ["BOGUS_CODE"],
                                 "note": "x"})
    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=provider)
    assert out["decision"] == "approve"
    assert out["degraded"] is True


def test_reviewer_return_routes_and_feeds_tally(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    provider = _VerdictProvider({"decision": "return",
                                 "reason_codes": ["LOW_CONFIDENCE_FIELDS"],
                                 "note": "VIN read is shaky"})
    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=provider)
    assert out["decision"] == "return"
    assert out["target"] == "ingest"  # extraction-ish codes re-extract
    assert out["ai_return_count"] == 1
    tally = ReturnTallyStore(os.path.join(dd, "tally.db"))
    try:
        assert any(r["reason_code"] == "LOW_CONFIDENCE_FIELDS"
                   for r in tally.tallies())
    finally:
        tally.close()


def test_reviewer_return_non_extraction_code_targets_ground(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    provider = _VerdictProvider({"decision": "return",
                                 "reason_codes": ["UNDECLARED_LIEN"],
                                 "note": "double-check the registry"})
    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=provider)
    assert out["target"] == "ground"


def test_reviewer_escalates_after_max_returns(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    bb.write("TL-2026-1099", "ai_return_count", MAX_AI_RETURNS - 1)
    bb.close()
    provider = _VerdictProvider({"decision": "return",
                                 "reason_codes": ["WEAK_COMPS"],
                                 "note": "still weak"})
    out = ai_review_case("TL-2026-1099", data_dir=dd, provider=provider)
    assert out["decision"] == "return"
    assert out["stage"] == "escalated"
    assert out["ai_return_count"] == MAX_AI_RETURNS
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    assert bb.get_case("TL-2026-1099")["stage"] == "escalated"
    bb.close()


def test_packet_carries_ai_review(tmp_path):
    dd = str(tmp_path)
    _seed_case(dd)
    packet = build_packet("TL-2026-1099", data_dir=dd)
    assert packet["ai_review"] == {"returns": 0, "last": None,
                                   "last_approval": None}


# ---------------------------------------------------------------------------
# Fallback runner: same nodes and routers, no LangGraph
# ---------------------------------------------------------------------------

def _stub_nodes(monkeypatch, reviewer_script):
    """Replace node bodies with canned routing hints; record visit order."""
    visits = []

    def mk(name, update):
        def _n(state, ctx):
            visits.append(name)
            return dict(update)
        return _n

    calls = {"n": 0}

    def _reviewer(state, ctx):
        visits.append("reviewer")
        calls["n"] += 1
        return {"review": reviewer_script[calls["n"] - 1]}

    monkeypatch.setitem(graph._NODES, "ingest", mk("ingest", {}))
    monkeypatch.setitem(graph._NODES, "ground",
                        mk("ground", {"grounding_passed": True}))
    monkeypatch.setitem(graph._NODES, "disposition",
                        mk("disposition", {"plan_outcome": "email"}))
    monkeypatch.setitem(graph._NODES, "outreach", mk("outreach", {}))
    monkeypatch.setitem(graph._NODES, "reviewer", _reviewer)
    return visits


def test_fallback_clean_path_visits_reviewer_then_ends(monkeypatch):
    visits = _stub_nodes(monkeypatch, [{"decision": "approve"}])
    state = graph._run_fallback({"case_id": "C", "data_dir": "d"}, {"provider": None})
    assert visits == ["ingest", "ground", "reviewer"]
    assert state["case_id"] == "C"


def test_fallback_reviewer_return_loops_then_ends(monkeypatch):
    visits = _stub_nodes(monkeypatch, [
        {"decision": "return", "stage": "ground", "target": "ground"},
        {"decision": "approve"},
    ])
    graph._run_fallback({"case_id": "C", "data_dir": "d"}, {"provider": None})
    assert visits == ["ingest", "ground", "reviewer", "ground", "reviewer"]


def test_graph_langgraph_reviewer_return_then_approve(tmp_path, monkeypatch):
    """Real LangGraph graph + real reviewer node: return -> rework -> approve.

    Ingest/ground are stubbed (their contracts are covered elsewhere); the
    graph wiring, routers, reviewer vetoes/LLM, tally, and budgets are real.
    """
    dd = str(tmp_path)
    _seed_case(dd)
    verdicts = [
        {"decision": "return", "reason_codes": ["WEAK_COMPS"],
         "note": "first pass: comps look thin"},
        {"decision": "approve", "reason_codes": [],
         "note": "second pass: acceptable"},
    ]

    class _Scripted:
        def __init__(self):
            self.n = 0

        def complete(self, **kw):
            v = verdicts[min(self.n, len(verdicts) - 1)]
            self.n += 1
            return json.dumps(v)

    def _stub(name, update):
        # Mirrors the real node contract: append to summaries, never clobber.
        def _n(s, c):
            sums = list(s.get("summaries") or [])
            sums.append(f"{name} (stubbed)")
            return {"summaries": sums, **update}
        return _n

    monkeypatch.setitem(graph._NODES, "ingest", _stub("ingest", {}))
    monkeypatch.setitem(graph._NODES, "ground",
                        _stub("ground", {"grounding_passed": True}))
    final = graph._run_graph({"case_id": "TL-2026-1099", "data_dir": dd},
                             {"provider": _Scripted()})
    assert final["review"]["decision"] == "approve"
    assert final["review"]["stage"] == "ready_for_review"
    reviewer_visits = [s for s in final["summaries"]
                       if s.startswith("reviewer:")]
    assert len(reviewer_visits) == 2
    bb = CaseBlackboard(os.path.join(dd, "blackboard.db"))
    try:
        assert bb.read("TL-2026-1099")["ai_return_count"] == 1
        assert bb.get_case("TL-2026-1099")["stage"] == "ready_for_review"
        tally = ReturnTallyStore(os.path.join(dd, "tally.db"))
        try:
            assert any(r["reason_code"] == "WEAK_COMPS"
                       for r in tally.tallies())
        finally:
            tally.close()
    finally:
        bb.close()


def test_fallback_visit_guard_stops_routing_loop(monkeypatch):
    visits = _stub_nodes(monkeypatch,
                         [{"decision": "return", "stage": "ground",
                           "target": "ground"}] * 20)
    with pytest.raises(RuntimeError, match="routing loop"):
        graph._run_fallback({"case_id": "C", "data_dir": "d"}, {"provider": None})
    assert len(visits) == 10  # guard trips before the 11th visit
