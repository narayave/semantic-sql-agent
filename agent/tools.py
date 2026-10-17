"""
The four tools the agent can call.

Each tool returns a plain string. Errors are returned, not raised: a tool error
is information the model needs in order to repair, not a crash. That decision
is what makes the repair loop possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

DB_PATH = Path(__file__).parent.parent / "data" / "payments.duckdb"
SEMANTIC_PATH = Path(__file__).parent.parent / "data" / "semantic_layer.yaml"

MAX_ROWS = 50

# run_sql is read only on purpose. An agent that can DROP TABLE is a liability,
# and "what guardrails did you put on the tools" is an interview question.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|copy|export|install|load|pragma)\b",
    re.IGNORECASE,
)


@dataclass
class ToolResult:
    text: str
    ok: bool = True
    # which failure bucket this belongs to, if any. used by the eval harness.
    bucket: str | None = None


class Toolbox:
    def __init__(self, db_path: Path = DB_PATH, semantic_path: Path = SEMANTIC_PATH):
        self.con = duckdb.connect(str(db_path), read_only=True)
        self.semantic = yaml.safe_load(semantic_path.read_text())
        self.terms_looked_up: list[str] = []
        self.sql_attempts: list[str] = []
        self.sql_errors: list[str] = []
        self.last_rows: list[tuple] | None = None

    # ---------------- tools ----------------

    def list_tables(self) -> ToolResult:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
        lines = []
        for (name,) in rows:
            meta = self.semantic.get("tables", {}).get(name, {})
            grain = meta.get("grain", "")
            lines.append(f"{name}  ({grain})" if grain else name)
        return ToolResult("\n".join(lines))

    def get_schema(self, table: str) -> ToolResult:
        cols = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        if not cols:
            known = [r[0] for r in self.con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main'").fetchall()]
            return ToolResult(
                f"No table named '{table}'. Known tables: {', '.join(known)}",
                ok=False,
            )
        meta = self.semantic.get("tables", {}).get(table, {})
        out = [f"TABLE {table}"]
        if meta.get("grain"):
            out.append(f"grain: {meta['grain']}")
        if meta.get("notes"):
            out.append(f"notes: {meta['notes'].strip()}")
        out.append("columns:")
        col_notes = meta.get("columns", {}) or {}
        for name, dtype in cols:
            note = col_notes.get(name)
            out.append(f"  {name} {dtype}" + (f"  -- {note}" if note else ""))
        return ToolResult("\n".join(out))

    def lookup_term(self, term: str) -> ToolResult:
        """Resolve a business term against the semantic layer."""
        terms = self.semantic.get("terms", {})
        key = term.strip().lower()
        self.terms_looked_up.append(key)
        if key in terms:
            entry = terms[key]
            txt = f"{term}: {entry['definition'].strip()}"
            if entry.get("sql_hint"):
                txt += f"\nsql hint: {entry['sql_hint'].strip()}"
            return ToolResult(txt)
        # partial match before giving up. real users do not type exact keys.
        hits = [k for k in terms if key in k or k in key]
        if hits:
            entry = terms[hits[0]]
            txt = (f"No exact entry for '{term}'. Closest is '{hits[0]}':\n"
                   f"{entry['definition'].strip()}")
            if entry.get("sql_hint"):
                txt += f"\nsql hint: {entry['sql_hint'].strip()}"
            return ToolResult(txt)
        return ToolResult(
            f"No definition for '{term}'. Defined terms: {', '.join(sorted(terms))}",
            ok=False,
            bucket="semantic_miss",
        )

    def run_sql(self, sql: str) -> ToolResult:
        self.sql_attempts.append(sql)
        if _FORBIDDEN.search(sql):
            msg = "Rejected: run_sql is read only. SELECT statements only."
            self.sql_errors.append(msg)
            return ToolResult(msg, ok=False, bucket="sql_error")
        try:
            cur = self.con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except Exception as exc:  # duckdb raises a family of types
            msg = f"SQL error: {exc}"
            self.sql_errors.append(msg)
            return ToolResult(msg, ok=False, bucket="sql_error")

        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        self.last_rows = rows
        if not rows:
            return ToolResult("0 rows. The query ran but matched nothing.")
        header = " | ".join(cols)
        body = "\n".join(" | ".join(_fmt(v) for v in r) for r in rows)
        note = f"\n...truncated at {MAX_ROWS} rows" if truncated else ""
        return ToolResult(f"{header}\n{body}{note}")


def _fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


# Tool schemas, in Anthropic tool-use format. Typed, because an untyped tool is
# a prompt and the model will improvise the arguments.
TOOL_SCHEMAS = [
    {
        "name": "list_tables",
        "description": "List every table in the warehouse with its grain. Call this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_schema",
        "description": (
            "Get columns, types and column level notes for one table. The notes "
            "carry warnings such as which columns are text rather than dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "lookup_term",
        "description": (
            "Resolve a business term (for example 'active merchant', 'chargeback "
            "rate', 'settled volume') against the semantic layer. ALWAYS call "
            "this before writing SQL for any term that is not literally a column "
            "name. Guessing a definition is the most common way to be confidently wrong."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Execute a read only SELECT against DuckDB and return rows. If it "
            "fails you get the error text back; read it and fix the query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]
