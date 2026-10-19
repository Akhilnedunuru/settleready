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


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    """The sandbox can't install chromadb; stub retrieval (as in test_module4)."""
    import settleready.agents.researcher as R
    monkeypatch.setattr(
        R, "retrieve",
        lambda state, question, persist_dir="", k=3, provider=None:
        [{"text": "synthetic statute", "citation": f"{state}.md",
          "state": state, "score": 0.1}])


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


def test_gen_doc_builds_missing_title(tmp_path):
    from settleready.data import seed as seed_mod
    seed_main(seed=42, out=str(tmp_path), num_cases=15)
    import os
    pdf_dir = os.path.join(str(tmp_path), "pdfs", "TL-2026-1048")
    assert not os.path.exists(os.path.join(pdf_dir, "title.pdf"))
    path = seed_mod.gen_doc(str(tmp_path), "TL-2026-1048", "title")
    assert os.path.exists(path)
    assert os.path.exists(os.path.join(pdf_dir, "title.pdf"))


def _spec_vals(data_dir, case_id):
    import json as _json, os as _os
    cases = _json.load(open(_os.path.join(data_dir, "cases.json")))
    c = next(x for x in cases if x["case_id"] == case_id)
    return {
        "vin": c["vehicle"]["vin"], "mileage": c["vehicle"]["mileage"],
        "title_state": c["title_state"], "policy_number": c["policy"]["number"],
        "holder_name": c["policy"]["holder"], "loss_type": c["loss_type"],
        "loss_date": c["loss"]["date"], "lien_declared": c["declared_lien"]}


def test_reply_with_attachment_resolves_missing_doc(data_dir):
    """Customer attaches the missing title -> reply re-triggers -> clean."""
    import os, base64
    from settleready.agents import intake as I
    from settleready.pipeline import run_case
    from settleready.memory.blackboard import CaseBlackboard

    seed_main(seed=42, out=data_dir, num_cases=15)
    vals = _spec_vals(data_dir, "TL-2026-1048")

    class RichProvider(FakeProvider):
        def complete(self, system, user, json_mode=False, max_tokens=0, temperature=0.0):
            return json.dumps({"fields":
                               {f: {"value": v, "confidence": 0.95, "span": "x"}
                                for f, v in vals.items()}})

    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1048", "claimant_email", "customer@example.com")
    bb.close()
    out = run_case("TL-2026-1048", data_dir=data_dir, provider=RichProvider())
    assert out["next"] == "needs_info"  # title missing

    # customer replies with the title PDF attached (generated on demand)
    from settleready.data import seed as seed_mod
    title_path = seed_mod.gen_doc(data_dir, "TL-2026-1048", "title")
    with open(title_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    from settleready.tools.email_client import EmailRecord
    reply = EmailRecord(id="r1", to="claims@demo.local", from_="customer@example.com",
                        subject="Re: Claim filed via mobile app — TL-2026-1048",
                        body="Here is the title scan.", ts="t", direction="inbound",
                        attachments=[{"filename": "title.pdf", "content_b64": content_b64}])
    w = I.IntakeWatcher(data_dir=data_dir,
                         run_fn=lambda cid: run_case(cid, data_dir=data_dir,
                                                    provider=RichProvider()))
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        res = w._handle_reply(reply, bb)
    finally:
        bb.close()
    assert res["action"] == "reply_retriggered"
    assert res["attachments_saved"] == ["title.pdf"]
    assert res["routed"] == "ready_for_review"


def test_reply_text_confirmation_resolves_lien_conflict(data_dir):
    """Customer confirms the lien in words -> statement feeds extraction."""
    import os
    from settleready.agents import intake as I
    from settleready.pipeline import run_case
    from settleready.memory.blackboard import CaseBlackboard

    seed_main(seed=42, out=data_dir, num_cases=15)
    vals = _spec_vals(data_dir, "TL-2026-1047")

    class StatementAwareProvider(FakeProvider):
        def complete(self, system, user, json_mode=False, max_tokens=0, temperature=0.0):
            fields = {f: {"value": v, "confidence": 0.95, "span": "x"}
                      for f, v in vals.items()}
            if "customer_reply" in user:  # customer confirmed the lien in words
                fields["lien_declared"] = {"value": True, "confidence": 0.9,
                                           "span": "customer reply"}
            return json.dumps({"fields": fields})

    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.write("TL-2026-1047", "claimant_email", "customer@example.com")
    bb.close()
    # first run: seeded spec has undeclared lien -> conflict -> needs_info + email
    out = run_case("TL-2026-1047", data_dir=data_dir, provider=StatementAwareProvider())
    assert out["next"] == "needs_info"

    from settleready.tools.email_client import EmailRecord
    reply = EmailRecord(id="r2", to="claims@demo.local", from_="customer@example.com",
                        subject="Re: Claim filed via mobile app — TL-2026-1047",
                        body="Yes, there is a lien with Lone Star Credit Union, payoff $7,417.",
                        ts="t", direction="inbound", attachments=[])
    w = I.IntakeWatcher(data_dir=data_dir,
                         run_fn=lambda cid: run_case(cid, data_dir=data_dir,
                                                    provider=StatementAwareProvider()))
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    try:
        res = w._handle_reply(reply, bb)
    finally:
        bb.close()
    assert res["action"] == "reply_retriggered"
    assert res["routed"] == "ready_for_review"


def test_followup_count_and_escalation_at_three(data_dir):
    from settleready.agents.outreach import maybe_send_info_request
    from settleready.memory.blackboard import CaseBlackboard
    import os
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    bb.create_case("TL-2026-1043")
    bb.set_stage("TL-2026-1043", "needs_info")
    bb.write("TL-2026-1043", "claimant_email", "customer@example.com")
    bb.close()

    from settleready.tools.email_client import MockEmailClient
    client = MockEmailClient(data_dir=data_dir)
    r1 = maybe_send_info_request(data_dir, "TL-2026-1043", ["need the title"],
                                 holder_name="X", client=client)
    r2 = maybe_send_info_request(data_dir, "TL-2026-1043", ["need the police report"],
                                 holder_name="X", client=client)
    r3 = maybe_send_info_request(data_dir, "TL-2026-1043", ["need the repair estimate"],
                                 holder_name="X", client=client)
    assert (r1["followup_count"], r2["followup_count"], r3["followup_count"]) == (1, 2, 3)
    assert r3["escalated"] is True
    bb = CaseBlackboard(os.path.join(data_dir, "blackboard.db"))
    assert bb.get_case("TL-2026-1043")["stage"] == "escalated"
    bb.close()
