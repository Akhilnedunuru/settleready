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
