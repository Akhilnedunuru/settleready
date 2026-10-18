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
import re
import time

from settleready.memory.blackboard import CaseBlackboard
from settleready.tools.email_client import EmailRecord, MockEmailClient

CASE_ID_RE = re.compile(r"TL-\d{4}-\d+")
REWORK_STAGES = ("needs_info", "analyst_rework")  # waiting on the customer/analyst


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


class IntakeWatcher:
    """Polls the inbox and fires the pipeline. `run_fn` is injectable for tests."""

    def __init__(self, data_dir: str = "data", run_fn=None,
                 mail: MockEmailClient | None = None) -> None:
        self._data_dir = data_dir
        self._mail = mail or MockEmailClient(data_dir=data_dir)
        if run_fn is None:
            from settleready.pipeline import run_case

            def run_fn(case_id: str):  # noqa: F811
                return run_case(case_id, data_dir=data_dir)
        self._run_fn = run_fn
        self._state_path = os.path.join(data_dir, "intake_state.json")

    # -- state ---------------------------------------------------------------
    def _seen(self) -> set[str]:
        try:
            with open(self._state_path) as f:
                return set(json.load(f).get("seen", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _mark_seen(self, email_id: str) -> None:
        seen = self._seen()
        seen.add(email_id)
        with open(self._state_path, "w") as f:
            json.dump({"seen": sorted(seen)}, f, indent=2)

    def status(self) -> dict:
        """For the UI: what the watcher has processed."""
        return {"seen_count": len(self._seen()),
                "state_file": self._state_path}

    # -- polling ---------------------------------------------------------------
    def poll_once(self) -> list[dict]:
        """Process every unseen inbound email. Returns the actions taken."""
        actions = []
        seen = self._seen()
        bb = CaseBlackboard(os.path.join(self._data_dir, "blackboard.db"))
        try:
            for email in self._mail.poll_inbox():
                if email.id in seen:
                    continue
                self._mark_seen(email.id)
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
        pdf_dir = os.path.join(self._data_dir, "pdfs", case_id)
        if not os.path.isdir(pdf_dir):
            return {"email": email.id, "action": "no_documents",
                    "detail": f"no PDFs for {case_id} yet"}
        summary = self._run_fn(case_id)
        return {"email": email.id, "action": "pipeline_triggered",
                "case_id": case_id, "routed": summary.get("next")}

    def _handle_reply(self, email: EmailRecord, bb: CaseBlackboard) -> dict:
        case_id = extract_case_id(email)
        case = bb.get_case(case_id) if case_id else None
        if not case:
            return {"email": email.id, "action": "reply_uncorrelated",
                    "detail": f"subject={email.subject!r}"}
        bb.log(case_id, case["stage"], "customer_reply",
               {"from": email.from_, "body": email.body[:500]})
        if case["stage"] in REWORK_STAGES:
            summary = self._run_fn(case_id)
            return {"email": email.id, "action": "reply_retriggered",
                    "case_id": case_id, "routed": summary.get("next")}
        return {"email": email.id, "action": "reply_logged", "case_id": case_id}

    def watch(self, interval_s: int = 15) -> None:
        """Daemon loop: poll forever. Ctrl-C stops."""
        print(f"Watching inbox every {interval_s}s (Ctrl-C to stop)…")
        while True:
            for a in self.poll_once():
                print(f"[{a['action']}] {a.get('case_id', '')} {a.get('detail', '')}"
                      .strip())
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
