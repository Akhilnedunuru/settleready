"""Intake watcher tests: routing, case-ID extraction, trigger + dedup, replies."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settleready.agents.intake import (
    IntakeWatcher,
    extract_case_id,
    is_reply,
    route_claim,
)
from settleready.memory.blackboard import CaseBlackboard
from settleready.tools.email_client import MockEmailClient


def _mail(tmp_path, **kw):
    m = MockEmailClient(data_dir=str(tmp_path))
    rec = m.inject_inbound(to="claims@demo.local", from_="c@example.com",
                           subject=kw.get("subject", "Claim filed via mobile app — TL-2026-1045"),
                           body=kw.get("body", "docs attached"))
    return m, rec


def test_extract_case_id():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _, rec = _mail(d)
        assert extract_case_id(rec) == "TL-2026-1045"
        assert is_reply(rec) is False


def test_route_claim():
    import tempfile
    from settleready.tools.email_client import EmailRecord
    with tempfile.TemporaryDirectory() as d:
        mail, rec = _mail(d, subject="Claim filed via mobile app — TL-2026-1045")
        assert route_claim(rec) == "total_loss"
        mail2, rec2 = _mail(d, subject="Password reset request")
        assert route_claim(rec2) == "other"
        assert is_reply(rec2) is False


def test_is_reply():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _, rec = _mail(d, subject="Re: claim documents — TL-2026-1045")
        assert is_reply(rec) is True


def _watcher(tmp_path, calls):
    os.makedirs(tmp_path / "pdfs" / "TL-2026-1045", exist_ok=True)
    return IntakeWatcher(data_dir=str(tmp_path),
                         run_fn=lambda cid: calls.append(cid) or {"next": "needs_info"})


def test_new_claim_triggers_pipeline_once(tmp_path):
    calls = []
    _mail(tmp_path)  # claim email for TL-2026-1045
    w = _watcher(tmp_path, calls)
    actions = w.poll_once()
    assert calls == ["TL-2026-1045"]
    assert actions[0]["action"] == "pipeline_triggered"
    # second poll: seen-tracking means no re-fire
    assert w.poll_once() == []
    assert calls == ["TL-2026-1045"]


def test_non_claim_email_routed_other(tmp_path):
    calls = []
    _mail(tmp_path, subject="Office lunch menu")
    w = _watcher(tmp_path, calls)
    actions = w.poll_once()
    assert actions[0]["action"] == "routed_other"
    assert calls == []


def test_customer_reply_retriggers_waiting_case(tmp_path):
    calls = []
    _mail(tmp_path, subject="Re: claim documents — TL-2026-1045",
          body="attaching the title scan")
    bb = CaseBlackboard(os.path.join(str(tmp_path), "blackboard.db"))
    bb.create_case("TL-2026-1045", loss_type="theft", title_state="TX")
    bb.set_stage("TL-2026-1045", "needs_info")
    bb.close()
    w = _watcher(tmp_path, calls)
    actions = w.poll_once()
    assert actions[0]["action"] == "reply_retriggered"
    assert calls == ["TL-2026-1045"]
    bb = CaseBlackboard(os.path.join(str(tmp_path), "blackboard.db"))
    events = [e["event"] for e in bb.audit_log("TL-2026-1045")]
    assert "customer_reply" in events
    bb.close()


def test_reply_to_unknown_case_logged_not_crashed(tmp_path):
    calls = []
    _mail(tmp_path, subject="Re: hello — no case here")
    w = _watcher(tmp_path, calls)
    actions = w.poll_once()
    assert actions[0]["action"] == "reply_uncorrelated"
    assert calls == []


# -- real-email pieces ------------------------------------------------------------
def _mime_bytes():
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "customer@example.com"
    m["To"] = "claims@demo.local"
    m["Subject"] = "Claim filed via mobile app — TL-2026-1045"
    m["Message-ID"] = "<abc123@mail.example.com>"
    m.set_content("Please find my claim documents attached.")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf",
                     filename="claim_form.pdf")
    return m.as_bytes()


def test_imap_mime_parsing():
    import email as _email
    from settleready.tools.imap_client import ImapEmailClient
    client = ImapEmailClient.__new__(ImapEmailClient)  # no network, no creds needed
    rec = client._to_record(_email.message_from_bytes(_mime_bytes()))
    assert rec.id == "abc123@mail.example.com"
    assert rec.subject == "Claim filed via mobile app — TL-2026-1045"
    assert "claim documents" in rec.body
    assert len(rec.attachments) == 1
    assert rec.attachments[0]["filename"] == "claim_form.pdf"
    import base64
    assert base64.b64decode(rec.attachments[0]["content_b64"]) == b"%PDF-1.4 fake"


def test_get_email_client_factory(monkeypatch):
    import os
    from settleready.tools.email_client import MockEmailClient, get_email_client
    from settleready.tools.imap_client import ImapEmailClient
    monkeypatch.delenv("SETTLEREADY_EMAIL_SOURCE", raising=False)
    assert isinstance(get_email_client(data_dir="/tmp"), MockEmailClient)
    monkeypatch.setenv("SETTLEREADY_EMAIL_SOURCE", "gmail")
    monkeypatch.setenv("GMAIL_ADDRESS", "demo@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh")
    c = get_email_client(data_dir="/tmp")
    assert isinstance(c, ImapEmailClient)
    assert c._password == "abcdefgh"  # spaces stripped


def test_watcher_saves_pdf_attachments(tmp_path):
    import base64
    calls = []
    mail = MockEmailClient(data_dir=str(tmp_path))
    rec = mail.inject_inbound(to="claims@demo.local", from_="c@example.com",
                              subject="Claim filed via mobile app — TL-2026-1045",
                              body="docs attached")
    # attach a PDF to the stored record (demo transport carries none)
    import json
    from dataclasses import asdict
    recs = mail.poll_inbox()
    recs[0].attachments = [{"filename": "claim_form.pdf",
                             "content_b64": base64.b64encode(b"%PDF-1.4 fake").decode()}]
    with open(tmp_path / "inbox.json", "w") as f:
        json.dump([asdict(r) for r in recs], f)
    w = IntakeWatcher(data_dir=str(tmp_path),
                      run_fn=lambda cid: calls.append(cid) or {"next": "ready_for_review"})
    actions = w.poll_once()
    assert actions[0]["action"] == "pipeline_triggered"
    assert actions[0]["attachments_saved"] == ["claim_form.pdf"]
    assert (tmp_path / "pdfs" / "TL-2026-1045" / "claim_form.pdf").exists()
    assert calls == ["TL-2026-1045"]
