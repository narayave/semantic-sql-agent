"""
SnowflakeToolbox: the same four tools, pointed at Snowflake instead of DuckDB.

Nothing else in the repo changes. agent/loop.py, agent/trace.py, the tool
schemas and the eval harness all work unmodified, because this class returns the
same ToolResult shape as agent/tools.py.

Snowflake differences that actually bite, all handled below:
  - unquoted identifiers are folded to UPPERCASE, so table lookups must compare
    case-insensitively or the schema tool silently returns nothing
  - information_schema is per-database, and you must filter TABLE_SCHEMA or you
    get every schema in the account including INFORMATION_SCHEMA itself
  - a session carries four pieces of context: role, warehouse, database, schema.
    Miss one and you get "Cannot perform SELECT. This session does not have a
    current database", which reads like a permissions error and is not
  - errors arrive as snowflake.connector.errors.ProgrammingError. The message is
    good, so the repair loop works the same way it does on DuckDB
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .tools import ToolResult, _fmt

SEMANTIC_PATH = Path(__file__).parent.parent / "data" / "semantic_layer.yaml"
MAX_ROWS = 50

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|merge|grant|revoke|"
    r"copy|put|remove|call|use)\b",
    re.IGNORECASE,
)


class SnowflakeToolbox:
    """Drop-in replacement for Toolbox. Same four methods, same return type."""

    def __init__(self, semantic_path: Path = SEMANTIC_PATH, **overrides):
        import snowflake.connector  # imported here so the repo still runs without it

        cfg = {
            "account":   os.environ.get("SNOWFLAKE_ACCOUNT"),
            "user":      os.environ.get("SNOWFLAKE_USER"),
            "password":  os.environ.get("SNOWFLAKE_PASSWORD"),
            "role":      os.environ.get("SNOWFLAKE_ROLE"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE"),
            "database":  os.environ.get("SNOWFLAKE_DATABASE"),
            "schema":    os.environ.get("SNOWFLAKE_SCHEMA"),
        }
        cfg.update({k: v for k, v in overrides.items() if v is not None})

        missing = [k for k in ("account", "user", "database", "schema") if not cfg.get(k)]
        if missing:
            raise SystemExit(
                "Missing Snowflake settings: " + ", ".join(missing) +
                ". See .env.example. Without database and schema every query "
                "fails with a misleading 'no current database' error."
            )

        # key-pair auth if a key path is set, otherwise password
        key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
        if key_path:
            cfg.pop("password", None)
            cfg["private_key_file"] = key_path
            if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
                cfg["private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]

        self.db = cfg["database"]
        self.schema = cfg["schema"]
        self.con = snowflake.connector.connect(
            **{k: v for k, v in cfg.items() if v is not None}
        )

        self.semantic = yaml.safe_load(semantic_path.read_text())
        self.terms_looked_up: list[str] = []
        self.sql_attempts: list[str] = []
        self.sql_errors: list[str] = []
        self.last_rows: list[tuple] | None = None

    # ---------------- internals ----------------

    def _q(self, sql: str, params: tuple = ()):
        cur = self.con.cursor()
        try:
            cur.execute(sql, params)
            return [d[0] for d in cur.description], cur.fetchmany(MAX_ROWS + 1)
        finally:
            cur.close()

    # ---------------- tools ----------------

    def list_tables(self) -> ToolResult:
        try:
            _, rows = self._q(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (self.schema.upper(),),
            )
        except Exception as exc:
            return ToolResult(f"Could not list tables: {exc}", ok=False)
        if not rows:
            return ToolResult(
                f"No tables in {self.db}.{self.schema}. Check SNOWFLAKE_DATABASE "
                f"and SNOWFLAKE_SCHEMA.", ok=False)
        lines = []
        for (name,) in rows:
            meta = (self.semantic.get("tables", {}) or {}).get(name.lower(), {})
            grain = meta.get("grain", "")
            lines.append(f"{name}  ({grain})" if grain else name)
        return ToolResult("\n".join(lines))

    def get_schema(self, table: str) -> ToolResult:
        try:
            _, cols = self._q(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND UPPER(table_name) = %s "
                "ORDER BY ordinal_position",
                (self.schema.upper(), table.upper()),
            )
        except Exception as exc:
            return ToolResult(f"Could not read schema: {exc}", ok=False)
        if not cols:
            known = self.list_tables()
            return ToolResult(
                f"No table named '{table}' in {self.db}.{self.schema}.\n"
                f"Known tables:\n{known.text}", ok=False)
        meta = (self.semantic.get("tables", {}) or {}).get(table.lower(), {})
        out = [f"TABLE {self.db}.{self.schema}.{table.upper()}"]
        if meta.get("grain"):
            out.append(f"grain: {meta['grain']}")
        if meta.get("notes"):
            out.append(f"notes: {meta['notes'].strip()}")
        out.append("columns:")
        col_notes = meta.get("columns", {}) or {}
        for name, dtype in cols:
            note = col_notes.get(name.lower()) or col_notes.get(name)
            out.append(f"  {name} {dtype}" + (f"  -- {note}" if note else ""))
        return ToolResult("\n".join(out))

    def lookup_term(self, term: str) -> ToolResult:
        terms = self.semantic.get("terms", {}) or {}
        key = term.strip().lower()
        self.terms_looked_up.append(key)
        if key in terms:
            e = terms[key]
            txt = f"{term}: {e['definition'].strip()}"
            if e.get("sql_hint"):
                txt += f"\nsql hint: {e['sql_hint'].strip()}"
            return ToolResult(txt)
        hits = [k for k in terms if key in k or k in key]
        if hits:
            e = terms[hits[0]]
            txt = (f"No exact entry for '{term}'. Closest is '{hits[0]}':\n"
                   f"{e['definition'].strip()}")
            if e.get("sql_hint"):
                txt += f"\nsql hint: {e['sql_hint'].strip()}"
            return ToolResult(txt)
        return ToolResult(
            f"No definition for '{term}'. Defined terms: {', '.join(sorted(terms))}",
            ok=False, bucket="semantic_miss")

    def run_sql(self, sql: str) -> ToolResult:
        self.sql_attempts.append(sql)
        if _FORBIDDEN.search(sql):
            msg = "Rejected: run_sql is read only. SELECT statements only."
            self.sql_errors.append(msg)
            return ToolResult(msg, ok=False, bucket="sql_error")
        try:
            cols, rows = self._q(sql)
        except Exception as exc:
            # Snowflake messages are descriptive; hand them straight back so the
            # model can repair. This is the whole repair loop.
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

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass
