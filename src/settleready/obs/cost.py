"""Token counting and cost estimation (Module 6: observability).

Wraps any LLMProvider to count tokens and estimate cost per call.
Uses tiktoken when available; falls back to a chars/4 heuristic.

Per-call records go to the tracer span attrs; aggregate totals are
available via .totals(). Costs use the gpt-4o-mini price table by default;
override with SETTLEREADY_PRICE_IN / SETTLEREADY_PRICE_OUT (per 1M tokens).

Usage:
    from settleready.obs.cost import MeteredProvider
    provider = MeteredProvider(get_provider(), tracer=tracer)
    # provider.complete(...) works as normal, but counts tokens/cost
    provider.totals()  # {"prompt_tokens": ..., "completion_tokens": ...,
                       #  "total_tokens": ..., "est_cost_usd": ...}
"""
from __future__ import annotations

import os
from typing import Any

# gpt-4o-mini list prices (per 1M tokens) — update if the model changes.
_DEFAULT_PRICE_IN = 0.15
_DEFAULT_PRICE_OUT = 0.60


def _get_encoder(model: str = "gpt-4o-mini"):
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model)
    except Exception:
        return None


class MeteredProvider:
    """LLMProvider wrapper that meters tokens and cost."""

    def __init__(self, inner: Any, tracer=None, model: str = "gpt-4o-mini"):
        self._inner = inner
        self.tracer = tracer
        self.model = model
        self.name = getattr(inner, "name", "metered") + "+metered"
        self._enc = _get_encoder(model)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.price_in = float(os.environ.get("SETTLEREADY_PRICE_IN",
                                             _DEFAULT_PRICE_IN))
        self.price_out = float(os.environ.get("SETTLEREADY_PRICE_OUT",
                                              _DEFAULT_PRICE_OUT))

    def _count(self, text: str) -> int:
        if self._enc is not None:
            try:
                return len(self._enc.encode(text or ""))
            except Exception:
                pass
        return max(1, len(text or "") // 4)  # chars/4 heuristic

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.2, json_mode: bool = False) -> str:
        # Count prompt tokens before the call.
        prompt_text = (system or "") + "\n" + (user or "")
        pt = self._count(prompt_text)
        out = self._inner.complete(system=system, user=user,
                                   max_tokens=max_tokens,
                                   temperature=temperature, json_mode=json_mode)
        ct = self._count(out)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.calls += 1
        cost = pt / 1e6 * self.price_in + ct / 1e6 * self.price_out
        if self.tracer is not None:
            # Attach to the most recent open span, if any.
            for span in reversed(self.tracer.spans):
                if span.end_ts is None:
                    span.set(llm_calls=span.attrs.get("llm_calls", 0) + 1,
                             prompt_tokens=span.attrs.get("prompt_tokens", 0) + pt,
                             completion_tokens=span.attrs.get("completion_tokens", 0) + ct,
                             est_cost_usd=round(span.attrs.get("est_cost_usd", 0.0) + cost, 6))
                    break
        return out

    def embed(self, text: str) -> list[float]:
        return self._inner.embed(text)

    def totals(self) -> dict:
        total = self.prompt_tokens + self.completion_tokens
        cost = (self.prompt_tokens / 1e6 * self.price_in
                + self.completion_tokens / 1e6 * self.price_out)
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": total,
            "est_cost_usd": round(cost, 6),
            "model": self.model,
        }
