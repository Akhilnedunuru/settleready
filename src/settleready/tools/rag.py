"""RAG retriever over the state statute corpus (module 3).

Design (see docs/architecture.md):
- Section-aware chunking of state total-loss statutes + valuation methodologies.
- **Jurisdiction filter BEFORE vector search** — never retrieve the wrong state's law.
- Returns cited snippets; the agent may not assert a threshold without a citation.

CLI:  python -m settleready.tools.rag build-index --corpus data/statutes --persist data/chroma
      python -m settleready.tools.rag query --state TX --question "total loss threshold"
"""
from __future__ import annotations


def build_index(corpus_dir: str, persist_dir: str) -> None:
    """TODO(module 3): chunk -> embed (text-embedding-3-small) -> Chroma."""
    raise NotImplementedError


def retrieve(state: str, question: str, k: int = 5) -> list[dict]:
    """TODO(module 3): state-filtered vector search -> [{'text', 'citation', 'score'}]."""
    raise NotImplementedError


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-index")
    b.add_argument("--corpus", required=True)
    b.add_argument("--persist", required=True)
    q = sub.add_parser("query")
    q.add_argument("--state", required=True)
    q.add_argument("--question", required=True)
    a = p.parse_args()
    if a.cmd == "build-index":
        build_index(a.corpus, a.persist)
    else:
        print(retrieve(a.state, a.question))
