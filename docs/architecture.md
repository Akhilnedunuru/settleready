# SettleReady — System Architecture

**Project:** SettleReady — autonomous total-loss case preparation agent

This document describes the system architecture: what each component does, how data flows
between components, and the design decisions behind the structure. It is written for a
reader with no prior context on the project.

---

## 1. Architecture flowchart

```mermaid
flowchart TB
    CUST(["Customer"])
    SRANALYST(["Senior Analyst"])
    EXISTING["Mobile app + confirmation email<br/>(existing system — out of scope)"]

    CUST -->|"files total-loss claim"| EXISTING
    EXISTING -->|"claim record + email"| INBOX

    subgraph INTAKE["1 — INTAKE"]
        INBOX["Inbox Watcher<br/>polls inbox for new claim emails"]
        ROUTER{"Router<br/>total-loss claim?"}
        CLASSIFIER["Reply Classifier<br/>provides info vs. asks a question"]
        SENDER{"Sender check<br/>from stored claimant email?"}
        INBOX --> ROUTER
        ROUTER -- "reply" --> CLASSIFIER
        CLASSIFIER -- "provides info" --> SENDER
    end

    ROUTER -- "no → other queues" --> DONEQ["Straight-through / other queues<br/>(out of scope)"]
    ROUTER -- "yes" --> PARSE
    SENDER -- "mismatch" --> REJECT["Rejected — nothing stored<br/>sender_mismatch event"]
    SENDER -- "match" --> PARSE
    CLASSIFIER -- "asks a question" --> QSTAGE["customer_question stage<br/>analyst replies from the UI"]

    STATE[("Case Blackboard<br/>persistent case state<br/>docs · fields · confidence ·<br/>verification results · status")]

    subgraph EXTRACT["2 — DOCUMENT UNDERSTANDING"]
        PARSE["Doc Parser<br/>PDF / image parsing + OCR fallback"]
        FIELDEX["Field Extractor (LLM)<br/>VIN · mileage · title state ·<br/>lienholder · loss facts<br/>+ confidence per field"]
        FALLBACK["Deterministic Fallback<br/>regex extraction, confidence<br/>capped at 0.7"]
        PARSE --> FIELDEX
        FIELDEX -- "LLM unavailable" --> FALLBACK
    end
    FIELDEX -->|"writes fields"| STATE
    FALLBACK -->|"writes fields"| STATE

    subgraph VERIFY["3 — VERIFICATION (parallel fan-out)"]
        FANOUT["Fan-out dispatcher"]
        TDB[("State Title DB<br/>title brands · liens")]
        LIENAPI[("Lienholder Registry<br/>lien amounts")]
        CLUE[("CLUE Reports<br/>claim history")]
        COMPSVC[("Valuation Service<br/>comparable listings")]
        FANOUT --> TDB & LIENAPI & CLUE & COMPSVC
        CONFLICT{"Conflict Check<br/>extracted vs. verified<br/>records agree?"}
        TDB & LIENAPI & CLUE & COMPSVC --> CONFLICT
        CONFTHRESH{"Confidence Check<br/>all required fields ≥ 0.8?"}
        CONFLICT --> CONFTHRESH
    end
    STATE --> FANOUT
    CONFTHRESH -- "conflict or low confidence" --> DISPO

    subgraph KNOWLEDGE["4 — KNOWLEDGE (RAG)"]
        CORPUS[("Statute Corpus<br/>state total-loss laws<br/>+ valuation methodologies")]
        RETRIEVE["Retriever<br/>filter by state → vector search<br/>→ cited snippets"]
        CITECHECK["Citation Check<br/>every claim grounded in<br/>a retrieved passage"]
        CORPUS --> RETRIEVE
        RETRIEVE --> CITECHECK
    end
    CONFTHRESH -- "clean" --> RETRIEVE

    ACV["ACV Calculator (deterministic tool)<br/>mean of comps − $150 per full 10k miles<br/>over 50k − condition adjustment"]
    COMPSVC -->|"comparables"| ACV
    CITECHECK --> ACV

    subgraph DISPOSITION["5 — DISPOSITION PLANNER (non-clean cases only)"]
        GEN["Generator<br/>proposes up to 3 email groupings"]
        CRITIC["Critic<br/>scores each 0–10 against<br/>a fixed rubric"]
        CTRL["Controller<br/>beam search, width 3, depth 3<br/>keeps the best plan"]
        GEN --> CRITIC --> CTRL
    end
    DISPO --> GEN
    CTRL -- "best plan" --> OUTREACH
    CTRL -- "no plan scores ≥ 5<br/>or budget exhausted" --> ESC["escalated"]

    subgraph OUTREACH["6 — OUTREACH"]
        OUTREACH["Info-Request Sender<br/>one combined email for the<br/>remaining gaps"]
        BOUNCE{"Bounce / send failure?"}
        OUTREACH --> BOUNCE
        BOUNCE -- "yes" --> BFLAG["email_bounced / email_send_failed<br/>analyst corrects the address"]
        BOUNCE -- "no" --> NEEDS["needs_info — waits for<br/>customer reply"]
    end

    subgraph REVIEWER["7 — AI REVIEWER (clean cases only)"]
        VETO["Deterministic Vetoes<br/>structural failures that<br/>can never be approved"]
        LLMREV["LLM First-Pass<br/>one verdict: approve or return"]
        VETO --> LLMREV
        LLMREV -- "LLM fails or invalid verdict" --> ESC
        LLMREV -- "return (max 2)" --> REWORK["analyst_rework —<br/>pipeline re-runs"]
        LLMREV -- "approve" --> READY["ready_for_review"]
    end
    ACV --> VETO
    REWORK --> PARSE

    subgraph HUMAN["8 — HUMAN REVIEW"]
        UI["Review UI (Streamlit)<br/>packet view + full audit trail"]
        DECISION{"Senior analyst<br/>decision"}
        UI --> DECISION
    end
    READY --> UI
    DECISION -- "approve" --> FINAL[("Case Record<br/>settlement-ready")]
    DECISION -- "return + reason code (max 2)" --> REWORK

    subgraph CROSS["CROSS-CUTTING SERVICES"]
        AUDIT[("Audit Log<br/>append-only: every action + its source")]
        PII["PII Redactor<br/>masks SSNs, card numbers,<br/>dates of birth before storage"]
        LLM["LLM Provider Interface<br/>pluggable model backend<br/>with retry + backoff"]
        OBS["Observability<br/>traces · token/cost metering ·<br/>structured logs · metrics"]
        GUARD["Guardrails<br/>prompt-injection block ·<br/>PII flag · schema + confidence checks"]
    end
    FIELDEX -.-> LLM
    GEN -.-> LLM
    CRITIC -.-> LLM
    LLMREV -.-> LLM
    RETRIEVE -.-> LLM
    PARSE -.-> AUDIT
    FIELDEX -.-> AUDIT
    CONFLICT -.-> AUDIT
    ACV -.-> AUDIT
    DECISION -.-> AUDIT
    SENDER -.-> AUDIT
    SRANALYST --> UI
```

---

## 2. Component glossary

| # | Component | What it does | Key inputs → outputs |
|---|-----------|--------------|----------------------|
| 1 | Inbox Watcher | Polls the inbox for new claim emails and attachments; marks each email seen only after successful processing, so a failed run retries on the next poll | Raw emails + PDFs → normalized intake event |
| 2 | Router | Classifies the claim; total-loss goes to the pipeline, everything else to other queues; new-thread emails for an existing case route as replies, not duplicates | Intake event → routed claim or out-of-scope |
| 3 | Reply Classifier | Hybrid classifier (keyword fast paths + LLM fallback) that sorts customer replies into `provides_info` or `asks_question`. Questions go to the `customer_question` stage — no pipeline re-run, no re-send — and the analyst answers from the UI | Reply text + attachments → classification + reason |
| 4 | Sender Check | Compares the reply sender against the stored claimant email, before anything is logged or stored. A mismatch is rejected with a `sender_mismatch` event. Gated on `SETTLEREADY_ENFORCE_SENDER_CHECK` (off by default for demo; when off, replies are accepted but a `sender_unverified` event is logged) | Reply sender + stored claimant → accept or reject |
| 5 | Doc Parser | Extracts text from PDFs/images, with OCR fallback for scans; preserves table layout | Attachments → text + layout blocks |
| 6 | Field Extractor (LLM) | Pulls 8 required fields (VIN, mileage, title state, policy number, holder name, loss type, loss date, lien declared) plus the lienholder name, each with a confidence score and source span | Parsed text → fields + confidence + provenance |
| 7 | Deterministic Fallback | Regex extraction that runs only when the LLM produces nothing at all. Confidence is capped at 0.7 (below the 0.8 threshold), so every field it touches still routes to human verification | Parsed text → fields at 0.7 confidence |
| 8 | Case Blackboard | The system's persistent memory: every stage reads and writes the current case state | Writes from all stages → case snapshot |
| 9 | Verification fan-out | Queries four sources in parallel: title DB, lienholder registry, CLUE reports, valuation service | VIN / policy number → verified records |
| 10 | Conflict Check | Compares extracted vs. verified records. A mismatch is preserved as a structured conflict — both values, both sources, status `unresolved` — never silently resolved by picking one. An unresolved conflict blocks `ready_for_review` | Both record sets → conflicts + flags |
| 11 | Confidence Check | Every required field must be at or above 0.8 confidence. Any field below threshold fails verification, routing the case to rework | Fields + confidences → pass or fail |
| 12 | Statute Corpus + Retriever | Chunked, embedded state total-loss laws; retrieval is filtered by state *before* vector search, and returns cited snippets | Title state → cited legal snippets |
| 13 | Citation Check | Deterministic string matching that verifies every legal claim in a synthesized answer is grounded in a retrieved passage. Runs on every answer | Answer + citations + chunks → pass or fail |
| 14 | ACV Calculator | Deterministic math only: mean of comparables − $150 per full 10k miles over 50k − condition adjustment (fair −$300, poor −$800) | Comparables + vehicle facts → ACV + worksheet |
| 15 | Disposition Planner | For non-clean cases only: a beam search (width 3, depth 3) over email groupings. A generator proposes candidate emails, a critic scores each 0–10 against a fixed rubric (coverage, budget efficiency, actor correctness, grounding), and a controller keeps the best plan. The first email of the best plan is what gets sent — one combined email beats two single-ask emails on budget. If no plan scores 5 or above, or the follow-up budget is exhausted, the case escalates. Clean cases skip it entirely | Verification gaps → best email plan or escalation |
| 16 | Outreach | Sends the info-request email for the remaining gaps (max 3 follow-ups, deduped by need-set so re-asking the same thing doesn't burn budget). A bounce or send failure flags the case (`email_bounced` / `email_send_failed`) for a human to correct the address, instead of leaving it waiting for a reply to an email that never arrived | Gaps + claimant email → sent email or flagged case |
| 17 | AI Reviewer | Clean cases only. Deterministic vetoes first (structural failures that can never be approved), then one LLM first-pass verdict. If the model fails or returns an invalid verdict, the case escalates — it never approves by default. Max 2 AI returns, then escalation | Packet → approve, return, or escalate |
| 18 | Review UI | Streamlit app: packet view, audit trail, approve / return-with-reason-codes, pipeline re-run, customer-question replies, email-address correction. Approve is only offered when the case is `ready_for_review` with no unresolved conflicts and a computed ACV | Packet → analyst decision |
| 19 | Audit Log | Append-only record of every action and the source it came from. PII is redacted before any entry is written | All stages → traceable history |
| 20 | PII Redactor | Masks SSNs, card numbers, and dates of birth before storage — in customer statements, event bodies, extraction spans, and analyst-reply audit text | Raw text → redacted text |
| 21 | LLM Provider Interface | Pluggable model backend (OpenAI live, Ollama stub) with retry and exponential backoff on transient errors; auth errors and bad requests fail fast | Prompts → completions |
| 22 | Return Tally Store | Persistent tally of reviewer returns by (reason_code, field, title_state); at 3 returns the check is promoted to a mandatory upfront verification, rendered first in the next report | Return events → tallies; title_state → promoted check list |
| 23 | Observability | Per-run traces with timing and token/cost metering, structured JSON logs, guardrail decisions, and an aggregate metrics dashboard (total runs, failure rate, cost across all cases) | Pipeline runs → traces, logs, metrics |
| 24 | Guardrails | Input screening (prompt-injection block, PII flag) on customer text before the agent acts on it; output screening (extraction schema, confidence) on what the agent produces | Text in/out → pass, flag, or block |

---

## 3. Data flow — one claim, end to end

Traced example: **Claim #TL-2026-1043** — a 2019 Honda Accord reported stolen in Texas.

1. The customer files the claim in the mobile app; the existing system sends the confirmation email. (Out of scope — the pipeline starts here.)
2. The **Inbox Watcher** picks up the claim email with a police report PDF attached.
3. The **Router** classifies the loss type as theft → total-loss → enters the pipeline.
4. The **Doc Parser** extracts the PDF text; the **Field Extractor** pulls VIN, mileage (92,000), title state (TX), lienholder, and theft date — each with a confidence score and the exact source span — and writes them to the **Case Blackboard**.
5. **Verification fan-out** runs four queries in parallel: Title DB → clean, no brands; Lienholder Registry → lien of $4,200 with Capital Auto Finance; CLUE → no prior total losses; Valuation Service → comparable 2019 Accords.
6. The **Conflict Check** spots a mismatch — the claimant declared "no lien" but the registry reports $4,200. Both values are preserved as a structured conflict (`UNDECLARED_LIEN`, status `unresolved`); neither is silently overwritten. The case also has a missing title document.
7. Because the case is non-clean, the **Disposition Planner** runs a beam search over email groupings and selects one combined email: confirm the lien + send the title document.
8. **Outreach** sends the email (follow-up 1 of 3). The customer replies confirming the lien ("Yes, the lienholder is Capital Auto Finance, payoff $4,200") — the **Reply Classifier** marks it `provides_info`, the **Sender Check** confirms the address, and the pipeline re-runs. The title gap is still open, so the planner selects a title-only follow-up.
9. Once the title arrives and verification passes, the **Retriever** filters the statute corpus to Texas and returns the TX total-loss threshold and valuation method, with citations. The **Citation Check** verifies every legal claim is grounded.
10. The **ACV Calculator** computes: comparables average $18,400, mileage adjustment −$600 (92,000 miles = 4 full 10k over 50k × $150), condition $0 → **ACV $17,800**, with a full worksheet.
11. The **AI Reviewer** runs its deterministic vetoes (none fire), then its LLM first-pass — approve. The case routes to `ready_for_review`.
12. In the **Review UI**, the senior analyst sees the packet plus the complete audit trail. She approves → the **Case Record** is marked settlement-ready. The **Audit Log** retains every step, source, and decision.

---

## 4. Key design decisions

- **Blackboard pattern for state.** All agents read and write one shared case state instead of calling each other directly. This keeps agents decoupled and makes the review loop trivial (a return just updates state and re-triggers stages).
- **Conflicts are preserved, never resolved by picking.** On any mismatch, both values stay recorded with their sources and status `unresolved`. A case carrying an unresolved conflict can never be `ready_for_review`. The analyst decides; the agent never silently picks a winner.
- **Mocks behind interfaces.** Every external system — inbox, title DB, lienholder, CLUE, valuation, email — implements a narrow interface. The MVP ships mocks; swapping in a real connector later is configuration, not a rewrite.
- **Money math never touches the LLM.** ACV is computed by a deterministic tool; the LLM narrates and assembles but never invents numbers. This is the single most important guardrail in a system whose outputs have legal weight.
- **State-filtered RAG.** The retriever filters by jurisdiction *before* vector search. Retrieving the wrong state's law is the most dangerous silent failure in this design — the output looks confident and is wrong — so it is eliminated structurally, not hoped away.
- **Fail closed, not open.** When the reviewer model fails, when extraction confidence is low, when the LLM is down — the case goes to a human. A guessed disposition is the failure; an escalation is a successful outcome.
- **Structured returns, not free text.** The reviewer returns cases with reason codes, not prose. This keeps the rework loop machine-actionable: each code maps to a known re-execution plan.
- **Learning without retraining:** a return tally table logs every reviewer return against (reason code, field, title state); at 3 returns the check is promoted to a mandatory upfront verification (e.g., GA undeclared liens → lien check runs first on every Georgia case).
- **Budgets are hard caps.** 3 customer follow-ups, 2 analyst returns, 2 AI-reviewer returns. When one is exhausted, the case escalates. The system never stretches, borrows, or resets a budget.
- **Audit-first.** Every write to the Blackboard carries its source, and PII is redacted before anything is stored. The audit log isn't a debugging aid — it's the reason a human can trust the packet.
