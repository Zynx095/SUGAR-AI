"""Providers: wire parsing, fallback chains, Claude message conversion. No network."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import run

from sugar.providers.base import LLMProvider, ProviderError, ProviderHealth, StreamChunk
from sugar.providers.claude import ANTHROPIC_EXTRAS_KEY, _echoable, _transcript, openai_to_anthropic
from sugar.providers.ollama import OllamaProvider, ThinkTagFilter
from sugar.providers.openai_compat import OpenAICompatibleProvider
from sugar.providers.pool import AllProvidersFailed, ProviderPool


def sse(*chunks: dict | str) -> bytes:
    lines = []
    for chunk in chunks:
        payload = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines.append(f"data: {payload}\n\n")
    return "".join(lines).encode()


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def collect(provider: LLMProvider, **kwargs) -> list[StreamChunk]:
    async def go():
        return [c async for c in provider.stream([{"role": "user", "content": "hi"}], **kwargs)]
    return run(go())


def make_freellm(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "freellm", "http://freellm.test/v1", "freellmapi-key", "auto",
        transport=httpx.MockTransport(handler), extra_body={"reasoning_effort": "low"},
    )


def test_freellm_streams_text_and_reports_routed_model():
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        body = sse(delta(role="assistant"), delta(content="Hel"), delta(content="lo."),
                   {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "[DONE]")
        return httpx.Response(200, content=body, headers={"x-routed-via": "google/gemini-2.5-flash"})

    chunks = collect(make_freellm(handler))
    assert "".join(c.text for c in chunks if c.kind == "text") == "Hello."
    assert chunks[0].kind == "meta" and chunks[0].data["model"] == "google/gemini-2.5-flash"
    assert any(c.kind == "usage" for c in chunks)
    request = seen_requests[0]
    assert request.headers["authorization"] == "Bearer freellmapi-key"
    body = json.loads(request.content)
    assert body["model"] == "auto" and body["stream"] is True and body["reasoning_effort"] == "low"


def test_freellm_accumulates_tool_calls_and_keeps_provider_extras():
    def handler(request):
        body = sse(
            delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                               "function": {"name": "app__launch", "arguments": '{"na'},
                               "thought_signature": "sig-abc"}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": 'me": "Chrome"}'}}]),
            "[DONE]",
        )
        return httpx.Response(200, content=body)

    chunks = collect(make_freellm(handler), tools=[{"type": "function", "function": {"name": "app__launch"}}])
    calls = [c for c in chunks if c.kind == "tool_calls"][0].tool_calls
    assert calls[0].name == "app__launch"
    assert calls[0].arguments == {"name": "Chrome"}
    replay = calls[0].to_message_part()
    assert replay["thought_signature"] == "sig-abc"  # echoed back for Gemini
    assert json.loads(replay["function"]["arguments"]) == {"name": "Chrome"}


def test_freellm_in_band_error_raises():
    def handler(request):
        return httpx.Response(200, content=sse({"error": {"message": "all keys exhausted"}}))

    with pytest.raises(ProviderError, match="all keys exhausted"):
        collect(make_freellm(handler))


def test_freellm_http_error_is_classified():
    def handler(request):
        return httpx.Response(429, json={"error": "slow down"})

    with pytest.raises(ProviderError) as info:
        collect(make_freellm(handler))
    assert info.value.retryable and info.value.status == 429


def test_think_tag_filter_splits_reasoning_across_chunk_boundaries():
    f = ThinkTagFilter()
    out = []
    for piece in ["<th", "ink>plan it</th", "ink>The answer", " is 4."]:
        out += f.feed(piece)
    out += f.flush()
    assert "".join(c.text for c in out if c.kind == "reasoning") == "plan it"
    assert "".join(c.text for c in out if c.kind == "text") == "The answer is 4."


def test_ollama_streams_ndjson():
    def handler(request):
        lines = [
            {"message": {"role": "assistant", "content": "Hi "}, "done": False},
            {"message": {"role": "assistant", "content": "there."}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 3, "prompt_eval_count": 9},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    provider = OllamaProvider("http://ollama.test", "gemma3:4b", transport=httpx.MockTransport(handler))
    chunks = collect(provider)
    assert "".join(c.text for c in chunks if c.kind == "text") == "Hi there."


def test_ollama_connection_failure_is_a_provider_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    provider = OllamaProvider("http://ollama.test", "gemma3:4b", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="not running"):
        collect(provider)


class FakeProvider(LLMProvider):
    def __init__(self, name, *, fail_before=False, fail_after=False, tools=False):
        self.name = name
        self.supports_tools = tools
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.calls = 0

    async def stream(self, messages, **kwargs):
        self.calls += 1
        if self.fail_before:
            raise ProviderError(self.name, "down")
        yield StreamChunk("text", text=f"from {self.name}")
        if self.fail_after:
            raise ProviderError(self.name, "dropped mid-stream")

    async def health_check(self):
        return ProviderHealth(True)


def consume(pool: ProviderPool, chain, **kwargs) -> str:
    async def go():
        return "".join([c.text async for c in pool.stream(chain, [{"role": "user", "content": "x"}], **kwargs)
                        if c.kind == "text"])
    return run(go())


def test_pool_falls_back_and_benches_failed_provider(bus):
    a, b = FakeProvider("a", fail_before=True), FakeProvider("b")
    pool = ProviderPool({"a": a, "b": b}, bus)
    assert consume(pool, ["a", "b"]) == "from b"
    assert consume(pool, ["a", "b"]) == "from b"
    assert a.calls == 1  # benched after the first failure
    assert pool.status()["a"]["available"] is False


def test_pool_does_not_replace_a_provider_that_already_spoke(bus):
    pool = ProviderPool({"a": FakeProvider("a", fail_after=True), "b": FakeProvider("b")}, bus)
    with pytest.raises(ProviderError):
        consume(pool, ["a", "b"])


def test_pool_prefers_tool_capable_providers(bus):
    plain, capable = FakeProvider("plain"), FakeProvider("capable", tools=True)
    pool = ProviderPool({"plain": plain, "capable": capable}, bus)
    assert consume(pool, ["plain", "capable"], tools=[{"type": "function"}]) == "from capable"


def test_pool_reports_total_failure(bus):
    pool = ProviderPool({"a": FakeProvider("a", fail_before=True)}, bus)
    with pytest.raises(AllProvidersFailed):
        consume(pool, ["a"])


def test_openai_to_anthropic_conversion_groups_tool_results():
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "open chrome and vscode"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "app__launch", "arguments": '{"name":"chrome"}'}},
            {"id": "t2", "type": "function", "function": {"name": "app__launch", "arguments": '{"name":"code"}'}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "tool", "tool_call_id": "t2", "content": "failed", "is_error": True},
    ]
    system, converted = openai_to_anthropic(messages)
    assert system == "be brief"
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert [b["type"] for b in converted[1]["content"]] == ["tool_use", "tool_use"]
    results = converted[2]["content"]  # all results in ONE user message
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"] and results[1]["is_error"] is True


def test_anthropic_blocks_are_replayed_verbatim_with_fallback_echo_rule():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "s1"},
        {"type": "text", "text": "partial"},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "s2"},
        {"type": "text", "text": "final"},
    ]
    assert [b.get("signature") or b.get("text") for b in _echoable(blocks)] == ["partial", "s2", "final"]
    _, converted = openai_to_anthropic([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "final", ANTHROPIC_EXTRAS_KEY: blocks},
    ])
    assert converted[1]["content"][0]["type"] == "text"


def test_cli_transcript_keeps_history_and_isolates_latest_message():
    system, prompt = _transcript([
        {"role": "system", "content": "You are Sugar."},
        {"role": "user", "content": "My project is JIVA."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "What is my project?"},
    ])
    assert system == "You are Sugar."
    assert prompt.endswith("What is my project?")
    assert "User: My project is JIVA." in prompt
