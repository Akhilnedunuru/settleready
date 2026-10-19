"""Outreach tests: the analyst agent's info-request emails to the customer."""
import json
import os

import pytest

from settleready.agents.outreach import (
    compose_info_request,
    needs_from_report,
    needs_from_return,
)
from settleready.data.seed import main as seed_main
from settleready.memory import events
from settleready.memory.blackboard import CaseBlackboard


@pytest.fixture
def data_dir(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=1)
    return str(tmp_path)


class FakeProvider:
    def complete(self, system, user, json_mode=False, max_tokens=0, temperature=0.0):
        return json.dumps({"fields": {
            f: {"value": "X", "confidence": 0.95, "span": "x"}
            for f in ["vin", "mileage", "title_state", "policy_number",
                      "holder_name", "loss_type", "loss_date", "lien_declared"]}})


def _outbox(data_dir):
    from settleready.tools.email_client import MockEmailClient
    return MockEmailClient(data_dir=data_dir).outbox()


def test_needs_from_report_names_docs_and_conflicts():
    report = {
        "conflicts": [{"reason_code": "UNDECLARED_LIEN",
                       "detail": "registry shows Lone Star $7,417"}],
        "checks": [{"check": "completeness", "status": "fail",
                    "detail": "Missing required documents: ['title']"}],
    }
    needs = needs_from_report(report)
    assert any("lien" in n for n in needs)
    assert any("title" in n for n in needs)


def test_needs_from_return_internal_reason_sends_nothing():
    assert needs_from_return("WEAK_COMPS") == []
    assert needs_from_return("UNDECLARED_LIEN", "registry shows X") != []


def test_compose_keeps_case_id_in_subject():
    subject, body = compose_info_request("TL-2026-1047", "Priya", ["need a title"])
    assert "TL-2026-1047" in subject
    assert subject.startswith("Re:")
    assert "TL-2026-1047" in body


def test_needs_info_sends_one_customer_email(data_dir):
    from settleready.pipeline import run_case
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1043", "claimant_email", "customer@example.com")
    bb.close()

    out = run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    assert out["next"] == "needs_info"
    assert out["outreach"]["sent"] is True

    box = _outbox(data_dir)
    assert len(box) == 1
    assert box[0].to == "customer@example.com"
    assert "TL-2026-1043" in box[0].subject
    assert "title" in box[0].body.lower()

    evs = [e for e in events.tail(data_dir, case_id="TL-2026-1043", limit=50)
           if e["event"] == "email_sent"]
    assert len(evs) == 1


def test_no_duplicate_email_on_rerun(data_dir):
    from settleready.pipeline import run_case
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1043", "claimant_email", "customer@example.com")
    bb.close()

    run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    assert len(_outbox(data_dir)) == 1


def test_missing_claimant_email_skips_gracefully(data_dir):
    from settleready.pipeline import run_case
    out = run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    assert out["next"] == "needs_info"
    assert out["outreach"]["sent"] is False
    assert len(_outbox(data_dir)) == 0


def test_reviewer_return_sends_customer_email(data_dir):
    from settleready.agents.reviewer import submit_review
    from settleready.pipeline import run_case
    # pipeline first WITHOUT claimant email (nothing sent yet)
    out = run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    assert out["outreach"]["sent"] is False

    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1043", "claimant_email", "customer@example.com")
    bb.close()

    res = submit_review("TL-2026-1043", "return",
                        reason_code="UNDECLARED_LIEN", data_dir=data_dir)
    assert res["outreach"]["sent"] is True
    box = _outbox(data_dir)
    assert len(box) == 1
    assert "lien" in box[0].body.lower()


def test_reviewer_return_internal_reason_sends_nothing(data_dir):
    from settleready.agents.reviewer import submit_review
    from settleready.pipeline import run_case
    run_case("TL-2026-1043", data_dir=data_dir, provider=FakeProvider())
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1043", "claimant_email", "customer@example.com")
    bb.close()

    res = submit_review("TL-2026-1043", "return",
                        reason_code="WEAK_COMPS", data_dir=data_dir)
    assert res["outreach"]["sent"] is False
    assert len(_outbox(data_dir)) == 0
