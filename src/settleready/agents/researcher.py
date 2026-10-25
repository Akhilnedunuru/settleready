"""Researcher agent (module 4): answers legal/methodology questions with citations.

Flow: state-filtered retrieval (tools/rag.py) -> LLM synthesis constrained to
the retrieved chunks -> structured answer with citations. The agent may NOT
assert a threshold or rule without citing a retrieved snippet — if retrieval
comes back empty, it says so instead of answering from parametric memory.
"""
from __future__ import annotations

import json

from settleready.memory.events import emit as _emit_event
from settleready.tools.rag import retrieve

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

    Returns {"question", "state", "answer", "citations": [...], "chunks": [...]}.
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
                "answer": "The retrieved statutes do not cover this.",
                "citations": [], "chunks": []}

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
    return {"question": question, "state": title_state.upper(),
            "answer": result.get("answer", ""), "citations": citations, "chunks": chunks}


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
