"""
The agent loop.

Model picks a tool, reads the result, decides what is next, repeats until it
answers or until it is stopped. Three things in here are worth more in an
interview than the rest of the repo combined:

  1. the repair path   - a SQL error comes back as a tool result, not an
                         exception, so the model can read it and fix the query
  2. the stop condition - max steps, plus an explicit "say you cannot" rule, so
                         the loop cannot run away
  3. the trace          - every step is recorded, which is what makes failures
                         diagnosable afterwards
"""

from __future__ import annotations

import time

from .llm import ModelTurn, ToolCall
from .tools import TOOL_SCHEMAS, Toolbox
from .trace import Step, Trace

MAX_STEPS = 8

SYSTEM = """You answer questions about a payments warehouse by querying it.

Rules, in order of importance:

1. Any term that is not literally a column name must be resolved with
   lookup_term BEFORE you write SQL for it. "active merchant", "chargeback
   rate", "settled volume" and similar all have exact definitions that you will
   otherwise get wrong. Guessing is the single most common failure here.
2. Inspect the schema with get_schema before writing SQL against a table. The
   column notes carry warnings that matter, such as amounts being stored in
   cents and dates being stored as text.
3. If run_sql returns an error, read the error and fix the query. Do not repeat
   the same statement.
4. When you have the number, state it in one or two sentences and stop. Give
   the figure, and name any definition you applied that a reader might assume
   differently.
5. If you cannot answer with these tools, say so plainly and say what is
   missing. A wrong answer is worse than no answer."""


def run(question: str, llm, toolbox: Toolbox | None = None,
        max_steps: int = MAX_STEPS, verbose: bool = False) -> Trace:
    tb = toolbox or Toolbox()
    tr = Trace(question=question)
    messages: list[dict] = [{"role": "user", "content": question}]

    for step_n in range(1, max_steps + 1):
        t0 = time.time()
        turn: ModelTurn = llm.turn(SYSTEM, messages, TOOL_SCHEMAS)
        tr.add(Step(n=step_n, kind="model", output=turn.text,
                    ms=int((time.time() - t0) * 1000)))

        if not turn.wants_tools:
            tr.answer = turn.text.strip()
            # The model saying it cannot answer is a legitimate outcome, not a
            # crash. Bucket it separately so evals can tell the two apart.
            tr.stop_reason = ("gave_up" if _is_refusal(tr.answer) else "answered")
            if verbose:
                print(f"[{step_n}] answer: {tr.answer}")
            return tr

        results = []
        for call in turn.tool_calls:
            res = _dispatch(tb, call)
            tr.add(Step(n=step_n, kind="tool", name=call.name, args=call.input,
                        output=res.text, ok=res.ok))
            if verbose:
                flag = "ok" if res.ok else "ERR"
                print(f"[{step_n}] {call.name}({_brief(call.input)}) -> {flag}")
                print("    " + res.text.replace("\n", "\n    ")[:600])
            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": res.text,
                "is_error": not res.ok,
            })
        messages.append({"role": "user", "content": results})

    tr.stop_reason = "max_steps"
    tr.answer = None
    return tr


def _dispatch(tb: Toolbox, call: ToolCall):
    fn = {
        "list_tables": lambda a: tb.list_tables(),
        "get_schema": lambda a: tb.get_schema(a.get("table", "")),
        "lookup_term": lambda a: tb.lookup_term(a.get("term", "")),
        "run_sql": lambda a: tb.run_sql(a.get("sql", "")),
    }.get(call.name)
    if fn is None:
        from .tools import ToolResult
        return ToolResult(f"No tool named '{call.name}'.", ok=False)
    return fn(call.input or {})


_REFUSAL_MARKERS = (
    "cannot answer", "can't answer", "not able to answer",
    "unable to answer", "do not have", "don't have enough",
)


def _is_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def _brief(args: dict) -> str:
    out = []
    for k, v in (args or {}).items():
        s = str(v).replace("\n", " ")
        out.append(f"{k}={s[:70]}{'...' if len(s) > 70 else ''}")
    return ", ".join(out)
