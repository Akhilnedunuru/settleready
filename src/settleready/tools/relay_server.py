"""Relay server — operator side of the shared demo mailbox.

Runs on a machine the operator controls (e.g. the Oracle Always-Free VM).
It is the ONLY process that ever holds the Gmail app password. Clones of
this repo talk to it over HTTP with a demo token and never see the secret:

  clone's watcher --HTTP--> relay --IMAP/SMTP--> shared demo mailbox

Security model, stated plainly:
- The demo token is NOT a secret and NOT authentication: it is published
  in .env.example so any clone can use the relay. Its purpose is a
  kill-switch — rotate RELAY_TOKEN and old clones stop working, without
  ever touching the Gmail password.
- Abuse is bounded, not prevented: sends are rate-limited (10/hour/IP),
  inbox reads 60/hour/IP, and sends are refused unless the subject carries
  a case ID (TL-YYYY-NNNN), so the relay cannot be used as a generic spam
  cannon. The demo mailbox is sacrificial: if it is ever abused, rotate
  the token, then the app password.
- The token travels in a header, so the relay MUST sit behind HTTPS
  (e.g. Caddy with a DuckDNS domain, as the voice UI already does).

Operator setup (on the server machine, NOT in the repo):
  1. Clone the repo.
  2. Create relay.env next to the repo (never committed):
       GMAIL_ADDRESS=settlesready.demo@gmail.com
       GMAIL_APP_PASSWORD=<redacted>
       RELAY_TOKEN=public-demo-token
  3. Run:  bash scripts/relay_server.sh [--port 8899]
  4. Publish RELAY_URL + RELAY_TOKEN in .env.example for clones.

Run:  .venv/bin/python -m settleready.tools.relay_server [--port 8899]
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from settleready.tools.email_client import CASE_ID_RE

DEFAULT_PORT = 8899
INBOX_LIMIT_PER_HOUR = 60
SEND_LIMIT_PER_HOUR = 10
MAX_BODY_BYTES = 1_000_000


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines without overwriting variables already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("\"'")
            os.environ.setdefault(key, val)


class RateLimiter:
    """Per-IP fixed-window limiter (in-memory; fine for a demo relay)."""

    def __init__(self, limit: int, window_s: int = 3600) -> None:
        self._limit, self._window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(ip, deque())
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True


def send_allowed(subject: str) -> bool:
    """Sends must concern a case (subject carries TL-YYYY-NNNN)."""
    return CASE_ID_RE.search(subject or "") is not None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep stdout to one line per request
        pass

    # -- plumbing --------------------------------------------------------
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        return self.headers.get("X-Demo-Token") == self.server.token

    def _ip(self) -> str:
        return self.client_address[0]

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/inbox":
            if not self._authed():
                return self._json(401, {"error": "bad or missing demo token"})
            if not self.server.inbox_limiter.allow(self._ip()):
                return self._json(429, {"error": "inbox rate limit (60/hour/IP)"})
            try:
                recs = self.server.backend.poll_inbox()
            except Exception as e:  # noqa: BLE001 — upstream failure, not a crash
                return self._json(502, {"error": f"mailbox read failed: {e}"})
            print(f"[relay] GET /inbox from {self._ip()} -> {len(recs)} messages",
                  flush=True)
            return self._json(200, {"messages": [asdict(r) for r in recs]})
        return self._json(404, {"error": "unknown route"})

    def do_POST(self) -> None:
        if self.path != "/send":
            return self._json(404, {"error": "unknown route"})
        if not self._authed():
            return self._json(401, {"error": "bad or missing demo token"})
        if not self.server.send_limiter.allow(self._ip()):
            return self._json(429, {"error": "send rate limit (10/hour/IP)"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if n <= 0 or n > MAX_BODY_BYTES:
            return self._json(413, {"error": "body too large or empty"})
        try:
            payload = json.loads(self.rfile.read(n))
            to, subject, body = payload["to"], payload["subject"], payload["body"]
        except Exception:
            return self._json(400, {"error": "expected JSON {to, subject, body}"})
        if not send_allowed(subject):
            return self._json(403, {"error": "send refused: subject must carry a case ID"})
        try:
            rec = self.server.backend.send(to=to, subject=subject, body=body)
        except Exception as e:  # noqa: BLE001 — upstream failure, not a crash
            return self._json(502, {"error": f"mailbox send failed: {e}"})
        print(f"[relay] POST /send from {self._ip()} to {to} subj={subject!r}",
              flush=True)
        return self._json(200, asdict(rec))


def create_server(host: str, port: int, token: str, backend,
                  inbox_limit: int = INBOX_LIMIT_PER_HOUR,
                  send_limit: int = SEND_LIMIT_PER_HOUR) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.token = token
    server.backend = backend
    server.inbox_limiter = RateLimiter(inbox_limit)
    server.send_limiter = RateLimiter(send_limit)
    server.daemon_threads = True
    return server


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="SettleReady demo-mailbox relay server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--env-file", default="relay.env",
                   help="operator-side secrets file (never committed)")
    a = p.parse_args()

    load_env_file(a.env_file)
    missing = [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RELAY_TOKEN")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Refusing to start: missing {', '.join(missing)} — set them in "
            f"{a.env_file} (operator machine only, never in the repo).")
    from settleready.tools.imap_client import ImapEmailClient
    backend = ImapEmailClient()  # reads GMAIL_* from the environment just loaded
    server = create_server(a.host, a.port, os.environ["RELAY_TOKEN"], backend)
    print(f"[relay] serving shared demo mailbox on {a.host}:{a.port} "
          f"(token kill-switch active; Gmail password stays on this machine)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[relay] stopping.", flush=True)


if __name__ == "__main__":
    main()
