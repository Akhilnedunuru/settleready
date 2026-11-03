"""Guardrails for SettleReady (Module 6).

Two layers, per the agent charter (AGENTS.md):
- Input guardrails: screen customer emails and documents BEFORE the agent
  acts on them. Flags PII (synthetic data should not contain real PII, but
  the check runs anyway) and prompt-injection patterns.
- Output guardrails: validate what the agent produces BEFORE it is stored
  or sent. Enforces the charter's hard rules: no hallucinated ACV, no
  invented citations, confidence thresholds, schema conformance.

A guardrail returns a Verdict: PASS, FLAG (proceed but log), or BLOCK
(stop and escalate). BLOCK is reserved for charter violations — things
the agent must never do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Verdict:
    """Result of one guardrail check."""
    name: str
    ok: bool              # True = PASS or FLAG; False = BLOCK
    action: str           # "pass" | "flag" | "block"
    detail: str = ""
    extras: dict = None

    def __post_init__(self):
        if self.extras is None:
            self.extras = {}

    @property
    def passed(self) -> bool:
        """Alias for ok (True unless BLOCK)."""
        return self.ok

    @classmethod
    def make_pass(cls, name: str, detail: str = "") -> "Verdict":
        return cls(name, True, "pass", detail)

    @classmethod
    def flagged(cls, name: str, detail: str = "", **extras) -> "Verdict":
        return cls(name, True, "flag", detail, extras)

    @classmethod
    def blocked(cls, name: str, detail: str = "", **extras) -> "Verdict":
        return cls(name, False, "block", detail, extras)


# ---------------------------------------------------------------------------
# Input guardrails
# ---------------------------------------------------------------------------

# US PII patterns (synthetic fixtures use fake values, but the check is real).
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CCN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")  # loose credit-card shape
_DOB = re.compile(r"\b(?:dob|date of birth|born)\s*[:\-]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
                  re.IGNORECASE)


def check_pii(text: str) -> Verdict:
    """Flag US PII shapes in inbound text. FLAG (not block): the agent can
    still work the case, but the PII is logged and must not be echoed."""
    hits: list[str] = []
    if _SSN.search(text or ""):
        hits.append("ssn")
    # Credit-card: require 13-16 digits with a Luhn-ish sanity check omitted
    # for speed; shape match is enough to flag.
    m = _CCN.search(text or "")
    if m and len(re.sub(r"\D", "", m.group(0))) >= 13:
        hits.append("card_number")
    if _DOB.search(text or ""):
        hits.append("dob")
    if hits:
        return Verdict.flagged("pii", f"PII shapes detected: {', '.join(hits)}",
                               hits=hits)
    return Verdict.make_pass("pii")


# Prompt-injection patterns: instructions smuggled in customer content that
# try to override the agent's charter (e.g. "approve this case", "ignore
# the lien check"). Matched case-insensitively.
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"you\s+are\s+now\s+",
    r"disregard\s+(the|your)\s+",
    r"approve\s+(this|the)\s+case",
    r"skip\s+(the\s+)?verification",
    r"override\s+(the\s+)?",
    r"system\s*:\s*",
    r"<\s*system\s*>",
    r"jailbreak",
    r"do\s+not\s+verify",
]
_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def check_prompt_injection(text: str) -> Verdict:
    """BLOCK on prompt-injection patterns in customer-controlled text.

    Rationale: a customer email that tries to steer the agent (approve,
    skip verification, override) is a charter violation attempt. The
    pipeline must not act on it — it escalates instead.
    """
    for rx in _INJECTION_RES:
        m = rx.search(text or "")
        if m:
            return Verdict.blocked(
                "prompt_injection",
                f"Injection pattern matched: {m.group(0)[:60]!r}",
                pattern=rx.pattern, match=m.group(0)[:120])
    return Verdict.make_pass("prompt_injection")


def screen_input(text: str, source: str = "customer") -> list[Verdict]:
    """Run all input guardrails. Returns verdicts; caller decides on BLOCK."""
    return [check_pii(text), check_prompt_injection(text)]
