"""Case Blackboard — the agent's short-term (working) memory (module 2).

One structured, persistent state object per case that every stage reads and writes:
extracted fields + confidence, documents received, verification results,
review-loop position. Backed by SQLite (data/blackboard.db); Pydantic-validated.
"""
from __future__ import annotations


class CaseBlackboard:
    """TODO(module 2): SQLite-backed case state with typed read/write/update."""

    def __init__(self, db_path: str = "data/blackboard.db") -> None:
        raise NotImplementedError
