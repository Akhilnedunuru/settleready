"""Extraction agent: ReAct loop (Thought -> Action -> Observation) over parsed
documents, producing structured fields with confidence scores + source spans.

Design (checkpoint 2.1): the loop reasons about which fields are still missing
or below the confidence threshold, acts by extracting from the most promising
document, and observes the merged result written to its working state. It stops
when every required field clears the threshold or the iteration budget is
spent; anything left becomes a human-verification flag — never a silent guess.

Each trace entry is {"thought": ..., "action": ..., "observation": ...} so the
Streamlit UI (and LangSmith) can render the agent's reasoning live.
"""
from __future__ import annotations

import json

REQUIRED_FIELDS = [
    "vin", "mileage", "title_state", "policy_number",
    "holder_name", "loss_type", "loss_date", "lien_declared",
]
# Asked alongside the required fields on the first pass but never gated on:
# the lienholder NAME the claimant wrote on the form. Verification compares
# it against the lienholder registry so a disagreement between the submitted
# document and the authoritative record is preserved as a structured
# conflict (both values + sources), not silently resolved.
SUPPLEMENTARY_FIELDS = ["lien_holder"]
CONFIDENCE_THRESHOLD = 0.8
MAX_ITERATIONS = 3

_SYSTEM = """You are a precise insurance-document extraction assistant.
Extract ONLY the requested fields from the document text below.
For each field return: "value" (or null if absent), "confidence" (0.0-1.0),
and "span" (the exact short quote from the text supporting the value, or "").
Be conservative: when unsure, lower the confidence instead of guessing.
Types: mileage is an integer (no commas); lien_declared is true/false/null;
everything else is a string or null.
Yes/No checkboxes render as "YES [ ] NO [X]" in the text: the [X] mark belongs
to the option immediately BEFORE it ("YES NO X" means NO is selected).
Respond with ONLY valid JSON, exactly:
{"fields": {"<field>": {"value": ..., "confidence": ..., "span": ...}}}"""

# Extraction order: customer follow-up statements first (freshest testimony),
# then claim form (usually holds the most fields).
_DOC_PRIORITY = {"claim_form": 0, "police_report": 1, "title": 2, "repair_estimate": 3}


def _extract_deterministic(parsed_docs: list[dict]) -> dict:
    """Regex-based fallback when the LLM is unavailable.

    Extracts the most critical fields from the document text using patterns.
    Confidence is capped at 0.7 (below the 0.8 threshold) so every field
    still routes to human verification — the fallback keeps the pipeline
    moving, it does not silently approve low-quality extraction.
    """
    import re as _re

    text = "\n".join(
        p["text"] for d in parsed_docs for p in d.get("pages", []))
    fields: dict = {}

    def _add(field: str, value, span: str = ""):
        if value is not None and str(value).strip():
            fields[field] = {"value": _normalize(field, value),
                             "confidence": 0.7,
                             "source": "deterministic_fallback",
                             "span": span[:80]}

    # VIN: 17 alphanumeric chars (excluding I, O, Q).
    m = _re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text)
    if m:
        _add("vin", m.group(0), m.group(0))
    # Mileage: digits near "mileage" / "odometer".
    m = _re.search(r"(?:mileage|odometer)[^\d]{0,20}([\d,]{1,7})", text, _re.I)
    if m:
        _add("mileage", m.group(1), m.group(0))
    # Policy number: alphanumeric near "policy".
    m = _re.search(r"policy(?:\s+number)?[^\w]{0,10}([A-Z0-9-]{6,})", text, _re.I)
    if m:
        _add("policy_number", m.group(1), m.group(0))
    # Title state: two-letter code near "title" or "state".
    m = _re.search(r"(?:title|state)[^\w]{0,15}\b([A-Z]{2})\b", text)
    if m:
        _add("title_state", m.group(1), m.group(0))
    # Holder name: line after "holder" or "claimant".
    m = _re.search(r"(?:holder|claimant|insured)[^\w]{0,15}([A-Z][a-z]+ [A-Z][a-z]+)", text)
    if m:
        _add("holder_name", m.group(1), m.group(0))
    # Loss type: theft / fire / collision keywords.
    for loss in ("theft", "fire", "collision"):
        if _re.search(rf"\b{loss}\b", text, _re.I):
            _add("loss_type", loss, loss)
            break
    # Lien declared: look for lien checkbox patterns.
    if _re.search(r"lien.{0,30}yes", text, _re.I):
        _add("lien_declared", True, "lien yes")
    elif _re.search(r"lien.{0,30}no", text, _re.I):
        _add("lien_declared", False, "lien no")
    return fields


def _doc_rank(doc: dict) -> int:
    base = doc["source"].split(".")[0]
    if base.startswith("customer_reply"):
        return -1
    return _DOC_PRIORITY.get(base, 9)


def _normalize(field: str, value):
    """Light type coercion so downstream tools get clean types."""
    if value is None:
        return None
    if field == "mileage":
        try:
            return int(str(value).replace(",", "").strip())
        except (ValueError, TypeError):
            return value
    if field == "lien_declared":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True
        if s in ("false", "no", "n", "0"):
            return False
        return None
    return str(value).strip()


def _page_of_span(doc: dict, span: str) -> str:
    """Which page of the doc contains the supporting span?"""
    needle = (span or "")[:40].strip()
    if needle:
        for p in doc["pages"]:
            if needle in p["text"]:
                return str(p["page"])
    return "?"


def extract_fields(parsed_docs: list[dict], provider=None, case_id: str = "",
                   data_dir: str | None = None) -> dict:
    """Run the ReAct extraction loop.

    Args:
        parsed_docs: [{"source": "claim_form.pdf",
                       "pages": [{"page": 1, "text": "..."}, ...]}, ...]
        provider: an LLMProvider (defaults to get_provider()).
        case_id: for trace readability.
        data_dir: if given, each Thought/Action/Observation step is emitted
            to the event bus so the UI can stream the agent's thinking live.

    Returns:
        {"fields": {field: {"value", "confidence", "source", "span"}},
         "trace": [{"thought", "action", "observation"}, ...],
         "flags": [fields still below threshold -> human verification]}
    """
    if provider is None:
        from settleready.llm.provider import get_provider
        provider = get_provider()

    def _emit_react(step: dict, iteration: int) -> None:
        if data_dir:
            from settleready.memory.events import emit
            emit(data_dir, case_id, "react",
                 {"iteration": iteration, **step})

    docs = sorted(parsed_docs, key=_doc_rank)
    state: dict = {}
    # Fields whose current value came from a customer statement. A plain
    # document may not overturn those, even at higher confidence.
    overridden: set[str] = set()
    trace: list[dict] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        missing = [f for f in REQUIRED_FIELDS
                   if f not in state or state[f]["confidence"] < CONFIDENCE_THRESHOLD]
        if not missing:
            trace.append({
                "thought": f"Iteration {iteration}: all {len(REQUIRED_FIELDS)} fields "
                           f"at/above confidence {CONFIDENCE_THRESHOLD}. Done.",
                "action": "none",
                "observation": "extraction complete",
            })
            _emit_react(trace[-1], iteration)
            break

        thought = (f"Iteration {iteration}: still need {missing} "
                   f"({len(state)}/{len(REQUIRED_FIELDS)} fields secured). "
                   f"Extracting from the next most promising document.")
        doc = docs[(iteration - 1) % len(docs)]
        ask = list(missing)
        if iteration == 1:
            ask += [f for f in SUPPLEMENTARY_FIELDS if f not in state]
        action = f"extract({ask}) from {doc['source']}"
        text = "\n\n".join(f"[page {p['page']}]\n{p['text']}" for p in doc["pages"])
        user = (f"Document: {doc['source']} (case {case_id})\n\n{text}\n\n"
                f"Extract these fields: {ask}")

        try:
            raw = provider.complete(system=_SYSTEM, user=user,
                                    json_mode=True, max_tokens=1500, temperature=0.0)
            result = json.loads(raw).get("fields", {})
            error = None
            if data_dir:
                from settleready.memory.events import emit
                emit(data_dir, case_id, "tool_call",
                     {"tool": "llm.complete",
                      "args": {"purpose": f"extract {ask} from {doc['source']}",
                               "iteration": iteration},
                      "result": (f"{len([f for f in ask if (result.get(f) or {}).get('value')])}"
                                 f"/{len(ask)} fields returned")})
        except Exception as e:  # LLM failure or bad JSON: observe, don't crash
            result, error = {}, f"LLM call failed: {e}"

        newly = []
        if error is None:
            for f in ask:
                r = result.get(f) or {}
                try:
                    conf = float(r.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                val = _normalize(f, r.get("value"))
                if val is None:
                    continue
                prev = state.get(f)
                # Customer statements override: the claimant's latest word on a
                # field they address supersedes the form — and sticks. A plain
                # document may not overturn an override-sourced value even at
                # higher confidence, or a hedged (<0.8) reply would lose to the
                # form it was correcting. The verification layer still
                # cross-checks against authoritative records, so a mistaken
                # statement surfaces as a visible conflict, never a silent pass.
                if (prev is None or doc.get("override")
                        or (f not in overridden and conf > prev["confidence"])):
                    state[f] = {
                        "value": val,
                        "confidence": round(min(conf, 1.0), 2),
                        "source": f"{doc['source']} p.{_page_of_span(doc, r.get('span', ''))}",
                        "span": r.get("span", ""),
                    }
                    if doc.get("override"):
                        overridden.add(f)
                    newly.append(f"{f}={val!r}@{state[f]['confidence']}")

        still_missing = [f for f in REQUIRED_FIELDS
                         if f not in state or state[f]["confidence"] < CONFIDENCE_THRESHOLD]
        observation = (error or
                       f"extracted/updated: {newly or 'none'}; "
                       f"still below threshold: {still_missing or 'none'}")
        trace.append({"thought": thought, "action": action, "observation": observation})
        _emit_react(trace[-1], iteration)

    # Deterministic fallback: if the LLM produced nothing at all (provider
    # down, all calls failed), try regex extraction so the pipeline can
    # continue. Confidence is capped at 0.7, so every field still routes
    # to human verification — the fallback keeps the case moving, it does
    # not silently approve low-quality extraction.
    if not state:
        fallback = _extract_deterministic(parsed_docs)
        if fallback:
            state.update(fallback)
            trace.append({
                "thought": "LLM extraction produced nothing — using deterministic regex fallback.",
                "action": "extract_deterministic",
                "observation": f"fallback extracted: {sorted(fallback.keys())} (confidence capped at 0.7)",
            })
            if data_dir:
                from settleready.memory.events import emit
                emit(data_dir, case_id, "extraction_fallback",
                     {"fields": sorted(fallback.keys())})

    flags = [f for f in REQUIRED_FIELDS
             if f not in state or state[f]["confidence"] < CONFIDENCE_THRESHOLD]
    return {"fields": state, "trace": trace, "flags": flags}


if __name__ == "__main__":
    import argparse
    from settleready.tools.pdf_parser import parse_document

    p = argparse.ArgumentParser(description="Extract fields from a case's PDFs (ReAct loop)")
    p.add_argument("--pdf-dir", required=True, help="e.g. data/pdfs/TL-2026-1043")
    p.add_argument("--case", default="", help="case id for trace readability")
    a = p.parse_args()

    import glob, os
    docs = [{"source": os.path.basename(f), "pages": parse_document(f)}
            for f in sorted(glob.glob(os.path.join(a.pdf_dir, "*.pdf")))]
    out = extract_fields(docs, case_id=a.case)
    print(f"\nFields ({len(out['fields'])}/{len(REQUIRED_FIELDS)}), flags: {out['flags']}")
    for f, v in out["fields"].items():
        print(f"  {f:14s} {v['value']!r:45.45} conf={v['confidence']} [{v['source']}]")
    print(f"\nTrace ({len(out['trace'])} steps):")
    for t in out["trace"]:
        print(f"  THOUGHT: {t['thought']}\n  ACTION:  {t['action']}\n  OBS:     {t['observation']}\n")
