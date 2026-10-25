"""Relay email transport (shared demo mailbox).

Problem: a clone that can run the watcher can read any secret shipped to
its machine — so the Gmail app password must never reach a clone at all.
Instead, clones talk to a small relay server (tools/relay_server.py) that
runs on the operator's machine and is the ONLY process holding the app
password. The clone sends a demo token (a kill-switch, not a secret: anyone
may use the relay, and the operator revokes access by rotating the token)
and the relay reads/sends the shared demo mailbox on its behalf.

Clone-side config (in .env — no Gmail password here):
  SETTLEREADY_EMAIL_SOURCE=relay
  RELAY_URL=https://your-relay-host:8899
  RELAY_TOKEN=public-demo-token

The watcher/UI only ever see the EmailClient protocol, so this transport
swaps in with zero agent-code changes.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from settleready.tools.email_client import EmailRecord


class RelayEmailClient:
    """EmailClient over HTTP to the operator's relay server."""

    def __init__(self, base_url: str | None = None,
                 token: str | None = None) -> None:
        self._base = (base_url or os.environ.get("RELAY_URL", "")).rstrip("/")
        self._token = token if token is not None else os.environ.get("RELAY_TOKEN", "")
        if not self._base:
            raise RuntimeError(
                "RELAY_URL is not set — point it at the operator's relay "
                "server (see tools/relay_server.py), or use "
                "SETTLEREADY_EMAIL_SOURCE=gmail with your own mailbox.")

    # -- interface ---------------------------------------------------------
    def poll_inbox(self) -> list[EmailRecord]:
        return [EmailRecord(**d) for d in self._request("GET", "/inbox")["messages"]]

    def send(self, *, to: str, subject: str, body: str,
             from_: str = "settleready@demo.local") -> EmailRecord:
        # from_ is the demo mailbox itself on the relay; the caller-supplied
        # value is ignored so clones cannot spoof the sender.
        rec = self._request("POST", "/send",
                            {"to": to, "subject": subject, "body": body})
        return EmailRecord(**rec)

    def outbox(self) -> list[EmailRecord]:
        raise NotImplementedError(
            "Relay has no outbox read; sent mail lives in the demo mailbox's "
            "Sent folder (same limitation as the IMAP transport).")

    # -- internals ---------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Demo-Token": self._token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("error", e.reason)
            except Exception:
                detail = e.reason
            raise RuntimeError(f"relay {method} {path} failed ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"relay unreachable at {self._base}: {e.reason}")
