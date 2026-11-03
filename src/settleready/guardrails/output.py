"""Output guardrails: validate agent outputs before they are stored or sent.

These enforce the charter's hard rules (AGENTS.md §§4, 10):
- Extraction outputs must match the schema and meet the confidence bar.
- ACV must come from the deterministic calculator — never from an LLM string.
- Legal answers must cite retrieved chunks — never from memory.
- Customer emails must only contain approved asks.

BLOCK = charter violation. The pipeline must not proceed; it escalates.
"""
from __future__ import annotations

import re

from settleready.guardrails import Verdict

# The 8 required extraction fields (mirrors agents/extraction.py).
REQUIRED_FIELDS = ("vin", "mileage", "title_state", "policy_number",
                   "holder_name", "loss_type", "loss_date", "lien_declared")
CONFIDENCE_THRESHOLD = 0.8

# Dollar amounts the LLM must never emit as a "computed" value.
_DOLLAR = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?")


def check_extraction_schema(fields: dict) -> Verdict:
    """Fields present must have value/source/confidence structure.
    Missing fields are FLAG (not block) — the pipeline flags them as
    missing and routes to needs_info; that's the designed behavior."""
    if not isinstance(fields, dict):
        return Verdict.blocked("extraction_schema",
                               "Fields is not a dict",
                               type=str(type(fields)))
    bad = [f for f, v in fields.items()
           if not isinstance(v, dict) or
           not all(k in v for k in ("value", "source", "confidence"))]
    if bad:
        return Verdict.blocked("extraction_schema",
                               f"Fields lack value/source/confidence: {bad}",
                               bad=bad)
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        return Verdict.flagged("extraction_schema",
                               f"Fields not extracted (will be flagged): {missing}",
                               missing=missing)
    return Verdict.make_pass("extraction_schema")


def check_confidence(fields: dict) -> Verdict:
    """Fields below 0.8 are not extracted — they must be flagged, not filled."""
    low = [f for f, v in fields.items()
           if isinstance(v, dict)
           and v.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
           and v.get("value") not in (None, "")]
    if low:
        # FLAG, not block: the pipeline already routes these to needs_info.
        # The guardrail ensures the flag is present (not silently filled).
        return Verdict.flagged("confidence",
                               f"Below-threshold fields must stay flagged: {low}",
                               low=low)
    return Verdict.make_pass("confidence")


def check_no_llm_money(text: str, context: str = "llm_output") -> Verdict:
    """BLOCK if an LLM output contains a dollar amount presented as computed.

    Money math is deterministic (tools/acv.py). An LLM emitting '$22,300'
    as if it calculated it is a charter violation — even if the number
    happens to be right.
    """
    if _DOLLAR.search(text or ""):
        return Verdict.blocked(
            "no_llm_money",
            f"LLM output in {context} contains a dollar amount — "
            "money math must come from the deterministic ACV tool.",
            sample=_DOLLAR.search(text or "").group(0))
    return Verdict.make_pass("no_llm_money")


def check_citations(answer: str, citations: list[str],
                    retrieved_ids: list[str]) -> Verdict:
    """Every citation must come from the retrieved set; no invented cites."""
    if not citations and answer and "do not cover" not in answer:
        return Verdict.flagged("citations",
                               "Answer has no citations — verify grounding.")
    invented = [c for c in citations if c not in retrieved_ids]
    if invented:
        return Verdict.blocked("citations",
                               f"Citations not in retrieved set: {invented}",
                               invented=invented)
    return Verdict.make_pass("citations")


def check_outreach_asks(needs: list[str], approved_asks: set[str]) -> Verdict:
    """Customer emails may only contain approved asks (AGENTS.md §3)."""
    bad = [n for n in needs if n not in approved_asks]
    if bad:
        return Verdict.blocked("outreach_asks",
                               f"Unapproved asks in customer email: {bad}",
                               bad=bad)
    return Verdict.make_pass("outreach_asks")


def screen_output(kind: str, payload: dict) -> list[Verdict]:
    """Dispatch to the right output checks by kind.

    kind: "extraction" | "legal" | "outreach" | "llm_text"
    """
    if kind == "extraction":
        fields = payload.get("fields", {})
        return [check_extraction_schema(fields), check_confidence(fields)]
    if kind == "legal":
        return [check_citations(payload.get("answer", ""),
                                payload.get("citations", []),
                                payload.get("retrieved_ids", []))]
    if kind == "outreach":
        return [check_outreach_asks(payload.get("needs", []),
                                    payload.get("approved_asks", set()))]
    if kind == "llm_text":
        return [check_no_llm_money(payload.get("text", ""),
                                   payload.get("context", "llm_output"))]
    return [Verdict.make_pass("unknown_kind", f"No checks for kind={kind}")]
