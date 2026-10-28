"""Extraction-agent tests with a FakeProvider — no API key, no network.

Covers: the ReAct loop terminates, high-confidence fields stick, low-confidence
fields become flags (never silent guesses), and trace entries are well-formed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settleready.agents.extraction import (
    CONFIDENCE_THRESHOLD,
    REQUIRED_FIELDS,
    extract_fields,
)


class FakeProvider:
    """Canned ReAct responses: doc1 yields vin+policy_number at high confidence,
    doc2 yields mileage at LOW confidence (0.5) to exercise the flag path."""

    name = "fake"

    def complete(self, *, system, user, max_tokens=1024, temperature=0.2, json_mode=False):
        if "claim_form.pdf" in user:
            fields = {
                "vin": {"value": "1HGCV1F34KA000001", "confidence": 0.97,
                        "span": "VIN 1HGCV1F34KA000001"},
                "policy_number": {"value": "TX-A-4829137", "confidence": 0.95,
                                  "span": "Policy number TX-A-4829137"},
            }
        else:
            fields = {
                "mileage": {"value": 92000, "confidence": 0.5,
                            "span": "Mileage 92,000 (faded)"},
            }
        return json.dumps({"fields": fields})

    def embed(self, text):
        return [0.0] * 8


def _docs():
    return [
        {"source": "claim_form.pdf",
         "pages": [{"page": 1, "text": "VIN 1HGCV1F34KA000001\nPolicy number TX-A-4829137"}]},
        {"source": "police_report.pdf",
         "pages": [{"page": 1, "text": "Mileage 92,000 (faded)"}]},
    ]


def test_loop_terminates_with_trace():
    out = extract_fields(_docs(), provider=FakeProvider(), case_id="TL-TEST")
    assert out["trace"], "expected a ReAct trace"
    for step in out["trace"]:
        assert set(step) == {"thought", "action", "observation"}
        assert step["thought"].startswith("Iteration")
    assert len(out["trace"]) <= 3 + 1  # MAX_ITERATIONS + possible completion step


def test_high_confidence_fields_stick():
    out = extract_fields(_docs(), provider=FakeProvider())
    assert out["fields"]["vin"]["value"] == "1HGCV1F34KA000001"
    assert out["fields"]["vin"]["confidence"] >= CONFIDENCE_THRESHOLD
    assert "claim_form.pdf" in out["fields"]["vin"]["source"]


def test_low_confidence_becomes_flag_not_guess():
    out = extract_fields(_docs(), provider=FakeProvider())
    assert "mileage" in out["flags"], "0.5-confidence mileage must be flagged for humans"
    # ...but the value is still recorded with its low confidence for the reviewer
    assert out["fields"]["mileage"]["confidence"] == 0.5


def test_missing_fields_all_flagged():
    out = extract_fields(_docs(), provider=FakeProvider())
    for f in ("title_state", "holder_name", "loss_type", "loss_date", "lien_declared"):
        assert f in out["flags"]
    assert len(out["flags"]) + sum(
        1 for f in REQUIRED_FIELDS
        if f in out["fields"] and out["fields"][f]["confidence"] >= CONFIDENCE_THRESHOLD
    ) == len(REQUIRED_FIELDS)


class OverrideRoutingProvider:
    """Reply first: a hedged (0.7) lien confirmation, then the form's crisp
    'no lien' at 1.0. Before the sticky-override fix, the form overwrote the
    reply and the UNDECLARED_LIEN conflict survived the customer's answer."""

    def complete(self, *, system, user, max_tokens=1024, temperature=0.2, json_mode=False):
        if "customer_reply" in user:
            fields = {
                "lien_declared": {"value": True, "confidence": 0.7,
                                  "span": "there is a lien"},
                "lien_holder": {"value": "Lone Star Credit Union", "confidence": 0.7,
                                "span": "Lone Star"},
            }
        else:
            fields = {
                "vin": {"value": "V", "confidence": 1.0, "span": "V"},
                "mileage": {"value": 50000, "confidence": 1.0, "span": "50000"},
                "title_state": {"value": "GA", "confidence": 1.0, "span": "GA"},
                "policy_number": {"value": "GA-B-1", "confidence": 1.0, "span": "GA-B-1"},
                "holder_name": {"value": "X", "confidence": 1.0, "span": "X"},
                "loss_type": {"value": "FIRE", "confidence": 1.0, "span": "FIRE"},
                "loss_date": {"value": "2026-09-20", "confidence": 1.0, "span": "2026-09-20"},
                "lien_declared": {"value": False, "confidence": 1.0, "span": "YES NO X"},
            }
        return json.dumps({"fields": fields})


def test_override_value_sticks_despite_higher_confidence_form():
    docs = [
        {"source": "customer_reply_1.txt", "override": True,
         "pages": [{"page": 1, "text": "Yes, there is a lien with Lone Star."}]},
        {"source": "claim_form.pdf",
         "pages": [{"page": 1, "text": "form"}]},
    ]
    out = extract_fields(docs, provider=OverrideRoutingProvider())
    assert out["fields"]["lien_declared"]["value"] is True
    assert "customer_reply" in out["fields"]["lien_declared"]["source"]
    assert out["fields"]["lien_holder"]["value"] == "Lone Star Credit Union"
