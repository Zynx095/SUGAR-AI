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
from sugar.intelligence.domain import REFUSAL
from sugar.providers.base import LLMProvider, ProviderHealth, StreamChunk

WAKE = ["sid", "syd", "sidd", "sydd", "cid"]
AFTER_HEY = ["side", "said", "seed", "sit", "sed", "cyd", "sith"]


@pytest.mark.parametrize(("text", "found", "rest"), [
    ("Hey SID, run the tests.", True, "run the tests."),
    ("Hey Sid what is a closure", True, "what is a closure"),
    ("SID, fix the build.", True, "fix the build."),
    ("Sid.", True, ""),
    ("Syd, status", True, "status"),
    ("S.I.D. open the project", True, "open the project"),
    ("S I D, status", True, "status"),
    ("run the tests, Sid.", True, "run the tests"),
    ("Hey side, explain recursion", True, "explain recursion"),  # Whisper's usual mishearing, after "hey"
    ("Hesed, run the tests.", True, "run the tests."),  # measured: Whisper large-v3-turbo fused "Hey Sid"
    ("The Hasidic tradition", False, "The Hasidic tradition"),
    # What Whisper actually produced from the user's "Hey SID" on this machine's microphone (data/logs):
    ("He said.", True, ""),
    ("acid", True, ""),
    ("okay let's try it he said", True, "okay let's try it"),
    ("He said, what is a closure?", True, "what is a closure?"),
    ("Tell me a joke about Java", False, "Tell me a joke about Java"),
    ("put it on the left side", False, "put it on the left side"),
    ("I said hello", False, "I said hello"),
    ("His name is Sid", False, "His name is Sid"),
    ("Sidney called", False, "Sidney called"),
    ("Pass the salt", False, "Pass the salt"),
])
def test_wake_word(text, found, rest):
    assert split_wake_word(text, WAKE, AFTER_HEY) == (found, rest)


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


def test_typed_refusal_and_programming_answer(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["A closure captures variables from its enclosing scope."]}])
        provider = app.pool.providers["freellm"]
        await app.start()
        app.settings.conversation.scope = "engineering_only"
        refused = await collect_replies(app, lambda: app.conversation.handle_text("tell me a joke"))
        app.settings.conversation.scope = "universal"
        scripted_before = len(provider.turns)
        answer = await collect_replies(app, lambda: app.conversation.handle_text("What is a closure in Python?"))
        history = app.memory.recent_turns(app.conversation.conversation_id)
        await app.shutdown()
        return refused[0], scripted_before, answer[0], history

    refused, scripted_before, answer, history = run(scenario())
    assert refused["text"] == REFUSAL and refused["route"] == "rejected"
    assert scripted_before == 1  # the refusal never reached the model
    assert answer["text"].startswith("A closure captures") and answer["route"] == "coding_qa"
    assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]


def _fake_claude(app):
    """Replace Claude Code with a recorder (unit tests never start the real CLI)."""
    started = []

    async def start_task(project, task, *, mode=None, resume_note=None, destructive_confirmed=False):
        from sugar.coding.sessions import CodingSession

        started.append({"project": project.path, "task": task, "mode": mode, "destructive": destructive_confirmed})
        session = CodingSession(project.name, project.path, status="running", task=task, mode=mode or "work")
        return session

    app.sessions.start_task = start_task
    return started


@pytest.mark.parametrize(("answer", "runs"), [("yes", True), ("no", False)])
def test_spoken_confirmation_gates_a_destructive_engineering_task(settings, tmp_path, answer, runs):
    async def scenario():
        app = make_app(settings, [])
        started = _fake_claude(app)
        project = tmp_path / "shop"
        project.mkdir()
        app.working.set_project("shop", str(project))
        await app.start()
        asked = asyncio.Event()
        app.bus.subscribe("permission.request", lambda e: asked.set())
        task = asyncio.create_task(collect_replies(app, lambda: app.conversation.handle_text(
            "Delete the node_modules folder")))
        await asyncio.wait_for(asked.wait(), 5)
        await app.conversation.handle_text(answer)
        replies = await task
        await app.shutdown()
        return started, replies

    started, replies = run(scenario())
    assert "This needs your confirmation" in replies[0]["text"]
    if runs:
        assert len(started) == 1 and started[0]["destructive"] is True
    else:
        assert started == []


def test_stop_cancels_a_running_turn(settings):
    async def scenario():
        app = make_app(settings, [{"text": ["word "] * 200}], delay=0.02)
        await app.start()
        cancelled = asyncio.Event()
        app.bus.subscribe("turn.cancelled", lambda e: cancelled.set())
        await app.conversation.handle_text("explain recursion in detail")
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

        app.conversation._stt = FakeSTT("Hey SID, what is a closure?")
        start, end = voice_events(2)
        replies = await collect_replies(app, lambda: _feed(app, start, end))
        engaged = app.conversation.is_engaged()
        await app.shutdown()
        return replies, engaged

    replies, engaged = run(scenario())
    assert replies[0]["text"] == "Sure." and replies[0]["route"] == "coding_qa"
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
        await app.conversation.handle_text("explain how the python garbage collector works")
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


@pytest.mark.parametrize(("text", "kind"), [
    ("Hey Sid, run the tests.", "exact"), ("Syd, status", "exact"), ("S.I.D. open it", "exact"),
    ("He said the build is failing.", "variant"), ("Haced run the tests.", "variant"), ("acid", "variant"),
])
def test_wake_match_kind(text, kind):
    from sugar.intelligence.conversation import wake_match_kind

    assert wake_match_kind(text, WAKE) == kind


def test_look_alike_wake_is_verified_before_waking(settings):
    """'He said…' wakes SID only if a second, name-biased listen hears 'SID'."""
    async def scenario(verified):
        app = make_app(settings, [{"text": ["Sure."]}])

        class VerifyingSTT(FakeSTT):
            def verify_wake(self, audio):
                return verified, "hey SID run the tests" if verified else "he said the build is failing", 5

        app.conversation._stt = VerifyingSTT("He said, run the tests.")
        app.conversation._pipeline = FakePipeline()
        await app.start()
        app.conversation.disengage()
        events = []
        app.bus.subscribe("wake.*", events.append)
        app.bus.subscribe("stt.ignored", events.append)
        start, end = voice_events(1)
        app.conversation.on_voice_event(start)
        app.conversation.on_voice_event(end)
        await asyncio.sleep(0.5)
        await app.shutdown()
        return [e.type for e in events]

    assert run(scenario(False)) == ["wake.rejected", "stt.ignored"]
    assert run(scenario(True))[0] == "wake.detected"


def test_stop_while_only_claude_code_is_running_asks_first(settings):
    from sugar.coding.sessions import CodingSession

    async def scenario(answer):
        app = make_app(settings, [])
        session = CodingSession("shop", "D:/work/shop", status="running", task="add tests")
        app.sessions._sessions["d:/work/shop"] = session
        stopped = []

        async def stop(target, reason=""):
            stopped.append(reason)
            target.status = "cancelled"
            return True

        app.sessions.stop = stop
        await app.start()
        asked = asyncio.Event()
        questions = []
        app.bus.subscribe("permission.request", lambda e: (questions.append(e.data["action"]), asked.set()))
        await app.conversation.handle_text("stop")
        await asyncio.wait_for(asked.wait(), 3)
        await app.conversation.handle_text(answer)
        await asyncio.sleep(0.3)
        await app.shutdown()
        return questions, stopped

    questions, stopped = run(scenario("yes"))
    assert questions == ["stop the Claude Code task in shop"] and stopped == ["the user said stop"]
    assert run(scenario("no"))[1] == []
