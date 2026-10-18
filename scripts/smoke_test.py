#!/usr/bin/env python3
"""Smoke test: verifies the LLM provider (chat + embeddings) works.

Usage: .venv/bin/python scripts/smoke_test.py
Requires: .env with OPENAI_API_KEY (or SETTLEREADY_PROVIDER=ollama + local ollama).
Costs ~$0.01 on OpenAI.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from settleready.llm.provider import get_provider


def main() -> int:
    provider = get_provider()
    print(f"Provider: {provider.name}")

    print("Chat...", end=" ", flush=True)
    reply = provider.complete(
        system="You are a terse assistant.",
        user="Reply with exactly: smoke test ok",
        max_tokens=20,
    )
    print(f"-> {reply.strip()!r}")
    assert "smoke test ok" in reply.lower(), "unexpected chat reply"

    print("Embeddings...", end=" ", flush=True)
    vec = provider.embed("2019 Honda Accord, total loss, Texas")
    print(f"-> dim={len(vec)}")
    assert len(vec) > 0, "empty embedding"

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
