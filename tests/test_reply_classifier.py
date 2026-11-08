"""Tests for the hybrid customer-reply classifier."""
from settleready.agents.reply_classifier import classify_reply


class FakeProvider:
    """Returns a fixed classification; records calls."""

    def __init__(self, reply_type="asks_question"):
        self.calls = 0
        self.reply_type = reply_type

    def complete(self, *, system, user, max_tokens=1024, temperature=0.0,
                 json_mode=False):
        self.calls += 1
        import json
        return json.dumps({"type": self.reply_type,
                           "reason": "LLM said so"})


def test_attachment_is_provides_info():
    out = classify_reply("See attached.", has_attachments=True)
    assert out["type"] == "provides_info"
    assert out["method"] == "heuristic"


def test_confirmation_pattern_is_provides_info():
    out = classify_reply(
        "Yes, the lienholder is Capital Auto Finance and the payoff is $4,200.")
    assert out["type"] == "provides_info"
    assert out["method"] == "heuristic"


def test_question_mark_is_asks_question():
    out = classify_reply("When will my claim be processed?")
    assert out["type"] == "asks_question"
    assert out["method"] == "heuristic"


def test_question_starter_is_asks_question():
    out = classify_reply("How long does this usually take")
    assert out["type"] == "asks_question"


def test_ambiguous_uses_llm():
    fake = FakeProvider("asks_question")
    out = classify_reply("I am writing about my claim.", provider=fake)
    assert out["type"] == "asks_question"
    assert out["method"] == "llm"
    assert fake.calls == 1


def test_ambiguous_without_provider_defaults_to_provides_info():
    out = classify_reply("I am writing about my claim.")
    assert out["type"] == "provides_info"
    assert out["method"] == "heuristic"


def test_llm_failure_defaults_to_provides_info():
    class Broken:
        def complete(self, **kwargs):
            raise RuntimeError("LLM down")
    out = classify_reply("I am writing about my claim.", provider=Broken())
    assert out["type"] == "provides_info"
