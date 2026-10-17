#!/usr/bin/env python3
"""
Ask the agent a question.

  python run.py "What is the chargeback rate for high risk merchants?"
  python run.py --demo           # offline, no API key, shows the repair loop
"""

from __future__ import annotations

import argparse

from agent.llm import AnthropicLLM, ScriptedLLM
from agent.loop import run

DEMO_SCRIPT = [
    [("list_tables", {})],
    [("get_schema", {"table": "txns"})],
    [("lookup_term", {"term": "settled volume"})],
    # Deliberately wrong on purpose: wrong column, and it ignores the casing
    # drift. The point of the demo is watching it recover.
    [("run_sql", {"sql": "SELECT SUM(amount) FROM txns WHERE status = 'settled'"})],
    [("run_sql", {"sql": "SELECT ROUND(SUM(txn_amt_cents)/100.0, 2) AS settled_volume_usd "
                         "FROM txns WHERE LOWER(status) = 'settled'"})],
    "Settled volume is the figure above, in dollars. That counts settled "
    "transactions only, matched case insensitively because the status column "
    "has casing drift, and converts from cents.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default="")
    ap.add_argument("--demo", action="store_true",
                    help="offline scripted run, no API key needed")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.demo:
        q = args.question or "What is total settled volume?"
        llm = ScriptedLLM(DEMO_SCRIPT)
    else:
        if not args.question:
            ap.error("give a question, or pass --demo")
        q = args.question
        llm = AnthropicLLM(model=args.model)

    print(f"Q: {q}\n")
    tr = run(q, llm, max_steps=args.max_steps, verbose=not args.quiet)
    print("\n" + "-" * 52)
    print(tr.answer or "(no answer)")
    print("-" * 52)
    print(f"stop={tr.stop_reason}  tool_calls={tr.n_tool_calls}  "
          f"sql_repairs={tr.n_sql_errors}")
    print(f"trace -> {tr.write('run')}")


if __name__ == "__main__":
    main()
