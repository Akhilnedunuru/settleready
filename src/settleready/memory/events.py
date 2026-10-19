"""Lightweight event bus (module 5).

The intake watcher, pipeline, and agents emit events to a file-backed JSONL
log (data/events.jsonl). The Streamlit UI tails this log and re-renders only
when new events arrive — event-driven updates instead of blind polling.
(Streamlit cannot receive push from an external process, so the UI ticks a
cheap stat() on this file every ~2s; renders are caused by events, the tick
is just the transport.)

Event shapes:
  {"ts", "case_id", "event", "detail"}
Well-known events: email_received, case_created, duplicate_skipped,
pipeline_started, pipeline_finished, pipeline_failed, stage,
react (Thought/Action/Observation), review, packet_built.
"""
from __future__ import annotations

import json
import os
import time

_FILENAME = "events.jsonl"


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, _FILENAME)


def emit(data_dir: str, case_id: str, event: str, detail: dict | None = None) -> None:
    """Append one event. Never raises — the event bus must not break the pipeline."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        rec = {"ts": time.time(), "case_id": case_id, "event": event}
        if detail:
            rec["detail"] = detail
        with open(_path(data_dir), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def seq(data_dir: str) -> int:
    """Cheap change detector: number of events logged so far."""
    try:
        with open(_path(data_dir), "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def tail(data_dir: str, case_id: str | None = None, limit: int = 100) -> list[dict]:
    """Newest-first events, optionally filtered to one case."""
    try:
        with open(_path(data_dir)) as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if case_id is not None and rec.get("case_id") != case_id:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out
