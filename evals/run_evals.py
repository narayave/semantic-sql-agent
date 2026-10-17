#!/usr/bin/env python3
"""
Eval harness.

Scores execution accuracy and, more usefully, buckets every failure by cause.
The bucket distribution is the thing worth talking about: "62 percent pass, and
every miss was a semantic miss" tells you to fix the semantic layer. "Every miss
was a SQL error" tells you to fix the prompt or the schema tool. An aggregate
score on its own tells you nothing actionable.

Buckets
  pass           answer matched ground truth
  semantic_miss  wrong, and never looked up a term the case depends on
  sql_error      never produced a query that ran
  wrong_answer   query ran, result did not match
  no_answer      hit max steps, or declined when an answer existed
  bad_refusal    declined on an answerable question
  missed_refusal answered a question it should have declined

Usage
  python evals/run_evals.py --scripted        # offline, no API key, proves plumbing
  python evals/run_evals.py                   # real model
  python evals/run_evals.py --only vol_settled,count_active
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import duckdb
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import AnthropicLLM, ScriptedLLM  # noqa: E402
from agent.loop import run  # noqa: E402
from agent.tools import DB_PATH, Toolbox  # noqa: E402

GOLDEN = Path(__file__).parent / "golden.yaml"
TOL = 0.01  # relative tolerance for float comparison


def truth_value(sql: str):
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def cells(rows) -> list:
    return [c for r in (rows or []) for c in r]


def matches(got, want) -> bool:
    g, w = cells(got), cells(want)
    if len(g) != len(w) or not w:
        return False
    for a, b in zip(g, w):
        if a is None or b is None:
            if a is not b:
                return False
            continue
        try:
            fa, fb = float(a), float(b)
            if abs(fb) < 1e-9:
                if abs(fa) > 1e-9:
                    return False
            elif abs(fa - fb) / abs(fb) > TOL:
                return False
        except (TypeError, ValueError):
            if str(a).strip() != str(b).strip():
                return False
    return True


def classify(case: dict, tr, tb: Toolbox, want) -> str:
    expects_refusal = bool(case.get("expect_refusal"))

    if expects_refusal:
        if tr.stop_reason in ("gave_up", "max_steps"):
            return "pass"
        return "missed_refusal"

    if tr.stop_reason == "max_steps":
        return "no_answer"
    if tr.stop_reason == "gave_up":
        return "bad_refusal"

    if tb.last_rows is None:
        return "sql_error" if tb.sql_errors else "no_answer"

    if matches(tb.last_rows, want):
        return "pass"

    needed = {t.lower() for t in (case.get("requires_terms") or [])}
    looked = {t.lower() for t in tb.terms_looked_up}
    if needed and not needed.issubset(looked):
        return "semantic_miss"
    return "wrong_answer"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true",
                    help="offline adapter, no API key, exercises the plumbing")
    ap.add_argument("--only", default="", help="comma separated case ids")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--max-steps", type=int, default=8)
    args = ap.parse_args()

    cases = yaml.safe_load(GOLDEN.read_text())
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        cases = [c for c in cases if c["id"] in keep]

    buckets, rows = Counter(), []
    total_calls = total_repairs = 0

    for case in cases:
        want = truth_value(case["truth"]) if case.get("truth") else None
        tb = Toolbox()
        if args.scripted:
            # Offline: answer straight from the ground truth query so the
            # harness itself can be verified. Proves scoring, not the model.
            script = ([("lookup_term", {"term": t})]
                      for t in (case.get("requires_terms") or []))
            seq = list(script)
            if case.get("truth"):
                seq.append([("run_sql", {"sql": case["truth"]})])
                seq.append("Answered from the warehouse.")
            else:
                seq.append("I cannot answer that. There is no refunds table.")
            llm = ScriptedLLM(seq)
        else:
            llm = AnthropicLLM(model=args.model)

        tr = run(case["q"], llm, toolbox=tb, max_steps=args.max_steps)
        bucket = classify(case, tr, tb, want)
        buckets[bucket] += 1
        total_calls += tr.n_tool_calls
        total_repairs += tr.n_sql_errors
        tr.write(f"eval-{case['id']}")

        mark = "PASS" if bucket == "pass" else "FAIL"
        trap = " [trap]" if case.get("trap") else ""
        print(f"{mark:4}  {case['id']:<24} {bucket:<15}{trap}")
        rows.append({"id": case["id"], "bucket": bucket,
                     "tool_calls": tr.n_tool_calls,
                     "sql_errors": tr.n_sql_errors,
                     "trap": bool(case.get("trap")),
                     "terms_looked_up": tb.terms_looked_up,
                     "trace_id": tr.trace_id})

    n = len(cases)
    passed = buckets["pass"]
    traps = [r for r in rows if r["trap"]]
    trap_pass = sum(1 for r in traps if r["bucket"] == "pass")

    print("\n" + "=" * 52)
    print(f"execution accuracy   {passed}/{n}  ({100*passed/n:.0f}%)")
    if traps:
        print(f"  on trap cases      {trap_pass}/{len(traps)}  "
              f"({100*trap_pass/len(traps):.0f}%)")
    print(f"avg tool calls       {total_calls/n:.1f}")
    print(f"sql repairs needed   {total_repairs}")
    print("\nfailure buckets")
    for b, c in buckets.most_common():
        if b != "pass":
            print(f"  {b:<16} {c}")
    if passed == n:
        print("  none")

    out = Path(__file__).parent / "last_run.json"
    out.write_text(json.dumps(
        {"summary": dict(buckets), "n": n, "cases": rows}, indent=2))
    print(f"\nwrote {out}")
    return 0 if passed == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
