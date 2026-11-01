"""Reviewer loop — the senior analyst's approve / return gate (module 5).

A return logs (reason_code, field, title_state) to the return tally store
(episodic memory). At 3 returns the check promotes to a mandatory upfront
verification — learning without retraining. The loop is bounded: 2 returns,
then the case escalates to a human instead of cycling forever.

Reason codes are the same vocabulary the verification agent uses for conflicts,
so machine-found and human-found issues feed one tally.
"""
from __future__ import annotations

import os

from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore

MAX_RETURNS = 2  # then human escalation (checkpoint 2.1: bounded outer loop)

# reason_code -> (tally field, human-readable description)
REASON_CODES = {
    "UNDECLARED_LIEN": ("lien_status", "Lien missing or misdeclared — re-verify lienholder"),
    "LIENHOLDER_MISMATCH": ("lien_holder", "Form and registry name different lienholders — verify which is current"),
    "WEAK_COMPS": ("comps", "Comparable set too weak — widen radius / re-pull"),
    "MISSING_TITLE_DOC": ("title_document", "Title document missing or unreadable"),
    "LOW_CONFIDENCE_FIELDS": ("extraction", "Key fields below confidence threshold"),
    "WRONG_STATE_LAW": ("legal_research", "Wrong state's law cited"),
}


def submit_review(case_id: str, decision: str, reason_code: str | None = None,
                  note: str = "", data_dir: str = "data") -> dict:
    """Record the senior analyst's decision.

    Args:
        decision: "approve" or "return".
        reason_code: required for "return" — one of REASON_CODES.
        note: free-text reviewer note (stored, not parsed).

    Returns:
        {"decision", "stage", "return_count", "promoted" (bool),
         "promotion_detail" (str|None), "tally": [...]}
    """
    if decision not in ("approve", "return"):
        raise ValueError("decision must be 'approve' or 'return'")
    if decision == "return" and reason_code not in REASON_CODES:
        raise ValueError(f"reason_code must be one of {sorted(REASON_CODES)}")

    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    case = bb.get_case(case_id)
    if not case:
        bb.close()
        raise ValueError(f"unknown case {case_id} — run the pipeline first")
    state = bb.read(case_id)
    fields = state.get("fields", {})
    title_state = (fields.get("title_state") or {}).get("value") or case.get("title_state") or ""

    if decision == "approve":
        bb.set_stage(case_id, "approved", event="reviewer_approved",
                     details={"note": note})
        bb.close()
        return {"decision": "approve", "stage": "approved", "return_count": 0,
                "promoted": False, "promotion_detail": None, "tally": []}

    # -- return ------------------------------------------------------------
    tally_field, _desc = REASON_CODES[reason_code]
    tally = ReturnTallyStore(os.path.join(data_dir, "tally.db"))
    promoted = tally.log_return(reason_code=reason_code, field=tally_field,
                                title_state=title_state)
    promotion_detail = None
    if promoted:
        checks = tally.upfront_checks(title_state)
        promotion_detail = (f"Promoted to mandatory upfront verification for {title_state}: "
                            f"{checks[-1]['check_id']} — {checks[-1]['description']}")
    tally_rows = tally.tallies()
    tally.close()

    returns = state.get("return_count", 0) + 1
    bb.write(case_id, "return_count", returns)
    bb.write(case_id, "last_return", {"reason_code": reason_code, "note": note,
                                      "promoted": promoted})
    # Customer-actionable returns go back to the claimant as a targeted
    # follow-up (the analyst's job); internal ones (e.g. WEAK_COMPS) don't.
    from settleready.agents.outreach import maybe_send_info_request, needs_from_return
    holder = (fields.get("holder_name") or {}).get("value", "")
    outreach = maybe_send_info_request(
        data_dir, case_id, needs_from_return(reason_code, note),
        holder_name=holder or "")
    if returns >= MAX_RETURNS:
        bb.set_stage(case_id, "escalated", event="reviewer_returned",
                     details={"reason_code": reason_code, "note": note,
                              "return_count": returns, "escalated": True,
                              "promoted": promoted})
        stage = "escalated"
    else:
        bb.set_stage(case_id, "analyst_rework", event="reviewer_returned",
                     details={"reason_code": reason_code, "note": note,
                              "return_count": returns, "promoted": promoted})
        stage = "analyst_rework"
    bb.close()
    return {"decision": "return", "stage": stage, "return_count": returns,
            "promoted": promoted, "promotion_detail": promotion_detail,
            "outreach": outreach, "tally": tally_rows}


MAX_AI_RETURNS = 2  # AI reviewer returns, then human escalation (checkpoint 5.1)

# Reason codes that point back at extraction get a second extraction pass;
# everything else re-enters at grounding (re-verify / re-research).
_REWORK_TARGET = {
    "LOW_CONFIDENCE_FIELDS": "ingest",
    "MISSING_TITLE_DOC": "ingest",
}

_REVIEWER_SYSTEM = """\
You are the independent first-pass reviewer for SettleReady, an autonomous \
total-loss case-preparation agent. A separate analyst pipeline prepared the \
packet below. Your job is a second mind before a human senior analyst sees \
it: catch real problems, do not invent them. Default to APPROVE when the \
packet is complete and internally consistent.

Hard checks already passed (shown for context, not for you to redo): \
verification passed with no unresolved conflicts, legal research returned \
cited statutes, ACV was computed deterministically.

What to look for:
- Fields the settlement depends on (VIN, lienholder, mileage, title state) \
with confidence below 0.8 — is the packet honest about the uncertainty?
- Legal answer: does it actually answer the total-loss threshold question \
for THIS state, with citations?
- ACV worksheet: do the inputs (mileage, comparables) match the extracted \
fields? Anything arithmetically off or internally contradictory?

Reason codes (use only these): UNDECLARED_LIEN, LIENHOLDER_MISMATCH, \
WEAK_COMPS, MISSING_TITLE_DOC, LOW_CONFIDENCE_FIELDS, WRONG_STATE_LAW.

Reply with JSON only:
{"decision": "approve" | "return", "reason_codes": ["..."], "note": "..."}
reason_codes is required when decision is "return", empty list when "approve". \
Keep the note to one or two sentences."""


def _summarize_packet(packet: dict) -> str:
    """Compact, reviewer-oriented rendering of the assembled packet."""
    fields = packet.get("fields") or {}
    field_lines = []
    for name, v in fields.items():
        if isinstance(v, dict):
            field_lines.append(
                f"  - {name}: {v.get('value')} (confidence {v.get('confidence')})")
    verification = packet.get("verification") or {}
    check_lines = [f"  - {c.get('check')}: {c.get('status')}"
                   for c in verification.get("checks", [])]
    legal = packet.get("legal") or {}
    acv = packet.get("acv") or {}
    return "\n".join([
        f"Case {packet.get('case_id')} — loss type {packet.get('loss_type')}, "
        f"stage {packet.get('stage')}",
        "Fields:",
        *field_lines,
        f"Extraction flags: {packet.get('extraction_flags') or []}",
        f"Verification: passed={verification.get('passed')}",
        *check_lines,
        f"Legal answer: {(legal.get('answer') or '')[:600]}",
        f"Legal citations: {legal.get('citations')}",
        f"ACV: ${acv.get('acv'):,}" if acv.get("acv") is not None else "ACV: missing",
        f"ACV worksheet: {acv.get('worksheet', {}).get('steps')}",
        f"Follow-ups used: {packet.get('followup_count', 0)}/3, "
        f"AI returns used: {(packet.get('ai_review') or {}).get('returns', 0)}/{MAX_AI_RETURNS}",
    ])


def _llm_review(summary: str, provider) -> dict | None:
    """Ask the reviewer model for a verdict. None on any failure."""
    try:
        raw = provider.complete(system=_REVIEWER_SYSTEM, user=summary,
                                max_tokens=300, temperature=0, json_mode=True)
        import json as _json
        verdict = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 — degraded path below
        return None
    if not isinstance(verdict, dict):
        return None
    decision = verdict.get("decision")
    if decision not in ("approve", "return"):
        return None
    codes = verdict.get("reason_codes") or []
    codes = [c for c in codes if c in REASON_CODES]
    if decision == "return" and not codes:
        return None  # a return with no valid reason code is not actionable
    return {"decision": decision, "reason_codes": codes,
            "note": str(verdict.get("note") or "")[:500]}


def _deterministic_vetoes(packet: dict) -> dict | None:
    """Hard safety net: returns a return-verdict no LLM call can overrule.

    These are structural failures the reviewer must never approve — a
    miscalibrated model approving them would be worse than no reviewer.
    """
    verification = packet.get("verification") or {}
    if not verification.get("passed"):
        codes = [c.get("reason_code") for c in verification.get("conflicts", [])
                 if c.get("reason_code")]
        if not codes:
            codes = ["LOW_CONFIDENCE_FIELDS"]
        return {"reason_codes": codes,
                "note": "Deterministic veto: verification did not pass."}
    legal = packet.get("legal") or {}
    if (legal.get("answer") or "").startswith(
            "The retrieved statutes do not cover this"):
        return {"reason_codes": ["WRONG_STATE_LAW"],
                "note": "Deterministic veto: legal research declined — "
                        "no cited legal basis for this state's total-loss rule."}
    return None


def ai_review_case(case_id: str, data_dir: str = "data", provider=None) -> dict:
    """Reviewer agent (checkpoint 5.1): AI first pass over the packet.

    Deterministic vetoes run first (no LLM cost, cannot be overruled); then
    an LLM second-mind review. Approve -> ready_for_review (the human senior
    analyst still makes the final call). Return (bounded) -> the case rework
    target; after MAX_AI_RETURNS -> escalated to a human.

    An LLM failure degrades to approve-with-note: the vetoes are the hard
    safety net, the model is the soft second mind, and a broken model must
    not silently become a blocker. The degradation is recorded in the audit
    trail and the event bus.

    Returns {"decision", "stage", "target"|None, "reason_codes", "note",
             "ai_return_count", "degraded", "promoted"}.
    """
    from settleready.agents.builder import build_packet
    from settleready.memory.events import emit as _emit_event

    if provider is None:
        # Resolved lazily: veto-only paths never need an API key.
        def _provider():
            from settleready.tools.provider import get_provider
            return get_provider()
    else:
        def _provider():
            return provider
    packet = build_packet(case_id, data_dir=data_dir)
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        state = bb.read(case_id)
        ai_returns = state.get("ai_return_count", 0)

        veto = _deterministic_vetoes(packet)
        if veto is not None:
            verdict = {"decision": "return", **veto, "degraded": False}
        else:
            llm_verdict = _llm_review(_summarize_packet(packet), _provider())
            if llm_verdict is None:
                _emit_event(data_dir, case_id, "reviewer_degraded",
                            {"reason": "reviewer LLM unavailable or unparseable; "
                                       "deterministic checks passed"})
                verdict = {"decision": "approve", "reason_codes": [],
                           "note": "Reviewer LLM unavailable — approved on "
                                   "deterministic checks only.",
                           "degraded": True}
            else:
                verdict = {**llm_verdict, "degraded": False}

        fields = packet.get("fields") or {}
        title_state = (fields.get("title_state") or {}).get("value", "")
        acv = (packet.get("acv") or {}).get("acv")

        if verdict["decision"] == "approve":
            if acv is None:
                # Not a review judgment — the pipeline failed to compute.
                raise RuntimeError(f"reviewer: ACV missing for {case_id}")
            bb.write(case_id, "last_ai_review",
                     {"decision": "approve", "note": verdict["note"],
                      "degraded": verdict["degraded"]})
            bb.set_stage(case_id, "ready_for_review", event="routed",
                         details={"acv": acv, "ai_reviewer": "approved",
                                  "degraded": verdict["degraded"]})
            return {"decision": "approve", "stage": "ready_for_review",
                    "target": None, "reason_codes": [],
                    "note": verdict["note"], "ai_return_count": ai_returns,
                    "degraded": verdict["degraded"], "promoted": False}

        # -- return ------------------------------------------------------
        codes = verdict["reason_codes"]
        tally = ReturnTallyStore(os.path.join(data_dir, "tally.db"))
        promoted_any = False
        try:
            for code in codes:
                field, _desc = REASON_CODES[code]
                if tally.log_return(reason_code=code, field=field,
                                    title_state=title_state):
                    promoted_any = True
        finally:
            tally.close()
        ai_returns += 1
        bb.write(case_id, "ai_return_count", ai_returns)
        bb.write(case_id, "last_ai_return",
                 {"reason_codes": codes, "note": verdict["note"]})
        details = {"reason_codes": codes, "note": verdict["note"],
                   "ai_return_count": ai_returns, "promoted": promoted_any,
                   "reviewer": "ai"}
        if ai_returns >= MAX_AI_RETURNS:
            bb.set_stage(case_id, "escalated", event="ai_reviewer_returned",
                         details={**details, "escalated": True})
            return {"decision": "return", "stage": "escalated", "target": None,
                    "reason_codes": codes, "note": verdict["note"],
                    "ai_return_count": ai_returns, "degraded": False,
                    "promoted": promoted_any}
        target = next((_REWORK_TARGET[c] for c in codes if c in _REWORK_TARGET),
                      "ground")
        _emit_event(data_dir, case_id, "ai_reviewer_returned",
                    {**details, "target": target})
        return {"decision": "return", "stage": target, "target": target,
                "reason_codes": codes, "note": verdict["note"],
                "ai_return_count": ai_returns, "degraded": False,
                "promoted": promoted_any}
    finally:
        bb.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Submit a senior-analyst review decision")
    p.add_argument("--case", required=True)
    p.add_argument("--decision", required=True, choices=["approve", "return"])
    p.add_argument("--reason", default=None, choices=sorted(REASON_CODES))
    p.add_argument("--note", default="")
    p.add_argument("--data", default="data")
    a = p.parse_args()
    out = submit_review(a.case, a.decision, reason_code=a.reason,
                        note=a.note, data_dir=a.data)
    print(f"{a.case}: {out['decision']} -> {out['stage']} (returns: {out['return_count']})")
    if out["promotion_detail"]:
        print("PROMOTED:", out["promotion_detail"])
