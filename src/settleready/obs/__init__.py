"""Observability for SettleReady (Module 6).

Three pieces:
- tracing: trace_id per pipeline run + spans with timing for each stage/agent.
- logging: structured JSONL logs with levels, correlated by trace_id/case_id.
- cost:    token counting + cost estimation via a provider wrapper.

All three are stdlib-only and write to data/obs/ (gitignored).
"""
