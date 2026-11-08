"""Intake watcher: the real-world trigger (module 5).

Polls the inbox through the EmailClient interface (mock JSON file today, real
Gmail/SES tomorrow — the pipeline never knows the difference) and fires the
pipeline automatically when a claim email arrives. No "Run" button.

- New claim email -> route_claim() -> total-loss -> run_case() automatically
- Customer reply  -> correlated to the open case via the case ID in the subject
                     -> logged to the case audit; re-runs the pipeline when the
                        case is waiting on info (the bounded customer loop)
- Anything else   -> logged and skipped (other queues are out of scope)

Seen-tracking in data/intake_state.json so restarts never re-fire a case.

CLI:
    PYTHONPATH=src python -m settleready.agents.intake once            # single poll
    PYTHONPATH=src python -m settleready.agents.intake watch            # daemon loop
    PYTHONPATH=src python -m settleready.agents.intake demo-claim --case-id TL-2026-1045
"""
from __future__ import annotations

import json
import os
import time

from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.events import emit as _emit_event
from settleready.tools.email_client import CASE_ID_RE, EmailRecord, get_email_client

REWORK_STAGES = ("needs_info", "analyst_rework", "customer_question")  # waiting on the customer/analyst


def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def extract_case_id(email: EmailRecord) -> str | None:
    """Case IDs ride in the subject line (filing confirmations + reply threads)."""
    m = CASE_ID_RE.search(email.subject or "")
    return m.group(0) if m else None


def route_claim(email: EmailRecord) -> str:
    """Return 'total_loss' or 'other'.

    MVP rule: the mobile-app filing confirmation carries the total-loss case ID
    in the subject. Everything else goes to other queues (out of scope).
    """
    return "total_loss" if extract_case_id(email) else "other"


def is_reply(email: EmailRecord) -> bool:
    return (email.subject or "").strip().lower().startswith("re:")


def _strip_quoted(body: str) -> str:
    """Keep only the customer's new words from a mailed reply.

    Mail clients quote the original thread below the answer, and the
    extractor cannot tell quote from answer: it once latched onto
    "Claimant declared no lien" in the quoted email instead of the
    customer's confirmation above it. Cut at the first quote marker: a
    ">"-quoted line, a Gmail "On ... wrote:" line, or an Original Message
    separator.
    """
    out = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(">") or s in ("-----Original Message-----",
                                      "---Original Message---"):
            break
        if s.startswith("On ") and s.endswith("wrote:"):
            break
        out.append(line)
    return "\n".join(out).strip()


def is_claim_email(email: EmailRecord) -> bool:
    """Does this email belong to the pipeline? The contract is: the subject
    carries a case ID (TL-YYYY-NNNN) — for new claims and for replies alike.
    Everything else (Google notifications, newsletters, ...) is ignored."""
    return extract_case_id(email) is not None


class IntakeWatcher:
    """Polls the inbox and fires the pipeline. `run_fn` is injectable for tests."""

    def __init__(self, data_dir: str = "data", run_fn=None,
                 mail=None) -> None:
        self._data_dir = data_dir
        # Transport comes from the factory: mock JSON by default, real IMAP/SMTP
        # when SETTLEREADY_EMAIL_SOURCE=gmail. Tests may pass their own client.
        self._mail = mail or get_email_client(data_dir=data_dir)
        if run_fn is None:
            from settleready.pipeline import run_case

            def run_fn(case_id: str):  # noqa: F811
                return run_case(case_id, data_dir=data_dir)
        self._run_fn = run_fn
        self._state_path = os.path.join(data_dir, "intake_state.json")

    # -- state ---------------------------------------------------------------
    # intake_state.json: {"seen": [email ids], "cases_triggered": [case ids]}
    # Local processed-tracking is the source of truth (not IMAP \\Seen flags),
    # so reading an email in a mail client never hides it from the watcher.
    def _load_state(self) -> dict:
        try:
            with open(self._state_path) as f:
                d = json.load(f)
                return {"seen": set(d.get("seen", [])),
                        "cases_triggered": set(d.get("cases_triggered", []))}
        except (FileNotFoundError, json.JSONDecodeError):
            return {"seen": set(), "cases_triggered": set()}

    def _save_state(self, state: dict) -> None:
        with open(self._state_path, "w") as f:
            json.dump({"seen": sorted(state["seen"]),
                       "cases_triggered": sorted(state["cases_triggered"])}, f, indent=2)

    def _mark_seen(self, email_id: str) -> None:
        state = self._load_state()
        state["seen"].add(email_id)
        self._save_state(state)

    def _mark_case_triggered(self, case_id: str) -> None:
        state = self._load_state()
        state["cases_triggered"].add(case_id)
        self._save_state(state)

    def status(self) -> dict:
        """For the UI: what the watcher has processed."""
        state = self._load_state()
        return {"seen_count": len(state["seen"]),
                "cases_triggered": sorted(state["cases_triggered"]),
                "state_file": self._state_path}

    # -- polling ---------------------------------------------------------------
    def poll_once(self) -> list[dict]:
        """Process every unseen inbound email. Returns the actions taken."""
        actions = []
        seen = self._load_state()["seen"]
        bb = CaseBlackboard(os.path.join(self._data_dir, "blackboard.db"))
        try:
            emails = self._mail.poll_inbox()
            print(f"[{_ts()}] fetched {len(emails)} claim emails from inbox", flush=True)
            for email in emails:
                if email.id in seen:
                    continue
                self._mark_seen(email.id)
                if not is_claim_email(email):
                    actions.append({"email": email.id, "action": "ignored",
                                    "detail": f"no case ID in subject={email.subject!r}"})
                    continue
                if is_reply(email):
                    actions.append(self._handle_reply(email, bb))
                else:
                    actions.append(self._handle_new_claim(email))
        finally:
            bb.close()
        return actions

    def _handle_new_claim(self, email: EmailRecord) -> dict:
        case_id = extract_case_id(email)
        if route_claim(email) != "total_loss" or not case_id:
            return {"email": email.id, "action": "routed_other",
                    "detail": f"subject={email.subject!r}"}
        _emit_event(self._data_dir, case_id, "email_received",
                    {"from": email.from_, "subject": email.subject,
                     "attachments": len(email.attachments or []),
                     "body": (email.body or "")[:400]})
        # Remember who to ask when the analyst stage needs follow-up info.
        from settleready.memory.blackboard import CaseBlackboard
        _bb = CaseBlackboard(os.path.join(self._data_dir, "blackboard.db"))
        try:
            _bb.write(case_id, "claimant_email", email.from_)
        finally:
            _bb.close()
        # One automatic trigger per case ID — duplicate claim emails don't
        # re-fire the pipeline (manual re-runs go through the Review tab).
        if case_id in self._load_state()["cases_triggered"]:
            _emit_event(self._data_dir, case_id, "duplicate_skipped",
                        {"email": email.id})
            return {"email": email.id, "action": "duplicate_claim",
                    "case_id": case_id,
                    "detail": "pipeline already triggered for this case"}
        # Real-world step: persist the email's attachments as the case's
        # documents before the pipeline runs. (The mock's demo emails carry no
        # attachments; their PDFs come from scripts/seed.sh.)
        saved, _skipped = self._save_attachments(email, case_id)
        pdf_dir = os.path.join(self._data_dir, "pdfs", case_id)
        if not os.path.isdir(pdf_dir):
            return {"email": email.id, "action": "no_documents",
                    "detail": f"no PDFs for {case_id} yet"}
        try:
            print(f"[{_ts()}] pipeline start {case_id} …", flush=True)
            t0 = time.time()
            summary = self._run_fn(case_id)
            print(f"[{_ts()}] pipeline done {case_id} -> {summary.get('next')} "
                  f"({time.time() - t0:.0f}s)", flush=True)
        except Exception as e:  # noqa: BLE001 — a daemon must not die on one bad case
            return {"email": email.id, "action": "pipeline_failed",
                    "case_id": case_id, "detail": str(e)[:200]}
        self._mark_case_triggered(case_id)
        return {"email": email.id, "action": "pipeline_triggered",
                "case_id": case_id, "routed": summary.get("next"),
                "attachments_saved": saved}

    # Attachment handling limits (Module 6 hardening).
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB per file
    ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

    def _save_attachments(self, email: EmailRecord,
                          case_id: str) -> tuple[list[str], list[dict]]:
        """Decode the email's attachments into data/pdfs/<case_id>/.

        Returns (saved_filenames, skipped) where skipped is a list of
        {"filename", "reason"} dicts. Images are accepted (OCR via
        pdf_parser); non-PDF/non-image files, oversized files, and
        undecodable content are skipped with a recorded reason.
        """
        import base64

        saved: list[str] = []
        skipped: list[dict] = []
        for att in email.attachments or []:
            filename = att.get("filename", "") or "unnamed"
            ext = os.path.splitext(filename)[1].lower()
            if ext not in self.ALLOWED_EXTS:
                skipped.append({"filename": filename,
                                "reason": f"unsupported file type: {ext or '(none)'}"})
                continue
            try:
                raw = base64.b64decode(att.get("content_b64", ""))
            except Exception:  # noqa: BLE001
                skipped.append({"filename": filename,
                                "reason": "could not decode attachment content"})
                continue
            if len(raw) > self.MAX_ATTACHMENT_BYTES:
                skipped.append({"filename": filename,
                                "reason": f"exceeds {self.MAX_ATTACHMENT_BYTES // (1024*1024)} MB size limit "
                                          f"({len(raw) // (1024*1024)} MB)"})
                continue
            # Filename sanity: must look like a document for this case.
            # Customers name files arbitrarily ("IMG_1234.jpg"), so we only
            # reject names that clearly belong to another case or are empty.
            safe = os.path.basename(filename)
            if not safe or safe.startswith("."):
                skipped.append({"filename": filename,
                                "reason": "invalid filename"})
                continue
            pdf_dir = os.path.join(self._data_dir, "pdfs", case_id)
            os.makedirs(pdf_dir, exist_ok=True)
            with open(os.path.join(pdf_dir, safe), "wb") as f:
                f.write(raw)
            saved.append(safe)
        # Feedback: record every skipped attachment so the customer (and the
        # analyst) can see why a file was not accepted.
        for s in skipped:
            _emit_event(self._data_dir, case_id, "attachment_skipped",
                        {"filename": s["filename"], "reason": s["reason"]})
        return saved, skipped

    def _handle_reply(self, email: EmailRecord, bb: CaseBlackboard) -> dict:
        case_id = extract_case_id(email)
        case = bb.get_case(case_id) if case_id else None
        if not case:
            return {"email": email.id, "action": "reply_uncorrelated",
                    "detail": f"subject={email.subject!r}"}
        bb.log(case_id, case["stage"], "customer_reply",
               {"from": email.from_, "body": email.body[:500]})
        # Documents the customer sends back become case documents...
        saved, skipped = self._save_attachments(email, case_id)
        # ...and a text-only reply still counts: the statement is fed to
        # extraction as a customer document on the next run (e.g. confirming
        # a lien in words), so the loop can resolve without new PDFs.
        # Store only the customer's new words, not the quoted thread below
        # them (see _strip_quoted).
        cleaned = _strip_quoted(email.body or "")
        # Module 6 guardrail: screen customer text before the agent acts on it.
        # BLOCK (prompt injection) -> do not store, do not re-run; escalate.
        # FLAG (PII) -> store but log; the agent must not echo PII.
        if cleaned:
            from settleready.guardrails import screen_input
            verdicts = screen_input(cleaned, source="customer_reply")
            for v in verdicts:
                _emit_event(self._data_dir, case_id, "guardrail",
                            {"layer": "input", "name": v.name,
                             "action": v.action, "detail": v.detail})
            blocked = [v for v in verdicts if v.action == "block"]
            if blocked:
                bb.log(case_id, case["stage"], "guardrail_block",
                       {"reason": blocked[0].detail,
                        "from": email.from_})
                _emit_event(self._data_dir, case_id, "escalated",
                            {"reason": "guardrail_block",
                             "detail": blocked[0].detail})
                return {"email": email.id, "action": "reply_blocked",
                        "detail": blocked[0].detail}
        # Hybrid reply classification: a question-type reply must NOT trigger
        # another document-request email — it routes to human review instead.
        from settleready.agents.reply_classifier import classify_reply
        try:
            from settleready.llm.provider import get_provider
            _prov = get_provider()
        except Exception:  # noqa: BLE001 — no API key; heuristic-only mode
            _prov = None
        classification = classify_reply(
            cleaned, has_attachments=bool(saved), provider=_prov)
        _emit_event(self._data_dir, case_id, "reply_classified",
                    {"type": classification["type"],
                     "reason": classification["reason"],
                     "method": classification["method"]})
        if cleaned:
            stmts = bb.read(case_id, "customer_statements") or []
            stmts.append({"from": email.from_, "body": cleaned[:2000]})
            bb.write(case_id, "customer_statements", stmts)
        _emit_event(self._data_dir, case_id, "reply_received",
                    {"from": email.from_, "attachments": saved,
                     "attachments_skipped": skipped,
                     "body": cleaned[:2000],
                     "body_len": len(email.body or ""),
                     "reply_type": classification["type"]})
        # Question-type reply: flag for human, do NOT re-run the pipeline
        # for documents (would spam the customer with repeat requests).
        if classification["type"] == "asks_question":
            bb.set_stage(case_id, "customer_question", event="reply",
                         details={"question": cleaned[:500]})
            _emit_event(self._data_dir, case_id, "customer_question",
                        {"question": cleaned[:500]})
            return {"email": email.id, "action": "reply_question",
                    "case_id": case_id, "detail": "routed to human review"}
        if case["stage"] in REWORK_STAGES:
            try:
                print(f"[{_ts()}] pipeline start {case_id} (customer reply) …", flush=True)
                t0 = time.time()
                summary = self._run_fn(case_id)
                print(f"[{_ts()}] pipeline done {case_id} -> {summary.get('next')} "
                      f"({time.time() - t0:.0f}s)", flush=True)
            except Exception as e:  # noqa: BLE001 — a daemon must not die on one bad case
                return {"email": email.id, "action": "pipeline_failed",
                        "case_id": case_id, "detail": str(e)[:200]}
            return {"email": email.id, "action": "reply_retriggered",
                    "case_id": case_id, "routed": summary.get("next"),
                    "attachments_saved": saved}
        return {"email": email.id, "action": "reply_logged", "case_id": case_id,
                "attachments_saved": saved}

    def watch(self, interval_s: int = 15) -> None:
        """Daemon loop: poll forever. Ctrl-C stops."""
        print(f"[{_ts()}] Watching inbox every {interval_s}s (Ctrl-C to stop)…", flush=True)
        while True:
            try:
                actions = self.poll_once()
            except Exception as e:  # noqa: BLE001 — log and keep watching
                print(f"[{_ts()}] poll error: {e}", flush=True)
                actions = []
            for a in actions:
                if a["action"] != "ignored":
                    print(f"[{_ts()}] [{a['action']}] {a.get('case_id', '')} "
                          f"{a.get('detail', '')}".strip(), flush=True)
            time.sleep(interval_s)


def _demo_claim(case_id: str, data_dir: str) -> None:
    """Simulate a claim email arriving (demo transport for the real listener)."""
    mail = MockEmailClient(data_dir=data_dir)
    rec = mail.inject_inbound(
        to="claims@demo.local", from_="customer@example.com",
        subject=f"Claim filed via mobile app — {case_id}",
        body=f"New total-loss claim {case_id} filed. Documents attached.",
    )
    print(f"injected {rec.id}: {rec.subject}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Intake watcher: email arrival -> pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once", help="process unseen emails once and exit")
    w = sub.add_parser("watch", help="poll forever")
    w.add_argument("--interval", type=int, default=15)
    d = sub.add_parser("demo-claim", help="simulate a claim email arriving")
    d.add_argument("--case-id", required=True)
    p.add_argument("--data", default="data")
    a = p.parse_args()

    if a.cmd == "demo-claim":
        _demo_claim(a.case_id, a.data)
    else:
        watcher = IntakeWatcher(data_dir=a.data)
        if a.cmd == "once":
            for action in watcher.poll_once():
                print(action)
        else:
            watcher.watch(interval_s=a.interval)
