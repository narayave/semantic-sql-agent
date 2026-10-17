# semantic-sql-agent

A text-to-SQL agent over a messy payments warehouse, with a semantic layer, a
tool loop that repairs its own broken SQL, a stopping condition, and an eval
harness that buckets failures by cause.

It is a small, honest version of what a product like Snowflake Cortex Analyst
does. The interesting parts are not the SQL. They are the loop, the stop
condition, and the evals.

## Run it

```bash
pip install -r requirements.txt
python data/seed.py                 # build the warehouse
python run.py --demo                # offline, no API key, shows the repair loop
python evals/test_harness.py        # negative tests on the scorer
```

With a key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python run.py "What is the chargeback rate for high risk merchants?"
python evals/run_evals.py
```

## What is here

See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams of the whole flow.

```
data/seed.py              builds a deliberately messy DuckDB warehouse
data/semantic_layer.yaml  business term definitions   <- the important file
agent/tools.py            four tools, typed schemas, read-only guard
agent/loop.py             the agent loop
agent/llm.py              Anthropic adapter + offline scripted adapter
agent/trace.py            per-run JSONL traces
evals/golden.yaml         17 cases with ground-truth SQL, 8 of them traps
evals/run_evals.py        execution accuracy + failure buckets
evals/test_harness.py     tests that the scorer can actually detect failure
```

## The four tools

| tool | why it exists |
|---|---|
| `list_tables` | orientation, with the grain of each table |
| `get_schema` | columns plus the notes that carry the traps |
| `lookup_term` | resolves business terms against the semantic layer |
| `run_sql` | read-only SELECT, returns rows **or the error text** |

`run_sql` returning errors instead of raising is the design decision that makes
the repair loop possible. A tool error is information the model needs, not a
crash.

## The messy part is on purpose

The warehouse looks like a real one that nobody cleaned up:

- `status` holds `SETTLED`, `settled` and `Settled`, so `status = 'settled'`
  silently undercounts by two thirds
- amounts are in **cents**, in a column called `txn_amt_cents`
- dates are stored as **text**, not `DATE`
- `risk_tier` is `NULL` for merchants never assessed, which is not the same as
  low risk
- `closed_dt IS NULL` is the only marker of an active merchant
- there is no category **name** anywhere, only an MCC integer

None of that is guessable. That is the argument for the semantic layer.

## The semantic layer

`data/semantic_layer.yaml` defines the terms that do not exist as columns:
*active merchant*, *settled volume*, *chargeback rate*, *dispute loss rate*,
*net payout*, *take rate*, *high risk merchant*.

Each has a definition in prose and a SQL hint. Without the file, a model has to
invent a definition, and it will invent a different one each run. With it, the
definition is versioned, reviewable by whoever owns the metric, and identical
every time. This is the part that makes it an engineering artifact rather than
a prompt.

## The loop

Max 8 steps. Each step the model picks tools, reads the results, decides what
is next. Three things matter:

1. **Repair.** When `run_sql` fails, the error text comes back as a tool
   result. The model reads it and fixes the query. `python run.py --demo`
   walks through exactly this: a query against a column that does not exist,
   the binder error, then the corrected query.
2. **Stopping.** A step cap, plus an explicit instruction to say it cannot
   answer rather than invent one. Declining is recorded as `gave_up`, which is
   a different outcome from `max_steps`, because they need different fixes.
3. **Tracing.** Every model turn and tool call is written to
   `traces/*.jsonl`. You cannot debug an agent without this, and the eval set
   gets built out of real traces later.

## Evals

Scoring is **execution accuracy**: run the agent, take the result of its last
successful query, compare against the ground-truth query in `golden.yaml`.
Truth is stored as SQL rather than as a number, so the expected answers
recompute whenever the warehouse is reseeded.

The aggregate score is the least useful number in the output. The buckets are
the point:

| bucket | meaning | what it tells you to fix |
|---|---|---|
| `semantic_miss` | wrong, and never looked up a term it needed | the semantic layer, or the instruction to use it |
| `sql_error` | never produced a query that ran | the schema tool, or the prompt |
| `wrong_answer` | query ran, result did not match | the term definition itself |
| `no_answer` | hit the step cap | the loop, or the step budget |
| `bad_refusal` | declined something answerable | too cautious |
| `missed_refusal` | invented an answer to an unanswerable question | too confident |

Eight of the seventeen cases are **traps**: answerable the obvious wrong way.
Total settled volume is wrong if failed transactions slip in. Active merchants
is wrong if churned ones are counted. Chargeback rate is wrong if the
denominator is all transactions instead of settled ones. An agent that skips
`lookup_term` should score near zero on the traps and near perfect on the rest,
and the bucket split will say so.

There is also one case with no answer at all: there is no refunds table. The
correct behaviour is to decline. An agent with no way to decline is an agent
that hallucinates.

`evals/test_harness.py` drives six deliberately broken agents through the
scorer and asserts each failure lands in the right bucket, because a scorer
that has only ever seen passing runs has not been tested.

## Guardrails

`run_sql` rejects anything other than a read. An agent that can `DROP TABLE` is
a liability, and the tool is opened `read_only=True` as well so the guard is
not the only line of defence. Results are capped at 50 rows so a wide query
cannot blow the context window.

## Where to take it next

- Swap the synthetic warehouse for a real public dataset and watch the eval
  score drop. That drop is the interesting result.
- Expose the same four tools as an **MCP server**. Same code, different
  wrapper, roughly two hours.
- Add an LLM-as-judge for the prose answer, then calibrate the judge against
  your own labels, because a judge is a system that needs evaluating too.
- Log token cost per case and put cost next to accuracy in the scorecard.
- Run the evals in CI and fail the build on regression. Prompts and tool
  definitions are code; changing one moves quality in ways nobody predicts.
