"""Evaluation harness (module 5).

Metrics: extraction accuracy vs. labeled fixtures, per-field confidence
calibration, jurisdiction precision (right state's law retrieved), ACV error
vs. expected band, packet completeness, tally-promotion behavior.

`pytest tests/` runs the interface tests; `python -m settleready.eval.metrics`
runs the full fixture-based eval and prints a score table.
"""
from __future__ import annotations


def run_eval(fixtures_dir: str = "data") -> dict:
    """TODO(module 5): return {metric: score} over labeled fixtures."""
    raise NotImplementedError
