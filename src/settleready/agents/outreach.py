"""Customer outreach: the analyst agent's targeted info-request emails.

When verification fails (``needs_info``) or the senior analyst returns a case
with a customer-actionable reason, the agent emails the claimant asking for
exactly what's missing — no generic "please send documents" blasts.

The email's subject keeps the case ID (``Re: Claim filed via mobile app —
TL-2026-1047``), so when the customer replies the intake watcher correlates
the thread and re-triggers the pipeline on the updated documents. That reply
path already exists; this module closes the loop on the outbound side.

Sends are deduplicated: one email per distinct set of outstanding needs, so
re-runs and repeated returns don't spam the customer.
"""
from __future__ import annotations

import json

from settleready.memory.events import emit as _emit_event

# reason_code -> plain-language ask for the customer
_CUSTOMER_ASKS = {
    "UNDECLARED_LIEN": (
        "our title records show a lien on the vehicle ({detail}) that was not "
        "declared on the claim form — please confirm the lienholder's name and "
        "the current payoff amount"),
    "PHANTOM_LIEN": (
        "the claim form declares a lien, but our title records show none — "
        "please confirm whether a lienholder should be listed"),
    "LIENHOLDER_MISMATCH": (
        "the lienholder named on the claim form ({detail}) differs from the "
        "one in our title records — please confirm which lienholder is "
        "current (for example, after a refinance or payoff)"),
    "MISSING_TITLE_DOC": (
        "we are missing the certificate of title — please reply with a clear "
        "scan or photo of the front and back of the title"),
    "LOW_CONFIDENCE_FIELDS": (
        "some details on the claim form were hard to read ({detail}) — please "
        "confirm the correct values"),
    "WRONG_STATE_LAW": (
        "the loss appears to fall under a different state's rules than the "
        "title state — please confirm where the vehicle is titled"),
    # WEAK_COMPS is analyst-internal (widen the comparable search), not
    # something the customer can answer — no email for it.
}

_DOC_LABELS = {
    "claim_form": "the claim form",
    "police_report": "the police report",
    "title": "the certificate of title",
    "repair_estimate": "the repair estimate",
}


def _missing_docs_need(doc: str) -> str:
    label = _DOC_LABELS.get(doc, doc)
    return (f"we are missing {label} — please reply with a clear scan or "
            f"photo of it")


def needs_from_report(report: dict) -> list[str]:
    """Plain-language customer needs from a verification report."""
    needs: list[str] = []
    for c in report.get("conflicts", []):
        tmpl = _CUSTOMER_ASKS.get(c.get("reason_code"))
        if tmpl:
            needs.append(tmpl.format(detail=c.get("detail", "")))
    for c in report.get("checks", []):
        if c.get("check") == "completeness" and c.get("status") == "fail":
            detail = c.get("detail", "")
            # "...Missing required documents: ['title']" -> ['title']
            import ast
            try:
                docs = ast.literal_eval(detail.split("Missing required documents:")[1].strip())
            except Exception:  # noqa: BLE001
                docs = []
            for d in docs:
                need = _missing_docs_need(d)
                if need not in needs:
                    needs.append(need)
    return needs


def needs_from_return(reason_code: str, detail: str = "") -> list[str]:
    """Plain-language customer needs from a reviewer return. Empty = internal."""
    tmpl = _CUSTOMER_ASKS.get(reason_code)
    return [tmpl.format(detail=detail)] if tmpl else []


def compose_info_request(case_id: str, holder_name: str,
                         needs: list[str]) -> tuple[str, str]:
    subject = f"Re: Claim filed via mobile app — {case_id}"
    items = "\n".join(f"{i + 1}. {n[0].upper() + n[1:]}." for i, n in enumerate(needs))
    body = (
        f"Hello {holder_name or 'there'},\n\n"
        f"This is SettleReady, assisting with your total-loss claim {case_id}.\n"
        f"Before we can finalize your settlement, we need the following:\n\n"
        f"{items}\n\n"
        f"Please reply directly to this email with the requested information "
        f"or documents attached. Keep the subject line as-is so your reply is "
        f"matched to your claim automatically.\n\n"
        f"Thank you."
    )
    return subject, body


def maybe_send_info_request(data_dir: str, case_id: str, needs: list[str],
                            holder_name: str = "", client=None) -> dict:
    """Send the info-request email unless already sent for these needs.

    Returns {"sent": bool, "reason": str}.
    """
    from settleready.memory.blackboard import CaseBlackboard

    bb = CaseBlackboard(f"{data_dir}/blackboard.db")
    try:
        if not needs:
            return {"sent": False, "reason": "nothing to ask"}
        claimant = bb.read(case_id, "claimant_email")
        if not claimant:
            _emit_event(data_dir, case_id, "email_skipped",
                        {"reason": "no claimant email on file"})
            return {"sent": False, "reason": "no claimant email on file"}
        sig = json.dumps(sorted(needs))
        if bb.read(case_id, "info_requested") == sig:
            _emit_event(data_dir, case_id, "email_skipped",
                        {"reason": "already asked for this"})
            return {"sent": False, "reason": "already asked for this"}
        subject, body = compose_info_request(case_id, holder_name, needs)
        if client is None:
            from settleready.tools.email_client import get_email_client
            client = get_email_client(data_dir=data_dir)
        try:
            client.send(to=claimant, subject=subject, body=body)
        except Exception as e:  # noqa: BLE001 — a failed send must not fail the case
            _emit_event(data_dir, case_id, "email_failed",
                        {"to": claimant, "error": str(e)[:200]})
            return {"sent": False, "reason": f"send failed: {e}"}
        bb.write(case_id, "info_requested", sig)
        followups = (bb.read(case_id, "followup_count") or 0) + 1
        bb.write(case_id, "followup_count", followups)
        stage = (bb.get_case(case_id) or {}).get("stage", "")
        bb.log(case_id, stage, "info_request_sent",
               {"to": claimant, "needs": needs, "followup_count": followups})
        _emit_event(data_dir, case_id, "email_sent",
                    {"to": claimant, "subject": subject, "needs": needs,
                     "followup_count": followups})
        # Bounded outer loop (checkpoint 2.1): 3 customer follow-ups, then a
        # human takes over — the agent stops asking on its own.
        escalated = False
        if followups >= 3:
            bb.set_stage(case_id, "escalated", event="followup_budget_exhausted",
                         details={"followup_count": followups})
            _emit_event(data_dir, case_id, "escalated",
                        {"reason": "followup budget exhausted (3)"})
            escalated = True
        return {"sent": True, "reason": f"sent to {claimant}",
                "followup_count": followups, "escalated": escalated}
    finally:
        bb.close()
