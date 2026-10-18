"""Evaluation harness (module 5).

Metrics over the labeled synthetic fixtures:
- extraction accuracy: extracted fields vs. cases.json ground truth
- extraction coverage: fraction of required fields present at all
- low-confidence rate: fraction of extracted fields below the 0.8 threshold
- ACV error: |computed - expected| / expected vs. seeded ground truth
- citation precision: legal citations == ["<STATE>.md"]

Reads the Blackboard (pipeline output); cases without pipeline output are
reported as "not run" rather than scored zero.

Run:  PYTHONPATH=src python -m settleready.eval.metrics --data data
"""
from __future__ import annotations

import json
import os

from settleready.agents.extraction import CONFIDENCE_THRESHOLD, REQUIRED_FIELDS
from settleready.memory.blackboard import CaseBlackboard

_GROUND_TRUTH_FIELDS = {
    "vin": lambda c: c["vehicle"]["vin"],
    "mileage": lambda c: c["vehicle"]["mileage"],
    "title_state": lambda c: c["title_state"],
    "policy_number": lambda c: c["policy"]["number"],
    "holder_name": lambda c: c["policy"]["holder"],
    "loss_type": lambda c: c["loss_type"],
    "loss_date": lambda c: c["loss"]["date"],
    "lien_declared": lambda c: c["declared_lien"],
}


def _norm(field: str, value):
    if value is None:
        return None
    if field == "mileage":
        try:
            return int(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return value
    if field == "lien_declared":
        return bool(value)
    return str(value).strip().lower()


def evaluate_case(spec: dict, state: dict) -> dict:
    """Score one case's pipeline output against its fixture ground truth."""
    fields = state.get("fields", {})
    per_field, correct, present, low_conf = {}, 0, 0, 0
    for f in REQUIRED_FIELDS:
        got = _norm(f, (fields.get(f) or {}).get("value"))
        want = _norm(f, _GROUND_TRUTH_FIELDS[f](spec))
        if got is not None:
            present += 1
            hit = got == want
            correct += hit
            per_field[f] = "ok" if hit else f"got {got!r}, want {want!r}"
            if (fields[f].get("confidence") or 0) < CONFIDENCE_THRESHOLD:
                low_conf += 1
        else:
            per_field[f] = "missing"

    acv_info = state.get("acv") or {}
    acv_error = None
    if acv_info.get("acv") is not None:
        acv_error = abs(acv_info["acv"] - spec["expected_acv"]) / spec["expected_acv"]

    legal = state.get("legal_research") or {}
    cite_ok = None
    if legal.get("citations") is not None:
        cite_ok = legal["citations"] == [f"{spec['title_state']}.md"]

    return {
        "case_id": spec["case_id"],
        "extraction_accuracy": round(correct / len(REQUIRED_FIELDS), 3),
        "extraction_coverage": round(present / len(REQUIRED_FIELDS), 3),
        "low_confidence_rate": round(low_conf / max(present, 1), 3),
        "acv_error_pct": round(acv_error * 100, 2) if acv_error is not None else None,
        "citation_ok": cite_ok,
        "per_field": per_field,
    }


def run_eval(data_dir: str = "data") -> dict:
    """Score all fixture cases with pipeline output. Returns {"cases", "summary"}."""
    with open(os.path.join(data_dir, "cases.json")) as f:
        specs = json.load(f)
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    results, skipped = [], []
    for spec in specs:
        state = bb.read(spec["case_id"])
        if not state.get("fields"):
            skipped.append(spec["case_id"])
            continue
        results.append(evaluate_case(spec, state))
    bb.close()

    def _mean(key):
        vals = [r[key] for r in results if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "n_cases": len(specs),
        "n_scored": len(results),
        "skipped": skipped,
        "mean_extraction_accuracy": _mean("extraction_accuracy"),
        "mean_extraction_coverage": _mean("extraction_coverage"),
        "mean_low_confidence_rate": _mean("low_confidence_rate"),
        "mean_acv_error_pct": _mean("acv_error_pct"),
        "citation_precision": _mean("citation_ok"),
    }
    return {"cases": results, "summary": summary}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Evaluate pipeline output vs. fixture ground truth")
    p.add_argument("--data", default="data")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    out = run_eval(a.data)
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        s = out["summary"]
        print(f"cases: {s['n_scored']}/{s['n_cases']} scored"
              + (f" (not run: {s['skipped']})" if s["skipped"] else ""))
        for c in out["cases"]:
            print(f"  {c['case_id']}: accuracy={c['extraction_accuracy']} "
                  f"coverage={c['extraction_coverage']} lowconf={c['low_confidence_rate']} "
                  f"acv_err={c['acv_error_pct']}% cite_ok={c['citation_ok']}")
        print("summary:", {k: v for k, v in s.items()
                            if k not in ("n_cases", "n_scored", "skipped")})
