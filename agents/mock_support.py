"""Helpers for the scripted offline models (LLM_PROVIDER=mock).

The mocks follow the same tool-calling protocol as a real LLM (pydantic-ai
FunctionModel), so the complete distributed flow - registry lookup, A2A calls,
MCP calls - can be tested without an API key. They are deliberately simple and
only understand the demo e-mails.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart


def user_prompt(messages: list[ModelMessage]) -> str:
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    return p.content if isinstance(p.content, str) else " ".join(map(str, p.content))
    return ""


def tool_returns(messages: list[ModelMessage]) -> list[tuple[str, Any]]:
    out = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    out.append((p.tool_name, p.content))
    return out


def as_json(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    try:
        return json.loads(content)
    except Exception:
        return content


def call(*calls: tuple[str, dict]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=n, args=a, tool_call_id=f"mock_{uuid.uuid4().hex[:8]}") for n, a in calls])


def answer(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


DATA_MARKER = "### DATA"


def final(info, data: dict) -> ModelResponse:
    """Return a structured result through the agent's output tool (like a real LLM would)."""
    return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=data,
                                             tool_call_id=f"mock_{uuid.uuid4().hex[:8]}")])


def data_block(prompt: str) -> Any:
    """JSON appended by the orchestrator after the last '### DATA' marker (fallback: first {...last })."""
    if DATA_MARKER in prompt:
        tail = prompt.rsplit(DATA_MARKER, 1)[1]
        return json.loads(tail[tail.index("{"):])
    return json.loads(prompt[prompt.index("{"): prompt.rindex("}") + 1])


def retried(messages: list[ModelMessage]) -> bool:
    from pydantic_ai.messages import RetryPromptPart
    return any(isinstance(p, RetryPromptPart) for msg in messages if isinstance(msg, ModelRequest) for p in msg.parts)


# ---------------------------------------------------------------------------
# Helpers used by the real agents' output validators (not only by the mocks)
# ---------------------------------------------------------------------------
def prompt_text(ctx) -> str:
    """The task text of the current run. Via A2A the message arrives in the history, so ctx.prompt can be empty."""
    if isinstance(getattr(ctx, "prompt", None), str) and ctx.prompt:
        return ctx.prompt
    return user_prompt(ctx.messages)


def tool_results(messages: list[ModelMessage], tool_name: str) -> list[Any]:
    """All return values of one tool in this run (oldest first)."""
    return [as_json(c) for n, c in tool_returns(messages) if n == tool_name]
