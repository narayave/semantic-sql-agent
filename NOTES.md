# Notes

My working log for this project: what I ran, what broke, what I learned.
Newest findings first.

---

## The core skill: from a failure to the right fix

The agent doesn't learn between runs. The model is fixed; I am the learning
loop. Raising accuracy means changing what the model *sees* (definitions,
schema notes, tool descriptions, prompt) or fixing how I *measure* it.

The hard part isn't making a change, it's knowing **which** change. That
skill is called **error analysis**: read the trace, find the first point where
the run went wrong, and let that point pick the lever.

### Diagnosis checklist

Walk a failed trace top to bottom. Stop at the first "no".

| # | question | if "no", the problem is | lever |
|---|---|---|---|
| 1 | Is the final answer actually **wrong**? (read the prose, compare to truth) | the test, not the agent | scorer in `run_evals.py`, or question/truth in `golden.yaml` |
| 2 | Did it call `lookup_term` for the term the question needs? | it guessed instead of looking up | system prompt or `lookup_term` description |
| 3 | Did the lookup return the right definition? (not "no match", not a different term) | the term is missing or named differently | `semantic_layer.yaml`: add the term or a synonym |
| 4 | Is the definition clear enough that only one SQL reading fits? | the definition is ambiguous | `semantic_layer.yaml`: tighten definition or `sql_hint` |
| 5 | Did its SQL follow the definition? | it ignored what it read | system prompt |
| 6 | Did it read columns correctly? (cents, casing, text dates, NULLs) | missed a data trap | schema notes returned by `get_schema` |
| 7 | Did it finish within the step budget? | ran out of steps | `MAX_STEPS`, or why it wasted steps |
| 8 | All of the above fine, still wrong? | the model's reasoning | try a bigger model |

Rule: **change one thing, rerun the full eval, compare buckets, not just the
total.** A fix for one case can break another.

### Where each lever lives

| lever | file | what I change | fixes failures like |
| --- | --- | --- | --- |
| Term definitions | `data/semantic_layer.yaml` | a term's prose or SQL hint, or add a missing term | it looked up the term but the definition was vague or incomplete |
| Schema notes | notes returned by `get_schema` | warnings about columns | it misread a column's units, type or meaning |
| Tool descriptions | `TOOL_SCHEMAS` in `agent/tools.py` | how each tool is described to Claude | it skipped a tool or used it wrong |
| System prompt | `SYSTEM` in `agent/loop.py` | the rules Claude follows | behaviour across many cases, e.g. the format of its final query |
| Loop settings | `MAX_STEPS` in `agent/loop.py` | the step budget | runs that time out |
| Model | `--model` | a bigger model | reasoning failures that context changes can't fix |
| The test itself | `evals/golden.yaml`, `evals/run_evals.py` | an unclear question, a wrong truth query, a scorer that's too strict | the agent was right and the test was wrong |

---

## Run log

Same 17 cases, same prompt, same data. Only the model changes.

| date | model | accuracy | traps | buckets | notes |
|---|---|---|---|---|---|
| 2026-10-17 | `claude-haiku-5-5` | 7/17 | | 9 wrong_answer, 1 semantic_miss | at least 3 "wrong" were actually right (Finding 1) |
| 2026-10-17 | `claude-sonnet-5-5` | 3/17 | 3/8 | 10 wrong_answer, 3 semantic_miss, 1 missed_refusal | newer model scored *worst*; see Finding 2 |
| 2026-10-17 | `claude-sonnet-5` | 14/17 | 7/8 | 2 wrong_answer, 1 semantic_miss | best so far |

---

## Findings

### Finding 2: the newer model scored worse because it was more thorough (hypothesis)

Sonnet 5.5 scored 3/17, Sonnet 5 scored 14/17. Before concluding the newer
model is worse, I compared the same case across both runs.

`vol_settled`, "What is the total settled volume in dollars?"

| | Sonnet 5 | Sonnet 5.5 |
|---|---|---|
| looked up "settled volume" | yes | yes |
| final SQL | `SELECT SUM(txn_amt_cents)/100.0 ... WHERE LOWER(status)='settled'` | same, plus `COUNT(*)` |
| result | `696570.28` | `696570.28`, `14047` |
| prose answer | $696,570.28 | $696,570.28 |
| scored | pass | **fail** |

Both answers are identical and correct. Sonnet 5.5 added a row count for
context, the result had two columns instead of one, and the scorer failed it.

**Lesson:** a model comparison is only as good as the scorer. Here the scorer
penalised a model for being *more* helpful.

**To confirm:** check the other Sonnet 5.5 failures. How many have the right
number in the prose answer?

### Finding 1: the scorer grades the last query, not the answer

Under Haiku, 3 of 9 `wrong_answer` cases had the right answer:

- `count_churned`: answered 12 (correct), then ran one more query to check the
  dates. The scorer compared that last query, a list of dates, against 12.
- `top_merchant_by_volume`: found merchant 1113 (correct), but returned the
  top 5 plus a dollar column; truth is a single value.
- `cnp_share`: got 44.24% (correct), but returned `0.4424` as a fraction plus
  extra columns; truth is `44.24`. The question says "share", so both readings
  are fair.

**Lesson:** before fixing the agent, check the measurement. Tuning the agent
against a broken scorer is tuning against noise.

**Options, each with a trade-off (undecided):**
1. Prompt rule: "final query returns only the answer value." Simple, but bends
   the agent to fit the test.
2. Looser scorer: pass if the truth value appears anywhere in the result.
   Forgiving, but easier to pass by accident.
3. Grade the prose answer with an LLM judge. Most realistic, most work, and the
   judge itself needs checking against my own labels.

---

## Next

- [ ] Confirm Finding 2 across all Sonnet 5.5 failures
- [ ] Deep dive: `dispute_loss_rate`, which fails on Sonnet 5 too (a real failure?)
- [ ] Decide how to fix the scorer, then rerun all three models
- [ ] Record the model name in each run's output so runs can't get mixed up
