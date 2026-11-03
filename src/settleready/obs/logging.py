"""Structured JSONL logging for SettleReady (Module 6: observability).

One JSON object per line in data/obs/pipeline.jsonl. Every record carries
timestamp, level, trace_id, case_id, component, and message — so a run can
be reconstructed by filtering on trace_id.

Levels: DEBUG, INFO, WARNING, ERROR. The default level is INFO; set
SETTLEREADY_LOG_LEVEL=DEBUG for verbose output.

This is separate from the case event log (memory/events.py), which is the
user-visible audit trail. This log is for operators: timings, retries,
provider errors, guardrail decisions.
"""
from __future__ import annotations

import json
import os
import time

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class JsonLogger:
    """Append-only JSONL logger, correlated by trace_id/case_id."""

    def __init__(self, data_dir: str, trace_id: str = "", case_id: str = "",
                 component: str = "pipeline"):
        self.data_dir = data_dir
        self.trace_id = trace_id
        self.case_id = case_id
        self.component = component
        self._path = os.path.join(data_dir, "obs", "pipeline.jsonl")
        os.makedirs(os.path.join(data_dir, "obs"), exist_ok=True)
        self._min_level = _LEVELS.get(
            os.environ.get("SETTLEREADY_LOG_LEVEL", "INFO").upper(), 20)

    def _write(self, level: str, msg: str, **fields) -> None:
        if _LEVELS[level] < self._min_level:
            return
        record = {
            "ts": time.time(),
            "level": level,
            "trace_id": self.trace_id,
            "case_id": self.case_id,
            "component": self.component,
            "msg": msg,
        }
        record.update(fields)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass  # logging must never break the pipeline

    def debug(self, msg: str, **fields) -> None:
        self._write("DEBUG", msg, **fields)

    def info(self, msg: str, **fields) -> None:
        self._write("INFO", msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._write("WARNING", msg, **fields)

    def error(self, msg: str, **fields) -> None:
        self._write("ERROR", msg, **fields)

    def child(self, component: str, **fields) -> "JsonLogger":
        """Logger for a sub-component, inheriting trace/case correlation."""
        child = JsonLogger(self.data_dir, trace_id=self.trace_id,
                           case_id=self.case_id or fields.get("case_id", ""),
                           component=component)
        return child


def read_logs(data_dir: str, trace_id: str | None = None,
              case_id: str | None = None, level: str | None = None,
              limit: int = 500) -> list[dict]:
    """Read back JSONL logs, optionally filtered. Newest last."""
    path = os.path.join(data_dir, "obs", "pipeline.jsonl")
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if trace_id and rec.get("trace_id") != trace_id:
                    continue
                if case_id and rec.get("case_id") != case_id:
                    continue
                if level and rec.get("level") != level:
                    continue
                out.append(rec)
    except FileNotFoundError:
        pass
    return out[-limit:]
