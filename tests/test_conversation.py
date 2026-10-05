"""Conversation manager end-to-end with fake audio/STT and scripted models."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from conftest import run

from sugar.app.application import SugarApp
from sugar.audio.endpointing import EndpointEvent
from sugar.audio.stt import Transcript
from sugar.intelligence.conversation import parse_yes_no, split_wake_word
from sugar.providers.base import LLMProvider, ProviderHealth, StreamChunk, ToolCall
from sugar.tools.registry import PermissionLevel, Tool, ToolResult, params

WAKE = ["sugar", "shugar", "suga"]


@pytest.mark.parametrize(("text", "found", "rest"), [
    ("Sugar, open Chrome.", True, "open Chrome."),
    ("Hey Sugar what time is it", True, "what time is it"),
    ("Shugar.", True, ""),
    ("open chrome, sugar", True, "open chrome"),
    ("I love sugar in my tea", False, "I love sugar in my tea"),
    ("Pass the salt", False, "Pass the salt"),
])
def test_wake_word(text, found, rest):
    assert split_wake_word(text, WAKE) == (found, rest)


@pytest.mark.parametrize(("text", "value"), [
    ("Yes.", True), ("yeah do it", True), ("Go ahead", True), ("no", False), ("Nope, don't.", False),
    ("cancel", False), ("what's the weather", None), ("yes but not now please thanks", None),
])
def test_yes_no(text, value):
    assert parse_yes_no(text) == value


class ScriptedProvider(LLMProvider):
    name = "freellm"
    supports_tools = True

    def __init__(self, turns, delay=0.0):
        self.turns = list(turns)
        self.delay = delay

    async def stream(self, messages, **kwargs):
        turn = self.turns.pop(0) if self.turns else {"text": ["(no script)"]}
        yield StreamChunk("meta", data={"model": "scripted"})
        for piece in turn.get("text", []):
            await asyncio.sleep(self.delay)
            yield StreamChunk("text", text=piece)
        if turn.get("calls"):
            yield StreamChunk("tool_calls", tool_calls=turn["calls"])

    async def health_check(self):
        return ProviderHealth(True)


class FakeSTT:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, *, final, evidence=None):
        self.calls += 1
        return Transcript(self.text, "fake", 5, len(audio) / 16000)


class FakeEndpointer:
    def __init__(self):
        self.hints = {}

    def set_hint(self, utterance_id, completeness):
        self.hints[utterance_id] = completeness

    def snapshot(self, utterance_id=None):
        return None


class FakePipeline:
    def __init__(self):
        self.endpointer = FakeEndpointer()
        self.paused = False

    def pause(self):
        self.paused = True


def make_app(settings, turns, delay=0.0):
    settings.ui.enabled = False
    app = SugarApp(settings, voice=False)
    app.audio_output = False
    provider = ScriptedProvider(turns, delay)
    app.pool.providers.clear()
    app.pool.providers["freellm"] = provider  # the pool keeps its circuit-breaker state per name
    settings.llm.chat_chain = ["freellm"]
    app.apps.loaded_at = 9e12  # don't rescan Start-menu apps during tests
    return app


async def collect_replies(app, action, wait_s=5.0, count=1):
    replies = []
    done = asyncio.Event()

    def on_message(event):
        replies.append(event.data)
        if len(replies) >= count:
            done.set()

    unsubscribe = app.bus.subscribe("assistant.message", on_message)
    await action()
    await asyncio.wait_for(done.wait(), wait_s)
    unsubscribe()
    return replies


def test_typed_command_and_chat(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["Canberra is the capital."]}])
        await app.start()
        time_reply = await collect_replies(app, lambda: app.conversation.handle_text("what time is it"))
        chat_reply = await collect_replies(app, lambda: app.conversation.handle_text("capital of australia?"))
        history = app.memory.recent_turns(app.conversation.conversation_id)
        await app.shutdown()
        return time_reply[0], chat_reply[0], history

    time_reply, chat_reply, history = run(scenario())
    assert time_reply["text"].startswith("It's ")
    assert chat_reply["text"] == "Canberra is the capital." and chat_reply["route"] == "chat"
    assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]


def test_spoken_confirmation_unblocks_a_sensitive_tool(settings):
    async def scenario():
        app = make_app(settings, [
            {"calls": [ToolCall("c1", "danger__run", {"what": "rm build"})]},
            {"text": ["Done, I ran it."]},
        ])
        ran = []

        async def handler(args):
            ran.append(args["what"])
            return ToolResult(True, "ran it")

        app.registry.register(Tool("danger.run", "dangerous", params(["what"], what={"type": "string"}), handler,
                                   PermissionLevel.SENSITIVE, 5, frozenset({"agent", "chat"})))
        from sugar.intelligence import router as router_module
        router_module.CHAT_TOOLS.add("danger.run")
        await app.start()
        asked = asyncio.Event()
        app.bus.subscribe("permission.request", lambda e: asked.set())
        task = asyncio.create_task(collect_replies(app, lambda: app.conversation.handle_text("clean the build")))
        await asyncio.wait_for(asked.wait(), 5)
        await app.conversation.handle_text("yes")
        replies = await task
        router_module.CHAT_TOOLS.discard("danger.run")
        await app.shutdown()
        return ran, replies

    ran, replies = run(scenario())
    assert ran == ["rm build"]
    assert "I need your OK to" in replies[0]["text"] and replies[0]["text"].endswith("Done, I ran it.")


def test_stop_cancels_a_running_turn(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["word "] * 200}], delay=0.02)
        await app.start()
        cancelled = asyncio.Event()
        app.bus.subscribe("turn.cancelled", lambda e: cancelled.set())
        await app.conversation.handle_text("tell me a very long story")
        await asyncio.sleep(0.2)
        await app.conversation.handle_text("stop")
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.sleep(0.05)
        state = app.state.state.value
        turn = app.conversation._turn
        await app.shutdown()
        return state, turn

    state, turn = run(scenario())
    assert turn is None and state == "idle"


def voice_events(utterance_id, seconds=1.0):
    audio = np.zeros(int(16000 * seconds), dtype=np.float32)
    start = EndpointEvent("start", utterance_id, 1.0, speech_start_t=1.0)
    end = EndpointEvent("end", utterance_id, 2.5, audio=audio, voiced_ms=900, speech_start_t=1.0,
                        speech_end_t=2.0, mean_prob=0.9)
    return start, end


def test_voice_utterance_needs_wake_word_then_stays_engaged(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["Sure."]}])
        app.conversation._stt = FakeSTT("open the pod bay doors")
        app.conversation._pipeline = FakePipeline()
        await app.start()
        ignored = asyncio.Event()
        app.bus.subscribe("stt.ignored", lambda e: ignored.set())
        app.conversation.disengage()
        start, end = voice_events(1)
        app.conversation.on_voice_event(start)
        app.conversation.on_voice_event(end)
        await asyncio.wait_for(ignored.wait(), 2)

        app.conversation._stt = FakeSTT("Sugar, what time is it?")
        start, end = voice_events(2)
        replies = await collect_replies(app, lambda: _feed(app, start, end))
        engaged = app.conversation.is_engaged()
        await app.shutdown()
        return replies, engaged

    replies, engaged = run(scenario())
    assert replies[0]["text"].startswith("It's ")
    assert engaged


async def _feed(app, *events):
    for event in events:
        app.conversation.on_voice_event(event)


def test_barge_in_stops_speech_and_cancels_the_turn(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["Okay, so the project is currently "] + ["very "] * 100}], delay=0.02)
        await app.start()
        app.speech.muted = False

        class SlowSynth:
            engine_name = "fake"

            def synthesize(self, text):
                return np.full(4410, 0.1, dtype=np.float32)

        app.speech._synth = SlowSynth()

        class LiveStream:
            def stop(self): pass

            def close(self): pass

            active = True

        app.player._stream = LiveStream()
        barge = asyncio.Event()
        app.bus.subscribe("barge_in", lambda e: barge.set())
        await app.conversation.handle_text("what's the project status")
        for _ in range(100):  # wait until audio is "playing"
            await asyncio.sleep(0.01)
            app.player._callback(np.zeros((512, 1), dtype=np.float32), 512, None, type("F", (), {"output_underflow": False})())
            if app.speech.speaking and app.player.is_active:
                break
        app.conversation._stt = FakeSTT("wait stop")
        app.conversation._pipeline = FakePipeline()
        start, _ = voice_events(5)
        app.conversation.on_voice_event(start)
        app.conversation.on_barge_in(5, detected_at=__import__("time").perf_counter())
        await asyncio.wait_for(barge.wait(), 1)
        out = np.zeros((512, 1), dtype=np.float32)
        app.player._callback(out, 512, None, type("F", (), {"output_underflow": False})())  # plays the 10 ms fade
        await asyncio.sleep(0.1)  # nothing new may be queued by the cancelled turn
        app.player._callback(out, 512, None, type("F", (), {"output_underflow": False})())
        silent_after = float(np.abs(out).max())
        speaking = app.player.is_active
        turn = app.conversation._turn
        await app.shutdown()
        return speaking, turn, silent_after

    speaking, turn, silent_after = run(scenario())
    assert speaking is False
    assert silent_after == 0.0
    assert turn is None
