"""Citation verification (Checkpoint 3.1 feedback): every citation grounded.

The synthesis prompt *requires* citations; verify_citations *checks* them:
each cited file must be among the retrieved chunks, and each stated figure
must appear in a cited chunk's text. Failures go down the decline path.
"""
import json

from settleready.agents import researcher
from settleready.agents.researcher import verify_citations, verify_statute_provenance

_PROVENANCE = {"jurisdiction": "GA", "statute_version": "2026.1",
               "statute_status": "current", "effective_from": "2025-01-01",
               "effective_to": "", "reviewed_at": "2026-11-01"}
GA_CHUNKS = [
    {"citation": "GA.md", "state": "GA", "score": 0.9, **_PROVENANCE,
     "text": "TOTAL LOSS THRESHOLD: In Georgia, a vehicle is a total loss "
             "when repair costs exceed 75% of the fair retail value."},
    {"citation": "GA.md", "state": "GA", "score": 1.1, **_PROVENANCE,
     "text": "VALUATION METHOD: Fair retail value from at least 5 comparable "
             "listings, adjusted for mileage over 50,000 at $150 per 10,000 miles."},
]


def test_clean_answer_passes():
    ok, reason = verify_citations(
        "Georgia: repair costs exceeding 75% of fair retail value [GA.md].",
        ["GA.md"], GA_CHUNKS)
    assert (ok, reason) == (True, "")


def test_unknown_citation_fails():
    ok, reason = verify_citations("Some rule [TX.md].", ["TX.md"], GA_CHUNKS)
    assert not ok and "TX.md" in reason


def test_blended_figure_fails():
    ok, reason = verify_citations(
        "Georgia: repair costs exceeding 80% of fair retail value [GA.md].",
        ["GA.md"], GA_CHUNKS)
    assert not ok and "80" in reason


def test_figures_without_citations_fail():
    ok, reason = verify_citations("The threshold is 75%.", [], GA_CHUNKS)
    assert not ok and "no citations" in reason


def test_no_figures_passes_vacuously():
    ok, reason = verify_citations("The retrieved statutes do not cover this.",
                                  [], [])
    assert (ok, reason) == (True, "")


def test_figure_normalization():
    # "$150" == "150", "10,000" == "10000", "75 %" == "75%"
    ok, _ = verify_citations("Adjusted $150 per 10,000 miles [GA.md].",
                             ["GA.md"], GA_CHUNKS)
    assert ok
    # digit boundaries: "5" must not match "50,000"
    ok, _ = verify_citations("At least 5 comparable listings [GA.md].",
                             ["GA.md"], GA_CHUNKS)
    assert ok


class _Provider:
    def __init__(self, answer, cited):
        self._a, self._c = answer, cited

    def complete(self, *, system, user, **kw):
        return json.dumps({"answer": self._a, "cited_sources": self._c})


def _research(answer, cited, monkeypatch):
    monkeypatch.setattr(researcher, "retrieve", lambda *a, **k: GA_CHUNKS)
    return researcher.research("q?", "GA", data_dir="/tmp/x",
                               provider=_Provider(answer, cited))


def test_research_declines_on_failed_check(monkeypatch):
    out = _research("Georgia: threshold 80% of fair value [GA.md].", ["GA.md"],
                    monkeypatch)
    assert out["answer"] == "The retrieved statutes do not cover this."
    assert out["citations"] == []
    assert out["chunks"] == GA_CHUNKS  # kept for analyst transparency
    assert out["citation_check"]["passed"] is False


def test_research_keeps_verified_answer(monkeypatch):
    out = _research(
        "Georgia: repair costs exceeding 75% of fair retail value [GA.md].",
        ["GA.md"], monkeypatch)
    assert "75%" in out["answer"]
    assert out["citations"] == ["GA.md"]
    assert out["citation_check"]["passed"] is True
    assert out["provenance_check"]["passed"] is True


def test_statute_provenance_current_and_effective_passes():
    ok, reason, summaries = verify_statute_provenance(
        GA_CHUNKS, as_of_date="2026-09-20")
    assert ok is True and reason == ""
    assert summaries[0]["jurisdiction"] == "GA"
    assert summaries[0]["statute_version"] == "2026.1"


def test_statute_provenance_missing_metadata_fails_closed():
    chunks = [{k: v for k, v in GA_CHUNKS[0].items()
               if k not in _PROVENANCE}]
    ok, reason, _ = verify_statute_provenance(chunks, as_of_date="2026-09-20")
    assert ok is False
    assert "missing statute provenance" in reason


def test_statute_provenance_superseded_or_not_effective_fails():
    superseded = [{**GA_CHUNKS[0], "statute_status": "superseded"}]
    ok, reason, _ = verify_statute_provenance(
        superseded, as_of_date="2026-09-20")
    assert ok is False and "not 'current'" in reason

    not_yet = [{**GA_CHUNKS[0], "effective_from": "2027-01-01"}]
    ok, reason, _ = verify_statute_provenance(not_yet, as_of_date="2026-09-20")
    assert ok is False and "not effective" in reason

    expired = [{**GA_CHUNKS[0], "effective_to": "2025-12-31"}]
    ok, reason, _ = verify_statute_provenance(expired, as_of_date="2026-09-20")
    assert ok is False and "not effective" in reason


def test_research_declines_before_synthesis_on_provenance_failure(monkeypatch):
    chunks = [{**c, "statute_status": "superseded"} for c in GA_CHUNKS]
    monkeypatch.setattr(researcher, "retrieve", lambda *a, **k: chunks)
    out = researcher.research(
        "q?", "GA", data_dir="/tmp/x",
        provider=_Provider("Georgia: threshold 75% [GA.md].", ["GA.md"]),
        as_of_date="2026-09-20")
    assert out["answer"] == "The retrieved statutes do not cover this."
    assert out["citations"] == []
    assert out["provenance_check"]["passed"] is False
    assert "superseded" in out["provenance_check"]["reason"]
