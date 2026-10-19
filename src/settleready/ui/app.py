"""Streamlit analyst workbench (module 5).

The pipeline is triggered by the intake watcher (email arrival), not by this
UI — the workbench is where the senior analyst reviews what the agent prepared.

Tabs, in process order:
  Emails        inbox threads + intake watcher status
  Packet        assembled case file: fields, verification, cited legal note, ACV worksheet
  Review        approve / return-with-reason-codes (feeds the return tally store)
  Audit trail   every stage transition + the return tally table

Run:  streamlit run src/settleready/ui/app.py   (from the repo root)
"""
from __future__ import annotations

import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))  # .../src/settleready/ui
# ui -> settleready -> src -> repo root (three levels up from APP_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(APP_DIR)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
DATA_DIR = os.path.join(REPO_ROOT, "data")

import streamlit as st

from settleready.agents.builder import build_packet
from settleready.agents.reviewer import MAX_RETURNS, REASON_CODES, submit_review
from settleready.memory.blackboard import CaseBlackboard
from settleready.memory.tally import ReturnTallyStore
from settleready.pipeline import run_case
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
    """Tab guard: show the empty state when no cases exist yet."""
    case_id = _selected()
    if case_id is None:
        st.info("📭 No cases yet — the intake watcher creates one automatically "
                "when a claim email arrives.\n\n"
                "Run `python -m settleready.agents.intake watch`, then send a claim email "
                "whose subject contains the case ID (e.g. `TL-2026-1045`) with the claim "
                "PDFs attached.")
    return case_id


# ------------------------------------------------------------------ tabs ---
def tab_emails():
    st.header("Emails — intake")
    st.caption("Claim emails arrive here. The intake watcher "
               "`(python -m settleready.agents.intake watch)` picks them up and fires "
               "the pipeline automatically — no manual run needed.")
    try:
        from settleready.agents.intake import IntakeWatcher
        status = IntakeWatcher(data_dir=DATA_DIR).status()
        st.caption(f"📡 Watcher: {status['seen_count']} emails processed, "
                   f"{len(status['cases_triggered'])} cases auto-triggered.")
    except Exception:  # noqa: BLE001
        pass
    mail = get_email_client(data_dir=DATA_DIR)
    try:
        from settleready.agents.intake import is_claim_email
        inbox = [e for e in mail.poll_inbox() if is_claim_email(e)]
        print(f"[emails tab] poll -> {len(inbox)} claim emails", flush=True)
        st.caption(f"Last poll: {len(inbox)} claim email(s).")
    except Exception as e:  # noqa: BLE001 — bad creds shouldn't kill the app
        st.warning(
            "Couldn't reach the email inbox: "
            f"{e}\n\nIf you set `SETTLEREADY_EMAIL_SOURCE=gmail`, check `GMAIL_ADDRESS` / "
            "`GMAIL_APP_PASSWORD` in your `.env` (app password needs 2-Step Verification on). "
            "Remove that line to use the mock inbox.")
        inbox = []
    if not inbox:
        st.info("No claim emails yet — send one whose subject contains the case ID "
                "(e.g. `Claim filed via mobile app — TL-2026-1045`) with the claim PDFs attached.")
    for e in inbox:
        with st.expander(f"{e.subject} — from {e.from_}"):
            st.write(e.body)
            if e.attachments:
                st.caption("attachments: " + ", ".join(a["filename"] for a in e.attachments))
    # Demo transport only: with real Gmail, replies arrive as real emails.
    selected = _selected()
    if selected and hasattr(mail, "inject_inbound") and st.button("✉ Simulate customer reply"):
        mail.inject_inbound(
            to="claims@demo.local", from_="customer@example.com",
            subject=f"Re: claim documents — {selected}",
            body="Hi, attaching the missing title scan as requested.",
        )
        st.success("Customer reply injected into the mock inbox.")
        st.rerun()


def tab_packet():
    st.header("Case packet")
    case_id = _need_case()
    if case_id is None:
        return
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


def tab_review():
    st.header("Senior analyst review")
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

    if st.button("↻ Re-run analyst pipeline (rework)"):
        with st.spinner("Re-running intake → extract → verify → research → compute…"):
            try:
                out = run_case(case_id, data_dir=DATA_DIR)
            except Exception as e:  # noqa: BLE001 — surface it in the UI
                st.error(f"Pipeline failed: {e}")
                return
        st.success(f"Routed → **{out['next']}**")
        st.rerun()

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


def tab_audit():
    st.header("Audit trail")
    case_id = _need_case()
    if case_id is None:
        return
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
        st.error("No environment found — run ./scripts/fresh.sh (or ./scripts/seed.sh) first.")
        return

    st.sidebar.header("Case")
    ids = _case_ids()
    if ids:
        st.sidebar.selectbox("Select case", ids, key="case_id")
    else:
        st.sidebar.caption("No cases yet — they appear here when claim emails arrive.")
    st.sidebar.caption("Cases are created by the intake watcher when claim emails arrive.")

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
