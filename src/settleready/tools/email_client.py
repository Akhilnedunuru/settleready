"""Email interface + JSON-backed mock.

The pipeline only ever talks to ``EmailClient``. ``MockEmailClient`` persists to
``data/inbox.json`` / ``data/outbox.json`` so the Streamlit UI can render threads and the
demo can simulate customer replies. A real SMTP implementation can replace it later
without touching any agent code.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass
class EmailRecord:
    id: str
    to: str
    from_: str
    subject: str
    body: str
    ts: str
    direction: str  # "inbound" | "outbound"


class EmailClient(Protocol):
    def send(self, *, to: str, subject: str, body: str,
             from_: str = "settleready@demo.local") -> EmailRecord:
        """Draft-and-send an outbound email (mock: appends to outbox)."""
        ...

    def poll_inbox(self) -> list[EmailRecord]:
        """Return all inbound messages, oldest first."""
        ...

    def outbox(self) -> list[EmailRecord]:
        """Return all sent messages, oldest first."""
        ...


class MockEmailClient:
    """JSON-file mock. ``data_dir`` defaults to ./data (gitignored, created by seed.sh)."""

    def __init__(self, data_dir: str = "data") -> None:
        self._data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._inbox_path = os.path.join(data_dir, "inbox.json")
        self._outbox_path = os.path.join(data_dir, "outbox.json")

    # -- interface ---------------------------------------------------------
    def send(self, *, to: str, subject: str, body: str,
             from_: str = "settleready@demo.local") -> EmailRecord:
        rec = self._new(to=to, from_=from_, subject=subject, body=body, direction="outbound")
        self._append(self._outbox_path, rec)
        return rec

    def poll_inbox(self) -> list[EmailRecord]:
        return self._load(self._inbox_path)

    def outbox(self) -> list[EmailRecord]:
        return self._load(self._outbox_path)

    # -- helpers (fixtures / demo) ------------------------------------------
    def inject_inbound(self, *, to: str, from_: str, subject: str, body: str) -> EmailRecord:
        """Drop a message into the inbox (used by seed.sh and the demo's
        'Simulate customer reply' button)."""
        rec = self._new(to=to, from_=from_, subject=subject, body=body, direction="inbound")
        self._append(self._inbox_path, rec)
        return rec

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _new(*, to: str, from_: str, subject: str, body: str, direction: str) -> EmailRecord:
        return EmailRecord(
            id=uuid.uuid4().hex[:8],
            to=to,
            from_=from_,
            subject=subject,
            body=body,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            direction=direction,
        )

    def _load(self, path: str) -> list[EmailRecord]:
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [EmailRecord(**d) for d in json.load(f)]

    def _append(self, path: str, rec: EmailRecord) -> None:
        recs = self._load(path)
        recs.append(rec)
        with open(path, "w") as f:
            json.dump([asdict(r) for r in recs], f, indent=2)
