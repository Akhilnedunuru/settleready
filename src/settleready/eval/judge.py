"""LLM-as-judge evaluations (Module 6).

Complements the deterministic evals (eval/metrics.py) with qualitative
judgments an LLM is good at:
- extraction_faithfulness: are extracted values actually in the source docs?
- email_quality: is the outreach email clear, professional, asks-only?
- grounding_quality: does the legal answer stay within its citations?

Each judge returns a 1-5 score + rationale. The judge is a SEPARATE
provider call with temperature 0 — it never touches pipeline state.

Run: PYTHONPATH=src python -m settleready.eval.judge --data data --case TL-2026-1044
"""
from __future__ import annotations

import argparse
import json
import os

_JUDGE_SYSTEM = """You are an evaluator for an insurance claim-processing agent.
Score the agent's output on the given rubric from 1 (poor) to 5 (excellent).
Reply in JSON: {"score": <1-5>, "rationale": "<one or two sentences>"}.
Be strict: a 5 means no flaws; a 3 means usable but with issues; a 1 means
wrong or unsafe. Do not add fields."""


def _judge(provider, rubric: str, content: str) -> dict:
    """One judge call. Returns {"score": int, "rationale": str}."""
    raw = provider.complete(system=_JUDGE_SYSTEM,
                            user=f"Rubric:\n{rubric}\n\nContent to score:\n{content}",
                            max_tokens=300, temperature=0.0, json_mode=True)
    try:
        d = json.loads(raw)
        score = max(1, min(5, int(d.get("score", 1))))
        return {"score": score, "rationale": str(d.get("rationale", ""))[:500]}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"score": 1, "rationale": f"Judge returned unparseable output: {raw[:200]}"}


_EXTRACTION_RUBRIC = """Score extraction faithfulness (1-5):
5: Every field value appears verbatim (or as a clear normalization) in the cited source document/page; confidences are calibrated (low for ambiguous).
3: Values mostly grounded but one field is a stretch, or a confidence is overconfident.
1: A field value is invented (not in any source) or the source cited does not contain it."""


def judge_extraction(provider, fields: dict, docs: list[dict]) -> dict:
    """Are extracted field values actually grounded in the source docs?"""
    field_lines = "\n".join(
        f"- {f}: {v.get('value')!r} (source: {v.get('source')}, conf: {v.get('confidence')})"
        for f, v in fields.items())
    doc_lines = "\n".join(
        f"[{d.get('source')}] " + " ".join(
            p.get("text", "")[:400] for p in d.get("pages", [])[:2])[:800]
        for d in docs[:6])
    content = f"Extracted fields:\n{field_lines}\n\nSource documents (excerpts):\n{doc_lines}"
    return {"dimension": "extraction_faithfulness",
            **_judge(provider, _EXTRACTION_RUBRIC, content)}


_EMAIL_RUBRIC = """Score the customer outreach email (1-5):
5: Clear, professional, empathetic tone; asks ONLY for the listed missing items; no jargon; no promises about the claim outcome; includes case ID.
3: Usable but wordy, slightly cold, or buries the ask.
1: Unprofessional tone, asks for things not listed, promises an outcome, or omits the case ID."""


def judge_email(provider, subject: str, body: str, needs: list[str]) -> dict:
    """Is the outreach email clear, professional, and asks-only?"""
    content = (f"Subject: {subject}\n\nBody:\n{body}\n\n"
               f"Approved asks (must not exceed these): {needs}")
    return {"dimension": "email_quality",
            **_judge(provider, _EMAIL_RUBRIC, content)}


_GROUNDING_RUBRIC = """Score legal grounding (1-5):
5: Every factual claim in the answer is supported by a cited chunk; citations are real (from the provided set); no parametric legal knowledge.
3: Mostly grounded but one claim stretches a citation, or a citation is vague.
1: Contains a legal claim with no citation, or cites something not in the provided set."""


def judge_grounding(provider, answer: str, citations: list[str],
                    chunks: list[dict]) -> dict:
    """Does the legal answer stay within its citations?"""
    chunk_lines = "\n".join(
        f"[{c.get('citation')}] {c.get('text', '')[:500]}" for c in chunks[:8])
    content = (f"Answer:\n{answer}\n\nCitations: {citations}\n\n"
               f"Retrieved chunks:\n{chunk_lines}")
    return {"dimension": "grounding_quality",
            **_judge(provider, _GROUNDING_RUBRIC, content)}


def judge_case(data_dir: str, case_id: str, provider) -> dict:
    """Run all three judges on a case's pipeline output."""
    from settleready.memory.blackboard import CaseBlackboard
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        fields = bb.read(case_id, "fields") or {}
        legal = bb.read(case_id, "legal") or {}
        chunks = legal.get("chunks", [])
    finally:
        bb.close()
    # Docs: reconstruct minimal excerpts from PDFs is expensive; use fields'
    # sources as the doc list for the judge (faithfulness = value in source).
    docs = [{"source": v.get("source", ""), "pages": [{"text": ""}]}
            for v in fields.values()]
    results = {
        "case_id": case_id,
        "judges": [
            judge_extraction(provider, fields, docs),
            judge_grounding(provider, legal.get("answer", ""),
                            legal.get("citations", []), chunks),
        ],
    }
    # Email judge: score the most recent sent email, if any.
    from settleready.memory import events
    sent = [e for e in events.tail(data_dir, case_id=case_id, limit=200)
            if e["event"] == "email_sent"]
    if sent:
        d = sent[-1]["detail"]
        results["judges"].append(judge_email(
            provider, d.get("subject", ""), d.get("body", ""),
            d.get("needs", [])))
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--case", required=True)
    args = ap.parse_args()
    from settleready.llm.provider import get_provider
    provider = get_provider()
    print(json.dumps(judge_case(args.data, args.case, provider), indent=2))


if __name__ == "__main__":
    main()
