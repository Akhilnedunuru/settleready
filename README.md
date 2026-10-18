# SettleReady

**Autonomous total-loss case-preparation agent** — capstone project (Personal Assistant track).

Insurance total-loss claims (stolen, fire-destroyed, or irreparably damaged vehicles) always route
to analysts: title checks, lien verification, actual-cash-value computation, and state-specific
total-loss thresholds, assembled into a case file for senior-analyst review. Done manually across
emails, PDFs, and database portals, it is slow and error-prone. SettleReady automates the
preparation pipeline — it never finalizes a case without human approval.

## How it works (7 stages)

1. **Intake** — claim email + attachments arrive; router sends total-loss claims into the pipeline
2. **Extract** — parse PDFs (OCR fallback); LLM pulls fields with confidence scores + source spans
3. **Completeness check** — required-document checklist; gaps → drafted customer email → re-ingest loop
4. **Verify** — parallel mock lookups: title DB, lienholder, CLUE, valuation comps; conflicts resolved
5. **Knowledge (RAG)** — retrieve *this state's* total-loss law (jurisdiction filter before vector search), cited
6. **Compute & assemble** — deterministic ACV math + LLM-assembled cited packet
7. **Human review** — senior analyst approves → settlement-ready, or returns with reason codes → rework loop

Cross-cutting: **Case Blackboard** (persistent case memory), **return tally table** (learning without
retraining), append-only **audit log**, PII redaction, pluggable LLM provider.
Full detail: [`docs/architecture.md`](docs/architecture.md).

## Quickstart

```bash
git clone https://github.com/Akhilnedunuru/settleready.git
cd settleready

./scripts/setup.sh          # checks Python 3.11, creates venv, installs deps
cp .env.example .env        # paste your OPENAI_API_KEY (LangSmith key optional)

.venv/bin/python scripts/smoke_test.py  # verifies LLM + embeddings work (~$0.01)
./scripts/seed.sh             # generates synthetic fixtures (deterministic)
./scripts/build_index.sh      # builds the local Chroma RAG index

export PYTHONPATH="$PWD/src"
.venv/bin/python -m settleready.pipeline --case-id TL-2026-1044   # full pipeline, one case
.venv/bin/python -m settleready.eval.metrics                     # scores vs. fixture ground truth
streamlit run src/settleready/ui/app.py   # demo + analyst review UI
```

## Real-world email intake (no "Run" button)

The intake watcher polls the inbox and fires the pipeline automatically when a
claim email arrives. The mock JSON inbox is the default; set
`SETTLEREADY_EMAIL_SOURCE=gmail` (+ `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in
`.env`) to process real emails via IMAP/SMTP — free, stdlib only, no new
dependencies. Either way the agents only ever see the `EmailClient` protocol.

```bash
export PYTHONPATH="$PWD/src"
# terminal 1: the daemon (polls every 15s)
.venv/bin/python -m settleready.agents.intake watch
# terminal 2: simulate a claim email arriving (demo transport)
.venv/bin/python -m settleready.agents.intake demo-claim --case-id TL-2026-1045
# single poll (no daemon)
.venv/bin/python -m settleready.agents.intake once
```

Email contract: the subject must contain the case ID (`TL-2026-1045`); customer
replies must start with `Re:` and carry the case ID so they're correlated to the
open case. PDF attachments are saved to `data/pdfs/<case_id>/` before the
pipeline runs.

## Repository layout

```
scripts/          one-command setup, seeding, indexing, smoke test
src/settleready/
  llm/            provider interface (OpenAI live, Ollama stub — swap via config)
  agents/         intake, extraction, verification, builder  (module 4: LangGraph)
  tools/          email client, PDF parser, mock DBs, RAG retriever, ACV calculator
  memory/         Case Blackboard, return tally store
  data/           fixture generator (synthetic PDFs + mock records, seeded)
  eval/           metrics + labeled fixtures (module 5)
  ui/             Streamlit demo + review screen
  pipeline.py     wires every module together (final integration)
notebooks/        per-module exploration before productionizing
tests/            interface + regression tests
docs/             architecture, checkpoint writeups
data/             generated at runtime (gitignored): fixtures, chroma index, inbox/outbox
```

## Course module → code map

| Checkpoint | Build | Lives in |
|---|---|---|
| 1 · Concept | scaffold, fixture generator | `scripts/`, `src/settleready/data/` |
| 2 · Reasoning / memory / tools | ReAct loop, tool interfaces, Blackboard | `src/settleready/llm/`, `tools/`, `memory/` |
| 3 · Retrieval / RAG | statute corpus + state-filtered retriever | `src/settleready/tools/rag.py`, `data/` |
| 4 · Multi-agent | LangGraph orchestration + review loop | `src/settleready/agents/`, `pipeline.py` |
| 5 · Eval / guardrails | eval harness, PII redaction, audit log, UI | `src/settleready/eval/`, `ui/` |

## Environment variables

See `.env.example`. Only `OPENAI_API_KEY` is required. Everything else has sane defaults;
set `SETTLEREADY_PROVIDER=ollama` to use a local model once the Ollama provider is implemented.

## Cost

A full pipeline run on GPT-4o-mini costs roughly **$0.02–0.05**. Set a $10/month cap in the
OpenAI dashboard and you'll never think about it again.

## Data & privacy

All case data in this repo is **synthetic**. No real customer PII, ever — per program rules.
External systems (title DB, lienholder, CLUE, valuation, email) are mocked behind interfaces;
swapping a mock for a real connector is configuration, not a rewrite.
