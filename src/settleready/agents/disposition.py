"""Disposition Planner — the Checkpoint 4.1 Tree-of-Thought stage.

Sits between verification and routing and runs ONLY for non-clean cases
(verification failed). It decides what happens next: which gaps go to the
customer in an email, which are flagged for analyst rework, or whether the
case escalates to a human.

Multi-agent architecture, scoped to this one stage (the rest of the pipeline
stays single-agent + deterministic — see the Oct 28 discussion: splitting
agents is justified by role separation here, not by step count):

- Generator agent — proposes candidate next actions (email groupings).
- Critic agent — scores each candidate against a fixed rubric.
- Controller — the beam search itself: expand, score, prune, select.
- State manager — the Case Blackboard. Branch states are plan snapshots;
  the selected plan and the beam trail are written to the blackboard and
  the audit log, so the decision is reproducible.

Agents communicate ONLY through structured plan states and scores — never
through free-form dialogue. That keeps the audit trail deterministic, which
a claims agent needs and a chatting committee cannot provide.

Framework mapping (Checkpoint 4.1 plan, as implemented):
- Generator / critic LLM calls -> LangChain LCEL chains (prompt -> provider
  -> JSON parser), built on the EXISTING provider interface, so no second
  API-key path is introduced. The 4.1 doc named CrewAI for the generator;
  CrewAI's own process loop would fight the beam-search control flow owned
  by the controller, so the generator is a LangChain chain instead. The
  role separation the mapping was really about (generator never grades its
  own homework) is preserved.
- Controller -> LangGraph StateGraph (generate -> score -> prune -> select,
  looping back while depth remains). Where LangGraph is not installed, the
  SAME node functions run in a plain loop — the graph is the orchestration
  runner, not the logic.
- State manager -> Case Blackboard (the 4.1 doc's MCP role). A standalone
  MCP server exposing the blackboard is deferred, not faked: the blackboard
  already is the shared, inspectable state store.

Search definition (Checkpoint 4.1):
- A "thought" is one candidate next action plus the plan state it produces.
  Here an action is one email: a subset of the remaining customer gaps.
- Node = plan state {emails sent so far, gaps still open, follow-ups used,
  step scores}. Root = the failed-verification state.
- Branching factor <= 3 (generator proposes at most 3 email groupings per
  node), depth <= 3 (one email per level; the cap IS the follow-up budget),
  beam width 3.
- Termination: a plan is complete when no customer gap remains unassigned;
  analyst gaps are auto-flagged at completion (flagging costs no action).
  Search also stops at depth 3 or when the beam empties.
- Final output: the highest-scoring complete plan (ties: fewest emails,
  then earliest generation). A complete plan scoring below threshold, or no
  complete plan at all, escalates to a human — never a guessed disposition.

Receding horizon: the pipeline executes only the FIRST email of the best
plan. The rest of the plan is not a promise about the future — the case
re-plans on its next trigger, with fresh verification. That is why a
multi-email trajectory is worth searching even though one email is sent now:
the first move of the best trajectory is the best first move.

Robustness contract: the planner NEVER raises into the pipeline. No
provider, an LLM failure, bad JSON, or a missing LangGraph install all fall
back to deterministic generation + heuristic scoring, which degrades to
today's fixed mapping (but with grouping optimisation). The pipeline also
wraps the call in try/except and falls back to the old behaviour.
"""
from __future__ import annotations

import json
import os

from settleready.agents.outreach import _CUSTOMER_ASKS, _missing_docs_need
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.events import emit as _emit_event

BEAM_WIDTH = 3
MAX_DEPTH = 3
MAX_FOLLOWUPS = 3
SCORE_THRESHOLD = 5.0

# ---------------------------------------------------------------------------
# Gaps: the planner's input, derived from a verification report.
# A customer gap can be asked of the claimant; an analyst gap cannot (no
# customer template exists for it) and is flagged for analyst rework instead.
# ---------------------------------------------------------------------------


def gaps_from_report(report: dict) -> tuple[list[dict], list[dict]]:
    """Split a verification report into (customer_gaps, analyst_gaps).

    Customer gaps carry their ask text; analyst gaps carry only the reason
    and detail, because there is nothing the customer can answer.
    """
    customer: list[dict] = []
    analyst: list[dict] = []
    seen: set[str] = set()

    def _add_customer(gap_id: str, reason_code: str, ask_text: str) -> None:
        if gap_id not in seen:
            seen.add(gap_id)
            customer.append({"id": gap_id, "reason_code": reason_code,
                             "ask_text": ask_text})

    for c in report.get("conflicts", []):
        code = c.get("reason_code", "")
        tmpl = _CUSTOMER_ASKS.get(code)
        if tmpl:
            _add_customer(f"conflict:{code}:{c.get('field', '')}", code,
                          tmpl.format(detail=c.get("detail", "")))
        else:
            analyst.append({"id": f"conflict:{code}:{c.get('field', '')}",
                            "reason_code": code, "detail": c.get("detail", "")})
    for c in report.get("checks", []):
        if c.get("check") == "completeness" and c.get("status") == "fail":
            detail = c.get("detail", "")
            try:
                import ast
                docs = ast.literal_eval(
                    detail.split("Missing required documents:")[1].strip())
            except Exception:  # noqa: BLE001
                docs = []
            for d in docs:
                _add_customer(f"missing_doc:{d}", "MISSING_DOC",
                              _missing_docs_need(d))
        elif c.get("status") == "fail":
            # fail with no customer template: title_lookup, valuation_data.
            # The customer cannot fix a registry miss or a thin comp set.
            analyst.append({"id": f"check:{c.get('check', '')}",
                            "reason_code": c.get("check", ""),
                            "detail": c.get("detail", "")})
    return customer, analyst


def _previously_asked_needs(case_id: str, data_dir: str) -> list[str]:
    """Needs already emailed for this case (from the outreach signature).

    Outreach stores the need-set signature (sorted JSON) on the blackboard;
    parsing it recovers the need texts without any schema change.
    """
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        needs = bb.read(case_id, "info_requested_needs")
        if isinstance(needs, list):
            return needs
        sig = bb.read(case_id, "info_requested")
        if isinstance(sig, str):
            try:
                parsed = json.loads(sig)
                if isinstance(parsed, list):
                    return parsed
            except Exception:  # noqa: BLE001
                pass
        return []
    finally:
        bb.close()


# ---------------------------------------------------------------------------
# Critic: heuristic scoring (always available) + LLM rubric chain (optional).
# Rubric (Checkpoint 4.1): coverage 0-4, budget efficiency 0-3, actor
# correctness 0-2, evidence grounding 0-1. Total 0-10.
# ---------------------------------------------------------------------------


def heuristic_score(subset: list[str], remaining_before: list[str]) -> dict:
    """Score one candidate email without any LLM call."""
    n_sub, n_rem = len(subset), max(1, len(remaining_before))
    coverage = round(4.0 * n_sub / n_rem, 2)
    budget = 3.0 if (n_sub >= 2 or len(remaining_before) == 1) else 1.0
    return {"coverage": coverage, "budget": budget, "actor": 2.0,
            "grounding": 1.0, "total": round(coverage + budget + 3.0, 2)}


def _clamp_score(raw: dict) -> dict:
    cov = min(4.0, max(0.0, float(raw.get("coverage", 0.0))))
    bud = min(3.0, max(0.0, float(raw.get("budget", 0.0))))
    act = min(2.0, max(0.0, float(raw.get("actor", 0.0))))
    grd = min(1.0, max(0.0, float(raw.get("grounding", 0.0))))
    return {"coverage": cov, "budget": bud, "actor": act, "grounding": grd,
            "total": round(cov + bud + act + grd, 2)}


def deterministic_candidates(remaining: list[str]) -> list[dict]:
    """Fallback generator: all remaining, first gap, first two gaps."""
    cands: list[dict] = []
    if not remaining:
        return cands
    cands.append({"subset": list(remaining),
                  "rationale": "one email covering every outstanding gap"})
    cands.append({"subset": [remaining[0]],
                  "rationale": "ask the first gap on its own"})
    if len(remaining) >= 2:
        cands.append({"subset": remaining[:2],
                      "rationale": "ask the first two gaps together"})
    return cands[:BEAM_WIDTH]


# ---------------------------------------------------------------------------
# LangChain chains. Built lazily and only when a provider exists; any
# failure (missing package, LLM error, bad JSON) falls back to the
# deterministic path — the chains enhance the planner, they never gate it.
# ---------------------------------------------------------------------------

_GENERATOR_SYSTEM = (
    "DISPOSITION_GENERATOR. You are the generator agent in a disposition "
    "planner. Given the remaining customer gaps (each with an id), propose "
    "up to 3 email groupings: each grouping is a non-empty subset of the gap "
    "ids to ask in ONE email. Prefer groupings that resolve the case in the "
    "fewest emails. Reply with JSON only: "
    '{{"actions": [{{"subset": ["gap-id", "..."], "rationale": "..."}}]}}')

_CRITIC_SYSTEM = (
    "DISPOSITION_CRITIC. You are the critic agent in a disposition planner. "
    "Score ONE candidate email against this rubric and reply with JSON only: "
    '{{"coverage": 0-4, "budget": 0-3, "actor": 0-2, "grounding": 0-1}}. '
    "coverage: how much of the remaining gap set this email resolves. "
    "budget: 3 if the email covers 2+ gaps or is the last remaining gap, "
    "else 1 (each email costs one of 3 follow-ups). actor: 2, since every "
    "gap here is customer-actionable by construction. grounding: 1, since "
    "every gap comes from the verification report.")


def _build_chains(provider):
    """Return (generator_chain, critic_chain) or (None, None)."""
    try:
        from langchain_core.output_parsers import JsonOutputParser
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnableLambda
    except ImportError:
        return None, None

    def _call(prompt_value):
        messages = prompt_value.to_messages()
        system = next((m.content for m in messages if m.type == "system"), "")
        user = "\n".join(m.content for m in messages if m.type != "system")
        return provider.complete(system=system, user=user, temperature=0,
                                 json_mode=True)

    gen_prompt = ChatPromptTemplate.from_messages(
        [("system", _GENERATOR_SYSTEM),
         ("human", "Remaining gaps (JSON): {gaps}\nFollow-ups used so far: "
                   "{followups} of 3. Propose up to 3 email groupings.")])
    crit_prompt = ChatPromptTemplate.from_messages(
        [("system", _CRITIC_SYSTEM),
         ("human", "Candidate email covers gaps: {subset}\nRemaining gaps "
                   "before this email: {remaining}\nScore it.")])
    try:
        gen_chain = gen_prompt | RunnableLambda(_call) | JsonOutputParser()
        crit_chain = crit_prompt | RunnableLambda(_call) | JsonOutputParser()
    except Exception:  # noqa: BLE001
        return None, None
    return gen_chain, crit_chain


def _llm_candidates(chain, gaps: list[dict], followups: int) -> list[dict]:
    """Generator chain output, validated; [] on any failure."""
    if chain is None:
        return []
    try:
        out = chain.invoke({"gaps": json.dumps(
            [{"id": g["id"], "reason_code": g["reason_code"]} for g in gaps]),
            "followups": followups})
        actions = out.get("actions", []) if isinstance(out, dict) else []
        valid_ids = {g["id"] for g in gaps}
        cands = []
        for a in actions:
            subset = [s for s in (a.get("subset") or []) if s in valid_ids]
            if subset and subset not in [c["subset"] for c in cands]:
                cands.append({"subset": subset,
                              "rationale": a.get("rationale", "")})
        return cands[:BEAM_WIDTH]
    except Exception:  # noqa: BLE001
        return []


def _llm_score(chain, subset: list[str], remaining: list[str]) -> dict | None:
    """Critic chain output, clamped; None on any failure.

    All four rubric keys must be present: a JSON reply in some other shape
    (e.g. a provider returning an unrelated payload) is a failed scoring,
    NOT a zero score — silently zeroing would prune good plans.
    """
    if chain is None:
        return None
    try:
        out = chain.invoke({"subset": json.dumps(subset),
                            "remaining": json.dumps(remaining)})
        if not isinstance(out, dict):
            return None
        if not all(k in out for k in ("coverage", "budget", "actor",
                                      "grounding")):
            return None
        return _clamp_score(out)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Beam search core. These node functions are shared by both runners: the
# LangGraph StateGraph and the plain-loop fallback.
# State: {gaps, analyst_gaps, followups, beam, depth, trail, selected}.
# A plan: {emails: [[gap_ids]], remaining: [gap_ids], followups: int,
#          step_scores: [float], order: int}.
# ---------------------------------------------------------------------------


def _plan_mean(plan: dict) -> float:
    scores = plan["step_scores"]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def node_generate(state: dict, ctx: dict) -> dict:
    """Expand every incomplete plan in the beam with candidate emails."""
    beam = state["beam"]
    candidates: list[dict] = []
    for plan in beam:
        if not plan["remaining"]:
            continue
        remaining_gaps = [g for g in ctx["gaps"] if g["id"] in plan["remaining"]]
        llm = _llm_candidates(ctx["gen_chain"], remaining_gaps,
                              plan["followups"])
        # LLM proposals first, deterministic ones filling up to the cap;
        # duplicates (e.g. "all" == "first two" for a 2-gap case) collapse.
        cands, seen_subsets = [], set()
        for c in llm + deterministic_candidates(plan["remaining"]):
            key = tuple(c["subset"])
            if key not in seen_subsets:
                seen_subsets.add(key)
                cands.append(c)
        for c in cands[:BEAM_WIDTH]:
            # Hard constraints, checked before any scoring: non-empty subset
            # of remaining gaps, and the follow-up budget must survive it.
            if not c["subset"]:
                continue
            if not set(c["subset"]).issubset(set(plan["remaining"])):
                continue
            if plan["followups"] + 1 > MAX_FOLLOWUPS:
                continue
            candidates.append({"plan": plan, "subset": c["subset"],
                               "rationale": c.get("rationale", "")})
    return {"candidates": candidates}


def node_score(state: dict, ctx: dict) -> dict:
    """Score every candidate (critic chain, else heuristic) and extend plans."""
    extended: list[dict] = []
    for cand in state.get("candidates", []):
        plan = cand["plan"]
        scored = _llm_score(ctx["crit_chain"], cand["subset"],
                            plan["remaining"])
        if scored is None:
            scored = heuristic_score(cand["subset"], plan["remaining"])
        new_plan = {
            "emails": plan["emails"] + [list(cand["subset"])],
            "remaining": [g for g in plan["remaining"]
                          if g not in cand["subset"]],
            "followups": plan["followups"] + 1,
            "step_scores": plan["step_scores"] + [scored["total"]],
            "order": len(extended),
        }
        extended.append(new_plan)
    return {"extended": extended}


def node_prune(state: dict, ctx: dict) -> dict:
    """Keep complete plans unexpanded; beam keeps the top 3 by mean score."""
    complete = [p for p in state["beam"] if not p["remaining"]]
    extended = state.get("extended", [])
    ranked = sorted(complete + extended,
                    key=lambda p: (-_plan_mean(p), len(p["emails"]), p["order"]))
    new_beam = ranked[:BEAM_WIDTH]
    trail = state.get("trail", []) + [{
        "depth": state["depth"] + 1,
        "kept": [{"emails": p["emails"], "mean_score": _plan_mean(p),
                  "remaining": p["remaining"]} for p in new_beam]}]
    return {"beam": new_beam, "depth": state["depth"] + 1, "trail": trail,
            "candidates": [], "extended": []}


def _should_continue(state: dict) -> str:
    """Loop while an incomplete plan remains and depth is left."""
    if state["depth"] >= MAX_DEPTH:
        return "select"
    if not state["beam"]:
        return "select"
    if all(not p["remaining"] for p in state["beam"]):
        return "select"
    return "generate"


def node_select(state: dict, ctx: dict) -> dict:
    """Pick the best complete plan, or escalate when none passes."""
    complete = [p for p in state["beam"] if not p["remaining"]]
    complete.sort(key=lambda p: (-_plan_mean(p), len(p["emails"]), p["order"]))
    best = complete[0] if complete else None
    if best is not None and _plan_mean(best) >= SCORE_THRESHOLD:
        return {"selected": best, "outcome": "email"}
    return {"selected": None, "outcome": "escalate"}


def _initial_state(customer_gaps: list[dict], analyst_gaps: list[dict],
                   followups: int) -> dict:
    return {
        "gaps": customer_gaps,
        "analyst_gaps": analyst_gaps,
        "followups": followups,
        "beam": [{"emails": [], "remaining": [g["id"] for g in customer_gaps],
                  "followups": followups, "step_scores": [], "order": 0}],
        "depth": 0,
        "trail": [],
        "candidates": [],
        "extended": [],
        "selected": None,
        "outcome": None,
    }


def _run_loop(state: dict, ctx: dict) -> dict:
    """Plain-loop runner: same node functions, no LangGraph required."""
    while True:
        state.update(node_generate(state, ctx))
        state.update(node_score(state, ctx))
        state.update(node_prune(state, ctx))
        if _should_continue(state) == "select":
            break
    state.update(node_select(state, ctx))
    return state


def _run_graph(state: dict, ctx: dict) -> dict:
    """LangGraph runner: the beam search as a StateGraph."""
    from langgraph.graph import END, START, StateGraph
    from typing import TypedDict

    class _State(TypedDict, total=False):
        gaps: list
        analyst_gaps: list
        followups: int
        beam: list
        depth: int
        trail: list
        candidates: list
        extended: list
        selected: dict | None
        outcome: str | None

    def _generate(s: dict) -> dict:
        return node_generate(s, ctx)

    def _score(s: dict) -> dict:
        return node_score(s, ctx)

    def _prune(s: dict) -> dict:
        return node_prune(s, ctx)

    def _select(s: dict) -> dict:
        return node_select(s, ctx)

    # TypedDict schema: node outputs merge into state per key. A bare dict
    # schema would replace the whole state with each node's output instead,
    # which loses the beam on the first edge.
    graph = StateGraph(_State)
    graph.add_node("generate", _generate)
    graph.add_node("score", _score)
    graph.add_node("prune", _prune)
    graph.add_node("select", _select)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "score")
    graph.add_edge("score", "prune")
    graph.add_conditional_edges("prune", _should_continue,
                                {"generate": "generate", "select": "select"})
    graph.add_edge("select", END)
    return graph.compile().invoke(state)


# ---------------------------------------------------------------------------
# Public entry point: plan_disposition(case_id, report, ...).
# ---------------------------------------------------------------------------


def plan_disposition(case_id: str, report: dict, data_dir: str = "data",
                     provider=None) -> dict:
    """Plan the disposition of a failed-verification case.

    Returns {"outcome", "needs", "analyst_flags", "selected_plan", "trail"}.
    outcome is one of:
    - "email": send one email with `needs` (first email of the best plan).
    - "analyst_rework": no customer action possible; `analyst_flags` recorded.
    - "escalate": budget exhausted, or no plan passed the score threshold.
    - "no_action": everything was already asked; caller falls back to the
      previous behaviour (which dedupes to "already asked").
    Never raises: on any internal failure the caller falls back anyway, but
    this function itself degrades to the deterministic path first.
    """
    customer_gaps, analyst_gaps = gaps_from_report(report)
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        followups = bb.read(case_id, "followup_count") or 0
    finally:
        bb.close()
    asked = set(_previously_asked_needs(case_id, data_dir))
    remaining = [g for g in customer_gaps if g["ask_text"] not in asked]
    by_id = {g["id"]: g for g in customer_gaps}

    def _finish(outcome: str, needs: list[str], selected=None,
                trail: list | None = None) -> dict:
        result = {"outcome": outcome, "needs": needs,
                  "analyst_flags": analyst_gaps,
                  "selected_plan": selected, "trail": trail or []}
        bb2 = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
        try:
            bb2.write(case_id, "disposition_plan", result)
            if analyst_gaps:
                bb2.write(case_id, "analyst_flags", analyst_gaps)
        finally:
            bb2.close()
        _emit_event(data_dir, case_id, "disposition_planned",
                    {"outcome": outcome, "needs": needs,
                     "analyst_flags": [g["id"] for g in analyst_gaps]})
        return result

    if not remaining and not analyst_gaps:
        return _finish("no_action", [])
    if not remaining:
        return _finish("analyst_rework", [])
    if followups >= MAX_FOLLOWUPS:
        return _finish("escalate", [])

    gen_chain, crit_chain = _build_chains(provider) if provider else (None, None)
    ctx = {"gaps": remaining, "gen_chain": gen_chain, "crit_chain": crit_chain}
    state = _initial_state(remaining, analyst_gaps, followups)
    try:
        final = _run_graph(state, ctx)
    except Exception:  # noqa: BLE001 — missing/broken LangGraph install:
        # same node functions, plain loop. The graph is the runner, not the
        # logic, so the result is identical for deterministic inputs.
        final = _run_loop(_initial_state(remaining, analyst_gaps, followups),
                          ctx)
    if final.get("outcome") == "email" and final.get("selected"):
        first_email = final["selected"]["emails"][0]
        needs = [by_id[gid]["ask_text"] for gid in first_email
                 if gid in by_id]
        return _finish("email", needs, selected=final["selected"],
                       trail=final.get("trail", []))
    return _finish("escalate", [], selected=None,
                   trail=final.get("trail", []))
