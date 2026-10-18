"""Deterministic ACV calculator (module 4).

Money math NEVER goes through the LLM. The tool computes; the LLM narrates.

  acv = mean(comparables) - mileage_adjustment - condition_adjustment

All adjustments are documented constants in ADJUSTMENTS below so a reviewer can
audit the methodology. Formula matches data/seed.py::_expected_acv exactly, so
seeded `expected_acv` values double as eval ground truth.
"""
from __future__ import annotations

ADJUSTMENTS = {
    # dollars per full 10k miles above 50k
    "mileage_per_10k_over_50000": -150,
    "condition_fair": -300,
    "condition_poor": -800,
    "condition_good": 0,
}

_VALID_CONDITIONS = ("good", "fair", "poor")


def compute_acv(*, comparables: list[dict], mileage: int,
                condition: str = "good") -> tuple[int, dict]:
    """Compute actual cash value deterministically.

    Args:
        comparables: [{"price": int, ...}, ...] — at least 1.
        mileage: odometer reading.
        condition: "good" | "fair" | "poor".

    Returns:
        (acv:int, worksheet:dict) — worksheet shows every step for the audit log.
    """
    if not comparables:
        raise ValueError("need at least one comparable")
    if condition not in _VALID_CONDITIONS:
        raise ValueError(f"condition must be one of {_VALID_CONDITIONS}")

    prices = [c["price"] for c in comparables]
    comps_mean = round(sum(prices) / len(prices))

    steps = [{"name": "comparables_mean",
              "detail": f"mean of {len(prices)} comparables",
              "amount": comps_mean}]

    miles_over = max(0, (mileage - 50000) // 10000)
    mileage_adj = ADJUSTMENTS["mileage_per_10k_over_50000"] * miles_over
    steps.append({"name": "mileage_adjustment",
                  "detail": f"{miles_over} x $150 per 10k miles over 50k "
                            f"(odometer {mileage:,})",
                  "amount": mileage_adj})

    cond_adj = ADJUSTMENTS[f"condition_{condition}"]
    steps.append({"name": "condition_adjustment",
                  "detail": f"condition={condition}", "amount": cond_adj})

    acv = comps_mean + mileage_adj + cond_adj
    worksheet = {
        "formula": "mean(comparables) - $150 per 10k miles over 50k - condition adjustment",
        "steps": steps,
        "acv": acv,
        "inputs": {"n_comparables": len(prices), "mileage": mileage,
                   "condition": condition},
    }
    return acv, worksheet


if __name__ == "__main__":
    import argparse
    import json as _json

    p = argparse.ArgumentParser(description="Compute ACV from a comps JSON file")
    p.add_argument("--comps", required=True)
    p.add_argument("--mileage", type=int, required=True)
    p.add_argument("--condition", default="good")
    a = p.parse_args()
    with open(a.comps) as f:
        comps = _json.load(f)
    acv, ws = compute_acv(comparables=comps, mileage=a.mileage, condition=a.condition)
    print(f"ACV: ${acv:,}")
    for s in ws["steps"]:
        print(f"  {s['name']:20s} {s['amount']:+7d}  ({s['detail']})")
