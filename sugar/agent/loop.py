"""The agent loop: think → act → observe → answer.

    model streams text and/or tool calls
      ├─ text  → spoken immediately (a preface like "Let me check." is fine)
      └─ calls → permission-checked execution → results appended as tool
                 messages → the model runs again and reports what *actually*
                 happened (it never sees a success it didn't get)

Bounded by ``max_steps``; repeated identical calls end the loop. The whole
loop is a single cancellable coroutine, so "stop" cancels the model stream
and any running tool.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sugar.agent.executor import ToolExecutor
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.core.metrics import TurnTrace
from sugar.providers.base import Message, ToolCall
from sugar.providers.pool import ProviderPool
from sugar.tools.registry import ToolRegistry, ToolResult

log = logging.getLogger(__name__)


@dataclass
class ToolRecord:
    name: str
    arguments: dict[str, Any]
    result: ToolResult


@dataclass
class AgentResult:
    text: str
    provider: str | None = None
    model: str | None = None
    steps: int = 0
    tools: list[ToolRecord] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    def __init__(self, pool: ProviderPool, executor: ToolExecutor, registry: ToolRegistry, bus: EventBus) -> None:
        self._pool = pool
        self._executor = executor
        self._registry = registry
        self._bus = bus

    async def run(
        self,
        messages: list[Message],
        *,
        chain: list[str],
        tool_groups: set[str] | None,
        purpose: str,
        on_text: Callable[[str], None],
        on_tool_result: Callable[[ToolRecord], None] | None = None,
        trace: TurnTrace | None = None,
        max_steps: int = 6,
        model_for: dict[str, str] | None = None,
        tool_names: set[str] | None = None,
    ) -> AgentResult:
        tools = self._registry.schemas(tool_groups, tool_names) if tool_groups else None
        transcript = list(messages)
        result = AgentResult(text="")
        seen_calls: set[str] = set()
        spoken: list[str] = []
        first_token = True

        for step in range(1, max_steps + 1):
            result.steps = step
            text_parts: list[str] = []
            calls: list[ToolCall] = []
            extras: dict[str, Any] = {}
            if trace is not None:
                trace.mark("llm_start")
            started = time.perf_counter()
            async for chunk in self._pool.stream(chain, transcript, tools=tools, purpose=purpose,
                                                 model_for=model_for):
                if chunk.kind == "text":
                    if first_token:
                        first_token = False
                        ttft = int((time.perf_counter() - started) * 1000)
                        if trace is not None:
                            trace.mark("llm_first_token")
                        log_event("LLM_FIRST_TOKEN", ms=ttft, provider=result.provider, model=result.model)
                        self._bus.publish("llm.first_token", ms=ttft, provider=result.provider, model=result.model)
                    text_parts.append(chunk.text)
                    on_text(chunk.text)
                elif chunk.kind == "tool_calls":
                    calls.extend(chunk.tool_calls)
                elif chunk.kind == "meta":
                    result.provider = chunk.data.get("provider", result.provider)
                    result.model = chunk.data.get("model", result.model)
                    extras.update(chunk.data.get("assistant_extras") or {})
                elif chunk.kind == "usage":
                    result.usage = chunk.data
            if trace is not None:
                trace.mark("llm_done", overwrite=True)
            text = "".join(text_parts)
            spoken.append(text)
            self._bus.publish("llm.complete", provider=result.provider, model=result.model, step=step,
                              tool_calls=len(calls), usage=result.usage)
            if not calls:
                break

            transcript.append({"role": "assistant", "content": text,
                               "tool_calls": [call.to_message_part() for call in calls], **extras})
            repeated = False
            for call in calls:
                signature = call.name + json.dumps(call.arguments, sort_keys=True)
                if signature in seen_calls:
                    repeated = True
                    outcome = ToolResult.failure("Already tried that.", "repeated identical tool call")
                else:
                    seen_calls.add(signature)
                    if trace is not None:
                        trace.mark("tool_start")
                    outcome = await self._executor.execute(call.name, call.arguments, origin="model")
                    if trace is not None:
                        trace.mark("tool_done", overwrite=True)
                record = ToolRecord(call.name, call.arguments, outcome)
                result.tools.append(record)
                if on_tool_result is not None:
                    on_tool_result(record)
                transcript.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                   "content": outcome.for_model()})
            if repeated and step >= 2:
                transcript.append({"role": "user", "content": "Stop calling tools now and tell me the outcome."})
                tools = None
        else:
            # Out of steps while still acting: one last pass without tools so the user hears the outcome.
            log.info("agent loop hit max_steps=%d; summarising", max_steps)
            transcript.append({"role": "user", "content": "Stop here and briefly tell me what you did and what's left."})
            final: list[str] = []
            async for chunk in self._pool.stream(chain, transcript, tools=None, purpose=purpose, model_for=model_for):
                if chunk.kind == "text":
                    final.append(chunk.text)
                    on_text(chunk.text)
            spoken.append("".join(final))

        result.text = "".join(spoken).strip()
        return result
