"""Adversarial eval: every hostile scenario must be handled safely."""
import json

from settleready.eval.adversarial import run_all
from settleready.tools import rag as ragmod


class StubCollection:
    """GA chunks for GA queries; nothing for any other state."""

    def query(self, query_embeddings, where, n_results):
        if where == {"state": "GA"}:
            docs = ["TOTAL LOSS THRESHOLD: In Georgia, a vehicle is a total loss "
                    "when repair costs exceed 75% of the fair retail value.",
                    "VALUATION METHOD: Fair retail value from at least 5 "
                    "comparable listings."]
            return {"documents": [docs[:n_results]],
                    "metadatas": [[{"state": "GA", "source": "GA.md"}] * len(docs[:n_results])],
                    "distances": [[0.1, 0.2][:n_results]]}
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


class FakeProvider:
    name = "fake"

    def embed(self, text):
        return [0.1] * 8

    def complete(self, *, system, user, max_tokens=1024, temperature=0.2,
                 json_mode=False):
        if "[GA.md]" in user:
            return json.dumps({"answer": "Georgia: repair costs exceeding 75% of "
                                         "fair retail value [GA.md].",
                               "cited_sources": ["GA.md"]})
        return json.dumps({"answer": "nope", "cited_sources": []})


def test_adversarial_eval_all_scenarios_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(ragmod, "_collection",
                        lambda persist_dir, provider=None: StubCollection())
    results = run_all(data_dir=str(tmp_path), provider=FakeProvider(),
                      persist_dir="/tmp/does-not-matter")
    assert len(results) == 8
    failed = [r for r in results if not r["ok"]]
    assert failed == [], [(r["id"], r["detail"]) for r in failed]


def test_adversarial_eval_covers_all_categories(monkeypatch, tmp_path):
    monkeypatch.setattr(ragmod, "_collection",
                        lambda persist_dir, provider=None: StubCollection())
    results = run_all(data_dir=str(tmp_path), provider=FakeProvider(),
                      persist_dir="/tmp/does-not-matter")
    cats = {r["category"] for r in results}
    assert cats == {"conflicting-reports", "missing-fields", "near-threshold",
                    "retrieval-bait", "retrieval-abstain"}
