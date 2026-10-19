"""Streamlit analyst workbench (module 5).

The pipeline is triggered by the intake watcher (email arrival), not by this
UI — the workbench is where the senior analyst reviews what the agent prepared.

Event-driven updates: the watcher/pipeline/agents emit events to
data/events.jsonl (see settleready.memory.events). Each dynamic fragment ticks
every 2s but only re-renders from cheap local reads; there is no blind polling
timer and no IMAP polling from the UI. While a case's pipeline is running, the
Packet tab shows a live activity panel streaming the agent's ReAct thinking.

Tabs, in process order:
  Emails        claim emails (from the event log) + intake watcher status
  Packet        live processing view + assembled case file
  Review        approve / return-with-reason-codes (feeds the return tally store)
  Audit trail   every stage transition + the return tally table

Every submitted case is stored and tracked in the Case Blackboard SQLite DB
(data/blackboard.db): the cases table (one row per case, current stage) plus
an append-only audit log of every stage transition. The event log
(data/events.jsonl) is the real-time activity feed over the same history.

Run:  streamlit run src/settleready/ui/app.py   (from the repo root)
"""
from __future__ import annotations

import json
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))  # .../src/settleready/ui
# ui -> settleready -> src -> repo root (three levels up from APP_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(APP_DIR)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
DATA_DIR = os.path.join(REPO_ROOT, "data")

import streamlit as st

from settleready.agents.builder import build_packet
from settleready.agents.reviewer import MAX_RETURNS, REASON_CODES, submit_review
from settleready.memory import events
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore
from settleready.pipeline import is_running, run_case
from settleready.tools.email_client import get_email_client


def _case_ids() -> list[str]:
    """Dynamic case registry: cases the intake watcher created, oldest first."""
    bb = CaseBlackboard(os.path.join(DATA_DIR, "blackboard.db"))
    try:
        return [c["case_id"] for c in bb.list_cases()]
    finally:
        bb.close()


def _selected() -> str | None:
    ids = _case_ids()
    if not ids:
        return None
    if st.session_state.get("case_id") not in ids:
        st.session_state.case_id = ids[0]
    return st.session_state.case_id


def _need_case() -> str | None:
    """Tab guard: None when no cases exist yet (the picker owns the empty state)."""
    return _selected()


@st.fragment(run_every=2)
def case_picker() -> None:
    """Global case picker, always visible above the tabs.

    Event-driven: re-queries the case registry every 2s (one cheap SQLite
    read), so cases the watcher creates appear here ~instantly.
    """
    ids = _case_ids()
    if not ids:
        st.info("📭 No cases yet — the intake watcher creates one automatically "
                "when a claim email arrives.\n\n"
                "Run `bash scripts/start.sh`, then send a claim email "
                "whose subject contains the case ID (e.g. `TL-2026-1045`) with the claim "
                "PDFs attached.")
        return
    col1, col2 = st.columns([1, 4])
    with col1:
        st.selectbox("Case", ids, key="case_id")
    with col2:
        st.caption("↑ switch cases — every tab below follows this selection.")


# ------------------------------------------------------- event feed ---
def _render_event(ev: dict) -> None:
    """One event-log row, newest-first."""
    ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
    e, d = ev["event"], ev.get("detail") or {}
    if e == "react":
        st.markdown(f"`{ts}` 🧠 **iter {d.get('iteration')}** — {d.get('thought')}")
        st.caption(f"🔧 `{d.get('action')}` → 👁 {d.get('observation')}")
    elif e == "stage":
        st.write(f"`{ts}` → stage `{d.get('stage')}`")
    elif e == "email_received":
        st.write(f"`{ts}` 📧 {d.get('subject')} — from {d.get('from')}")
    elif e == "pipeline_started":
        st.write(f"`{ts}` ▶ pipeline started")
    elif e == "pipeline_finished":
        st.write(f"`{ts}` ⏹ pipeline finished")
    elif e == "pipeline_failed":
        st.error(f"`{ts}` pipeline failed: {d.get('error')}")
    elif e == "duplicate_skipped":
        st.write(f"`{ts}` ⏭ duplicate claim email skipped")
    elif e == "case_created":
        st.write(f"`{ts}` 🆕 case created")
    elif e == "documents_parsed":
        st.write(f"`{ts}` 📄 parsed {d.get('count')} documents: "
                 f"{', '.join(d.get('docs', []))}")
    elif e == "verification_checks":
        icons = {"pass": "✅", "fail": "❌", "conflict": "⚠️", "warn": "⚠️",
                 "upfront": "🧠"}
        for c in d.get("checks", []):
            st.write(f"{icons.get(c['status'], '•')} `{c['status']}` {c['check']}")
    elif e == "legal_research":
        cites = ", ".join(d.get("citations", [])) or "none"
        st.write(f"`{ts}` ⚖️ legal research — citations: {cites}")
    elif e == "tool_call":
        args = d.get("args") or {}
        arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
        st.write(f"`{ts}` 🔧 `{d.get('tool')}`({arg_str}) → {d.get('result')}")
    elif e == "state_fallback":
        st.write(f"`{ts}` 🧭 title_state empty — derived `{d.get('state')}` "
                 f"from {d.get('from')}")


def _render_event_feed(case_id: str, limit: int = 25) -> None:
    evs = events.tail(DATA_DIR, case_id=case_id, limit=limit)
    if not evs:
        st.caption("No activity yet for this case.")
    for ev in evs:
        _render_event(ev)


def live_panel() -> None:
    """Live processing view for the selected case (called inside tab_packet's
    fragment, so it ticks with the event log).

    While the pipeline is actively running (marker file written at pipeline
    start, removed at the end), this shows a running status box streaming the
    agent's activity — documents parsed, ReAct Thought/Action/Observation,
    verification checks — as the events arrive. Otherwise a quiet one-liner
    with the current stage.
    """
    case_id = _selected()
    if case_id is None:
        return
    bb = CaseBlackboard(os.path.join(DATA_DIR, "blackboard.db"))
    try:
        case = bb.get_case(case_id) or {}
    finally:
        bb.close()
    stage = case.get("stage", "?")
    if is_running(DATA_DIR, case_id):
        with st.status(f"Processing {case_id} — stage: {stage}",
                       state="running", expanded=True):
            st.caption("Live agent activity — streaming as it happens.")
            _render_event_feed(case_id, limit=15)
    else:
        last = events.tail(DATA_DIR, case_id=case_id, limit=1)
        last_ev = last[0]["event"] if last else "—"
        st.caption(f"Stage `{stage}` · last activity: {last_ev} · "
                   "this panel goes live while the pipeline runs.")


@st.fragment(run_every=2)
def tab_emails():
    st.header("Emails — intake")
    st.caption("Event-driven: this list rebuilds from the watcher's event log — "
               "a new claim email appears here the moment it's processed. "
               "No inbox polling, no refresh timer.")
    try:
        from settleready.agents.intake import IntakeWatcher
        status = IntakeWatcher(data_dir=DATA_DIR).status()
        st.caption(f"📡 Watcher: {status['seen_count']} emails processed, "
                   f"{len(status['cases_triggered'])} cases auto-triggered.")
    except Exception:  # noqa: BLE001
        pass

    claim_events = [e for e in events.tail(DATA_DIR, limit=100)
                    if e["event"] == "email_received"]
    if not claim_events:
        st.info("No claim emails yet — send one whose subject contains the case ID "
                "(e.g. `Claim filed via mobile app — TL-2026-1045`) with the claim PDFs attached.")
    for ev in claim_events:
        d = ev.get("detail") or {}
        ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
        with st.expander(f"{d.get('subject')} — {ts}"):
            st.write(f"From: {d.get('from')}")
            st.write(f"Case: `{ev.get('case_id')}`")
            n_att = d.get("attachments", 0)
            if n_att:
                st.caption(f"{n_att} PDF attachment(s) saved to "
                           f"`data/pdfs/{ev.get('case_id')}/`")
            if d.get("body"):
                st.write(d["body"])

    if st.button("🔄 Check inbox now"):
        # Manual IMAP peek only — the watcher owns continuous polling.
        mail = get_email_client(data_dir=DATA_DIR)
        try:
            from settleready.agents.intake import is_claim_email
            inbox = [e for e in mail.poll_inbox() if is_claim_email(e)]
            st.success(f"Inbox check: {len(inbox)} claim email(s) waiting — "
                       "the watcher will process them on its next poll.")
        except Exception as e:  # noqa: BLE001 — bad creds shouldn't kill the app
            st.warning(
                "Couldn't reach the email inbox: "
                f"{e}\n\nIf you set `SETTLEREADY_EMAIL_SOURCE=gmail`, check `GMAIL_ADDRESS` / "
                "`GMAIL_APP_PASSWORD` in your `.env` (app password needs 2-Step Verification on). "
                "Remove that line to use the mock inbox.")
    # Demo transport only: with real Gmail, replies arrive as real emails.
    selected = _selected()
    mail = get_email_client(data_dir=DATA_DIR)
    if selected and hasattr(mail, "inject_inbound") and st.button("✉ Simulate customer reply"):
        mail.inject_inbound(
            to="claims@demo.local", from_="customer@example.com",
            subject=f"Re: claim documents — {selected}",
            body="Hi, attaching the missing title scan as requested.",
        )
        st.success("Customer reply injected into the mock inbox.")
        st.rerun()


@st.fragment(run_every=2)
def tab_packet():
    st.header("Case packet")
    case_id = _need_case()
    if case_id is None:
        return
    live_panel()
    st.divider()
    try:
        packet = build_packet(case_id, data_dir=DATA_DIR)
    except ValueError as e:
        st.warning(str(e))
        return
    st.subheader(f"{packet['case_id']} — stage: `{packet['stage']}` "
                 f"(returns: {packet['return_count']}/{MAX_RETURNS})")

    st.markdown("**Extracted fields**")
    # str() the values: the column mixes str/int/bool, which pyarrow can't serialize
    rows = [{"field": f, "value": str(v.get("value")), "confidence": v.get("confidence"),
             "source": v.get("source", "")}
            for f, v in packet["fields"].items()]
    st.dataframe(rows, width="stretch")
    if packet["extraction_flags"]:
        st.warning(f"Below confidence threshold: {packet['extraction_flags']}")

    with st.expander("ReAct trace (Thought → Action → Observation)"):
        trace = packet.get("trace", [])
        if not trace:
            st.info("No trace recorded for this case yet.")
        for t in trace:
            st.markdown(f"**Thought:** {t['thought']}")
            st.markdown(f"**Action:** `{t['action']}`")
            st.markdown(f"**Observation:** {t['observation']}")
            st.divider()

    st.markdown("**Verification**")
    for c in packet["verification"].get("checks", []):
        st.write(f"`{c['status']:8s}` {c['check']}: {c['detail']}")
    for cf in packet["verification"].get("conflicts", []):
        st.error(f"CONFLICT {cf['reason_code']}: {cf['detail']}")

    st.markdown("**Legal research**")
    st.write(packet["legal"]["answer"] or "_not run_")
    if packet["legal"]["citations"]:
        st.caption("citations: " + ", ".join(packet["legal"]["citations"]))
    chunks = packet["legal"].get("chunks", [])
    with st.expander(f"Retrieved chunks for this case ({len(chunks)})"):
        if not chunks:
            st.info("No retrieval trace — the research stage runs only for cases "
                    "that pass verification.")
        for c in chunks:
            st.markdown(f"**[{c['state']}/{c['citation']}]** score=`{c['score']}`")
            st.write(c["text"])
            st.divider()
    with st.expander("RAG index contents — what was indexed"):
        try:
            from settleready.tools.rag import inspect_index
            indexed = inspect_index(persist_dir=os.path.join(DATA_DIR, "chroma"))
            st.write(f"{len(indexed)} chunks in the index")
            for item in indexed:
                st.markdown(f"**{item['id']}** [{item['state']}/{item['source']}]")
                st.caption(item["text"][:220] + "…")
        except Exception as e:  # noqa: BLE001 — surface it in the UI
            st.warning(f"Could not read the index: {e}")

    st.markdown("**ACV worksheet**")
    acv = packet["acv"]
    if acv:
        st.metric("Actual cash value", f"${acv['acv']:,}")
        for s in acv["worksheet"]["steps"]:
            st.write(f"`{s['amount']:+7d}` {s['name']} — {s['detail']}")
    else:
        st.write("_not computed_")


@st.fragment(run_every=2)
def tab_review():
    st.header("Senior analyst review")
    st.caption("Follows the event log — a finished pipeline or a new return "
               "updates this tab within ~2s.")
    case_id = _need_case()
    if case_id is None:
        return
    try:
        packet = build_packet(case_id, data_dir=DATA_DIR)
    except ValueError as e:
        st.warning(str(e))
        return

    st.write(f"Case **{case_id}** — stage `{packet['stage']}`, "
             f"returns used: {packet['return_count']}/{MAX_RETURNS}")
    if packet["verification"].get("conflicts"):
        for cf in packet["verification"]["conflicts"]:
            st.error(f"{cf['reason_code']}: {cf['detail']}")

    # Terminal states: nothing left to decide here.
    if packet["stage"] == "approved":
        st.success("✅ Approved — the case is complete. No further action needed.")
        return
    if packet["stage"] == "escalated":
        st.error("⬆️ Escalated to a human — the return budget is exhausted. "
                 "No further action in the UI.")
        return

    if st.button("↻ Re-run analyst pipeline (rework)"):
        with st.spinner("Re-running intake → extract → verify → research → compute…"):
            try:
                out = run_case(case_id, data_dir=DATA_DIR)
            except Exception as e:  # noqa: BLE001 — surface it in the UI
                st.error(f"Pipeline failed: {e}")
                return
        st.success(f"Routed → **{out['next']}**")
        st.rerun()
    st.caption("Re-run re-executes the full pipeline on the current documents — use it "
               "after the customer sends missing files. It does not reset the return "
               "count or change a review decision.")

    note = st.text_input("Reviewer note", "")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ Approve", type="primary"):
            out = submit_review(case_id, "approve", note=note, data_dir=DATA_DIR)
            st.success(f"Case approved → stage `{out['stage']}`")
    with col2:
        reason = st.selectbox("Reason code", sorted(REASON_CODES),
                              format_func=lambda c: f"{c} — {REASON_CODES[c][1]}")
        if st.button("↩ Return to analyst"):
            out = submit_review(case_id, "return", reason_code=reason,
                                note=note, data_dir=DATA_DIR)
            st.warning(f"Returned ({out['return_count']}/{MAX_RETURNS}) → `{out['stage']}`")
            if out["promoted"]:
                st.info(f"🧠 {out['promotion_detail']}")
            if out["stage"] == "escalated":
                st.error("Return budget exhausted — escalated to a human. "
                         "The loop stops here by design.")


@st.fragment(run_every=2)
def tab_audit():
    st.header("Audit trail")
    case_id = _need_case()
    if case_id is None:
        return
    st.caption("Follows the event log — new stage transitions appear within ~2s. "
               "Every submitted case is tracked here from creation to decision.")
    bb = CaseBlackboard(os.path.join(DATA_DIR, "blackboard.db"))
    log = bb.audit_log(case_id)
    bb.close()
    if not log:
        st.info("No audit entries yet — the watcher triggers the pipeline on email arrival.")
    else:
        st.dataframe([{"time": e["ts"], "stage": e["stage"], "event": e["event"],
                       "details": json.dumps(e["details"])}
                      for e in log], width="stretch")

    st.subheader("Return tally (episodic memory)")
    tally = ReturnTallyStore(os.path.join(DATA_DIR, "tally.db"))
    rows = tally.tallies()
    tally.close()
    if rows:
        st.dataframe(rows, width="stretch")
        st.caption("Promotion threshold: 3 returns → mandatory upfront verification. "
                   "Promoted rows run before the verify fan-out on future cases.")
    else:
        st.info("No returns logged yet.")


# ------------------------------------------------------------------ main ---
def main() -> None:
    st.set_page_config(page_title="SettleReady", layout="wide")
    st.title("SettleReady — total-loss case preparation")
    st.caption("Synthetic demo data only. The agent prepares; the senior analyst decides.")

    if not os.path.exists(os.path.join(DATA_DIR, "statutes")):
        st.error("No environment found — run bash scripts/fresh.sh (or bash scripts/seed.sh) first.")
        return

    case_picker()  # global, always visible above the tabs, auto-refreshes

    tabs = st.tabs(["Emails", "Packet", "Review", "Audit trail"])
    with tabs[0]:
        tab_emails()
    with tabs[1]:
        tab_packet()
    with tabs[2]:
        tab_review()
    with tabs[3]:
        tab_audit()


if __name__ == "__main__":
    main()
