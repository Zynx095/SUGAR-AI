"""Claude providers.

Two transports, chosen at start-up:

* :class:`ClaudeAPIProvider` — the official ``anthropic`` SDK, used when an API
  key (``ANTHROPIC_API_KEY``) is configured. Streams text, supports tools.
* :class:`ClaudeCLIProvider` — the installed Claude CLI in print mode with all
  tools and MCP servers disabled, so it behaves as a plain model. This is what
  works with a Claude subscription (no API key). Text only.

Neither one pretends to be Claude Code; agentic coding goes through
``sugar.coding``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sugar.coding.claude_code import ClaudeCodeRunner, ClaudeRunRequest, probe_claude_cli
from sugar.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderHealth,
    StreamChunk,
    ToolCall,
    parse_tool_arguments,
)

log = logging.getLogger(__name__)

ANTHROPIC_EXTRAS_KEY = "_anthropic_content"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DROPPED_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}


def _openai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function", tool)
        converted.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                "eager_input_streaming": True,
            }
        )
    return converted


def _echoable(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the echo rule for responses that fell back mid-output.

    Thinking / tool_use blocks that precede the last ``fallback`` marker belong
    to the declined attempt and must not be sent back.
    """
    last_fallback = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    kept = []
    for index, block in enumerate(blocks):
        block_type = block.get("type")
        if block_type == "fallback":
            continue
        if index < last_fallback and block_type in DROPPED_BEFORE_FALLBACK:
            continue
        kept.append(block)
    return kept


def openai_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
            continue
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": str(message.get("content") or ""),
                    **({"is_error": True} if message.get("is_error") else {}),
                }
            )
            continue
        flush_results()
        if role == "assistant":
            if message.get(ANTHROPIC_EXTRAS_KEY):
                content = _echoable(message[ANTHROPIC_EXTRAS_KEY])
            else:
                content = []
                if message.get("content"):
                    content.append({"type": "text", "text": str(message["content"])})
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": function.get("name"),
                            "input": parse_tool_arguments(function.get("arguments")),
                        }
                    )
            if content:
                converted.append({"role": "assistant", "content": content})
        else:
            converted.append({"role": "user", "content": str(message.get("content") or "")})
    flush_results()
    return "\n\n".join(p for p in system_parts if p), converted


class ClaudeAPIProvider(LLMProvider):
    supports_tools = True

    def __init__(self, api_key: str, model: str, *, max_tokens: int = 16000, timeout_s: float = 180.0) -> None:
        import anthropic

        self.name = "claude"
        self.default_model = model
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=1)
        self._anthropic = anthropic

    def describe(self, model: str | None = None) -> str:
        return f"claude-api:{model or self.default_model}"

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,  # not accepted by current Opus models
        max_tokens: int | None = None,
        purpose: str = "chat",
    ) -> AsyncIterator[StreamChunk]:
        anthropic = self._anthropic
        system, converted = openai_to_anthropic(messages)
        effort = "low" if purpose == "chat" else "medium"
        if purpose == "chat":
            system = (system + "\n\nLatency-sensitive; begin your visible answer immediately.").strip()
        params: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": converted,
            "output_config": {"effort": effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = _openai_tools_to_anthropic(tools)

        try:
            async with self._client.beta.messages.stream(**params) as stream:
                yield StreamChunk("meta", data={"model": params["model"]})
                async for event in stream:
                    if event.type == "text":
                        yield StreamChunk("text", text=event.text)
                final = await stream.get_final_message()
        except ValueError as exc:  # unparseable eager tool input
            raise ProviderError(self.name, f"malformed tool input: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(self.name, "rate limited", status=429) from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError(self.name, "invalid API key", retryable=False, status=401) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(self.name, f"bad request: {exc.message}", retryable=False, status=400) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(self.name, f"HTTP {exc.status_code}", status=exc.status_code) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(self.name, "connection failed") from exc

        if final.stop_reason == "refusal":
            raise ProviderError(self.name, "request declined by safety classifiers", retryable=False)

        blocks = [block.model_dump(exclude_none=True) for block in final.content]
        calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in _echoable(blocks)
            if b.get("type") == "tool_use"
        ]
        if final.stop_reason == "max_tokens" and calls:
            # A truncated tool input parses as a plausible partial object; never run it.
            raise ProviderError(self.name, "tool input was truncated (max_tokens)")
        yield StreamChunk("meta", data={"model": final.model, "assistant_extras": {ANTHROPIC_EXTRAS_KEY: blocks}})
        if calls:
            yield StreamChunk("tool_calls", tool_calls=calls)
        usage = final.usage
        yield StreamChunk("usage", data={"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens})

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(True, "API key configured")

    async def aclose(self) -> None:
        await self._client.close()


def _transcript(messages: list[Message]) -> tuple[str, str]:
    """Flatten a chat into (system prompt, single prompt) for the CLI."""
    system_parts: list[str] = []
    turns: list[str] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            turns.append(f"User: {content}")
        elif role == "assistant":
            turns.append(f"Assistant: {content}")
        elif role == "tool":
            turns.append(f"[tool result] {content[:2000]}")
    if not turns:
        return "\n\n".join(system_parts), ""
    *history, latest = turns
    prompt = latest.removeprefix("User: ")
    if history:
        prompt = "Conversation so far:\n" + "\n".join(history) + "\n\nRespond to the user's latest message:\n" + prompt
    return "\n\n".join(system_parts), prompt


class ClaudeCLIProvider(LLMProvider):
    supports_tools = False

    def __init__(self, executable: str, model: str, workdir: Path, timeout_s: float = 180.0) -> None:
        self.name = "claude"
        self.default_model = model
        self._executable = executable
        self._workdir = workdir
        self._timeout_s = timeout_s
        workdir.mkdir(parents=True, exist_ok=True)

    def describe(self, model: str | None = None) -> str:
        return f"claude-cli:{model or self.default_model}"

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "chat",
    ) -> AsyncIterator[StreamChunk]:
        info = await probe_claude_cli(self._executable)
        system, prompt = _transcript(messages)
        if not prompt:
            raise ProviderError(self.name, "empty prompt", retryable=False)
        runner = ClaudeCodeRunner(info)
        request = ClaudeRunRequest(
            prompt=prompt,
            cwd=self._workdir,
            model=model or self.default_model,
            system_prompt=system or "You are a helpful assistant.",
            tools="",
            partial_messages=True,
            persist_session=False,
            isolated=True,
            effort="low" if purpose == "chat" else None,
        )
        queue: asyncio.Queue[StreamChunk | None] = asyncio.Queue()

        def on_event(event) -> None:
            if event.kind == "delta" and event.data.get("text"):
                queue.put_nowait(StreamChunk("text", text=event.data["text"]))
            elif event.kind == "init":
                queue.put_nowait(StreamChunk("meta", data={"model": f"claude-cli/{event.data.get('model')}"}))

        async def produce() -> Any:
            try:
                return await asyncio.wait_for(runner.run(request, on_event), timeout=self._timeout_s)
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
            result = await task
        except TimeoutError as exc:
            raise ProviderError(self.name, "Claude CLI timed out") from exc
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if not result.ok:
            raise ProviderError(self.name, result.error or "Claude CLI failed")

    async def health_check(self) -> ProviderHealth:
        try:
            info = await probe_claude_cli(self._executable)
        except (TimeoutError, OSError) as exc:
            return ProviderHealth(False, f"Claude CLI unavailable: {exc}")
        return ProviderHealth(True, f"Claude CLI {info.version}")
