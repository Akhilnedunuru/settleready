"""Suspicion score (Lab 6.2, lightweight): aggregate the signals the pipeline
already emits into a per-case 0-100 score with named weights.

This is NOT new detection — every signal below already exists as a guardrail,
verification check, or event. The score just wires them into one graded number
the human reviewer can read at a glance, instead of binary pass/fail.

Weights are deliberately coarse; the point is a ranked signal, not a calibrated
classifier. Thresholds: >= 80 escalate, 50-79 flag for review, < 50 routine.
"""
from __future__ import annotations

import os

# signal -> (weight, description)
SIGNALS = {
    "unresolved_conflict": (25, "Unresolved conflict on a case field"),
    "high_stakes_conflict": (15, "Conflict on a high-stakes field (lien/title)"),
    "low_confidence_fields": (15, "Required fields below 0.8 confidence"),
    "sender_mismatch": (30, "Reply from a non-claimant address was rejected"),
    "sender_unverified": (10, "Reply accepted with sender verification off"),
    "prompt_injection_block": (30, "Prompt-injection attempt blocked"),
    "citation_failure": (20, "Legal answer failed citation grounding"),
    "overconfidence_block": (15, "LLM output blocked for overconfident language"),
    "overconfidence_flag": (5, "LLM output flagged for overconfident language"),
    "reviewer_degraded": (20, "AI reviewer unavailable — fail-closed escalation"),
    "ai_return": (10, "AI reviewer return (each)"),
    "analyst_return": (10, "Analyst return (each)"),
    "email_bounced": (10, "Follow-up email bounced"),
    "email_send_failed": (10, "Follow-up email failed to send"),
    "pipeline_failed": (15, "Pipeline run failed and will retry"),
}

ESCALATE_AT = 80
FLAG_AT = 50


def compute_suspicion(case_id: str, data_dir: str) -> dict:
    """Compute the suspicion score for a case from its stored state + events.

    Returns {"score": int 0-100, "signals": [{signal, weight, detail}], "level": str}.
    Level: "escalate" >= 80, "flag" >= 50, "routine" otherwise.
    """
    from settleready.memory.blackboard import CaseBlackboard
    from settleready.memory.events import tail as _tail

    signals: list[dict] = []

    def _add(name: str, detail: str = ""):
        w, desc = SIGNALS[name]
        signals.append({"signal": name, "weight": w,
                        "description": desc, "detail": detail})

    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        state = bb.read(case_id) or {}
        case = bb.get_case(case_id) or {}
        fields = state.get("fields", {})
        # Low-confidence fields
        from settleready.agents.extraction import REQUIRED_FIELDS, CONFIDENCE_THRESHOLD
        low = [f for f in REQUIRED_FIELDS
               if (fields.get(f) or {}).get("confidence", 0.0) < CONFIDENCE_THRESHOLD]
        if low:
            _add("low_confidence_fields", f"fields: {', '.join(low)}")
        # Conflicts
        conflicts = state.get("conflicts", [])
        unresolved = [c for c in conflicts if c.get("status") == "unresolved"]
        if unresolved:
            _add("unresolved_conflict",
                 f"{len(unresolved)} unresolved: "
                 f"{[c.get('reason_code') for c in unresolved]}")
            high_stakes = [c for c in unresolved
                           if c.get("reason_code") in
                           ("UNDECLARED_LIEN", "LIENHOLDER_MISMATCH",
                            "TITLE_BRAND_PRESENT")]
            if high_stakes:
                _add("high_stakes_conflict",
                     f"{[c.get('reason_code') for c in high_stakes]}")
        # Return counts
        ai_returns = state.get("ai_return_count", 0) or 0
        for _ in range(min(ai_returns, 2)):
            _add("ai_return")
        analyst_returns = state.get("return_count", 0) or 0
        for _ in range(min(analyst_returns, 2)):
            _add("analyst_return")
        # Stage-based signals
        stage = case.get("stage", "")
        if stage == "email_bounced":
            _add("email_bounced")
        elif stage == "email_send_failed":
            _add("email_send_failed")
        elif stage == "escalated":
            # Escalation itself isn't a signal, but a degraded reviewer is.
            pass
    finally:
        bb.close()

    # Event-based signals (from the audit log / event bus)
    try:
        evts = _tail(data_dir, case_id=case_id, limit=500)
    except Exception:
        evts = []
    _seen = set()
    for e in evts:
        ev = e.get("event", "")
        if ev == "sender_mismatch" and "sender_mismatch" not in _seen:
            _add("sender_mismatch")
            _seen.add("sender_mismatch")
        elif ev == "sender_unverified" and "sender_unverified" not in _seen:
            _add("sender_unverified")
            _seen.add("sender_unverified")
        elif ev == "guardrail":
            d = e.get("detail", {}) or {}
            if d.get("name") == "prompt_injection" and d.get("action") == "block":
                if "prompt_injection_block" not in _seen:
                    _add("prompt_injection_block")
                    _seen.add("prompt_injection_block")
            elif d.get("name") == "overconfidence" and d.get("action") == "block":
                if "overconfidence_block" not in _seen:
                    _add("overconfidence_block")
                    _seen.add("overconfidence_block")
            elif d.get("name") == "overconfidence" and d.get("action") == "flag":
                if "overconfidence_flag" not in _seen:
                    _add("overconfidence_flag")
                    _seen.add("overconfidence_flag")
        elif ev == "citation_check":
            d = e.get("detail", {}) or {}
            if d.get("passed") is False and "citation_failure" not in _seen:
                _add("citation_failure")
                _seen.add("citation_failure")
        elif ev == "reviewer_degraded" and "reviewer_degraded" not in _seen:
            _add("reviewer_degraded")
            _seen.add("reviewer_degraded")
        elif ev == "pipeline_failed" and "pipeline_failed" not in _seen:
            _add("pipeline_failed")
            _seen.add("pipeline_failed")

    score = min(sum(s["weight"] for s in signals), 100)
    level = ("escalate" if score >= ESCALATE_AT
             else "flag" if score >= FLAG_AT else "routine")
    return {"score": score, "signals": signals, "level": level}
