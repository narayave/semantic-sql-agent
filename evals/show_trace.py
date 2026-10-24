"""
Print a trace as a readable story: question, each tool call and what came
back, then the answer. Raw JSONL is unreadable, and a trace nobody reads
debugs nothing.

  python evals/show_trace.py traces/eval-dispute_loss_rate-cbb5a1bbafba.jsonl
  python evals/show_trace.py dispute_loss_rate     # newest trace for that case
  python evals/show_trace.py chat                  # newest chat, every turn
"""

import json
import sys
from pathlib import Path

TRACE_DIR = Path(__file__).parent.parent / "traces"


def find(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    hits = sorted(TRACE_DIR.glob(f"*{arg}*.jsonl"), key=lambda f: f.stat().st_mtime)
    if not hits:
        sys.exit(f"no trace matching '{arg}' in {TRACE_DIR}")
    return hits[-1]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = find(sys.argv[1])
    print(path.name)
    # A chat file holds several turns; each starts with a header line.
    turns = []
    for rec in map(json.loads, path.open()):
        if "question" in rec:
            turns.append((rec, []))
        else:
            turns[-1][1].append(rec)
    for k, (head, steps) in enumerate(turns, 1):
        label = f"Turn {k} " if len(turns) > 1 else ""
        print(f"\n{label}Q: {head['question']}\n")
        for s in steps:
            if s["kind"] == "model" and s["output"]:
                print(f"[{s['n']}] Claude says: {s['output'].strip()}\n")
            elif s["kind"] == "tool":
                args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
                flag = "" if s["ok"] else "  <-- ERROR"
                print(f"[{s['n']}] {s['name']}({args}){flag}")
                print("     " + s["output"].strip().replace("\n", "\n     ") + "\n")
        print(f"stop: {head['stop_reason']}  tool calls: {head['tool_calls']}"
              f"  sql errors: {head['sql_errors']}")


if __name__ == "__main__":
    main()
