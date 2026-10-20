"""Module-4 tests: ACV math (exact) + RAG retrieval contract.

ChromaDB can't install in every CI sandbox, so the RAG tests run against a fake
collection implementing the same interface — they verify the *contract* that
matters: the state metadata filter is applied before ranking, and citations
flow through to the researcher.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import settleready.tools.rag as ragmod
from settleready.agents.researcher import research
from settleready.tools.acv import ADJUSTMENTS, compute_acv


# -- ACV: deterministic money math ----------------------------------------------
def test_acv_matches_seed_formula():
    comps = [{"price": p} for p in [18000, 18200, 18600, 18400, 18800, 18400]]  # mean 18400
    acv, ws = compute_acv(comparables=comps, mileage=92000)
    assert acv == 17800, "18400 - 4x$150 (92k miles)"
    assert ws["steps"][0]["amount"] == 18400
    assert "mean(comparables)" in ws["formula"]


def test_acv_no_mileage_adjustment_under_50k():
    comps = [{"price": 22300}] * 6
    acv, ws = compute_acv(comparables=comps, mileage=41000)
    assert acv == 22300


def test_acv_condition_adjustments():
    comps = [{"price": 20000}] * 4
    assert compute_acv(comparables=comps, mileage=30000, condition="poor")[0] == 19200
    assert compute_acv(comparables=comps, mileage=30000, condition="fair")[0] == 19700
    assert compute_acv(comparables=comps, mileage=30000, condition="good")[0] == 20000


def test_acv_constants_auditable():
    assert ADJUSTMENTS["mileage_per_10k_over_50000"] == -150


def test_acv_rejects_empty_comps():
    try:
        compute_acv(comparables=[], mileage=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty comparables")


# -- RAG: retrieval contract -------------------------------------------------------
class FakeCollection:
    """Stands in for Chroma; asserts the where-clause contract."""

    def __init__(self):
        self.last_where = None

    def query(self, query_embeddings, where, n_results):
        self.last_where = where
        assert where == {"state": "TX"}, \
            f"state filter must be applied BEFORE ranking, got {where}"
        docs = ["TOTAL LOSS THRESHOLD: repairs plus salvage >= 100% of ACV is a total loss.",
                "VALUATION METHOD: ACV from comparable vehicles of like kind and quality."]
        docs = docs[:n_results]
        return {"documents": [docs],
                "metadatas": [[{"state": "TX", "source": "TX.md"}] * len(docs)],
                "distances": [[0.1, 0.25][:len(docs)]]}


class FakeProvider:
    name = "fake"

    def embed(self, text):
        return [0.1] * 8

    def complete(self, *, system, user, max_tokens=1024, temperature=0.2, json_mode=False):
        assert "TX.md" in user, "researcher must ground the LLM in retrieved chunks"
        return json.dumps({"answer": "100% of ACV [TX.md]", "cited_sources": ["TX.md"]})


def test_retrieve_applies_state_filter_before_ranking(monkeypatch):
    fake = FakeCollection()
    monkeypatch.setattr(ragmod, "_collection", lambda persist_dir, provider=None: fake)
    out = ragmod.retrieve("tx", "total loss threshold",
                          persist_dir="/tmp/does-not-matter", provider=FakeProvider())
    assert fake.last_where == {"state": "TX"}
    assert len(out) == 2
    assert all(r["state"] == "TX" for r in out)
    assert out[0]["citation"] == "TX.md"
    assert out[0]["score"] == 0.1


def test_researcher_returns_cited_answer(monkeypatch):
    fake = FakeCollection()
    monkeypatch.setattr(ragmod, "_collection", lambda persist_dir, provider=None: fake)
    out = research("What is the total loss threshold?", "TX",
                   data_dir="/tmp/does-not-matter", provider=FakeProvider())
    assert out["state"] == "TX"
    assert "TX.md" in out["citations"]
    assert out["answer"]


def test_researcher_empty_retrieval_says_so(monkeypatch):
    class Empty(FakeCollection):
        def query(self, query_embeddings, where, n_results):
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    monkeypatch.setattr(ragmod, "_collection",
                        lambda persist_dir, provider=None: Empty())
    out = research("anything", "ZZ", data_dir="/tmp/x", provider=FakeProvider())
    assert "do not cover" in out["answer"]
    assert out["citations"] == []


def test_chunk_markdown():
    chunks = ragmod._chunk_markdown(
        "# SYNTHETIC DOCUMENT — FOR CAPSTONE DEMO ONLY.\n\n"
        "# Texas — Total Loss\n\n## TOTAL LOSS THRESHOLD\n\n"
        "In Texas, a vehicle is a total loss when repairs plus salvage reach 100% of ACV.\n\n"
        "## VALUATION METHOD\n\nACV from comparable vehicles of like kind and quality in the local market.")
    assert len(chunks) == 2
    assert all(len(c) > 40 for c in chunks)
    assert not any("SYNTHETIC DOCUMENT" in c for c in chunks)


# -- Retrieval A/B demo (checkpoint 3.1): baseline vs grounded ----------------------
def test_retrieval_demo_contrasts_baseline_and_grounded(monkeypatch):
    from settleready.eval.retrieval_demo import compare

    fake = FakeCollection()
    monkeypatch.setattr(ragmod, "_collection", lambda persist_dir, provider=None: fake)

    class DemoProvider:
        name = "fake"

        def embed(self, text):
            return [0.1] * 8

        def complete(self, *, system, user, max_tokens=1024, temperature=0.2,
                     json_mode=False):
            if "[TX.md]" in user:  # grounded path: retrieved chunks in the prompt
                return json.dumps({"answer": "100% of ACV [TX.md]",
                                   "cited_sources": ["TX.md"]})
            # parametric baseline: plausible-sounding, uncited
            return json.dumps({"answer": "Usually around 75-80% of the value.",
                               "cited_sources": []})

    out = compare("What is the total loss threshold?", "TX",
                  data_dir="/tmp/x", provider=DemoProvider())
    assert out["baseline"]["citations"] == []
    assert "75-80%" in out["baseline"]["answer"]
    assert "TX.md" in out["grounded"]["citations"]
    assert "100% of ACV" in out["grounded"]["answer"]
    assert len(out["grounded"]["chunks"]) == 2
