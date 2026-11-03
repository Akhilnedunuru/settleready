"""Eval report generator (Module 6).

Combines deterministic metrics (eval/metrics.py), LLM-judge scores
(eval/judge.py), guardrail stats, and observability summaries into one
markdown report.

Run: PYTHONPATH=src python -m settleready.eval.report --data data --out eval-report.md
"""
from __future__ import annotations

import argparse
import json
import os
import time


def _load_jsonl(path: str, limit: int = 10000) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return out[-limit:]


def build_report(data_dir: str) -> str:
    """Assemble the markdown eval report from all Module 6 signals."""
    lines = []
    lines.append("# SettleReady Eval Report (Module 6)")
    lines.append("")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M %Z')}_")
    lines.append("")

    # -- Deterministic metrics -------------------------------------------
    lines.append("## Deterministic metrics")
    lines.append("")
    try:
        from settleready.eval.metrics import run_eval
        metrics = run_eval(data_dir)
        for name, val in metrics.items():
            lines.append(f"- **{name}**: {val}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"_Metrics unavailable: {e}_")
    lines.append("")

    # -- Observability: traces -------------------------------------------
    lines.append("## Observability — pipeline timing")
    lines.append("")
    traces = _load_jsonl(os.path.join(data_dir, "obs", "traces.jsonl"))
    if traces:
        by_name: dict[str, list[float]] = {}
        for t in traces:
            by_name.setdefault(t.get("name", "?"), []).append(
                t.get("duration_ms", 0))
        lines.append("| Stage | Calls | Avg ms | Max ms |")
        lines.append("|---|---|---|---|")
        for name in sorted(by_name):
            ds = by_name[name]
            lines.append(f"| {name} | {len(ds)} | "
                         f"{sum(ds)/len(ds):.0f} | {max(ds):.0f} |")
        # Token/cost totals from span attrs
        tot_tok = sum(t.get("attrs", {}).get("prompt_tokens", 0)
                      + t.get("attrs", {}).get("completion_tokens", 0)
                      for t in traces)
        tot_cost = sum(t.get("attrs", {}).get("est_cost_usd", 0.0)
                       for t in traces)
        lines.append("")
        lines.append(f"**Tokens**: ~{tot_tok:,} · **Est. cost**: ${tot_cost:.4f}")
    else:
        lines.append("_No traces recorded yet — run the pipeline with tracing enabled._")
    lines.append("")

    # -- Guardrails -------------------------------------------------------
    lines.append("## Guardrails")
    lines.append("")
    from settleready.memory import events
    guard_events = [e for e in events.tail(data_dir, limit=5000)
                    if e.get("event") == "guardrail"]
    if guard_events:
        by_action: dict[str, int] = {}
        for e in guard_events:
            a = e.get("detail", {}).get("action", "?")
            by_action[a] = by_action.get(a, 0) + 1
        for action in ("block", "flag", "pass"):
            if action in by_action:
                lines.append(f"- **{action}**: {by_action[action]}")
        blocks = [e for e in guard_events
                  if e.get("detail", {}).get("action") == "block"]
        if blocks:
            lines.append("")
            lines.append("Blocks (charter violations caught):")
            for e in blocks[-10:]:
                d = e.get("detail", {})
                lines.append(f"  - `{e.get('case_id')}` {d.get('name')}: {d.get('detail')}")
    else:
        lines.append("_No guardrail events recorded._")
    lines.append("")

    # -- Logs: errors -----------------------------------------------------
    lines.append("## Errors (from structured logs)")
    lines.append("")
    logs = _load_jsonl(os.path.join(data_dir, "obs", "pipeline.jsonl"))
    errors = [l for l in logs if l.get("level") == "ERROR"]
    if errors:
        for l in errors[-10:]:
            lines.append(f"- `{l.get('case_id')}` [{l.get('component')}] {l.get('msg')}")
    else:
        lines.append("_No errors logged._")
    lines.append("")

    lines.append("---")
    lines.append("_LLM-judge scores: run `python -m settleready.eval.judge "
                 "--data data --case <id>` per case and append._")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="eval-report.md")
    args = ap.parse_args()
    report = build_report(args.data)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
