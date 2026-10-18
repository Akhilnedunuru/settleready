"""Case builder agent (module 5).

Assembles the review packet from verified Blackboard state: extracted fields
with confidence + source spans, verification checks, cited legal note, and the
deterministic ACV worksheet. Hard rule: every factual claim carries a citation
(source span or DB record); the LLM narrates, the ACV calculator computes.
"""
from __future__ import annotations

import os

from settleready.memory.blackboard import CaseBlackboard


def build_packet(case_id: str, data_dir: str = "data") -> dict:
    """Assemble the senior-analyst review packet for a case.

    Returns {"case_id", "stage", "fields", "verification", "legal",
             "acv", "returns", "citations"} — everything the Review tab renders.
    """
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    case = bb.get_case(case_id)
    if not case:
        bb.close()
        raise ValueError(f"unknown case {case_id} — run the pipeline first")
    state = bb.read(case_id)

    fields = state.get("fields", {})
    verification = state.get("verification", {})
    legal = state.get("legal_research", {})
    acv = state.get("acv", {})

    citations = []
    for name, f in fields.items():
        if isinstance(f, dict) and f.get("source"):
            citations.append(f"{name}: {f['source']}")
    for c in (legal.get("citations") or []):
        citations.append(f"legal: {c}")

    packet = {
        "case_id": case_id,
        "stage": case["stage"],
        "loss_type": case.get("loss_type", ""),
        "title_state": (fields.get("title_state") or {}).get("value", ""),
        "fields": fields,
        "extraction_flags": state.get("extraction_flags", []),
        "verification": verification,
        "legal": {"answer": legal.get("answer", ""),
                  "citations": legal.get("citations", [])},
        "acv": acv,
        "return_count": state.get("return_count", 0),
        "last_return": state.get("last_return"),
        "citations": citations,
    }
    bb.close()
    return packet


if __name__ == "__main__":
    import argparse
    import json as _json

    p = argparse.ArgumentParser()
    p.add_argument("--case", required=True)
    p.add_argument("--data", default="data")
    a = p.parse_args()
    print(_json.dumps(build_packet(a.case, data_dir=a.data), indent=2, default=str))
