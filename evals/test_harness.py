#!/usr/bin/env python3
"""
Negative tests for the eval harness.

A scorer that only ever sees passing runs has never been tested. These drive
deliberately broken agents through the classifier and assert the failure lands
in the right bucket. Run with: python evals/test_harness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm import ScriptedLLM  # noqa: E402
from agent.loop import run  # noqa: E402
from agent.tools import Toolbox  # noqa: E402
from evals.run_evals import classify, truth_value  # noqa: E402

CASE = {
    "id": "count_active",
    "q": "How many active merchants are there?",
    "truth": "SELECT COUNT(*) FROM merchants WHERE closed_dt IS NULL",
    "requires_terms": ["active merchant"],
}
WANT = truth_value(CASE["truth"])

FAILS = 0


def check(name: str, got: str, expected: str) -> None:
    global FAILS
    ok = got == expected
    FAILS += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<34} got={got}")


def drive(script):
    tb = Toolbox()
    tr = run(CASE["q"], ScriptedLLM(script), toolbox=tb)
    return classify(CASE, tr, tb, WANT), tb, tr


# 1. Skips lookup_term and counts every merchant. Classic guess.
b, *_ = drive([
    [("run_sql", {"sql": "SELECT COUNT(*) FROM merchants"})],
    "There are 120 active merchants.",
])
check("guesses definition", b, "semantic_miss")

# 2. Looks the term up, then still gets it wrong. Not a semantic miss.
b, *_ = drive([
    [("lookup_term", {"term": "active merchant"})],
    [("run_sql", {"sql": "SELECT COUNT(*) FROM merchants WHERE risk_tier='low'"})],
    "Some number.",
])
check("looked up, still wrong", b, "wrong_answer")

# 3. Never produces a runnable query.
b, *_ = drive([
    [("lookup_term", {"term": "active merchant"})],
    [("run_sql", {"sql": "SELECT COUNT(*) FROM merchant"})],
    [("run_sql", {"sql": "SELECT nope FROM merchants"})],
    "I think it is about a hundred.",
])
check("never runs valid sql", b, "sql_error")

# 4. Runs past max steps without answering.
tb = Toolbox()
tr = run(CASE["q"], ScriptedLLM([[("list_tables", {})]] * 20), toolbox=tb, max_steps=4)
check("runs away, hits max steps", classify(CASE, tr, tb, WANT), "no_answer")

# 5. Declines a question that was answerable.
b, *_ = drive([
    [("lookup_term", {"term": "active merchant"})],
    "I cannot answer that with the tools available.",
])
check("declines an answerable one", b, "bad_refusal")

# 6. Answers a question it should have declined.
UNANSWERABLE = {"id": "x", "q": "total refunds?", "truth": None,
                "expect_refusal": True, "requires_terms": []}
tb = Toolbox()
tr = run(UNANSWERABLE["q"], ScriptedLLM(["Refunds totalled $42,000."]), toolbox=tb)
check("invents an answer", classify(UNANSWERABLE, tr, tb, None), "missed_refusal")

# 7. Write guard holds.
tb = Toolbox()
res = tb.run_sql("DROP TABLE merchants")
check("blocks writes", "blocked" if not res.ok else "allowed", "blocked")

print("\nall good" if not FAILS else f"\n{FAILS} FAILED")
raise SystemExit(1 if FAILS else 0)
