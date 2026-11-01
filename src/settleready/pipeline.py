"""SettleReady pipeline entry point.

The multi-agent orchestration lives in settleready.agents.graph (Checkpoint
5.1: a LangGraph StateGraph over the ingestion, grounding, outreach, and
reviewer agents, with the Checkpoint 4.1 ToT disposition planner embedded as
the non-clean-case branch). This module owns the run shell: the running
marker the watcher and UI poll, the pipeline lifecycle events, and the
summary contract the CLI, UI, and tests consume.

Run:  PYTHONPATH=src python -m settleready.pipeline --case-id TL-2026-1043 --data data
"""
from __future__ import annotations

import os

from settleready.agents.graph import run_case_graph
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.events import emit as _emit_event


def _running_marker(data_dir: str, case_id: str) -> str:
    return os.path.join(data_dir, "running", case_id)


def is_running(data_dir: str, case_id: str, stale_after: int = 180) -> bool:
    """Is the pipeline actively processing this case? A marker file written
    at pipeline start (and removed at the end) says so; markers older than
    `stale_after` seconds are treated as crashed, not running."""
    import time
    try:
        age = time.time() - os.path.getmtime(_running_marker(data_dir, case_id))
        return age < stale_after
    except OSError:
        return False


def run_case(case_id: str, data_dir: str = "data", provider=None) -> dict:
    """Run the multi-agent graph for one case, via the blackboard.

    Returns {"case_id", "stages", "trace", "audit", "next"} plus optional
    "outreach" / "disposition" / "review" details from the graph's final state.
    """
    import time
    os.makedirs(os.path.join(data_dir, "running"), exist_ok=True)
    with open(_running_marker(data_dir, case_id), "w") as f:
        f.write(str(time.time()))
    _emit_event(data_dir, case_id, "pipeline_started")
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        bb.create_case(case_id)
        final = run_case_graph(case_id, data_dir=data_dir, provider=provider)
        summary: dict = {
            "case_id": case_id,
            "stages": final.get("summaries", []),
            "trace": final.get("trace", []),
            "audit": bb.audit_log(case_id),
            "next": (bb.get_case(case_id) or {}).get("stage", ""),
        }
        for summary_key, state_key in (("outreach", "outreach"),
                                       ("disposition", "plan"),
                                       ("review", "review")):
            if final.get(state_key):
                summary[summary_key] = final[state_key]
        return summary
    except Exception as e:
        _emit_event(data_dir, case_id, "pipeline_failed", {"error": str(e)[:300]})
        raise
    finally:
        try:
            os.remove(_running_marker(data_dir, case_id))
        except OSError:
            pass
        _emit_event(data_dir, case_id, "pipeline_finished")
        bb.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Run the SettleReady pipeline for one case")
    p.add_argument("--case-id", default="TL-2026-1043")
    p.add_argument("--data", default="data")
    p.add_argument("--show-trace", action="store_true",
                   help="print the ReAct Thought->Action->Observation trace")
    a = p.parse_args()

    out = run_case(a.case_id, data_dir=a.data)
    print(f"\nCase {out['case_id']} -> {out['next']}")
    for s in out["stages"]:
        print(f"  {s}")
    if a.show_trace:
        print("  ReAct trace:")
        for t in out.get("trace", []):
            print(f"    THOUGHT: {t['thought']}")
            print(f"    ACTION : {t['action']}")
            print(f"    OBS    : {t['observation']}\n")
    print("  audit:")
    for e in out["audit"]:
        print(f"    {e['ts']} [{e['stage']}] {e['event']}")
