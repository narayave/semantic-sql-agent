# CONTEXT.md

Orientation for anyone working in this repo, human or agent. Read this before
changing code. `README.md` is the pitch for a visitor. This file is the working
contract.

---

## 1. What this is

`semantic-sql-agent` answers business questions about a payments warehouse by
writing and running SQL, using a tool loop that can repair its own broken
queries, and an eval harness that classifies every failure by cause.

It is a small, honest version of what a product like Snowflake Cortex Analyst
does.

**The point of the project is not text-to-SQL.** Text-to-SQL is the vehicle.
The point is the three things around it:

1. a **semantic layer** that makes business terms explicit instead of guessed
2. a **tool loop** that recovers from failure instead of dying
3. an **eval harness** that says *why* something was wrong, not just that it was

If a change makes the SQL better but weakens one of those three, it is the
wrong change.

## 2. Status

Working end to end. Seeds, runs offline without an API key, runs against the
real model with one, evals pass, scorer has negative tests.

The warehouse is **synthetic**. This is deliberate so that the traps in the
data are controlled rather than accidental. Do not describe it as real data.
Swapping in a real public dataset is on the roadmap and the expected outcome is
that the eval score drops, which is the interesting result.

## 3. Quickstart

```bash
pip install -r requirements.txt
python data/seed.py              # build the DuckDB warehouse
python run.py --demo             # offline, no API key, shows the repair loop
python evals/test_harness.py     # negative tests on the scorer
```

With a key:

```bash
export ANTHROPIC_API_KEY=...
python run.py "What is the chargeback rate for high risk merchants?"
python evals/run_evals.py
python evals/run_evals.py --only vol_settled,count_active
```

## 4. Layout

```
data/seed.py              builds the messy DuckDB warehouse (deterministic, SEED=1337)
data/semantic_layer.yaml  business term definitions. THE important file.
agent/tools.py            four tools, typed schemas, read-only guard
agent/loop.py             the agent loop, system prompt, stop condition
agent/llm.py              AnthropicLLM + ScriptedLLM (offline replay)
agent/trace.py            per-run JSONL traces
evals/golden.yaml         17 cases, ground truth stored as SQL, 8 traps
evals/run_evals.py        execution accuracy + failure buckets
evals/test_harness.py     proves the scorer can detect failure
run.py                    CLI
```

## 5. Invariants

Do not break these without a deliberate decision. Each one is load bearing and
each one is an interview answer.

**Tool errors return, they never raise.** `ToolResult(ok=False)` carries the
error text back to the model as a tool result. This is what makes the repair
loop work. An exception kills the run; a returned error gets read and fixed.

**`run_sql` is read only, twice over.** A regex rejects write verbs, and the
DuckDB connection is opened `read_only=True`. Keep both. One is a guard, the
other is the actual boundary.

**Results are capped at 50 rows.** `MAX_ROWS` in `tools.py`. A wide query must
not be able to blow the context window.

**The loop has a hard step cap.** `MAX_STEPS = 8` in `loop.py`. An agent with
no stop condition is a billing incident.

**Declining is a valid outcome, tracked separately.** `stop_reason` is one of
`answered`, `gave_up`, `max_steps`. `gave_up` and `max_steps` are different
failures needing different fixes, so do not collapse them.

**Ground truth in `golden.yaml` is SQL, not a number.** Expected values are
recomputed from the warehouse at eval time, so reseeding does not invalidate
the eval set.

**Every term the model needs must be in the semantic layer.** If a golden case
depends on a definition, list it in that case's `requires_terms`. That list is
what lets the scorer separate `semantic_miss` from `wrong_answer`.

## 6. The data, and why it is ugly

The ugliness is the argument for the semantic layer. Preserve it.

| trap | detail |
|---|---|
| casing drift | `status` holds `SETTLED`, `settled`, `Settled`. `status = 'settled'` silently drops about two thirds of rows. Always `LOWER(status)`. |
| cents | `txn_amt_cents`, `amt_cents`, `fee_cents` are all **cents**. Divide by 100.0. |
| text dates | `created_dt`, `onboarded_dt`, `closed_dt`, `paid_dt`, `opened_dt` are `VARCHAR`, not `DATE`. |
| NULL is not a value | `risk_tier IS NULL` means never assessed. It is not low risk. |
| implicit state | an active merchant is `closed_dt IS NULL`. There is no status column. |
| codes not names | there is no category name anywhere, only an MCC integer. |
| open disputes | `outcome IS NULL` means unresolved. Excluded from loss rate on both sides. |

Tables: `merchants`, `txns`, `disputes`, `payouts`. Joins are
`txns.mrch_id -> merchants.mrch_id`, `disputes.txn_id -> txns.txn_id`,
`payouts.mrch_id -> merchants.mrch_id`.

## 7. The four tools

| tool | contract |
|---|---|
| `list_tables()` | table names plus grain |
| `get_schema(table)` | columns, types, and the notes carrying the traps |
| `lookup_term(term)` | resolves a business term; exact match, then substring, then a listing of known terms |
| `run_sql(sql)` | read only SELECT; returns rows **or** the error text |

Tool schemas live in `TOOL_SCHEMAS` in `tools.py` in Anthropic tool-use format.
Dispatch is in `loop.py::_dispatch`. Adding a tool means touching both.

Keep tool descriptions blunt about failure modes. The `lookup_term` description
says guessing a definition is the most common way to be confidently wrong, and
that sentence measurably changes behaviour.

## 8. Evals

Scoring is **execution accuracy**: run the agent, take the result of its last
successful query, compare to the ground truth query. Floats compare with 1%
relative tolerance (`TOL`).

Buckets, in `run_evals.py::classify`:

| bucket | meaning | what it tells you to fix |
|---|---|---|
| `pass` | matched ground truth | nothing |
| `semantic_miss` | wrong, and never looked up a term the case needs | the semantic layer, or the instruction to use it |
| `sql_error` | never produced a query that ran | the schema tool, or the prompt |
| `wrong_answer` | query ran, result did not match | the term definition itself |
| `no_answer` | hit the step cap | the loop, or the step budget |
| `bad_refusal` | declined something answerable | too cautious |
| `missed_refusal` | invented an answer to an unanswerable question | too confident |

**The aggregate score is the least useful number in the output.** The bucket
distribution is what drives the next change. Report both, lead with buckets.

Eight cases are marked `trap: true`, meaning they are answerable the obvious
wrong way. An agent that skips `lookup_term` should score near zero on traps
and near perfect on the rest. Trap accuracy is reported separately for exactly
that reason.

One case (`unanswerable_refunds`) has no answer: there is no refunds table. The
correct behaviour is to decline. An agent with no way to decline is an agent
that hallucinates.

`evals/test_harness.py` drives six deliberately broken agents through the
scorer and asserts each lands in the right bucket. **Run it after any change to
`classify`.** A scorer that has only ever seen passing runs has not been
tested.

## 9. How to extend

**Add a business term.** Add it to `terms:` in `semantic_layer.yaml` with a
`definition` and a `sql_hint`. Then add at least one golden case that depends
on it and list it in that case's `requires_terms`. A term with no eval case is
a term nobody is checking.

**Add a golden case.** Append to `evals/golden.yaml` with `id`, `q`, `truth`
(the ground truth SQL), and `requires_terms`. Mark `trap: true` if there is an
obvious wrong reading. Verify the truth SQL by hand before trusting it; a wrong
ground truth is worse than no eval.

**Add a tool.** Method on `Toolbox` returning `ToolResult`, schema in
`TOOL_SCHEMAS`, dispatch entry in `loop.py::_dispatch`. Return errors, do not
raise.

**Change the prompt.** `SYSTEM` in `loop.py`. Run the full eval before and
after and compare bucket distributions, not just the headline score. Prompts
are code and changing one moves quality in ways nobody predicts.

**Test without spending tokens.** `ScriptedLLM` replays a list where each entry
is either a list of `(tool_name, args)` tuples or a string that ends the turn
as the final answer. See `DEMO_SCRIPT` in `run.py`.

## 10. Conventions

- Python 3.11+. Standard library plus `duckdb`, `PyYAML`, `anthropic`.
- No framework. No LangChain, no agent library. The loop is 60 lines on
  purpose, because the whole point is understanding what is inside one.
- Comments explain **why**, not what. Several of them are the reasoning behind
  an invariant above; do not strip them.
- Traces go to `traces/*.jsonl`, gitignored. `data/payments.duckdb` is
  gitignored and rebuilt by `seed.py`.
- `seed.py` is deterministic (`SEED = 1337`). Keep it that way or every golden
  value shifts.

## 11. Known limitations

State these plainly rather than hiding them.

- The warehouse is synthetic.
- Scoring compares the last successful query result, not the prose answer. An
  agent could run the right query and then describe it wrongly and still pass.
  An LLM-as-judge over the prose is the fix, and it is on the roadmap.
- Single-turn only. No conversation, no follow-up questions, no clarification.
- No cost or token accounting yet.
- `lookup_term` substring matching is crude and will mis-resolve on a larger
  vocabulary.

## 12. Roadmap

1. **MCP server.** Expose the same four tools over MCP. Same code, different
   wrapper.
2. **Real dataset.** Swap the synthetic warehouse for a real public one and
   record the score drop.
3. **LLM-as-judge** on the prose answer, calibrated against hand labels,
   because a judge is itself a system that needs evaluating.
4. **Cost per case** reported next to accuracy.
5. **CI.** Run evals on every change and fail the build on regression.
6. **Clarifying questions.** Let the agent ask when a question is ambiguous,
   instead of picking an interpretation silently.

## 13. Glossary

Terms used in this repo and in the field.

- **Execution accuracy** — did the agent's query return the same result as the
  ground truth query. The standard text-to-SQL metric. Stricter than string
  matching the SQL, looser than requiring identical SQL.
- **Golden dataset** — a fixed set of inputs with known-good outputs. The
  regression suite for an AI system.
- **Semantic layer** — the mapping from business vocabulary to schema. Where
  "active merchant" becomes `closed_dt IS NULL`.
- **Failure-mode analysis** — bucketing wrong answers by cause rather than
  counting them.
- **Trace** — the structured record of every step a run took. You cannot debug
  an agent without one, and eval sets get built out of real traces.
- **Repair loop** — feeding a tool error back to the model so it can correct
  itself.
- **Stop condition** — the rule that ends a run. Step cap, plus permission to
  decline.
- **LLM-as-judge** — using a model to grade outputs at scale. Judges drift and
  carry position and verbosity biases, so a judge needs calibrating against
  human labels before it is trusted.
- **Guardrails** — the checks around a model. Here: read-only SQL, row caps,
  step caps.
