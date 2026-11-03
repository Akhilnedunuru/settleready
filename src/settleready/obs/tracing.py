"""Span tracing for SettleReady (Module 6: observability).

One trace_id per pipeline run, spans per stage/agent with timing.
Spans are written to data/obs/traces.jsonl (one JSON object per line)
and also mirrored into the case event log for the UI.

Usage:
    from settleready.obs.tracing import Tracer

    tracer = Tracer(data_dir="data", case_id="TL-2026-1044")
    with tracer.span("extraction", agent="analyst"):
        ...work...
    # span records duration_ms on exit, even on exception.

Nested spans are supported via span(parent=...); the common case is
flat spans per stage, which is what the pipeline uses.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from typing import Iterator


def new_trace_id() -> str:
    """Random trace id, hex, 16 chars."""
    return uuid.uuid4().hex[:16]


class Span:
    """One timed unit of work. Use via Tracer.span()."""

    def __init__(self, tracer: "Tracer", name: str, agent: str = "",
                 parent_id: str | None = None, **attrs):
        self.tracer = tracer
        self.span_id = uuid.uuid4().hex[:16]
        self.trace_id = tracer.trace_id
        self.case_id = tracer.case_id
        self.name = name
        self.agent = agent
        self.parent_id = parent_id
        self.attrs = dict(attrs)
        self.start_ts = time.time()
        self.end_ts: float | None = None
        self.status = "ok"
        self.error: str | None = None

    def set(self, **attrs) -> "Span":
        """Add attributes to the span."""
        self.attrs.update(attrs)
        return self

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.end_ts = time.time()
        if exc_type is not None:
            self.status = "error"
            self.error = f"{exc_type.__name__}: {exc}"
        self.tracer._finish(self)
        return False  # do not swallow exceptions

    @property
    def duration_ms(self) -> float:
        end = self.end_ts if self.end_ts is not None else time.time()
        return round((end - self.start_ts) * 1000, 2)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "case_id": self.case_id,
            "name": self.name,
            "agent": self.agent,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "attrs": self.attrs,
        }


class Tracer:
    """Creates spans for one case run; persists them to data/obs/traces.jsonl."""

    def __init__(self, data_dir: str, case_id: str,
                 trace_id: str | None = None):
        self.data_dir = data_dir
        self.case_id = case_id
        self.trace_id = trace_id or new_trace_id()
        self._path = os.path.join(data_dir, "obs", "traces.jsonl")
        os.makedirs(os.path.join(data_dir, "obs"), exist_ok=True)
        self.spans: list[Span] = []
        self._open: list[Span] = []  # currently open spans (stack)

    def current_span(self) -> Span | None:
        """The innermost currently-open span, if any."""
        return self._open[-1] if self._open else None

    @contextmanager
    def span(self, name: str, agent: str = "",
             parent: Span | None = None, **attrs) -> Iterator[Span]:
        """Context manager yielding a Span; finishes and persists on exit."""
        span = Span(self, name, agent=agent,
                    parent_id=parent.span_id if parent else None, **attrs)
        self._open.append(span)
        try:
            yield span
        except Exception as e:
            span.end_ts = time.time()
            span.status = "error"
            span.error = f"{type(e).__name__}: {e}"
            raise
        else:
            span.end_ts = time.time()
        finally:
            # Pop from open stack (may already be gone if nested weirdly).
            if span in self._open:
                self._open.remove(span)
            self._finish(span)

    def _finish(self, span: Span) -> None:
        if span not in self.spans:
            self.spans.append(span)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(span.to_dict()) + "\n")
        except OSError:
            pass  # observability must never break the pipeline

    def summary(self) -> dict:
        """Aggregate timing per span name (for the eval report / UI)."""
        by_name: dict[str, dict] = {}
        for s in self.spans:
            d = by_name.setdefault(s.name, {"count": 0, "total_ms": 0.0,
                                            "errors": 0})
            d["count"] += 1
            d["total_ms"] += s.duration_ms
            if s.status == "error":
                d["errors"] += 1
        for d in by_name.values():
            d["total_ms"] = round(d["total_ms"], 2)
            d["avg_ms"] = round(d["total_ms"] / d["count"], 2) if d["count"] else 0.0
        return {"trace_id": self.trace_id, "case_id": self.case_id,
                "spans": by_name,
                "total_ms": round(sum(s.duration_ms for s in self.spans), 2)}
