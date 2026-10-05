"""Agent loop and orchestrator with scripted providers (no network)."""

from __future__ import annotations

from typing import Any

from conftest import run

from sugar.agent.executor import ToolExecutor
from sugar.agent.loop import AgentLoop
from sugar.agent.permissions import PermissionManager
from sugar.config.settings import PermissionSettings
from sugar.core.metrics import TurnTrace
from sugar.providers.base import LLMProvider, ProviderHealth, StreamChunk, ToolCall
from sugar.providers.pool import ProviderPool
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params


class ScriptedProvider(LLMProvider):
    """Plays back a list of turns; each turn is text and/or tool calls."""

    name = "scripted"
    supports_tools = True

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def stream(self, messages, **kwargs):
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        yield StreamChunk("meta", data={"model": "scripted-1", "assistant_extras": turn.get("extras", {})})
        for piece in turn.get("text", []):
            yield StreamChunk("text", text=piece)
        if turn.get("calls"):
            yield StreamChunk("tool_calls", tool_calls=turn["calls"])

    async def health_check(self):
        return ProviderHealth(True)


def build(bus, provider, handler=None, level=PermissionLevel.READ):
    registry = ToolRegistry()

    async def weather(args):
        return ToolResult(True, f"It's 29 degrees in {args['location']}.", data={"temp": 29})

    registry.register(Tool("weather.current", "weather", params(["location"], location={"type": "string"}),
                           handler or weather, level, 5, frozenset({"agent", "chat"})))
    permissions = PermissionManager(PermissionSettings(confirmation_timeout_s=0.1), bus)
    executor = ToolExecutor(registry, permissions, bus)
    pool = ProviderPool({"scripted": provider}, bus)
    return AgentLoop(pool, executor, registry, bus)


def test_tool_call_then_grounded_answer(bus):
    provider = ScriptedProvider([
        {"text": ["Let me check. "], "calls": [ToolCall("c1", "weather__current", {"location": "Chennai"},
                                                        raw={"thought_signature": "sig"})]},
        {"text": ["It's 29 degrees ", "in Chennai."]},
    ])
    loop = build(bus, provider)
    spoken: list[str] = []
    trace = TurnTrace()
    result = run(loop.run([{"role": "user", "content": "weather?"}], chain=["scripted"], tool_groups={"chat"},
                          purpose="chat", on_text=spoken.append, trace=trace))
    assert "".join(spoken) == "Let me check. It's 29 degrees in Chennai."
    assert [t.name for t in result.tools] == ["weather__current"]
    second_request = provider.seen[1]
    assistant = second_request[-2]
    assert assistant["tool_calls"][0]["thought_signature"] == "sig"  # provider extras echoed back
    tool_message = second_request[-1]
    assert tool_message["role"] == "tool" and '"ok": true' in tool_message["content"]
    assert trace.has("llm_first_token") and trace.has("tool_start")


def test_failed_tool_result_reaches_the_model(bus):
    async def broken(args):
        raise RuntimeError("wttr.in is down")

    provider = ScriptedProvider([
        {"calls": [ToolCall("c1", "weather__current", {"location": "Paris"})]},
        {"text": ["I couldn't get the weather."]},
    ])
    loop = build(bus, provider, handler=broken)
    result = run(loop.run([{"role": "user", "content": "weather?"}], chain=["scripted"], tool_groups={"chat"},
                          purpose="chat", on_text=lambda t: None))
    assert result.tools[0].result.ok is False
    assert '"ok": false' in provider.seen[1][-1]["content"]


def test_unconfirmed_sensitive_tool_is_denied_not_executed(bus):
    ran = []

    async def handler(args):
        ran.append(args)
        return ToolResult(True, "ran")

    provider = ScriptedProvider([
        {"calls": [ToolCall("c1", "weather__current", {"location": "x"})]},
        {"text": ["Okay, I didn't do it."]},
    ])
    loop = build(bus, provider, handler=handler, level=PermissionLevel.SENSITIVE)
    result = run(loop.run([{"role": "user", "content": "do it"}], chain=["scripted"], tool_groups={"chat"},
                          purpose="chat", on_text=lambda t: None))
    assert ran == [] and result.tools[0].result.error == "permission denied by user"


def test_repeated_identical_calls_are_short_circuited_and_steps_bounded(bus):
    call = ToolCall("c", "weather__current", {"location": "Loop"})
    provider = ScriptedProvider([{"calls": [call]}] * 3 + [{"text": ["Summary."]}])
    loop = build(bus, provider)
    result = run(loop.run([{"role": "user", "content": "x"}], chain=["scripted"], tool_groups={"chat"},
                          purpose="chat", on_text=lambda t: None, max_steps=3))
    assert sum(1 for t in result.tools if t.result.ok) == 1
    assert result.text.endswith("Summary.")
