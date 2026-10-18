"""Pluggable LLM provider interface.

All agent code talks to an ``LLMProvider`` — never to a vendor SDK directly.
Swap backends with ``SETTLEREADY_PROVIDER=openai|ollama``; no pipeline changes needed.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal surface the agents need."""

    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> str:
        """Single-turn completion."""
        ...

    def embed(self, text: str) -> list[float]:
        """Embedding vector for retrieval."""
        ...


class OpenAIProvider:
    """Live provider (default). Needs OPENAI_API_KEY."""

    name = "openai"

    def __init__(self) -> None:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key or api_key.startswith("sk-paste"):
            raise RuntimeError("OPENAI_API_KEY is not set — copy .env.example to .env first.")
        self._client = OpenAI(api_key=api_key)
        self.chat_model = os.environ.get("SETTLEREADY_CHAT_MODEL", "gpt-4o-mini")
        self.embed_model = os.environ.get("SETTLEREADY_EMBED_MODEL", "text-embedding-3-small")

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.2, json_mode: bool = False) -> str:
        kwargs: dict = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self._client.chat.completions.create(**kwargs).choices[0].message.content or ""

    def embed(self, text: str) -> list[float]:
        return self._client.embeddings.create(model=self.embed_model, input=text).data[0].embedding


class OllamaProvider:
    """Local-model provider — stub (stretch goal).

    To implement: HTTP against OLLAMA_BASE_URL (default http://localhost:11434),
    chat via /api/chat and embeddings via /api/embeddings using OLLAMA_CHAT_MODEL.
    """

    name = "ollama"

    def __init__(self) -> None:
        raise NotImplementedError(
            "OllamaProvider is not implemented yet — keep SETTLEREADY_PROVIDER=openai, "
            "or implement complete()/embed() against your local Ollama server."
        )


def get_provider() -> LLMProvider:
    which = os.environ.get("SETTLEREADY_PROVIDER", "openai").lower()
    if which == "openai":
        return OpenAIProvider()
    if which == "ollama":
        return OllamaProvider()
    raise ValueError(f"Unknown SETTLEREADY_PROVIDER={which!r} (expected 'openai' or 'ollama')")
