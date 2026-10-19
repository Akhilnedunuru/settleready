"""Real email via IMAP/SMTP using only the Python standard library.

Implements the EmailClient protocol, so the intake watcher and the UI work
unchanged — only the transport differs from the mock. Free: works with any
IMAP provider; documented for Gmail with an app password.

Config via env (never commit these — they live in your local .env):
    SETTLEREADY_EMAIL_SOURCE=gmail
    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google Account -> Security -> App passwords
                                             # (needs 2-Step Verification on)

Connection is lazy (per poll/send call), so importing this or building the
client never touches the network — tests stay hermetic.
"""
from __future__ import annotations

import base64
import os
import smtplib
import uuid
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from settleready.tools.email_client import EmailRecord


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


class ImapEmailClient:
    """IMAP inbox polling + SMTP sending. One instance per process is fine."""

    def __init__(self, address: str | None = None, app_password: str | None = None,
                 imap_host: str = "imap.gmail.com",
                 smtp_host: str = "smtp.gmail.com") -> None:
        self._address = address or os.environ.get("GMAIL_ADDRESS", "")
        self._password = (app_password or os.environ.get("GMAIL_APP_PASSWORD", "")).replace(" ", "")
        self._imap_host = imap_host
        self._smtp_host = smtp_host
        if not self._address or not self._password:
            raise RuntimeError(
                "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env "
                "(Google Account -> Security -> App passwords).")

    # -- EmailClient protocol --------------------------------------------------
    def poll_inbox(self) -> list[EmailRecord]:
        """Fetch all messages, oldest first. Dedup is the watcher's local
        intake_state.json — NOT the IMAP \\Seen flag — so reading an email in
        a mail client never hides it from the watcher."""
        import imaplib
        imap = imaplib.IMAP4_SSL(self._imap_host)
        try:
            imap.login(self._address, self._password)
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            records = []
            for num in data[0].split():
                _, msg_data = imap.fetch(num, "(RFC822)")
                import email as _email
                msg = _email.message_from_bytes(msg_data[0][1])
                records.append(self._to_record(msg))
            return records
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def send(self, *, to: str, subject: str, body: str,
             from_: str | None = None) -> EmailRecord:
        msg = EmailMessage()
        msg["From"] = from_ or self._address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL(self._smtp_host, 465) as smtp:
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        return EmailRecord(
            id=uuid.uuid4().hex[:8], to=to, from_=msg["From"], subject=subject,
            body=body, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            direction="outbound", attachments=[])

    def outbox(self) -> list[EmailRecord]:
        raise NotImplementedError("SMTP has no outbox read; sent mail lives in Gmail's Sent folder.")

    # -- MIME parsing ------------------------------------------------------------
    def _to_record(self, msg) -> EmailRecord:
        body, attachments = "", []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                filename = part.get_filename()
                if filename and part.get_content_disposition() == "attachment":
                    payload = part.get_payload(decode=True) or b""
                    attachments.append({
                        "filename": _decode(filename),
                        "content_b64": base64.b64encode(payload).decode(),
                    })
                elif part.get_content_type() == "text/plain" and not body:
                    charset = part.get_content_charset() or "utf-8"
                    body = (part.get_payload(decode=True) or b"").decode(charset, errors="replace")
        else:
            charset = msg.get_content_charset() or "utf-8"
            body = (msg.get_payload(decode=True) or b"").decode(charset, errors="replace")

        try:
            ts = parsedate_to_datetime(msg.get("Date", "")).isoformat(timespec="seconds")
        except Exception:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        msg_id = (msg.get("Message-ID") or "").strip().strip("<>")
        return EmailRecord(
            id=msg_id or uuid.uuid4().hex[:8],
            to=_decode(msg.get("To")),
            from_=_decode(msg.get("From")),
            subject=_decode(msg.get("Subject")),
            body=body.strip()[:4000],
            ts=ts,
            direction="inbound",
            attachments=attachments,
        )
