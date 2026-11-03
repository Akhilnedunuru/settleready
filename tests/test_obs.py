"""Tests for Module 6: observability (tracing, logging, cost)."""
import json
import os
import tempfile

from settleready.obs.cost import MeteredProvider
from settleready.obs.logging import JsonLogger, read_logs
from settleready.obs.tracing import Tracer, new_trace_id


class _FakeProvider:
    name = "fake"

    def complete(self, *, system, user, max_tokens=1024, temperature=0.2,
                 json_mode=False):
        return "fake answer"

    def embed(self, text):
        return [0.0] * 4


def test_trace_id_unique():
    assert new_trace_id() != new_trace_id()
    assert len(new_trace_id()) == 16


def test_span_timing_and_persist():
    tmp = tempfile.mkdtemp()
    tracer = Tracer(tmp, "TL-TEST")
    with tracer.span("extraction", agent="analyst") as s:
        s.set(fields=8)
    assert s.duration_ms >= 0
    assert s.status == "ok"
    # Persisted to JSONL
    path = os.path.join(tmp, "obs", "traces.jsonl")
    assert os.path.exists(path)
    with open(path) as f:
        rec = json.loads(f.readline())
    assert rec["name"] == "extraction"
    assert rec["trace_id"] == tracer.trace_id
    assert rec["attrs"]["fields"] == 8


def test_span_error_marked():
    tmp = tempfile.mkdtemp()
    tracer = Tracer(tmp, "TL-TEST")
    try:
        with tracer.span("ground"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert tracer.spans[-1].status == "error"
    assert "boom" in (tracer.spans[-1].error or "")


def test_tracer_summary():
    tmp = tempfile.mkdtemp()
    tracer = Tracer(tmp, "TL-TEST")
    with tracer.span("a"):
        pass
    with tracer.span("a"):
        pass
    with tracer.span("b"):
        pass
    summ = tracer.summary()
    assert summ["spans"]["a"]["count"] == 2
    assert summ["spans"]["b"]["count"] == 1
    assert summ["total_ms"] >= 0


def test_json_logger_levels_and_filter():
    tmp = tempfile.mkdtemp()
    log = JsonLogger(tmp, trace_id="t1", case_id="C1", component="test")
    log.debug("hidden by default")
    log.info("visible", stage="extraction")
    log.error("bad", err="x")
    recs = read_logs(tmp)
    assert len(recs) == 2
    assert recs[0]["msg"] == "visible"
    assert recs[0]["trace_id"] == "t1"
    assert recs[0]["stage"] == "extraction"
    errs = read_logs(tmp, level="ERROR")
    assert len(errs) == 1 and errs[0]["msg"] == "bad"
    by_case = read_logs(tmp, case_id="C1")
    assert len(by_case) == 2


def test_metered_provider_counts():
    tmp = tempfile.mkdtemp()
    tracer = Tracer(tmp, "TL-TEST")
    inner = _FakeProvider()
    mp = MeteredProvider(inner, tracer=tracer)
    out = mp.complete(system="sys", user="hello world")
    assert out == "fake answer"
    totals = mp.totals()
    assert totals["calls"] == 1
    assert totals["prompt_tokens"] > 0
    assert totals["completion_tokens"] > 0
    assert totals["est_cost_usd"] >= 0
