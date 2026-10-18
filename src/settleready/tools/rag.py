"""RAG retriever over the state statute corpus (module 4).

Design (see docs/architecture.md):
- Section-aware chunking of state total-loss statutes + valuation methodologies.
- **Jurisdiction filter BEFORE vector search** — the metadata `where` clause runs
  inside Chroma, so the wrong state's law can never outrank the right one.
- Returns cited snippets; the agent may not assert a threshold without a citation.

CLI:  PYTHONPATH=src python -m settleready.tools.rag build-index --corpus data/statutes --persist data/chroma
      PYTHONPATH=src python -m settleready.tools.rag query --state TX --question "total loss threshold" --persist data/chroma
"""
from __future__ import annotations

import glob
import os

COLLECTION = "statutes"


class _ProviderEmbeddings:
    """Chroma embedding-function adapter around our LLMProvider."""

    def __init__(self, provider) -> None:
        self._provider = provider

    def __call__(self, input):  # chroma expects __call__(self, input)
        return [self._provider.embed(t) for t in input]


def _chunk_markdown(text: str) -> list[str]:
    """Split on headings/blank lines; drop the synthetic banner line."""
    chunks, buf = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            if buf:
                chunks.append(" ".join(buf).strip())
                buf = []
            if s.startswith("##"):
                buf.append(s.lstrip("# ").strip())
            continue
        if "SYNTHETIC DOCUMENT" in s:
            continue
        buf.append(s)
    if buf:
        chunks.append(" ".join(buf).strip())
    return [c for c in chunks if len(c) > 40]


def _collection(persist_dir: str, provider=None):
    import chromadb
    if provider is None:
        from settleready.llm.provider import get_provider
        provider = get_provider()
    client = chromadb.PersistentClient(path=persist_dir)
    return client.get_or_create_collection(
        COLLECTION, embedding_function=_ProviderEmbeddings(provider))


def build_index(corpus_dir: str, persist_dir: str, provider=None) -> int:
    """Chunk -> embed (text-embedding-3-small) -> Chroma. Returns chunk count."""
    files = sorted(glob.glob(os.path.join(corpus_dir, "*.md")))
    if not files:
        raise FileNotFoundError(f"No statute files in {corpus_dir} — run scripts/seed.sh first.")
    collection = _collection(persist_dir, provider)
    # rebuild deterministically
    try:
        ids = collection.get()["ids"]
        if ids:
            collection.delete(ids=ids)
    except Exception:
        pass
    n = 0
    for path in files:
        state = os.path.splitext(os.path.basename(path))[0]  # TX.md -> TX
        with open(path) as f:
            chunks = _chunk_markdown(f.read())
        collection.add(
            ids=[f"{state}-{i}" for i in range(len(chunks))],
            documents=chunks,
            metadatas=[{"state": state, "source": os.path.basename(path)} for _ in chunks],
        )
        n += len(chunks)
    return n


def retrieve(state: str, question: str, persist_dir: str = "data/chroma",
             k: int = 3, provider=None) -> list[dict]:
    """State-filtered vector search -> [{'text', 'citation', 'state', 'score'}].

    The `where={"state": state}` clause is applied by Chroma BEFORE ranking,
    so retrieval can never return another state's law.
    """
    collection = _collection(persist_dir, provider)
    if provider is None:
        from settleready.llm.provider import get_provider
        provider = get_provider()
    res = collection.query(
        query_embeddings=[provider.embed(question)],
        where={"state": state.upper()},
        n_results=k,
    )
    out = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        out.append({"text": doc, "citation": meta["source"],
                    "state": meta["state"], "score": round(float(dist), 4)})
    return out


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
    q.add_argument("--persist", default="data/chroma")
    q.add_argument("--k", type=int, default=3)
    a = p.parse_args()
    if a.cmd == "build-index":
        print(f"indexed {build_index(a.corpus, a.persist)} chunks")
    else:
        for r in retrieve(a.state, a.question, persist_dir=a.persist, k=a.k):
            print(f"[{r['state']}/{r['citation']} score={r['score']}]\n{r['text']}\n")
