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
