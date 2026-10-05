"""Provider-neutral LLM interface.

Messages use the OpenAI chat format (``role`` / ``content`` / ``tool_calls`` /
``tool_call_id``) because it is what FreeLLMAPI, Ollama and most tools speak;
providers with a different wire format (Anthropic) translate internally.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

Message = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)  # provider extras to echo back verbatim

    def to_message_part(self) -> dict[str, Any]:
        part = dict(self.raw) if self.raw else {}
        part.update(
            {
                "id": self.id,
                "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
            }
        )
        return part


@dataclass
class StreamChunk:
    kind: Literal["text", "reasoning", "tool_calls", "meta", "usage"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall]
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderHealth:
    ok: bool
    detail: str = ""
    latency_ms: int | None = None


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, *, retryable: bool = True, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
        self.status = status


class LLMProvider(ABC):
    name: str = "provider"
    supports_tools: bool = False
    is_local: bool = False

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "chat",
    ) -> AsyncIterator[StreamChunk]:
        """Yield chunks as they arrive. Tool calls arrive as one ``tool_calls`` chunk."""

    @abstractmethod
    async def health_check(self) -> ProviderHealth: ...

    async def generate(self, messages: list[Message], **kwargs: Any) -> Completion:
        text: list[str] = []
        calls: list[ToolCall] = []
        model = kwargs.get("model") or ""
        usage: dict[str, Any] = {}
        async for chunk in self.stream(messages, **kwargs):
            if chunk.kind == "text":
                text.append(chunk.text)
            elif chunk.kind == "tool_calls":
                calls.extend(chunk.tool_calls)
            elif chunk.kind == "meta":
                model = chunk.data.get("model", model)
            elif chunk.kind == "usage":
                usage = chunk.data
        return Completion("".join(text), calls, self.name, model, usage)

    def describe(self, model: str | None = None) -> str:
        return f"{self.name}:{model or 'default'}"

    async def aclose(self) -> None:
        return None


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string (OpenAI) or an object (Ollama)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}
