# SettleReady

**Autonomous total-loss case-preparation agent.**

When a vehicle is stolen, burned, or damaged beyond repair, the claim always routes to an analyst: check the title, verify the lienholder, confirm the claims history, research the state's total-loss law, compute the actual cash value, and assemble a case file for a senior analyst to review. Done manually across emails, PDFs, and database portals, that work is slow and error-prone. SettleReady automates the preparation — it never finalizes a case on its own. A human always makes the final call.

## The problem

Total-loss preparation is a chain of lookups and cross-checks, and every link can fail quietly:

- A lien the claimant didn't declare changes the settlement entirely.
- A title brand (salvage, flood) the form doesn't mention invalidates the valuation.
- A confidently wrong field — a mistyped VIN, a low-confidence extraction — looks right until someone pays on it.
- A customer reply from the wrong address, or a follow-up email that bounced, can strand a case with no one noticing.

SettleReady is built around one idea: **every step is checked by a different mechanism, and no single failure reaches the customer.** Extraction is verified against registries. Plans are scored by a critic before they're sent. The reviewer is checked by a human. And when any layer can't do its job — the model is down, confidence is low, the budget is spent — the case goes to a human instead of failing silently.

## Architecture

Full detail — flowchart, component glossary, traced example, and design decisions — in [`docs/architecture.md`](docs/architecture.md).

```
Claim email + PDFs
      │
      ▼
   Intake ─────────► Match the email to its case (case ID in the subject).
      │              Sort replies into "new information" vs. "customer
      │              question". Save attachments (PDF/images, up to 10 MB).
      ▼
 Ingestion ────────► Read the documents and pull out the 8 required facts.
      │
      ▼
 Grounding ────────► Check the facts against the official registries.
      │              If everything checks out, also research the state
      │              law and compute what the car was worth.
      │
      ├── NOT clean ─► Disposition planner ─► Outreach
      │                  (plans ONE combined      (sends the follow-up email;
      │                   follow-up email)         the case then waits)
      │                                                │
      │                  customer replies ─────────────┘
      │                  (the reply goes back to Intake and the
      │                   case is processed again)
      │
      └── clean ─► AI Reviewer ─► Human review
                      (fixed-rule vetoes    (senior analyst approves
                       first, then an        or returns the case)
                       AI first pass)
```

**What each step does, in plain words**

1. **Intake.** An email arrives (the watcher checks the inbox every 65 seconds, and the "Check inbox now" button in the UI runs the same check immediately). Intake works out which case the email belongs to, saves any attachments, and sorts replies: a reply that provides information re-runs the case; a reply that asks a question is flagged for the analyst instead, so it never triggers pointless rework. Replies are also checked against the claimant's email address — see *Sender verification* below for how strict that check is.
2. **Ingestion (extraction).** Reads the claim form and the customer's documents and pulls out the 8 required facts: VIN, mileage, title state, policy number, holder name, loss type, loss date, and whether a loan on the car was declared. Every fact carries a confidence score and a pointer to where it was found. If the AI model is unavailable, a simpler pattern-matching backup extracts what it can, and its confidence is capped at 0.7.
3. **Grounding (checking, then research and value).** First, the facts are checked against the official records: the title registry, the lienholder registry (a lien is money still owed on the car to a bank or lender), the CLUE claims-history database, and price comparables for similar cars. If the form and a registry disagree, **both** values are kept side by side as a conflict — nothing is silently overwritten. Any required fact with confidence below 0.8 also fails this step. Only when the checks pass does Grounding continue: it looks up the state's total-loss law (the search can only return that state's law, and the law must have been in force on the loss date), checks every legal statement against the passages it came from, and computes the car's value (the ACV) using fixed math — never the AI model — then independently re-checks that math.
4. **Disposition planner.** Only for cases that are **not** clean. It plans the follow-up email to the customer using a small search: a generator proposes ways to group the missing items into emails, a critic scores each plan against a fixed rubric, and the best plan wins. One combined email beats two separate asks. Only the first email of the plan is sent; the rest is re-planned when the customer replies.
5. **Outreach.** Sends the follow-up email. Hard limits: at most 3 follow-ups per case, and the same need is never asked twice. If an email bounces or fails to send, the case is flagged for a human instead of being left waiting.
6. **AI Reviewer.** Only sees clean cases. It applies fixed-rule vetoes first (for example: a value worksheet that doesn't recompute correctly can never pass), then an AI first pass. If the AI model fails or answers in a format it can't use, the case is escalated to a human — the reviewer never approves by default. It can return a case for rework at most 2 times.
7. **Human review.** A senior analyst makes the final call. They can approve only a case that is `ready_for_review`, has no unresolved conflicts, and has a computed value. They can return a case with a reason code at most 2 times; after that the case escalates. **The system prepares cases — it never settles one on its own.**

**Supporting systems**

- **Case Blackboard** — per-case memory: fields, checks, conflicts, decisions, all with evidence. Append-only audit log.
- **Return tally** — episodic memory. At 3 returns with the same state and reason, that check is promoted to a mandatory upfront verification, rendered first in the next report.
- **Guardrails** — PII redaction before storage (SSNs, card numbers, dates of birth), prompt-injection blocking on inbound text, confidence and schema checks on outbound extraction, sender verification on replies.
- **Observability** — per-run traces with timing and token/cost metering, structured JSON logs, guardrail decisions, and an aggregate metrics dashboard (total runs, failure rate, cost).
- **Deterministic ACV** — money math never goes through the LLM: mean of comparables − $150 per full 10k miles over 50k − condition adjustment (fair −$300, poor −$800). Comparable prices are retained so Grounding and the AI Reviewer can independently recompute and validate the worksheet.
- **Statute provenance** — every statute chunk carries jurisdiction, version, currency status, effective interval, and review date. Research declines unless the provision is current and was effective on the case's loss date.

**Tech stack:** Python 3.11 · LangGraph (multi-agent orchestration + disposition planner) · GPT-4o-mini via a pluggable provider interface · Chroma (local vector store) · pdfplumber / pypdf / Tesseract OCR · Pydantic · Streamlit UI · SQLite (Case Blackboard + audit log).

## Setup

```bash
git clone https://github.com/Akhilnedunuru/settleready.git
cd settleready

bash scripts/setup.sh          # checks Python 3.11, creates venv, installs deps
cp .env.example .env           # paste your OPENAI_API_KEY
```

Only `OPENAI_API_KEY` is required. Everything else has sane defaults. Set a $10/month cap in the OpenAI dashboard — a full pipeline run costs roughly $0.02–0.06.

### Data setup

All case data is **synthetic** — no real customer PII, ever. External systems (title DB, lienholder registry, CLUE, valuation, email) are mocked behind interfaces, so swapping a mock for a real connector is configuration, not a rewrite.

Two ways to populate the app:

**A. Seeded demo (recommended first run).** Loads deterministic synthetic cases with ground truth, a seeded inbox email, and seeded return-tally history.

```bash
bash scripts/demo_reset.sh 15   # wipe state, load 15 synthetic cases, rebuild the RAG index
```

**B. Fresh start (real-world flow).** Only the environment is generated (statute corpus + mock registry records). The app boots empty; cases are created by the intake watcher as claim emails arrive.

```bash
bash scripts/fresh.sh            # environment only, no demo fixtures
bash scripts/build_index.sh      # builds the local Chroma RAG index
```

The individual steps, if you want them separately:

```bash
bash scripts/seed.sh [N]         # generate N synthetic cases (default 3)
bash scripts/build_index.sh      # build the Chroma RAG index from the statute corpus
bash scripts/gen_customer_docs.sh  # pre-generate the documents a customer would send back
```

## Usage

### Start the app

```bash
bash scripts/start.sh
```

This starts two processes in one terminal: the **intake watcher** (polls the inbox, fires the pipeline when a claim email arrives) and the **Streamlit UI** (the analyst workbench). To stop them reliably, run `bash scripts/stop.sh` from a second terminal — it terminates both by name (Ctrl-C can leave the background watcher running).

Or run the pieces individually:

```bash
export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.agents.intake watch   # watcher daemon
streamlit run src/settleready/ui/app.py               # UI only
.venv/bin/python -m settleready.pipeline --case-id TL-2026-1044  # one case, CLI
```

### The UI — five tabs

- **Case packet** — the prepared case: extracted fields, verification report, legal research, ACV worksheet, agent-flow diagram, and run activity. If the case is waiting on a customer question, reply from here. If an email bounced or failed to send, correct the claimant address here.
- **Review** — senior analyst decision: approve (only when the case is `ready_for_review` with no unresolved conflicts and a computed ACV) or return with a reason code. Also re-runs the pipeline.
- **Emails** — every inbound and outbound email for the case, grouped oldest-first, with attachment status.
- **Audit** — the append-only event log: every stage transition, tool call, guardrail decision, and return, with timestamps.
- **Observability** — per-run traces (timing, tokens, cost), structured logs, guardrail decisions, and aggregate metrics across all cases.

### Email intake

The watcher polls the inbox and fires the pipeline automatically — there is no "Run" button. The mock JSON inbox is the default. Set `SETTLEREADY_EMAIL_SOURCE=gmail` (+ `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in `.env`) to process real emails via IMAP/SMTP.

**Email contract:** the subject must contain the case ID (`TL-2026-1045`). Customer replies must start with `Re:` and carry the case ID so they're correlated to the open case. A reply from an address other than the stored claimant email is rejected when sender verification is on (see below). PDF and image attachments are saved to `data/pdfs/<case_id>/` before the pipeline runs.

To simulate a claim email arriving (demo transport):

```bash
.venv/bin/python -m settleready.agents.intake demo-claim --case-id TL-2026-1045
.venv/bin/python -m settleready.agents.intake once   # single poll, no daemon
```

### The demo cases

| Case | Scenario | Expected outcome |
|---|---|---|
| TL-2026-1043 | TX theft, undeclared lien + missing title | `needs_info` — the kitchen-sink non-clean case |
| TL-2026-1044 | GA fire, clean | `ready_for_review` — the happy path |
| TL-2026-1045 | TX collision, lien declared + matching | `ready_for_review` (a warning still passes) |
| TL-2026-1046+ | Generated, cycling 4 archetypes | clean · undeclared lien · missing title · lienholder mismatch |

### Budgets and limits

These are hard caps, not suggestions — when one is exhausted, the case escalates to a human:

- **3** customer follow-ups (deduped by need-set, so re-asking the same thing doesn't burn budget)
- **2** analyst returns
- **2** AI-reviewer returns
- **0.8** confidence threshold — any required field below it fails verification
- **10 MB** per attachment; supported types: PDF, PNG, JPG, JPEG, TIFF, BMP

### Sender verification (optional)

By default, sender verification is **off** so anyone can test the reply flow from their own email. Every reply still emits a `sender_unverified` event in the audit log. To enforce it — a reply is accepted only from the stored claimant email, and anything else is rejected before anything is stored — set in `.env`:

```
SETTLEREADY_ENFORCE_SENDER_CHECK=1
```

### LangSmith tracing (optional)

The project already has built-in observability — the Observability tab shows per-run traces, token/cost, structured logs, and guardrail decisions without any extra setup. LangSmith is an optional add-on: a hosted dashboard from the LangChain team that shows the same agent runs as a visual trace tree (every Thought → Action → Observation step, with timing and token usage per node). Useful for debugging a tricky run or showing the agent's reasoning in a demo.

It's free for individual use. To enable it:

1. Create an account at [smith.langchain.com](https://smith.langchain.com) and copy your API key.
2. Add these to `.env`:

```
LANGSMITH_API_KEY=lsv2_pt_your-key-here
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=settleready
```

Both the key **and** `LANGCHAIN_TRACING_V2=true` are required — the key alone does nothing. Traces appear in the LangSmith dashboard under the `settleready` project. Remove or unset the key to turn it off; nothing in the project depends on it.

### Evaluation

```bash
.venv/bin/python -m settleready.eval.metrics   # scores pipeline output vs. fixture ground truth
bash scripts/run_evals.sh                     # full eval suite (metrics + LLM-judge + adversarial)
```

The adversarial eval covers 8 hostile scenarios (spoofed replies, prompt injection, conflicting documents, missing data) where the correct behavior is safe failure, not a confident answer.

## Repository layout

```
scripts/          one-command setup, seeding, indexing, smoke test, start/stop
src/settleready/
  llm/            provider interface (OpenAI live, Ollama stub — swap via config)
  agents/         ingestion, grounding, disposition planner, outreach, reviewer,
                  and graph.py — the LangGraph multi-agent orchestration
                  (with a plain-loop fallback if LangGraph is unavailable)
  tools/          email client, PDF parser, mock DBs, RAG retriever, ACV calculator
  memory/         Case Blackboard, return tally store, event bus
  data/           fixture generator (synthetic PDFs + mock records)
  eval/           metrics, LLM-judge, adversarial scenarios
  obs/            tracing, structured logging, cost metering
  guardrails/     input/output screening, PII redaction
  ui/             Streamlit analyst workbench
  pipeline.py     wires every module together
tests/            184 tests (interface + regression)
docs/             architecture deep-dive
data/             generated at runtime (gitignored): fixtures, Chroma index, inbox/outbox
```

## Environment variables

See `.env.example`. Only `OPENAI_API_KEY` is required. Notable options:

| Variable | Default | What it does |
|---|---|---|
| `SETTLEREADY_PROVIDER` | `openai` | `openai` or `ollama` (local model, stub) |
| `SETTLEREADY_CHAT_MODEL` | `gpt-4o-mini` | Chat model for extraction, planning, review |
| `SETTLEREADY_EMBED_MODEL` | `text-embedding-3-small` | Embedding model for the statute index |
| `SETTLEREADY_EMAIL_SOURCE` | `mock` | `mock`, `relay` (shared demo mailbox), or `gmail` |
| `SETTLEREADY_ENFORCE_SENDER_CHECK` | unset (off) | Set to `1` to reject replies from non-claimant addresses |
| `SETTLEREADY_LOG_LEVEL` | `INFO` | Structured log verbosity |
| `LANGSMITH_API_KEY` | unset | Optional hosted tracing dashboard (see LangSmith section above); also needs `LANGCHAIN_TRACING_V2=true` |

## Data & privacy

All case data in this repo is **synthetic**. No real customer PII, ever. PII shapes (SSNs, card numbers, dates of birth) are redacted before storage, and the audit log never contains raw values.
