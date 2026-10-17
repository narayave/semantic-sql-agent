"""
Model adapters.

Two of them. AnthropicLLM talks to the real API. ScriptedLLM replays a canned
sequence of tool calls so the loop, the tools and the eval harness can be
tested offline, deterministically, for free. Being able to test agent plumbing
without a model in the way is worth the hour it takes to build.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class AnthropicLLM:
    """Thin wrapper over the Messages API tool-use loop."""

    def __init__(self, model: str = "claude-haiku-5-5", max_tokens: int = 2048):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "pip install anthropic, or run with --scripted to use the "
                "offline adapter."
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. See .env.example.")
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input)
                 for b in resp.content if b.type == "tool_use"]
        # Persist assistant turn verbatim so tool_result blocks line up.
        messages.append({"role": "assistant", "content": [
            b.model_dump() for b in resp.content
        ]})
        return ModelTurn(text=text, tool_calls=calls or None)


class ScriptedLLM:
    """
    Replays a list of turns. Each entry is either:
      - a str, which ends the turn as a final answer, or
      - a list of (tool_name, args_dict) tuples to call this turn.

    Used by tests and by `--scripted` so you can prove the loop works, including
    the SQL repair path, before spending a token.
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.i = 0

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        if self.i >= len(self.script):
            return ModelTurn(text="I cannot answer that with the tools available.")
        item = self.script[self.i]
        self.i += 1
        messages.append({"role": "assistant", "content": str(item)})
        if isinstance(item, str):
            return ModelTurn(text=item)
        calls = [ToolCall(f"s{self.i}_{k}", name, args)
                 for k, (name, args) in enumerate(item)]
        return ModelTurn(tool_calls=calls)
