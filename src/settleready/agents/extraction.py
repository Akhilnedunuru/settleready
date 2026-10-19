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
CONFIDENCE_THRESHOLD = 0.8
MAX_ITERATIONS = 3

_SYSTEM = """You are a precise insurance-document extraction assistant.
Extract ONLY the requested fields from the document text below.
For each field return: "value" (or null if absent), "confidence" (0.0-1.0),
and "span" (the exact short quote from the text supporting the value, or "").
Be conservative: when unsure, lower the confidence instead of guessing.
Types: mileage is an integer (no commas); lien_declared is true/false/null;
everything else is a string or null.
Respond with ONLY valid JSON, exactly:
{"fields": {"<field>": {"value": ..., "confidence": ..., "span": ...}}}"""

# Extraction order: claim form usually holds the most fields.
_DOC_PRIORITY = {"claim_form": 0, "police_report": 1, "title": 2, "repair_estimate": 3}


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

    docs = sorted(parsed_docs, key=lambda d: _DOC_PRIORITY.get(d["source"].split(".")[0], 9))
    state: dict = {}
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
        action = f"extract({missing}) from {doc['source']}"
        text = "\n\n".join(f"[page {p['page']}]\n{p['text']}" for p in doc["pages"])
        user = (f"Document: {doc['source']} (case {case_id})\n\n{text}\n\n"
                f"Extract these fields: {missing}")

        try:
            raw = provider.complete(system=_SYSTEM, user=user,
                                    json_mode=True, max_tokens=1500, temperature=0.0)
            result = json.loads(raw).get("fields", {})
            error = None
        except Exception as e:  # LLM failure or bad JSON: observe, don't crash
            result, error = {}, f"LLM call failed: {e}"

        newly = []
        if error is None:
            for f in missing:
                r = result.get(f) or {}
                try:
                    conf = float(r.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                val = _normalize(f, r.get("value"))
                if val is None:
                    continue
                prev = state.get(f)
                if prev is None or conf > prev["confidence"]:
                    state[f] = {
                        "value": val,
                        "confidence": round(min(conf, 1.0), 2),
                        "source": f"{doc['source']} p.{_page_of_span(doc, r.get('span', ''))}",
                        "span": r.get("span", ""),
                    }
                    newly.append(f"{f}={val!r}@{state[f]['confidence']}")

        still_missing = [f for f in REQUIRED_FIELDS
                         if f not in state or state[f]["confidence"] < CONFIDENCE_THRESHOLD]
        observation = (error or
                       f"extracted/updated: {newly or 'none'}; "
                       f"still below threshold: {still_missing or 'none'}")
        trace.append({"thought": thought, "action": action, "observation": observation})
        _emit_react(trace[-1], iteration)

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
