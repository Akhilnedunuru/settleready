"""Researcher agent (module 4): answers legal/methodology questions with citations.

Flow: state-filtered retrieval (tools/rag.py) -> LLM synthesis constrained to
the retrieved chunks -> deterministic citation verification -> structured
answer with citations. The agent may NOT assert a threshold or rule without
citing a retrieved snippet — if retrieval comes back empty, it says so
instead of answering from parametric memory. The synthesis prompt *requires*
citations, and verify_citations then *checks* them: every cited file must be
among the retrieved chunks and every stated figure must appear in a cited
chunk's text, or the answer goes down the decline path.
"""
from __future__ import annotations

import json
import re
from datetime import date

from settleready.memory.events import emit as _emit_event
from settleready.tools.rag import retrieve

_DECLINE = "The retrieved statutes do not cover this."

# Citation verification (Checkpoint 3.1 feedback): a wrong number wearing a
# real citation is worse than no citation, because the analyst trusts it.
# This check is deterministic string matching — no LLM call — so it runs on
# every answer at ~zero marginal cost.
_FIGURE_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?\s*%"),        # 75%, 75 %
    re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?"),   # $150, $ 1,200
    re.compile(r"\d[\d,]*(?:\.\d+)?"),        # 10,000, 5, 30
)


def _normalize_figure(fig: str) -> str:
    return re.sub(r"[\s$,%]", "", fig)


def _figures(answer: str) -> list[str]:
    """Distinct normalized figures stated in the answer, in order."""
    seen, out = set(), []
    for pat in _FIGURE_PATTERNS:
        for m in pat.finditer(answer):
            norm = _normalize_figure(m.group(0))
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


_PROVENANCE_REQUIRED = ("jurisdiction", "statute_version", "statute_status",
                        "effective_from", "reviewed_at")
_PROVENANCE_KEYS = (*_PROVENANCE_REQUIRED, "effective_to")


def _parse_iso_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def verify_statute_provenance(chunks: list[dict],
                              as_of_date: str | None = None) -> tuple[bool, str, list[dict]]:
    """Verify jurisdiction and currency metadata for retrieved statutes.

    Jurisdiction was already enforced by the retriever's pre-ranking state
    filter; this deterministic gate checks that the retrieved chunks also
    carry provenance, identify the same jurisdiction, are marked current,
    and were legally effective on `as_of_date` (the loss date when the graph
    supplies one; otherwise today). Missing or stale provenance fails closed:
    an unprovenanced statute is not treated as current law.

    Returns (passed, reason, provenance_summaries).
    """
    if not chunks:
        return False, "no retrieved statute chunks", []
    as_of = _parse_iso_date(as_of_date) if as_of_date else date.today()
    if as_of is None:
        return False, f"invalid as-of date {as_of_date!r}", []

    summaries: dict[str, dict] = {}
    for chunk in chunks:
        citation = chunk.get("citation") or "<unknown>"
        missing = [key for key in _PROVENANCE_KEYS if key not in chunk]
        missing.extend(key for key in _PROVENANCE_REQUIRED
                       if key in chunk and not str(chunk.get(key)).strip())
        if missing:
            return False, (f"{citation} is missing statute provenance: "
                           f"{sorted(set(missing))}"), []
        jurisdiction = str(chunk.get("jurisdiction")).upper()
        state = str(chunk.get("state") or "").upper()
        if state and jurisdiction != state:
            return False, (f"{citation} jurisdiction {jurisdiction} does not "
                           f"match retrieved state {state}"), []
        if str(chunk.get("statute_status")).lower() != "current":
            return False, (f"{citation} statute status is "
                           f"{chunk.get('statute_status')!r}, not 'current'"), []
        effective_from = _parse_iso_date(chunk.get("effective_from"))
        effective_to_raw = str(chunk.get("effective_to") or "").strip()
        effective_to = (_parse_iso_date(effective_to_raw)
                        if effective_to_raw else None)
        reviewed_at = _parse_iso_date(chunk.get("reviewed_at"))
        if effective_from is None or reviewed_at is None or (
                effective_to_raw and effective_to is None):
            return False, f"{citation} has invalid statute provenance dates", []
        if as_of < effective_from or (effective_to is not None and as_of > effective_to):
            return False, (f"{citation} was not effective on {as_of.isoformat()} "
                           f"(effective {effective_from.isoformat()} to "
                           f"{effective_to.isoformat() if effective_to else 'present'})"), []
        summaries[citation] = {
            "citation": citation,
            "jurisdiction": jurisdiction,
            "statute_version": chunk.get("statute_version"),
            "statute_status": chunk.get("statute_status"),
            "effective_from": chunk.get("effective_from"),
            "effective_to": chunk.get("effective_to"),
            "reviewed_at": chunk.get("reviewed_at"),
        }
    return True, "", list(summaries.values())


def verify_citations(answer: str, citations: list[str],
                     chunks: list[dict]) -> tuple[bool, str]:
    """Check that every citation is grounded in the retrieved chunks.

    Returns (True, "") when clean, else (False, reason). Two checks:
    (1) each cited filename appears in the returned chunk metadata;
    (2) each stated figure appears as a string in at least one cited chunk.
    An answer that states figures but cites nothing also fails — the prompt
    requires a citation for every factual claim. Digit boundaries keep "5"
    from matching "50,000".
    """
    by_citation: dict[str, list[dict]] = {}
    for c in chunks:
        by_citation.setdefault(c.get("citation"), []).append(c)
    for cited in citations or []:
        if cited not in by_citation:
            return False, f"citation {cited} not in retrieved chunks"
    figs = _figures(answer)
    if figs and not citations:
        return False, "answer states figures with no citations"
    cited_chunks = [ch for c in (citations or []) for ch in by_citation.get(c, [])]
    for fig in figs:
        pat = re.compile(r"(?<!\d)" + re.escape(fig) + r"(?!\d)")
        for chunk in cited_chunks:
            text = re.sub(r"[$,]", "", chunk.get("text", ""))
            if pat.search(text):
                break
        else:
            return False, f"figure {fig!r} not found in cited chunk text"
    return True, ""

_SYSTEM = """You are an insurance-law research assistant. Answer the question using
ONLY the retrieved statute excerpts below. Every factual claim in your answer
must cite its source as [source-filename]. If the excerpts do not contain the
answer, say "The retrieved statutes do not cover this." — never answer from
general knowledge.
Respond with ONLY valid JSON: {"answer": "...", "cited_sources": ["TX.md", ...]}"""


def research(question: str, title_state: str, data_dir: str = "data",
             provider=None, k: int = 3, case_id: str = "",
             persist_dir: str | None = None,
             as_of_date: str | None = None) -> dict:
    """Research a question against the title state's statutes.

    Returns {"question", "state", "answer", "citations": [...], "chunks": [...],
    "citation_check": {"passed": bool, "reason": str},
    "provenance_check": {"passed": bool, "reason": str},
    "statute_provenance": [...]}.
    Tool calls (vector retrieval, synthesis) are emitted to the event bus so
    the UI can show them live. persist_dir overrides the Chroma index path
    (default <data_dir>/chroma) — used by eval harnesses that run against an
    isolated world but the real index. as_of_date is the legal applicability
    date (normally the loss date); it defaults to today.
    """
    if provider is None:
        from settleready.llm.provider import get_provider
        provider = get_provider()

    chunks = retrieve(title_state, question,
                      persist_dir=persist_dir or f"{data_dir}/chroma",
                      k=k, provider=provider)
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "rag.retrieve",
                 "args": {"state": title_state.upper(), "k": k},
                 "result": f"{len(chunks)} chunk(s) retrieved"})
    if not chunks:
        return {"question": question, "state": title_state.upper(),
                "answer": _DECLINE, "citations": [], "chunks": [],
                "provenance_check": {"passed": False,
                                     "reason": "no retrieved statute chunks"},
                "statute_provenance": []}

    provenance_ok, provenance_reason, provenance = verify_statute_provenance(
        chunks, as_of_date=as_of_date)
    _emit_event(data_dir, case_id, "statute_provenance_check",
                {"passed": provenance_ok, "reason": provenance_reason,
                 "as_of_date": as_of_date,
                 "statutes": provenance})
    if not provenance_ok:
        # Currency/jurisdiction provenance is a hard gate, not a warning: an
        # unprovenanced or non-effective statute never reaches synthesis.
        return {"question": question, "state": title_state.upper(),
                "answer": _DECLINE, "citations": [], "chunks": chunks,
                "citation_check": {"passed": False,
                                   "reason": (f"statute provenance check failed: "
                                              f"{provenance_reason}")},
                "provenance_check": {"passed": False,
                                     "reason": provenance_reason},
                "statute_provenance": provenance}

    context = "\n\n".join(f"[{c['citation']}] {c['text']}" for c in chunks)
    user = f"Question: {question}\n\nRetrieved excerpts (state: {title_state.upper()}):\n{context}"
    raw = provider.complete(system=_SYSTEM, user=user,
                            json_mode=True, max_tokens=800, temperature=0.0)
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "llm.complete",
                 "args": {"purpose": "legal synthesis", "chunks": len(chunks)},
                 "result": "answer synthesized with citations"})
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"answer": raw, "cited_sources": []}

    citations = result.get("cited_sources") or [c["citation"] for c in chunks]
    answer = result.get("answer", "")
    ok, reason = verify_citations(answer, citations, chunks)
    _emit_event(data_dir, case_id, "citation_check",
                {"passed": ok, **({} if ok else {"reason": reason})})
    if not ok:
        # A blended or invented figure never reaches the analyst: the same
        # decline path as empty retrieval, with the retrieved chunks kept so
        # the analyst can see what the model saw.
        return {"question": question, "state": title_state.upper(),
                "answer": _DECLINE, "citations": [], "chunks": chunks,
                "citation_check": {"passed": False, "reason": reason},
                "provenance_check": {"passed": True, "reason": ""},
                "statute_provenance": provenance}
    return {"question": question, "state": title_state.upper(),
            "answer": answer, "citations": citations, "chunks": chunks,
            "citation_check": {"passed": True, "reason": ""},
            "provenance_check": {"passed": True, "reason": ""},
            "statute_provenance": provenance}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--question", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--data", default="data")
    a = p.parse_args()
    out = research(a.question, a.state, data_dir=a.data)
    print(f"Q: {out['question']}  (state: {out['state']})")
    print(f"A: {out['answer']}")
    print("citations:", out["citations"])
