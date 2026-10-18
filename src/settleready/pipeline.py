"""Final integration: wires every module's components into one pipeline (module 5+).

intake -> extract -> completeness gate -> verify -> RAG -> compute & assemble
                                                  -> human review -> (delta loop)

Each stage is swappable; the Case Blackboard is the contract between them.
"""
from __future__ import annotations


def run_case(case_id: str) -> dict:
    """TODO(module 4/5): orchestrate the full pipeline for one case."""
    raise NotImplementedError


if __name__ == "__main__":
    import sys
    print(run_case(sys.argv[1] if len(sys.argv) > 1 else "TL-2026-1043"))
