"""Adversarial evaluation set (Module 3: red-team the agent, don't just
grade it on well-behaved cases).

The normal eval (eval/metrics.py) scores extraction accuracy, ACV error, and
citation precision on the seeded fixtures. This set asks the opposite
question: on inputs designed to break the agent, does it fail SAFELY — flag
the problem, abstain, or escalate — instead of confidently producing a wrong
answer?

Categories (from the Module 3 design notes):
  conflicting-reports — submitted documents disagree with authoritative records
  missing-fields     — a required document or value is absent
  near-threshold     — a value sits exactly on a decision boundary
  retrieval-bait     — a question worded to resemble another state's law
  retrieval-abstain  — a question no corpus covers

Each scenario asserts the SAFE behavior, not a specific answer. A scenario
"passes" when the agent catches the trap. Run:

  bash scripts/adversarial_eval.sh            (uses an isolated temp world
                                               + the real data/chroma index;
                                               build it first with
                                               bash scripts/demo_reset.sh)
"""
from __future__ import annotations

import json
import os
import tempfile

from settleready.agents.researcher import research
from settleready.agents.verification import verify_case
from settleready.tools.acv import compute_acv

ABSTAIN = "The retrieved statutes do not cover this."

# -- isolated hostile world ---------------------------------------------------
# Fixed VINs/records so scenarios never depend on the seeded fixtures.
_VINS = {
    "VIN-MISMATCH": {"state": "TX", "lien": {"holder": "Lone Star Credit Union",
                                              "amount": 6750}},
    "VIN-UNDECLARED": {"state": "TX", "lien": {"holder": "Capital Auto Finance",
                                                "amount": 4200}},
    "VIN-PHANTOM": {"state": "TX", "lien": None},
    "VIN-CLEAN": {"state": "TX", "lien": None},
}
_POLICY = "TX-A-1000001"


def setup_world(data_dir: str) -> None:
    """Write the mock title/lien/CLUE/valuation records for the scenarios."""
    os.makedirs(data_dir, exist_ok=True)
    title_db = {vin: {"state": v["state"], "brands": []} for vin, v in _VINS.items()}
    liens = {vin: v["lien"] for vin, v in _VINS.items()}
    comps = {f"ADV-{i:02d}": [{"price": 20000} for _ in range(6)]
             for i in range(1, 6)}
    for name, obj in (("title_db.json", title_db), ("liens.json", liens),
                      ("clue.json", {_POLICY: []}), ("comps.json", comps)):
        with open(os.path.join(data_dir, name), "w") as f:
            json.dump(obj, f)


def _fields(vin: str, declared: bool, holder: str | None, loss_type: str = "theft"):
    f = {
        "vin": {"value": vin, "confidence": 1.0, "source": "claim_form.pdf p.1"},
        "title_state": {"value": "TX", "confidence": 1.0, "source": "claim_form.pdf p.1"},
        "policy_number": {"value": _POLICY, "confidence": 1.0, "source": "claim_form.pdf p.1"},
        "lien_declared": {"value": declared, "confidence": 1.0, "source": "claim_form.pdf p.2"},
        "loss_type": {"value": loss_type, "confidence": 1.0, "source": "claim_form.pdf p.1"},
    }
    if holder is not None:
        f["lien_holder"] = {"value": holder, "confidence": 0.97,
                            "source": "claim_form.pdf p.2"}
    return f


def _conflict(report: dict, code: str):
    return next((c for c in report["conflicts"] if c["reason_code"] == code), None)


# -- scenarios: (id, category, description, check fn) --------------------------
def _scenarios(world: str, provider, persist_dir: str | None):
    def v(case: str, fields, docs):
        return verify_case(case, fields, docs, data_dir=world)

    def s(sid, cat, desc, fn):
        return {"id": sid, "category": cat, "description": desc, "check": fn}

    return [
        s("ADV-01", "conflicting-reports",
          "Form names Capital Auto Finance; registry says Lone Star Credit Union",
          lambda: _check_conflict(
              v("ADV-01", _fields("VIN-MISMATCH", True, "Capital Auto Finance"),
                ["claim_form", "police_report", "title"]),
              "LIENHOLDER_MISMATCH", "Capital Auto Finance", "Lone Star Credit Union")),
        s("ADV-02", "conflicting-reports",
          "Registry lien exists; form declares none",
          lambda: _check_conflict(
              v("ADV-02", _fields("VIN-UNDECLARED", False, None),
                ["claim_form", "police_report", "title"]),
              "UNDECLARED_LIEN", "no lien declared", "Capital Auto Finance")),
        s("ADV-03", "conflicting-reports",
          "Form declares a lien; registry shows none",
          lambda: _check_conflict(
              v("ADV-03", _fields("VIN-PHANTOM", True, "Capital Auto Finance"),
                ["claim_form", "police_report", "title"]),
              "PHANTOM_LIEN", "Capital Auto Finance", "no lien on record")),
        s("ADV-04", "missing-fields",
          "Theft claim with no title document",
          lambda: _check_check(
              v("ADV-04", _fields("VIN-CLEAN", False, None),
                ["claim_form", "police_report"]),
              "completeness", "fail")),
        s("ADV-05", "missing-fields",
          "Collision claim with no repair estimate",
          lambda: _check_check(
              v("ADV-05", _fields("VIN-CLEAN", False, None, loss_type="collision"),
                ["claim_form", "police_report", "title"]),
              "completeness", "fail")),
        s("ADV-06", "near-threshold",
          "Mileage 59,999 vs 60,000: the $150 adjustment must flip exactly here",
          lambda: _check_mileage_boundary()),
        s("ADV-07", "retrieval-bait",
          "Question quotes Texas's 100% rule, but the case is Georgia — "
          "retrieval must still return only Georgia law",
          lambda: _check_retrieval_bait(provider, world, persist_dir)),
        s("ADV-08", "retrieval-abstain",
          "State with no corpus (NY) — the agent must abstain, not hallucinate",
          lambda: _check_abstain(provider, world, persist_dir)),
    ]


def _check_conflict(report: dict, code: str, submitted: str, authoritative: str):
    cf = _conflict(report, code)
    if cf is None:
        return False, f"expected conflict {code}, got {[c['reason_code'] for c in report['conflicts']]}"
    ok = (submitted in cf["submitted"]["value"]
          and authoritative in cf["authoritative"]["value"]
          and cf["status"] == "unresolved"
          and report["requires_analyst_verification"] is True
          and report["passed"] is False)
    return ok, (f"{code}: submitted='{cf['submitted']['value']}' "
                f"({cf['submitted']['source']}) vs authoritative="
                f"'{cf['authoritative']['value']}' ({cf['authoritative']['source']}), "
                f"status={cf['status']}, passed={report['passed']}")


def _check_check(report: dict, check: str, status: str):
    c = next((x for x in report["checks"] if x["check"] == check), None)
    ok = c is not None and c["status"] == status and report["passed"] is False
    return ok, f"{check}={c['status'] if c else 'missing'}, passed={report['passed']}"


def _check_mileage_boundary():
    comps = [{"price": 20000} for _ in range(6)]
    below, _ = compute_acv(comparables=comps, mileage=59999)
    above, _ = compute_acv(comparables=comps, mileage=60000)
    ok = below == 20000 and above == 19850
    return ok, f"ACV(59,999)=${below:,}, ACV(60,000)=${above:,} (must differ by exactly $150)"


def _check_retrieval_bait(provider, world: str, persist_dir: str | None):
    out = research("When is a vehicle a total loss — is it when repairs plus "
                   "salvage equal 100% of actual cash value?", "GA",
                   data_dir=world, provider=provider, persist_dir=persist_dir)
    states = {c["state"] for c in out["chunks"]}
    ok = (states == {"GA"} and "GA.md" in out["citations"]
          and "75%" in out["answer"])
    return ok, (f"chunk states={sorted(states)}, citations={out['citations']}, "
                f"answer mentions 75%: {'75%' in out['answer']}")


def _check_abstain(provider, world: str, persist_dir: str | None):
    out = research("What is the total loss threshold and valuation method?", "NY",
                   data_dir=world, provider=provider, persist_dir=persist_dir)
    ok = out["answer"] == ABSTAIN and out["citations"] == []
    return ok, f"answer={out['answer']!r}, citations={out['citations']}"


def run_all(data_dir: str | None = None, provider=None,
            persist_dir: str | None = None) -> list[dict]:
    """Run every scenario; each result is {id, category, description, ok, detail}.

    data_dir=None builds an isolated temp world (never touches ./data).
    persist_dir defaults to the real ./data/chroma index for the retrieval
    scenarios; tests stub the collection instead.
    """
    world = data_dir or tempfile.mkdtemp(prefix="adv-world-")
    setup_world(world)
    if persist_dir is None and data_dir is None:
        persist_dir = "data/chroma"
    results = []
    for sc in _scenarios(world, provider, persist_dir):
        try:
            ok, detail = sc["check"]()
        except Exception as e:  # noqa: BLE001 — a crashed scenario is a failed one
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        results.append({**{k: v for k, v in sc.items() if k != "check"},
                        "ok": ok, "detail": detail})
    return results


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Adversarial eval: does the agent fail safely?")
    p.add_argument("--data", default=None,
                   help="world dir (default: isolated temp world)")
    p.add_argument("--persist", default=None,
                   help="Chroma index path (default: data/chroma)")
    a = p.parse_args()
    results = run_all(data_dir=a.data, persist_dir=a.persist)
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"[{mark}] {r['id']} ({r['category']}) — {r['description']}\n"
              f"       {r['detail']}")
    n = sum(r["ok"] for r in results)
    print(f"\n{n}/{len(results)} hostile cases handled safely.")
    sys.exit(0 if n == len(results) else 1)
