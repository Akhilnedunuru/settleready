"""Verification agent (module 3/4).

Fans out to title DB, lienholder API, CLUE, and valuation service in parallel,
then runs conflict resolution: extracted vs. verified — verified record wins,
discrepancies become exception flags.

Reads promoted upfront checks from the return tally store at stage start.
"""
from __future__ import annotations


def verify_case(case_state: dict) -> dict:
    """TODO(module 3): fan-out + Conflict Resolver. Returns resolved facts + flags."""
    raise NotImplementedError
