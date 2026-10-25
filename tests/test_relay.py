"""Relay transport: clones use the shared demo mailbox without ever
receiving the Gmail password. The server alone holds it; clones get a
demo token (kill-switch, not a secret) plus rate limits and a case-ID
rule on sends.
"""
import json
import threading

import pytest

from settleready.tools import relay_server as rs
from settleready.tools.email_client import EmailRecord, get_email_client
from settleready.tools.relay_client import RelayEmailClient


class StubBackend:
    def __init__(self):
        self.sent = []
        self.inbox = [EmailRecord(
            id="abc123", to="claims@demo.local", from_="customer@example.com",
            subject="Claim filed via mobile app — TL-2026-1045",
            body="New claim", ts="2026-10-25T00:00:00+00:00",
            direction="inbound",
            attachments=[{"filename": "claim_form.pdf", "content_b64": "aGVsbG8="}])]

    def poll_inbox(self):
        return list(self.inbox)

    def send(self, *, to, subject, body, from_=None):
        rec = EmailRecord(id="sent1", to=to, from_="demo@relay.local",
                          subject=subject, body=body,
                          ts="2026-10-25T00:00:01+00:00", direction="outbound")
        self.sent.append(rec)
        return rec


@pytest.fixture()
def server():
    backend = StubBackend()
    srv = rs.create_server("127.0.0.1", 0, "tok", backend,
                           inbox_limit=100, send_limit=2)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, backend
    srv.shutdown()
    srv.server_close()


def _url(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_relay_client_roundtrip(server):
    srv, backend = server
    client = RelayEmailClient(base_url=_url(srv), token="tok")
    recs = client.poll_inbox()
    assert len(recs) == 1
    assert recs[0].subject.endswith("TL-2026-1045")
    assert recs[0].attachments[0]["content_b64"] == "aGVsbG8="
    sent = client.send(to="customer@example.com",
                       subject="Re: Claim filed via mobile app — TL-2026-1045",
                       body="please confirm")
    assert sent.direction == "outbound"
    assert len(backend.sent) == 1


def test_relay_client_bad_token_rejected(server):
    srv, _ = server
    client = RelayEmailClient(base_url=_url(srv), token="wrong")
    with pytest.raises(RuntimeError, match="401"):
        client.poll_inbox()


def test_relay_server_send_requires_case_id(server):
    srv, backend = server
    client = RelayEmailClient(base_url=_url(srv), token="tok")
    with pytest.raises(RuntimeError, match="403"):
        client.send(to="x@y.z", subject="hello, no case here", body="spam?")
    assert backend.sent == []


def test_relay_server_send_rate_limited(server):
    srv, backend = server
    client = RelayEmailClient(base_url=_url(srv), token="tok")
    subj = "Re: Claim filed via mobile app — TL-2026-1045"
    client.send(to="a@b.c", subject=subj, body="one")
    client.send(to="a@b.c", subject=subj, body="two")
    with pytest.raises(RuntimeError, match="429"):
        client.send(to="a@b.c", subject=subj, body="three")
    assert len(backend.sent) == 2


def test_send_allowed_rule():
    assert rs.send_allowed("Re: Claim filed via mobile app — TL-2026-1045")
    assert not rs.send_allowed("hello there")
    assert not rs.send_allowed("")


def test_factory_relay_branch(monkeypatch):
    monkeypatch.setenv("SETTLEREADY_EMAIL_SOURCE", "relay")
    monkeypatch.setenv("RELAY_URL", "http://127.0.0.1:8899")
    monkeypatch.setenv("RELAY_TOKEN", "tok")
    assert isinstance(get_email_client(), RelayEmailClient)


def test_factory_relay_requires_url(monkeypatch):
    monkeypatch.setenv("SETTLEREADY_EMAIL_SOURCE", "relay")
    monkeypatch.delenv("RELAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="RELAY_URL"):
        get_email_client()


def test_load_env_file_does_not_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_TOKEN", "from-env")
    p = tmp_path / "relay.env"
    p.write_text("RELAY_TOKEN=from-file\nGMAIL_ADDRESS=a@b.c\n")
    rs.load_env_file(str(p))
    import os
    assert os.environ["RELAY_TOKEN"] == "from-env"
    assert os.environ["GMAIL_ADDRESS"] == "a@b.c"
