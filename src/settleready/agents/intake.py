"""Intake agent (module 4).

Polls the inbox via EmailClient, classifies each claim, and routes total-loss
claims into the pipeline. Everything else -> other queues (out of scope).

Module 2: implement as a function using the ReAct loop (Thought -> Action -> Observation).
Module 4: promote to a LangGraph node.
"""
from __future__ import annotations


def route_claim(intake_event: dict) -> str:
    """Return 'total_loss' or 'other'. TODO(module 2): rule-based classifier on loss facts."""
    raise NotImplementedError
