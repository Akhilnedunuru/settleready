"""Deterministic ACV calculator (module 3).

Money math NEVER goes through the LLM. The tool computes; the LLM narrates.

  acv = mean(comparables) - mileage_adjustment - condition_adjustment + options_adjustment

All adjustments are documented constants in ADJUSTMENTS below so a reviewer can
audit the methodology. Returns (acv, worksheet_dict).
"""
from __future__ import annotations

ADJUSTMENTS = {
    "mileage_per_10k_over_60000": -150.0,  # dollars per 10k miles above 60k
    "condition_poor": -800.0,
    "condition_fair": -300.0,
}


def compute_acv(*, comparables: list[dict], mileage: int, condition: str,
                options_value: float = 0.0) -> tuple[float, dict]:
    """TODO(module 3): implement per ADJUSTMENTS; return (acv, worksheet)."""
    raise NotImplementedError
