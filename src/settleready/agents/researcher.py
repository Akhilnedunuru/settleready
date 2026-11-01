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
             persist_dir: str | None = None) -> dict:
    """Research a question against the title state's statutes.

    Returns {"question", "state", "answer", "citations": [...], "chunks": [...],
    "citation_check": {"passed": bool, "reason": str}}.
    Tool calls (vector retrieval, synthesis) are emitted to the event bus so
    the UI can show them live. persist_dir overrides the Chroma index path
    (default <data_dir>/chroma) — used by eval harnesses that run against an
    isolated world but the real index.
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
                "answer": _DECLINE, "citations": [], "chunks": []}

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
                "citation_check": {"passed": False, "reason": reason}}
    return {"question": question, "state": title_state.upper(),
            "answer": answer, "citations": citations, "chunks": chunks,
            "citation_check": {"passed": True, "reason": ""}}


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
