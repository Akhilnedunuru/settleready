"""Classify a customer reply: does it provide info, ask a question, or other?

Hybrid approach:
- Deterministic fast paths (no LLM): attachments, confirmation patterns,
  question patterns.
- LLM fallback for ambiguous cases (temperature 0, JSON output).
- If the LLM is unavailable or fails, defaults to "provides_info" (the
  current behaviour — re-run the pipeline). Safe degradation.

A question-type reply must NOT trigger another document-request email;
it routes to human review instead.
"""
from __future__ import annotations

import json
import re

# Question starters (case-insensitive, stripped leading whitespace).
_QUESTION_STARTERS = (
    "when", "how", "what", "why", "where", "who", "which",
    "can you", "could you", "would you", "will you", "do you",
    "is there", "are there", "is it", "can i", "could i",
    "please explain", "please tell", "please clarify",
)

# Confirmation patterns: the customer answering an outstanding gap.
# These indicate the reply provides info, not a question.
_CONFIRM_PATTERNS = (
    r"yes[,.]?\s+(the\s+)?lienholder",
    r"payoff (amount\s+)?is\s+\$?",
    r"attached",
    r"here is",
    r"here'?s the",
    r"confirming",
    r"the (lienholder|payoff|title) is",
)

_CLASSIFIER_SYSTEM = """You classify a customer reply to an insurance claim follow-up email.

Reply types:
- "provides_info": the customer gives information, confirms a fact, or sends a document.
- "asks_question": the customer asks a question, requests a status update, or needs help understanding something.
- "other": anything else (greetings, thanks, complaints without a question, etc.).

Return ONLY JSON: {"type": "provides_info" | "asks_question" | "other", "reason": "<one sentence>"}.
"""


def classify_reply(text: str, has_attachments: bool = False,
                   provider=None) -> dict:
    """Classify a customer reply.

    Returns {"type", "reason", "method"} where method is "heuristic" or "llm".
    """
    cleaned = (text or "").strip()
    lower = cleaned.lower()

    # -- Fast path 1: attachments mean the customer sent a document. ------
    if has_attachments:
        return {"type": "provides_info",
                "reason": "reply includes attachment(s)",
                "method": "heuristic"}

    # -- Fast path 2: confirmation patterns. ------------------------------
    for pat in _CONFIRM_PATTERNS:
        if re.search(pat, lower):
            return {"type": "provides_info",
                    "reason": f"matches confirmation pattern: {pat[:40]}",
                    "method": "heuristic"}

    # -- Fast path 3: question patterns. -----------------------------------
    if cleaned.endswith("?"):
        return {"type": "asks_question",
                "reason": "reply ends with a question mark",
                "method": "heuristic"}
    for starter in _QUESTION_STARTERS:
        if lower.startswith(starter):
            return {"type": "asks_question",
                    "reason": f"starts with question word: {starter}",
                    "method": "heuristic"}

    # -- Fallback: LLM classifier (only for genuinely ambiguous replies). --
    if provider is not None:
        try:
            out = json.loads(provider.complete(
                system=_CLASSIFIER_SYSTEM,
                user=f"Customer reply:\n{cleaned[:1000]}",
                temperature=0, json_mode=True))
            rtype = out.get("type", "other")
            if rtype not in ("provides_info", "asks_question", "other"):
                rtype = "other"
            return {"type": rtype,
                    "reason": out.get("reason", "LLM classification"),
                    "method": "llm"}
        except Exception:  # noqa: BLE001 — fall through to safe default
            pass

    # -- Safe default: treat as info (current behaviour). ------------------
    return {"type": "provides_info",
            "reason": "ambiguous; defaulting to provides_info",
            "method": "heuristic"}
