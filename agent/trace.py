"""
Trace recording.

One JSONL file per run, one line per step. You cannot debug an agent without
traces, and the eval set gets built out of them later. This is a deliberately
small stand in for OpenTelemetry spans.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

TRACE_DIR = Path(__file__).parent.parent / "traces"


@dataclass
class Step:
    n: int
    kind: str              # "model" | "tool"
    name: str = ""
    args: dict = field(default_factory=dict)
    output: str = ""
    ok: bool = True
    ms: int = 0


@dataclass
class Trace:
    question: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    steps: list[Step] = field(default_factory=list)
    answer: str | None = None
    stop_reason: str = ""      # "answered" | "max_steps" | "gave_up" | "error"
    started: float = field(default_factory=time.time)

    def add(self, step: Step) -> None:
        self.steps.append(step)

    @property
    def n_tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind == "tool")

    @property
    def n_sql_errors(self) -> int:
        return sum(1 for s in self.steps
                   if s.kind == "tool" and s.name == "run_sql" and not s.ok)

    @property
    def looked_up_terms(self) -> list[str]:
        return [s.args.get("term", "") for s in self.steps
                if s.kind == "tool" and s.name == "lookup_term"]

    def write(self, label: str = "run") -> Path:
        TRACE_DIR.mkdir(exist_ok=True)
        path = TRACE_DIR / f"{label}-{self.trace_id}.jsonl"
        with path.open("w") as fh:
            fh.write(json.dumps({
                "trace_id": self.trace_id,
                "question": self.question,
                "answer": self.answer,
                "stop_reason": self.stop_reason,
                "tool_calls": self.n_tool_calls,
                "sql_errors": self.n_sql_errors,
                "elapsed_ms": int((time.time() - self.started) * 1000),
            }) + "\n")
            for s in self.steps:
                fh.write(json.dumps(asdict(s)) + "\n")
        return path
