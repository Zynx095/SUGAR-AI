"""OpenAI-compatible streaming provider (used for FreeLLMAPI).

FreeLLMAPI specifics (discovered from the installed server, see
ARCHITECTURE_AUDIT.md §3.2):
  * ``POST {base}/chat/completions`` with ``Authorization: Bearer <key>``
  * model ``auto`` / ``auto:<axis|profile>`` lets its router pick a backend
  * the backend actually used is reported in the ``X-Routed-Via`` header
  * mid-stream failures arrive as an in-band ``{"error": …}`` chunk
  * Gemini tool calls carry ``thought_signature``, which must be echoed back
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from sugar.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderHealth,
    StreamChunk,
    ToolCall,
    parse_tool_arguments,
)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    supports_tools = True

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        default_model: str,
        *,
        timeout_s: float = 45.0,
        connect_timeout_s: float = 3.0,
        extra_body: dict[str, Any] | None = None,
        is_local: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.default_model = default_model
        self.is_local = is_local
        self._extra_body = {k: v for k, v in (extra_body or {}).items() if v is not None}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._has_key = bool(api_key)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            transport=transport,
        )

    def describe(self, model: str | None = None) -> str:
        return f"{self.name}:{model or self.default_model}"

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
        body: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        body.update(self._extra_body)

        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise ProviderError(
                        self.name,
                        f"HTTP {response.status_code}: {detail}",
                        retryable=response.status_code in RETRYABLE_STATUS,
                        status=response.status_code,
                    )
                routed = response.headers.get("x-routed-via")
                yield StreamChunk("meta", data={"model": routed or body["model"]})
                async for chunk in self._parse_sse(response):
                    yield chunk
        except httpx.ConnectError as exc:
            raise ProviderError(self.name, f"unreachable ({exc})") from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"transport error: {exc}") from exc

    async def _parse_sse(self, response: httpx.Response) -> AsyncIterator[StreamChunk]:
        pending: dict[int, dict[str, Any]] = {}
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("error") and not chunk.get("choices"):
                error = chunk["error"]
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise ProviderError(self.name, f"stream error: {message}")
            usage = chunk.get("usage")
            if usage:
                yield StreamChunk("usage", data=usage)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield StreamChunk("text", text=content)
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    yield StreamChunk("reasoning", text=reasoning)
                for call in delta.get("tool_calls") or []:
                    self._accumulate_tool_call(pending, call)
        if pending:
            calls = []
            for index in sorted(pending):
                acc = pending[index]
                if not acc["name"]:
                    continue
                calls.append(
                    ToolCall(
                        id=acc["id"] or f"call_{index}_{int(time.time() * 1000)}",
                        name=acc["name"],
                        arguments=parse_tool_arguments(acc["arguments"]),
                        raw=acc["extra"],
                    )
                )
            if calls:
                yield StreamChunk("tool_calls", tool_calls=calls)

    @staticmethod
    def _accumulate_tool_call(pending: dict[int, dict[str, Any]], call: dict[str, Any]) -> None:
        index = call.get("index", len(pending))
        acc = pending.setdefault(index, {"id": None, "name": "", "arguments": "", "extra": {}})
        if call.get("id"):
            acc["id"] = call["id"]
        function = call.get("function") or {}
        if function.get("name") and not acc["name"]:
            acc["name"] = function["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            acc["arguments"] = json.dumps(arguments)
        elif arguments:
            acc["arguments"] += arguments
        for key, value in call.items():
            if key not in {"index", "id", "type", "function"}:
                acc["extra"][key] = value

    async def health_check(self) -> ProviderHealth:
        if not self._has_key and not self.is_local:
            return ProviderHealth(False, "no API key configured")
        started = time.perf_counter()
        try:
            response = await self._client.get("/models", timeout=4.0)
        except httpx.HTTPError as exc:
            return ProviderHealth(False, f"unreachable: {exc.__class__.__name__}")
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code == 200:
            return ProviderHealth(True, "ok", latency)
        return ProviderHealth(False, f"HTTP {response.status_code}", latency)

    async def aclose(self) -> None:
        await self._client.aclose()
