"""Retrieval A/B demonstration (checkpoint 3.1 evidence).

Runs the SAME legal question twice against the SAME model:

  A) parametric baseline — the LLM answers from its own knowledge, no
     retrieved context, nothing to cite;
  B) grounded — the researcher (state-filtered retrieval -> synthesis
     constrained to the retrieved chunks, citations required).

The contrast is the point: retrieval doesn't just decorate the answer, it
changes what the answer says (the controlling state's actual rule, with a
citation the analyst can audit) and it gates the answer entirely — when
retrieval finds nothing, path B declines instead of guessing.

Requires the index: bash scripts/demo_reset.sh  (or seed.sh + build_index.sh)
Run:  bash scripts/retrieval_demo.sh [--state GA] [--question "..."]
"""
from __future__ import annotations

import json

from settleready.agents.researcher import research

_BASELINE_SYSTEM = """You are an insurance-law research assistant. Answer the
question from your own knowledge. Respond with ONLY valid JSON:
{"answer": "...", "cited_sources": []}"""

DEFAULT_QUESTION = "What is the total loss threshold and valuation method?"


def compare(question: str, title_state: str, data_dir: str = "data",
            provider=None, k: int = 3) -> dict:
    """Baseline (no retrieval) vs grounded (retrieval) for one question."""
    if provider is None:
        from settleready.llm.provider import get_provider
        provider = get_provider()

    raw = provider.complete(
        system=_BASELINE_SYSTEM,
        user=f"Question: {question} The vehicle is titled in {title_state.upper()}.",
        json_mode=True, max_tokens=800, temperature=0.0)
    try:
        b = json.loads(raw)
        baseline = {"answer": b.get("answer", ""),
                    "citations": b.get("cited_sources") or []}
    except json.JSONDecodeError:
        baseline = {"answer": raw, "citations": []}

    grounded = research(question, title_state, data_dir=data_dir,
                        provider=provider, k=k)
    return {"question": question, "state": title_state.upper(),
            "baseline": baseline, "grounded": grounded}


def _print(out: dict) -> None:
    print(f"Question ({out['state']}): {out['question']}\n")
    print("--- A) WITHOUT retrieval (parametric memory) ---")
    print(out["baseline"]["answer"])
    print(f"citations: {out['baseline']['citations'] or 'none'}\n")
    print("--- B) WITH retrieval (grounded) ---")
    print(out["grounded"]["answer"])
    print(f"citations: {out['grounded']['citations'] or 'none'}")
    print("\nretrieved chunks (evidence):")
    for c in out["grounded"]["chunks"]:
        print(f"  [{c['state']}/{c['citation']} score={c['score']}] {c['text'][:150]}…")
    if not out["grounded"]["chunks"]:
        print("  (none — the grounded path declines to answer rather than guess)")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Retrieval A/B: parametric vs grounded")
    p.add_argument("--question", default=DEFAULT_QUESTION)
    p.add_argument("--state", default="GA")
    p.add_argument("--data", default="data")
    p.add_argument("--k", type=int, default=3)
    a = p.parse_args()
    _print(compare(a.question, a.state, data_dir=a.data, k=a.k))
