"""Multi-agent orchestration — Checkpoint 5.1.

Four agents, one LangGraph StateGraph (with a plain-loop fallback when
LangGraph is unavailable — same node functions either way, mirroring the
Checkpoint 4.1 disposition planner):

  ingest      Ingestion agent: intake (parse PDFs + customer statements) and
              extraction (ReAct field pull). Merged per 5.1: the seam between
              "is this a new claim" and "what are the fields" was too thin
              for two agents.
  ground      Grounding agent: verification fan-out; when it passes, statute
              research (with citation verification) and deterministic ACV.
  disposition Checkpoint 4.1 ToT planner (itself a LangGraph subgraph): picks
              the next move for non-clean cases — one combined email, analyst
              rework, or escalation.
  outreach    Outreach agent: drafts and sends the customer follow-up (<=3
              follow-ups per case, then escalation). Conditional branch: runs
              only when the grounding agent flagged missing information.
  reviewer    Reviewer agent: AI first-pass approve/return over the assembled
              packet (<=2 returns, then human escalation). Independent of the
              agents that built the case — it never grades its own homework.

Coordination is hybrid: a sequential backbone (ingest -> ground) with
conditional branches and an iterative review loop, owned by the graph —
agents never negotiate turn order. All agent communication flows through the
Case Blackboard (durable); the graph state is per-run scratch. Budgets bound
every loop; escalation is terminal; a human senior analyst always makes the
final call.
"""
from __future__ import annotations

import glob
import os

from typing import TypedDict

from settleready.agents.disposition import plan_disposition
from settleready.agents.extraction import extract_fields
from settleready.agents.outreach import maybe_send_info_request, needs_from_report
from settleready.agents.researcher import research
from settleready.agents.reviewer import ai_review_case
from settleready.agents.verification import verify_case
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.events import emit as _emit_event
from settleready.tools.acv import compute_acv
from settleready.tools.mock_dbs import MockValuationService
from settleready.tools.pdf_parser import parse_document


class CaseState(TypedDict, total=False):
    """Per-run scratch. Durable case state lives in the Case Blackboard."""
    case_id: str
    data_dir: str
    summaries: list      # human-readable stage summaries (run_case "stages")
    trace: list          # ReAct Thought->Action->Observation steps
    grounding_passed: bool
    report: dict         # verification report (for the disposition node)
    plan: dict           # disposition plan
    plan_outcome: str    # email | analyst_rework | escalate | no_action
    outreach: dict
    review: dict         # reviewer agent result
    next_stage: str      # terminal stage resolved by routers


def _doc_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]  # claim_form.pdf -> claim_form


def _bb(state: dict) -> CaseBlackboard:
    return CaseBlackboard(os.path.join(state["data_dir"], "blackboard.db"))


def _summaries(state: dict) -> list:
    return list(state.get("summaries") or [])


# ---------------------------------------------------------------------------
# Agent nodes. Each is a thin wrapper around the agent's tested implementation;
# the graph owns only turn order. Signature: (state, ctx) -> state updates,
# where ctx = {"provider": ...}. Mirrors the disposition planner's pattern.
# ---------------------------------------------------------------------------

def node_ingest(state: dict, ctx: dict) -> dict:
    """Ingestion agent: intake (PDFs + customer statements) + ReAct extraction."""
    case_id, data_dir, provider = state["case_id"], state["data_dir"], ctx["provider"]
    bb = _bb(state)
    try:
        summaries = _summaries(state)
        # -- intake: parse the case's PDFs ----------------------------------
        intake_row = bb.set_stage(case_id, "intake")
        pdf_dir = os.path.join(data_dir, "pdfs", case_id)
        pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
        if not pdfs:
            raise FileNotFoundError(
                f"No PDFs for {case_id} in {pdf_dir} — run scripts/seed.sh first.")
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
        bb.set_audit_details(intake_row, {"documents": docs_present})
        # Customer follow-up statements (text replies) join the extraction
        # corpus as their own document, so a confirmation in words ("yes, the
        # lien is with Lone Star") can resolve a conflict without new PDFs.
        for i, stmt in enumerate(bb.read(case_id, "customer_statements") or []):
            docs.append({"source": f"customer_reply_{i + 1}.txt", "override": True,
                         "pages": [{"page": 1, "text": stmt["body"]}]})
        summaries.append(f"intake: parsed {len(docs)} documents {docs_present}")

        # -- extraction: ReAct loop -----------------------------------------
        extraction_row = bb.set_stage(case_id, "extraction")
        extraction = extract_fields(docs, provider=provider, case_id=case_id,
                                    data_dir=data_dir)
        # Module 6 guardrail: validate extraction output before storing.
        # BLOCK (schema violation) -> escalate; FLAG (low confidence) -> logged,
        # pipeline already routes these to needs_info.
        from settleready.guardrails.output import screen_output
        for v in screen_output("extraction", {"fields": extraction["fields"]}):
            _emit_event(data_dir, case_id, "guardrail",
                        {"layer": "output", "name": v.name,
                         "action": v.action, "detail": v.detail})
            if v.action == "block":
                raise RuntimeError(f"Guardrail BLOCK: {v.name}: {v.detail}")
        bb.write(case_id, "fields", extraction["fields"])
        bb.write(case_id, "extraction_trace", extraction["trace"])
        bb.write(case_id, "extraction_flags", extraction["flags"])
        n_ok = sum(1 for v in extraction["fields"].values()
                   if v["confidence"] >= 0.8)
        bb.set_audit_details(extraction_row, {
            "fields_at_confidence": n_ok,
            "fields_total": len(extraction["fields"]),
            "below_threshold": extraction["flags"]})
        summaries.append(
            f"extraction: {n_ok} fields at confidence>=0.8, "
            f"flags={extraction['flags']}")
        return {"summaries": summaries, "trace": extraction["trace"]}
    finally:
        bb.close()


def node_ground(state: dict, ctx: dict) -> dict:
    """Grounding agent: verification fan-out, then research + deterministic ACV."""
    case_id, data_dir, provider = state["case_id"], state["data_dir"], ctx["provider"]
    bb = _bb(state)
    try:
        summaries = _summaries(state)
        fields = bb.read(case_id, "fields") or {}
        docs_present = bb.read(case_id, "docs_present") or []

        # -- verification: fan-out ------------------------------------------
        verification_row = bb.set_stage(case_id, "verification")
        report = verify_case(case_id, fields, docs_present, data_dir=data_dir)
        bb.write(case_id, "verification", report)
        _emit_event(data_dir, case_id, "verification_checks",
                    {"passed": report["passed"],
                     "checks": [{"check": c["check"], "status": c["status"]}
                                for c in report["checks"]]})
        bb.set_audit_details(verification_row, {
            "passed": report["passed"],
            "pass": sum(1 for c in report["checks"] if c["status"] == "pass"),
            "fail": sum(1 for c in report["checks"] if c["status"] == "fail"),
            "conflict": sum(1 for c in report["checks"]
                             if c["status"] == "conflict"),
            "conflicts": [c["reason_code"] for c in report["conflicts"]]})
        summaries.append(
            f"verification: passed={report['passed']}, "
            f"conflicts={[c['reason_code'] for c in report['conflicts']]}, "
            f"flags={report['flags']}")
        if not report["passed"]:
            return {"summaries": summaries, "grounding_passed": False,
                    "report": report}

        # -- research: state law for this title state ------------------------
        research_row = bb.set_stage(case_id, "research")
        title_state = (fields.get("title_state") or {}).get("value", "")
        if not title_state:
            # Fallback: our policy numbers are prefixed with the state
            # (TX-A-...). An empty title_state would make state-filtered
            # retrieval return nothing; deriving it keeps legal research
            # working instead of silently producing an empty section.
            policy_no = (fields.get("policy_number") or {}).get("value", "")
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
        # Note: citation grounding is already verified by verify_citations
        # (3.1 feedback) inside research(); no duplicate guardrail needed here.
        # Module 6 guardrail: LLM must not emit dollar amounts (money math
        # is deterministic).
        from settleready.guardrails.output import screen_output as _screen
        for v in _screen("llm_text", {"text": legal.get("answer", ""),
                                      "context": "legal_synthesis"}):
            _emit_event(data_dir, case_id, "guardrail",
                        {"layer": "output", "name": v.name,
                         "action": v.action, "detail": v.detail})
            if v.action == "block":
                raise RuntimeError(f"Guardrail BLOCK: {v.name}: {v.detail}")
        check = legal.get("citation_check", {})
        bb.set_audit_details(research_row, {
            "title_state": title_state, "citations": legal["citations"],
            "citation_check": ("passed" if check.get("passed")
                               else f"failed: {check.get('reason', 'no chunks retrieved')}")})
        summaries.append(
            f"research: {title_state} threshold answered with citations "
            f"{legal['citations']}")

        # -- compute: deterministic ACV (money math never via LLM) -----------
        compute_row = bb.set_stage(case_id, "compute")
        comps = MockValuationService(data_dir).comparables(case_id)
        mileage = (fields.get("mileage") or {}).get("value") or 0
        acv, worksheet = compute_acv(comparables=comps, mileage=mileage)
        _emit_event(data_dir, case_id, "tool_call",
                    {"tool": "acv.compute_acv",
                     "args": {"comparables": len(comps), "mileage": mileage},
                     "result": f"ACV=${acv:,}"})
        bb.write(case_id, "acv", {"acv": acv, "worksheet": worksheet})
        bb.set_audit_details(compute_row, {
            "acv": acv, "comparables": len(comps), "mileage": mileage})
        summaries.append(f"compute: ACV=${acv:,} ({worksheet['formula']})")
        return {"summaries": summaries, "grounding_passed": True,
                "report": report}
    finally:
        bb.close()


def node_disposition(state: dict, ctx: dict) -> dict:
    """Disposition planner (4.1 ToT): the next move for a non-clean case."""
    case_id, data_dir, provider = state["case_id"], state["data_dir"], ctx["provider"]
    bb = _bb(state)
    try:
        summaries = _summaries(state)
        report = state.get("report") or bb.read(case_id, "verification") or {}
        next_stage = "needs_info"
        bb.set_stage(case_id, next_stage, event="routed",
                     details={"passed": False,
                              "conflicts": [c["reason_code"]
                                            for c in report.get("conflicts", [])]})
        # A beam search over email groupings decides what to ask the
        # claimant, what to flag for analyst rework, or whether to escalate —
        # replacing the old fixed conflict->ask mapping. Any planner failure
        # falls back to that mapping, so the planner can never break the
        # pipeline. The customer's reply re-triggers the pipeline via the
        # intake watcher, closing the loop.
        try:
            plan = plan_disposition(case_id, report, data_dir=data_dir,
                                    provider=provider)
        except Exception as e:  # noqa: BLE001
            _emit_event(data_dir, case_id, "disposition_fallback",
                        {"error": str(e)[:200]})
            plan = {"outcome": "no_action"}
        outcome = plan["outcome"]
        if outcome == "analyst_rework":
            bb.set_stage(case_id, "analyst_rework", event="disposition",
                         details={"analyst_flags": [g["id"] for g in
                                                    plan["analyst_flags"]]})
            next_stage = "analyst_rework"
        elif outcome == "escalate":
            bb.set_stage(case_id, "escalated", event="disposition_escalated",
                         details={"reason": "disposition planner escalated"})
            next_stage = "escalated"
        summaries.append(f"disposition: planner outcome={outcome}")
        return {"summaries": summaries, "plan": plan,
                "plan_outcome": outcome, "next_stage": next_stage}
    finally:
        bb.close()


def node_outreach(state: dict, ctx: dict) -> dict:
    """Outreach agent: send the one combined follow-up email (<=3 per case)."""
    case_id, data_dir = state["case_id"], state["data_dir"]
    bb = _bb(state)
    try:
        summaries = _summaries(state)
        plan = state.get("plan") or {}
        report = state.get("report") or bb.read(case_id, "verification") or {}
        fields = bb.read(case_id, "fields") or {}
        holder = (fields.get("holder_name") or {}).get("value", "")
        if state.get("plan_outcome") == "email":
            needs = plan.get("needs") or []
        else:  # no_action fallback: the old fixed conflict->ask mapping
            needs = needs_from_report(report)
        # Note: outreach asks guardrail not wired here — the `needs` are
        # deterministically generated from _CUSTOMER_ASKS templates (via
        # needs_from_report or the ToT planner selecting gap IDs), never
        # free-text LLM output, so there's no invention risk to guard.
        outreach = maybe_send_info_request(
            data_dir, case_id, needs, holder_name=holder or "")
        next_stage = "escalated" if outreach.get("escalated") else "needs_info"
        summaries.append(
            f"outreach: sent={outreach.get('sent')} "
            f"({outreach.get('reason')}) -> {next_stage}")
        return {"summaries": summaries, "outreach": outreach,
                "next_stage": next_stage}
    finally:
        bb.close()


def node_reviewer(state: dict, ctx: dict) -> dict:
    """Reviewer agent: AI first-pass approve/return over the assembled packet."""
    case_id, data_dir, provider = state["case_id"], state["data_dir"], ctx["provider"]
    summaries = _summaries(state)
    result = ai_review_case(case_id, data_dir=data_dir, provider=provider)
    summaries.append(
        f"reviewer: {result['decision']} -> {result['stage']}"
        + (f" ({result['note']})" if result.get("note") else "")
        + (" [degraded]" if result.get("degraded") else ""))
    return {"summaries": summaries, "review": result}


# ---------------------------------------------------------------------------
# Routers — the central orchestrator. Agents never choose what runs next.
# ---------------------------------------------------------------------------

def route_after_ground(state: dict) -> str:
    """Clean cases go to the reviewer; failed ones to the disposition planner."""
    return "reviewer" if state.get("grounding_passed") else "disposition"


def route_after_disposition(state: dict) -> str:
    """Email/no_action need the outreach agent; rework/escalate are terminal.

    Any unexpected outcome routes to outreach, not to a dead end: the
    outreach agent is idempotent (dedupes repeat asks) and budget-bounded,
    so acting is safer than stranding the case at needs_info with no email.
    """
    if state.get("plan_outcome") in ("analyst_rework", "escalate"):
        return "end"
    return "outreach"


def route_after_reviewer(state: dict) -> str:
    """Approve/escalate end the run; a return re-enters at its rework target."""
    review = state.get("review") or {}
    if review.get("decision") == "approve":
        return "end"
    if review.get("stage") == "escalated":
        return "end"
    return review.get("target") or "ground"


_NODES = {
    "ingest": node_ingest,
    "ground": node_ground,
    "disposition": node_disposition,
    "outreach": node_outreach,
    "reviewer": node_reviewer,
}
_ROUTERS = {
    "ground": route_after_ground,
    "disposition": route_after_disposition,
    "reviewer": route_after_reviewer,
}
# Fixed edges (no routing decision needed).
_FIXED = {"ingest": "ground", "outreach": "end"}


def _run_graph(state: dict, ctx: dict) -> dict:
    """LangGraph runner: the four agents as a StateGraph."""
    from langgraph.graph import END, START, StateGraph

    def _wrap(fn, _name):
        def _node(s: dict) -> dict:
            # Record the visit durably for the flow diagram — written straight
            # to the blackboard (not via state) so it survives regardless of
            # how LangGraph merges node updates.
            try:
                bb = CaseBlackboard(os.path.join(s["data_dir"], "blackboard.db"))
                path = bb.read(s["case_id"], "agent_path") or []
                path.append(_name)
                bb.write(s["case_id"], "agent_path", path)
                bb.close()
            except Exception:
                pass
            # Module 6 observability: span per node visit + structured log.
            tracer = ctx.get("tracer")
            if tracer is not None:
                from settleready.obs.logging import JsonLogger
                log = JsonLogger(s["data_dir"], trace_id=tracer.trace_id,
                                 case_id=s["case_id"], component=f"node.{_name}")
                log.info("node_started", node=_name)
                with tracer.span(_name, agent=_name):
                    try:
                        out = fn(s, ctx)
                    except Exception as e:
                        log.error("node_failed", node=_name, error=str(e)[:200])
                        raise
                    log.info("node_finished", node=_name)
                    return out
            return fn(s, ctx)
        return _node

    graph = StateGraph(CaseState)
    for _name, fn in _NODES.items():
        graph.add_node(_name, _wrap(fn, _name))
    graph.add_edge(START, "ingest")
    for src, dst in _FIXED.items():
        graph.add_edge(src, END if dst == "end" else dst)
    graph.add_conditional_edges("ground", route_after_ground,
                                {"reviewer": "reviewer",
                                 "disposition": "disposition"})
    graph.add_conditional_edges("disposition", route_after_disposition,
                                {"outreach": "outreach", "end": END})
    graph.add_conditional_edges("reviewer", route_after_reviewer,
                                {"ingest": "ingest", "ground": "ground",
                                 "end": END})
    return graph.compile().invoke(state)


def _run_fallback(state: dict, ctx: dict) -> dict:
    """Plain-loop runner: same nodes and routers, no LangGraph required.

    The reviewer loop is bounded by MAX_AI_RETURNS (each return burns one),
    so this always terminates; the visit guard is defense-in-depth against a
    router bug.
    """
    current = "ingest"
    visits = 0
    while current != "end":
        visits += 1
        if visits > 10:
            raise RuntimeError(
                "agent graph exceeded 10 node visits — possible routing loop")
        try:
            bb = CaseBlackboard(os.path.join(state["data_dir"], "blackboard.db"))
            path = bb.read(state["case_id"], "agent_path") or []
            path.append(current)
            bb.write(state["case_id"], "agent_path", path)
            bb.close()
        except Exception:
            pass
        state.update(_NODES[current](state, ctx))
        if current in _ROUTERS:
            current = _ROUTERS[current](state)
        else:
            current = _FIXED[current]
    return state


def run_case_graph(case_id: str, data_dir: str = "data", provider=None,
                   tracer=None) -> dict:
    """Run the multi-agent graph for one case. Returns the final graph state.

    The case must already exist in the blackboard (pipeline.run_case creates
    it). Raises on node failure — the caller owns the pipeline_failed event.
    """
    ctx = {"provider": provider, "tracer": tracer}
    state: dict = {"case_id": case_id, "data_dir": data_dir}
    # Clear any stale path from a previous run — the nodes record fresh visits.
    try:
        bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
        bb.write(case_id, "agent_path", [])
        bb.close()
    except Exception:
        pass
    try:
        import langgraph  # noqa: F401
        return _run_graph(state, ctx)
    except ImportError:
        return _run_fallback(state, ctx)
