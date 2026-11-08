"""Claude calls: Claude Agent SDK with the Claude Code subscription (no API key).

Used only while learning. Once a rule is fixed, it is never called.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

# Cheapest model that is good enough for simple reading; after failed rounds a stronger model is used.
DEFAULT_MODEL = "claude-haiku-5-5"
ESCALATION_MODEL = "claude-sonnet-5-5"

# On Windows the system prompt goes over the command line: keep it short and avoid the characters & | < > ^ % ! "
SYSTEM_PROMPT = (
    "You are a web scraping rule engineer. You read simplified HTML and write precise, robust "
    "CSS or XPath selectors. You answer only through the structured output tool."
)


@dataclass
class LLMResult:
    data: dict
    model: str
    cost_usd: float


class LLMError(RuntimeError):
    pass


def _force_subscription() -> None:
    # In -p mode, if ANTHROPIC_API_KEY is set, the API key is used instead of the subscription.
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)


def _extract_json(text: str) -> dict:
    dec = json.JSONDecoder()
    t = text or ""
    for i, ch in enumerate(t):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(t, i)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue
    raise LLMError("Model did not return valid JSON")


async def ask_json(prompt: str, schema: dict, model: str = DEFAULT_MODEL) -> LLMResult:
    """A single call without tools; returns JSON matching the schema."""
    _force_subscription()
    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        max_turns=4,  # the structured output tool can count as a turn
        setting_sources=[],
        mcp_servers={},
        output_format={"type": "json_schema", "schema": schema},
    )
    final: ResultMessage | None = None
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, ResultMessage):
                final = msg
    except Exception as e:  # ResultError / ProcessError
        raise LLMError(f"Claude call failed: {e}") from e
    if final is None:
        raise LLMError("Claude returned no result")
    if final.is_error:
        raise LLMError(f"Claude error: {final.subtype} {final.errors or final.result}")
    data = final.structured_output
    if data is None:
        data = _extract_json(final.result or "")
    if not isinstance(data, dict):
        raise LLMError("Unexpected output type")
    return LLMResult(data=data, model=model, cost_usd=final.total_cost_usd or 0.0)
