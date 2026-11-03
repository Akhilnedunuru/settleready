"""Streamlit analyst workbench (module 5).

The pipeline is triggered by the intake watcher (email arrival), not by this
UI — the workbench is where the senior analyst reviews what the agent prepared.

Event-driven updates: the watcher/pipeline/agents emit events to
data/events.jsonl (see settleready.memory.events). Each dynamic fragment ticks
every 2s but only re-renders from cheap local reads; there is no blind polling
timer and no IMAP polling from the UI. While a case's pipeline is running, the
Packet tab shows a live activity panel streaming the agent's ReAct thinking.
After the run finishes, the panel keeps the full activity history so the flow
can be replayed.

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


def _flash(kind: str, msg: str) -> None:
    """Queue a feedback message that survives the 2s fragment re-renders.

    Plain st.success/warning calls vanish on the next auto-refresh tick, which
    makes button clicks look dead. Flashed messages persist ~30s.
    """
    import time as _time
    msgs = st.session_state.get("flashes", [])
    msgs.append((kind, msg, _time.time()))
    st.session_state["flashes"] = msgs


def _show_flash() -> None:
    import time as _time
    now = _time.time()
    msgs = [m for m in st.session_state.get("flashes", []) if now - m[2] < 30]
    st.session_state["flashes"] = msgs
    for kind, msg, _ts in msgs:
        getattr(st, kind)(msg)


@st.fragment(run_every=2)
def case_picker() -> None:
    """Global case picker, always visible above the tabs.

    Event-driven: re-queries the case registry every 2s (one cheap SQLite
    read), so cases the watcher creates appear here ~instantly.
    (Kept global — rendering it per-tab causes duplicate widget keys,
    since Streamlit registers elements in all tabs, not just the visible one.)
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
    elif e == "email_sent":
        with st.expander(f"`{ts}` 📤 info request sent to {d.get('to')}", expanded=False):
            st.write(f"**Subject:** {d.get('subject')}")
            for n in d.get("needs", []):
                st.write(f"- {n}")
    elif e in ("email_skipped", "email_failed"):
        st.write(f"`{ts}` 📪 outreach {e.split('_')[1]}: {d.get('reason', d.get('error'))}")
    elif e == "reply_received":
        atts = ", ".join(d.get("attachments", [])) or "no attachments"
        st.write(f"`{ts}` 💬 customer reply from {d.get('from')} — {atts}")
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
    elif e == "disposition_planned":
        needs = ", ".join(d.get("needs", [])) or "none"
        flags = ", ".join(d.get("analyst_flags", [])) or "none"
        st.write(f"`{ts}` 🗺️ disposition planned → `{d.get('outcome')}` — "
                 f"asks: {needs}; analyst flags: {flags}")
    elif e == "disposition_fallback":
        st.write(f"`{ts}` 🗺️ disposition planner failed, fell back: {d.get('error')}")
    elif e == "citation_check":
        if d.get("passed"):
            st.write(f"`{ts}` ✅ citation check passed")
        else:
            st.write(f"`{ts}` ❌ citation check FAILED — {d.get('reason')} "
                     f"(answer declined)")


def _render_event_feed(case_id: str, limit: int = 25,
                        oldest_first: bool = False) -> None:
    """Render one case's event feed. Newest-first by default (live view);
    oldest_first=True reads a finished run top-to-bottom, in flow order."""
    evs = events.tail(DATA_DIR, case_id=case_id, limit=limit)
    if oldest_first:
        evs = list(reversed(evs))
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
    verification checks — as the events arrive. Otherwise the one-liner plus
    the case's full activity history, oldest first, so the finished run can
    be replayed.
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
                   "full run history below; this panel streams live "
                   "while the pipeline runs.")
        st.markdown("**Run activity — how this case flowed**")
        _render_event_feed(case_id, limit=50, oldest_first=True)


def _agent_flow_diagram(case_id: str) -> None:
    """Visual multi-agent flow: which nodes ran, in what order, with ToT/fallback.

    Built from the persisted agent_path — visible after the run completes
    (the pipeline runs synchronously, so the diagram renders once done).
    Green/bold = path taken; gray/dashed = not taken.
    """
    from settleready.pipeline import is_running
    live = is_running(DATA_DIR, case_id)
    st.markdown("**🤖 Agent flow — multi-agent path taken**" + (" 🔴 LIVE" if live else ""))
    bb = CaseBlackboard(os.path.join(DATA_DIR, "blackboard.db"))
    try:
        path = bb.read(case_id, "agent_path") or []
    finally:
        bb.close()
    if not path:
        if live:
            st.info("Pipeline running — the diagram will appear when the run completes.")
        else:
            st.info("No agent run recorded yet — re-run the pipeline to see the flow diagram.")
        return

    # ToT vs fallback: most recent disposition event wins (not just any).
    # ToT only runs for non-clean cases — clean cases skip disposition entirely.
    evs = events.tail(DATA_DIR, case_id=case_id, limit=200)
    disp_label = "Disposition"
    disp_detail = ""
    if "disposition" in path:
        for e in evs:  # newest-first; first match is the latest run
            if e["event"] == "disposition_planned":
                disp_label = "Disposition\\n🌳 ToT beam search"
                disp_detail = f"ToT planner ran at {e.get('ts', '?')}"
                break
            if e["event"] == "disposition_fallback":
                disp_label = "Disposition\\n⚙️ Fallback mapping"
                disp_detail = f"Fallback used at {e.get('ts', '?')}"
                break
        if not disp_detail:
            disp_detail = "Disposition ran but no planner event found in log."

    taken = set(zip(path, path[1:]))
    if path:
        taken.add((path[-1], "end"))

    def _edge(a: str, b: str, label: str = "") -> str:
        lbl = f', label="{label}"' if label else ""
        if (a, b) in taken:
            return f'{a} -> {b} [color="#1a7f37", penwidth=2.5{lbl}];'
        return f'{a} -> {b} [color=gray, style=dashed{lbl}];'

    dot = f"""
    digraph {{
        rankdir=LR;
        node [shape=box, style="rounded,filled", fillcolor=white, fontname="sans-serif"];
        edge [fontname="sans-serif", fontsize=10, fontcolor="#555555"];
        ingest [label="Ingest\\n(extract)"];
        ground [label="Ground\\n(verify)"];
        disposition [label="{disp_label}"];
        outreach [label="Outreach\\n(email)"];
        reviewer [label="Reviewer\\n(AI check)"];
        end [label="END", shape=ellipse, fillcolor="#e6f4ea"];
        {_edge("ingest", "ground")}
        {_edge("ground", "reviewer", "passed")}
        {_edge("ground", "disposition", "failed")}
        {_edge("disposition", "outreach")}
        {_edge("disposition", "end", "escalate")}
        {_edge("outreach", "end")}
        {_edge("reviewer", "end", "approve")}
        {_edge("reviewer", "ingest", "return")}
        {_edge("reviewer", "ground", "return")}
    }}
    """
    st.graphviz_chart(dot)
    st.caption(f"Path taken: {' → '.join(path)} → end")
    if disp_detail:
        st.caption(disp_detail)


def _render_email_message(ev: dict) -> None:
    """One email row inside a case's thread (called within the case expander)."""
    d = ev.get("detail") or {}
    ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
    kind = ev["event"]
    if kind == "reply_received":
        atts = ", ".join(d.get("attachments", [])) or "no attachments"
        with st.expander(f"💬 Customer reply — {ts}", expanded=False):
            st.write(f"From: {d.get('from')}")
            if d.get("body"):
                st.text(d["body"])
            else:
                st.caption("No body recorded for this reply.")
            st.write(f"Attachments: {atts}")
    elif kind == "email_sent":
        with st.expander(f"📤 Info request sent — {ts}", expanded=False):
            st.write(f"To: {d.get('to')}")
            for n in d.get("needs", []):
                st.write(f"- {n}")
    else:  # email_received
        with st.expander(f"📥 {d.get('subject')} — {ts}", expanded=False):
            st.write(f"From: {d.get('from')}")
            n_att = d.get("attachments", 0)
            if n_att:
                st.caption(f"{n_att} PDF attachment(s) saved to "
                           f"`data/pdfs/{ev.get('case_id')}/`")
            if d.get("body"):
                st.write(d["body"])


@st.fragment(run_every=2)
def tab_emails():
    st.header("Emails — intake")
    st.caption("Event-driven: this list rebuilds from the watcher's event log — "
               "a new claim email appears here the moment it's processed, "
               "grouped into one thread per case. "
               "No inbox polling, no refresh timer.")
    try:
        from settleready.agents.intake import IntakeWatcher
        status = IntakeWatcher(data_dir=DATA_DIR).status()
        st.caption(f"📡 Watcher: {status['seen_count']} emails processed, "
                   f"{len(status['cases_triggered'])} cases auto-triggered.")
    except Exception:  # noqa: BLE001
        pass

    claim_events = [e for e in events.tail(DATA_DIR, limit=500)
                    if e["event"] in ("email_received", "reply_received", "email_sent")]
    if not claim_events:
        st.info("No claim emails yet — send one whose subject contains the case ID "
                "(e.g. `Claim filed via mobile app — TL-2026-1045`) with the claim PDFs attached.")
    else:
        # One thread per case: the initial claim email, follow-up info
        # requests, and customer replies grouped together, oldest first.
        # Threads order by latest activity, newest first.
        threads: dict[str, list] = {}
        for ev in claim_events:
            threads.setdefault(ev.get("case_id") or "?", []).append(ev)
        for case_id, evs in sorted(threads.items(),
                                   key=lambda kv: max(e["ts"] for e in kv[1]),
                                   reverse=True):
            evs.sort(key=lambda e: e["ts"])
            with st.expander(f"📧 {case_id} — {len(evs)} message(s)",
                             expanded=False):
                for ev in evs:
                    _render_email_message(ev)

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
    try:
        packet = build_packet(case_id, data_dir=DATA_DIR)
    except ValueError as e:
        st.warning(str(e))
        return
    # Top row: collapsible run activity (left) + agent flow graph (right)
    col_activity, col_flow = st.columns([1.4, 1])
    with col_activity:
        if is_running(DATA_DIR, case_id):
            with st.status(f"Processing {case_id} — stage: {packet['stage']}",
                           state="running", expanded=True):
                st.caption("Live agent activity — streaming as it happens.")
                _render_event_feed(case_id, limit=15)
        with st.expander("📊 Run activity — how this case flowed", expanded=False):
            st.caption(f"Stage `{packet['stage']}` · full run history, oldest first.")
            _render_event_feed(case_id, limit=50, oldest_first=True)
    with col_flow:
        with st.container(border=True):
            _agent_flow_diagram(case_id)
    st.divider()

    with st.container(border=True):
        st.markdown("**📋 Extracted fields**")
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

    with st.container(border=True):
        st.markdown("**✅ Verification**")
        for c in packet["verification"].get("checks", []):
            st.write(f"`{c['status']:8s}` {c['check']}: {c['detail']}")
        conflicts = packet["verification"].get("conflicts", [])
        if conflicts:
            st.markdown("**Conflicts — analyst verification required**")
            st.caption("Both values are preserved with their sources. The registry "
                       "value is used in the packet, but an unresolved conflict is "
                       "never treated as verified information.")
            for cf in conflicts:
                st.error(f"CONFLICT {cf['reason_code']}: {cf['detail']}")
                if cf.get("submitted"):
                    st.write(f"- Submitted: **{cf['submitted']['value']}** "
                             f"_(source: {cf['submitted']['source']})_")
                    st.write(f"- Authoritative: **{cf['authoritative']['value']}** "
                             f"_(source: {cf['authoritative']['source']})_")
                    st.write(f"- Resolution: {cf['resolution']} "
                             f"(status: `{cf['status']}`)")

    with st.container(border=True):
        st.markdown("**📚 Legal research**")
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

    with st.container(border=True):
        st.markdown("**💰 ACV worksheet**")
        acv = packet["acv"]
        if acv:
            st.metric("Actual cash value", f"${acv['acv']:,}")
            for s in acv["worksheet"]["steps"]:
                st.write(f"`{s['amount']:+7d}` {s['name']} — {s['detail']}")
        else:
            st.write("_not computed_")

    with st.container(border=True):
        st.markdown("**📤 Customer outreach**")
        sent = [ev for ev in events.tail(DATA_DIR, case_id=packet["case_id"], limit=200)
                if ev["event"] == "email_sent"]
        if not sent:
            st.write("_no info-request emails sent yet_")
        for ev in sent:
            d = ev["detail"]
            with st.expander(f"📤 {ev['ts']} → {d.get('to')}", expanded=False):
                st.write(f"**Subject:** {d.get('subject')}")
                for n in d.get("needs", []):
                    st.write(f"- {n}")
                st.caption("The customer's reply (same subject) re-triggers the pipeline via the watcher.")


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
             f"returns used: {packet['return_count']}/{MAX_RETURNS}, "
             f"customer follow-ups: {packet['followup_count']}/3")
    ai = packet.get("ai_review") or {}
    if ai.get("last_approval"):
        appr = ai["last_approval"]
        st.caption("🤖 AI first-pass review: approved"
                   + (" (degraded — reviewer LLM was unavailable)"
                      if appr.get("degraded") else "")
                   + (f" — {appr['note']}" if appr.get("note") else ""))
    elif ai.get("returns"):
        last = ai.get("last") or {}
        st.caption(f"🤖 AI first-pass review: returned {ai['returns']}× "
                   f"(last: {', '.join(last.get('reason_codes', []))})")
    _show_flash()
    if packet["verification"].get("conflicts"):
        for cf in packet["verification"]["conflicts"]:
            st.error(f"{cf['reason_code']}: {cf['detail']}")

    # Always available — re-running a finished case is how you refresh the
    # agent-flow diagram and verify the pipeline after a code change.
    if st.button("↻ Re-run analyst pipeline (rework)"):
        with st.spinner("Re-running the multi-agent pipeline "
                        "(ingest → ground → reviewer)…"):
            try:
                out = run_case(case_id, data_dir=DATA_DIR)
            except Exception as e:  # noqa: BLE001 — surface it in the UI
                _flash("error", f"Pipeline failed: {e}")
                st.rerun()
                return
        _flash("success", f"Routed → **{out['next']}**")
        st.rerun()
    st.caption("Re-run re-executes the full pipeline on the current documents — use it "
               "after the customer sends missing files, or to refresh the agent-flow "
               "diagram. It does not reset the return count or change a review decision.")

    # Terminal states: nothing left to decide here.
    if packet["stage"] == "approved":
        st.success("✅ Approved — the case is complete. No further action needed.")
        return
    if packet["stage"] == "escalated":
        st.error("⬆️ Escalated to a human — the return budget is exhausted. "
                 "No further action in the UI.")
        return

    note = st.text_input("Reviewer note", "")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ Approve", type="primary"):
            with st.spinner("Recording approval…"):
                out = submit_review(case_id, "approve", note=note, data_dir=DATA_DIR)
            _flash("success", f"Case approved → stage `{out['stage']}`")
            st.rerun()
    with col2:
        reason = st.selectbox("Reason code", sorted(REASON_CODES),
                              format_func=lambda c: f"{c} — {REASON_CODES[c][1]}")
        if st.button("↩ Return to analyst"):
            with st.spinner("Returning to analyst…"):
                try:
                    out = submit_review(case_id, "return", reason_code=reason,
                                        note=note, data_dir=DATA_DIR)
                except Exception as e:  # noqa: BLE001 — surface it in the UI
                    _flash("error", f"Return failed: {e}")
                    st.rerun()
                    return
            _flash("warning", f"Returned ({out['return_count']}/{MAX_RETURNS}) → `{out['stage']}`")
            if out["promoted"]:
                _flash("info", f"🧠 {out['promotion_detail']}")
            if out["outreach"]["sent"]:
                _flash("info", f"📤 Customer emailed: {out['outreach']['reason']}")
            if out["stage"] == "escalated":
                _flash("error", "Return budget exhausted — escalated to a human. "
                                "The loop stops here by design.")
            st.rerun()


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
@st.fragment(run_every=2)
def tab_observability():
    """Module 6 observability: traces, costs, logs, guardrails for the case."""
    st.header("Observability")
    case_id = _need_case()
    if case_id is None:
        return
    st.caption("Module 6 — traces, token/cost, structured logs, and guardrail "
               "decisions for this case. Refreshes every 2s.")

    # -- Trace spans (timing per stage) -----------------------------------
    with st.container(border=True):
        st.markdown("**⏱️ Trace spans — timing per stage**")
        import json as _json
        traces = []
        tpath = os.path.join(DATA_DIR, "obs", "traces.jsonl")
        try:
            with open(tpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if r.get("case_id") == case_id:
                        traces.append(r)
        except FileNotFoundError:
            pass
        if not traces:
            st.info("No traces yet — run the pipeline to generate spans.")
        else:
            # Latest trace_id only (one run)
            latest_tid = traces[-1].get("trace_id")
            spans = [t for t in traces if t.get("trace_id") == latest_tid]
            st.caption(f"Trace `{latest_tid}` — {len(spans)} spans")
            st.dataframe([{
                "span": s.get("name"),
                "agent": s.get("agent"),
                "duration_ms": s.get("duration_ms"),
                "status": s.get("status"),
                "tokens": (s.get("attrs", {}).get("prompt_tokens", 0)
                           + s.get("attrs", {}).get("completion_tokens", 0)),
                "cost_usd": s.get("attrs", {}).get("est_cost_usd", 0),
            } for s in spans], width="stretch")
            tot_ms = sum(s.get("duration_ms", 0) for s in spans)
            tot_tok = sum(s.get("attrs", {}).get("prompt_tokens", 0)
                          + s.get("attrs", {}).get("completion_tokens", 0)
                          for s in spans)
            tot_cost = sum(s.get("attrs", {}).get("est_cost_usd", 0.0)
                           for s in spans)
            c1, c2, c3 = st.columns(3)
            c1.metric("Total time", f"{tot_ms:.0f} ms")
            c2.metric("Total tokens", f"{tot_tok:,}")
            c3.metric("Est. cost", f"${tot_cost:.4f}")

    # -- Guardrails --------------------------------------------------------
    with st.container(border=True):
        st.markdown("**🛡️ Guardrails — blocks and flags**")
        guard_evs = [e for e in events.tail(DATA_DIR, case_id=case_id, limit=500)
                     if e.get("event") == "guardrail"]
        if not guard_evs:
            st.info("No guardrail decisions recorded for this case.")
        else:
            for e in guard_evs:
                d = e.get("detail", {})
                action = d.get("action", "?")
                icon = "🔴" if action == "block" else "🟡" if action == "flag" else "🟢"
                st.write(f"{icon} `{d.get('layer')}/{d.get('name')}` — "
                         f"**{action}**: {d.get('detail')}")

    # -- Structured logs ---------------------------------------------------
    with st.container(border=True):
        st.markdown("**📝 Structured logs**")
        from settleready.obs.logging import read_logs
        level = st.selectbox("Level", ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"],
                             key="obs_log_level")
        logs = read_logs(DATA_DIR, case_id=case_id,
                         level=None if level == "ALL" else level, limit=100)
        if not logs:
            st.info("No logs for this case yet.")
        else:
            st.dataframe([{
                "time": time.strftime("%H:%M:%S", time.localtime(l["ts"])),
                "level": l.get("level"),
                "component": l.get("component"),
                "msg": l.get("msg"),
            } for l in logs], width="stretch")


def main() -> None:
    st.set_page_config(page_title="SettleReady", layout="wide")
    st.title("SettleReady — total-loss case preparation")
    st.caption("Synthetic demo data only. The agent prepares; the senior analyst decides.")

    if not os.path.exists(os.path.join(DATA_DIR, "statutes")):
        st.error("No environment found — run bash scripts/fresh.sh (or bash scripts/seed.sh) first.")
        return

    case_picker()  # global, always visible above the tabs, auto-refreshes

    tabs = st.tabs(["Emails", "Packet", "Review", "Audit trail", "Observability"])
    with tabs[0]:
        tab_emails()
    with tabs[1]:
        tab_packet()
    with tabs[2]:
        tab_review()
    with tabs[3]:
        tab_audit()
    with tabs[4]:
        tab_observability()


if __name__ == "__main__":
    main()
