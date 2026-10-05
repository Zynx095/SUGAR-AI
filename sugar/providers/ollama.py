"""Ollama provider (local, offline fallback).

Talks to ``/api/chat`` directly with streaming NDJSON instead of the
``ollama`` package: one less dependency, real streaming, and the same
cancellation semantics as the other providers.
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


class ThinkTagFilter:
    """Separates ``<think>…</think>`` reasoning (DeepSeek-R1 style) from answer text."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._buffer = ""

    def feed(self, text: str) -> list[StreamChunk]:
        self._buffer += text
        out: list[StreamChunk] = []
        while self._buffer:
            tag = self.CLOSE if self._inside else self.OPEN
            index = self._buffer.find(tag)
            if index == -1:
                # Keep a possible partial tag at the end for the next feed.
                keep = max(0, len(self._buffer) - len(tag) + 1)
                emit, self._buffer = self._buffer[:keep], self._buffer[keep:]
                if emit:
                    out.append(StreamChunk("reasoning" if self._inside else "text", text=emit))
                break
            emit = self._buffer[:index]
            if emit:
                out.append(StreamChunk("reasoning" if self._inside else "text", text=emit))
            self._buffer = self._buffer[index + len(tag):]
            self._inside = not self._inside
        return out

    def flush(self) -> list[StreamChunk]:
        rest, self._buffer = self._buffer, ""
        if not rest:
            return []
        return [StreamChunk("reasoning" if self._inside else "text", text=rest)]


class OllamaProvider(LLMProvider):
    is_local = True

    def __init__(
        self,
        base_url: str,
        default_model: str,
        *,
        keep_alive: str = "10m",
        timeout_s: float = 60.0,
        supports_tools: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = "ollama"
        self.default_model = default_model
        self.supports_tools = supports_tools
        self._keep_alive = keep_alive
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=2.0),
            transport=transport,
        )
        self.available_models: set[str] = set()

    def describe(self, model: str | None = None) -> str:
        return f"ollama:{model or self.default_model}"

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
        converted = []
        for message in messages:
            item: dict[str, Any] = {"role": message["role"], "content": message.get("content") or ""}
            if message.get("tool_calls"):
                item["tool_calls"] = [
                    {
                        "function": {
                            "name": call["function"]["name"],
                            "arguments": parse_tool_arguments(call["function"].get("arguments")),
                        }
                    }
                    for call in message["tool_calls"]
                ]
            converted.append(item)
        return converted

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
        model_name = model or self.default_model
        body: dict[str, Any] = {
            "model": model_name,
            "messages": self._convert_messages(messages),
            "stream": True,
            "keep_alive": self._keep_alive,
            "options": {},
        }
        if temperature is not None:
            body["options"]["temperature"] = temperature
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        if tools and self.supports_tools:
            body["tools"] = tools

        think = ThinkTagFilter()
        calls: list[ToolCall] = []
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise ProviderError(
                        self.name,
                        f"HTTP {response.status_code}: {detail}",
                        retryable=response.status_code >= 500,
                        status=response.status_code,
                    )
                yield StreamChunk("meta", data={"model": f"ollama/{model_name}"})
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise ProviderError(self.name, str(data["error"]))
                    message = data.get("message") or {}
                    if message.get("thinking"):
                        yield StreamChunk("reasoning", text=message["thinking"])
                    if message.get("content"):
                        for chunk in think.feed(message["content"]):
                            yield chunk
                    for index, call in enumerate(message.get("tool_calls") or []):
                        function = call.get("function") or {}
                        calls.append(
                            ToolCall(
                                id=call.get("id") or f"ollama_call_{len(calls) + index}",
                                name=function.get("name", ""),
                                arguments=parse_tool_arguments(function.get("arguments")),
                            )
                        )
                    if data.get("done"):
                        for chunk in think.flush():
                            yield chunk
                        yield StreamChunk(
                            "usage",
                            data={
                                "prompt_tokens": data.get("prompt_eval_count"),
                                "completion_tokens": data.get("eval_count"),
                            },
                        )
                        break
        except httpx.ConnectError as exc:
            raise ProviderError(self.name, "Ollama is not running") from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"transport error: {exc}") from exc
        if calls:
            yield StreamChunk("tool_calls", tool_calls=calls)

    async def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            response = await self._client.get("/api/tags", timeout=2.0)
        except httpx.HTTPError:
            return ProviderHealth(False, "Ollama is not running")
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return ProviderHealth(False, f"HTTP {response.status_code}", latency)
        self.available_models = {m.get("name", "") for m in response.json().get("models", [])}
        if not any(name.startswith(self.default_model) for name in self.available_models):
            return ProviderHealth(False, f"model {self.default_model} is not pulled", latency)
        return ProviderHealth(True, "ok", latency)

    async def aclose(self) -> None:
        await self._client.aclose()
