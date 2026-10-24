"""
Streamlit chat interface.

  streamlit run app.py              # Snowflake, reads env vars
  BACKEND=duckdb streamlit run app.py   # local, no credentials needed

Deliberately thin. The agent loop, tools and traces are unchanged; this is a
view onto them. The expander under each answer shows the tool calls and the SQL,
including any query the agent got wrong and repaired, because the reader wants
to see the reasoning, not just the number.
"""

from __future__ import annotations

import os
import uuid

import streamlit as st

from agent.llm import AnthropicLLM
from agent.loop import run
from agent.trace import TRACE_DIR

BACKEND = os.environ.get("BACKEND", "snowflake").lower()

st.set_page_config(page_title="Ask the warehouse", page_icon="::", layout="centered")
st.title("Ask the warehouse")
st.caption(
    "Natural language over a live warehouse. The agent resolves business terms "
    "against a semantic layer before writing SQL, and repairs its own failed "
    "queries."
)


@st.cache_resource(show_spinner="Connecting...")
def get_toolbox():
    if BACKEND == "duckdb":
        from agent.tools import Toolbox
        return Toolbox()
    from agent.snowflake_tools import SnowflakeToolbox
    return SnowflakeToolbox()


with st.sidebar:
    st.subheader("Session")
    st.write(f"Backend: `{BACKEND}`")
    try:
        tb_probe = get_toolbox()
        st.success("Connected")
        with st.expander("Tables"):
            st.text(tb_probe.list_tables().text)
        with st.expander("Defined terms"):
            st.text("\n".join(sorted((tb_probe.semantic.get("terms") or {}))))
    except SystemExit as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:
        st.error(f"Connection failed: {exc}")
        st.stop()
    if st.button("Clear conversation"):
        st.session_state.history = []
        st.session_state.messages = []
        st.session_state.trace_path = None
        st.rerun()

if "history" not in st.session_state:
    st.session_state.history = []      # what the page shows
    st.session_state.messages = []     # what the model sees, tool calls included
if not st.session_state.get("trace_path"):
    # One trace file per conversation; every turn appends to it.
    st.session_state.trace_path = TRACE_DIR / f"chat-{uuid.uuid4().hex[:12]}.jsonl"

for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])
        if turn.get("steps"):
            with st.expander(f"{len(turn['steps'])} tool calls"):
                for s in turn["steps"]:
                    flag = "" if s["ok"] else "  [error, repaired below]"
                    st.markdown(f"**{s['name']}**{flag}")
                    if s["name"] == "run_sql":
                        st.code(s["args"].get("sql", ""), language="sql")
                    elif s["args"]:
                        st.caption(", ".join(f"{k}={v}" for k, v in s["args"].items()))
                    st.text(s["output"][:1500])

if prompt := st.chat_input("e.g. what is the chargeback rate for high risk merchants?"):
    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            # Reset per-run counters; the conversation lives in `messages`.
            tb = get_toolbox()
            tb.terms_looked_up, tb.sql_attempts, tb.sql_errors = [], [], []
            msgs = st.session_state.messages
            mark = len(msgs)
            try:
                tr = run(prompt, AnthropicLLM(), toolbox=tb, history=msgs)
            except Exception as exc:
                # Drop the half-written turn so the next question still works.
                del msgs[mark:]
                st.error(f"Agent failed: {exc}")
                st.session_state.history.pop()
                st.stop()

        answer = tr.answer or "_No answer produced within the step budget._"
        st.markdown(answer)

        steps = [
            {"name": s.name, "args": s.args, "output": s.output, "ok": s.ok}
            for s in tr.steps if s.kind == "tool"
        ]
        if steps:
            with st.expander(f"{len(steps)} tool calls"):
                for s in steps:
                    flag = "" if s["ok"] else "  [error, repaired below]"
                    st.markdown(f"**{s['name']}**{flag}")
                    if s["name"] == "run_sql":
                        st.code(s["args"].get("sql", ""), language="sql")
                    elif s["args"]:
                        st.caption(", ".join(f"{k}={v}" for k, v in s["args"].items()))
                    st.text(s["output"][:1500])

        bits = [f"stop: `{tr.stop_reason}`", f"tool calls: {tr.n_tool_calls}"]
        if tr.n_sql_errors:
            bits.append(f"**SQL repairs: {tr.n_sql_errors}**")
        if tr.looked_up_terms:
            bits.append("terms: " + ", ".join(f"`{t}`" for t in tr.looked_up_terms))
        st.caption(" | ".join(bits))
        tr.write(path=st.session_state.trace_path)

    st.session_state.history.append(
        {"role": "assistant", "content": answer, "steps": steps}
    )
