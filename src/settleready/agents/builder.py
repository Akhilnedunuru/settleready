"""Case builder agent (module 3/4).

Assembles the packet: valuation worksheet, title report, compliance note.
Hard rule: every factual claim must carry a citation (source span or DB record);
the LLM narrates, the ACV calculator computes.
"""
from __future__ import annotations


def build_packet(case_state: dict) -> dict:
    """TODO(module 3): assemble cited packet from verified Blackboard state."""
    raise NotImplementedError
