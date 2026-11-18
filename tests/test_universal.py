"""Universal SID: categories reach the right model path, context layers stay separate, voice/personality
switches are real, health safeguards hold, and provider routing falls back honestly."""

from __future__ import annotations

import asyncio
import json

from conftest import run

from sugar.app.application import SIDApp
from sugar.intelligence.health import contains_personal_health_data, emergency_message
from sugar.providers.base import LLMProvider, ProviderError, ProviderHealth, StreamChunk
from sugar.providers.pool import ProviderPool
from sugar.providers.routing import ProviderRouter


class Recording(LLMProvider):
    supports_tools = True

    def __init__(self, name="freellm", turns=None, fail=False, local=False):
        self.name = name
        self.turns = list(turns or [])
        self.calls: list[dict] = []
        self.fail = fail
        self.is_local = local

    async def stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.fail:
            raise ProviderError(self.name, "connection refused")
        turn = self.turns.pop(0) if self.turns else {"text": [f"({self.name} answer)"]}
        yield StreamChunk("meta", data={"model": self.name})
        for piece in turn.get("text", []):
            yield StreamChunk("text", text=piece)

    async def health_check(self):
        return ProviderHealth(not self.fail, "down" if self.fail else "ok")


def make_app(settings, providers):
    settings.ui.enabled = False
    app = SIDApp(settings, voice=False)
    app.audio_output = False
    app.apps.loaded_at = 9e12
    app.pool.providers.clear()
    for provider in providers:
        app.pool.providers[provider.name] = provider
    started = []

    async def start_task(*args, **kwargs):
        started.append(args)
        raise AssertionError("Claude Code must not start")

    app.sessions.start_task = start_task
    events: list = []
    app.bus.subscribe("*", events.append)
    return app, started, events


async def say(app, text, wait_s=5.0):
    done = asyncio.Event()
    replies = []

    def on_message(event):
        replies.append(event.data)
        done.set()

    unsubscribe = app.bus.subscribe("assistant.message", on_message)
    await app.conversation.handle_text(text)
    await asyncio.wait_for(done.wait(), wait_s)
    unsubscribe()
    return replies[0]


def test_acceptance_categories_answer_without_claude_code(settings):
    async def scenario():
        provider = Recording(turns=[{"text": ["Gravity so strong light can't escape."]},
                                    {"text": ["In the ashen keep, the crows remember."]},
                                    {"text": ["Haemoglobin carries oxygen in red blood cells."]}])
        app, started, events = make_app(settings, [provider])
        await app.start()
        replies = [await say(app, "Explain how black holes work."), await say(app, "Write a dark fantasy poem."),
                   await say(app, "Explain what haemoglobin measures in a blood test.")]
        await app.shutdown()
        return replies, started, provider, events

    replies, started, provider, events = run(scenario())
    assert [r["route"] for r in replies] == ["answer"] * 3 and started == []
    categories = [e.data["category"] for e in events if e.type == "route.selected"]
    assert categories == ["general_knowledge", "creative_writing", "health_information"]
    health_system = provider.calls[2]["messages"][0]["content"]
    assert "Never diagnose" in health_system


def test_roleplay_has_its_own_context_layer(settings):
    async def scenario():
        provider = Recording(turns=[{"text": ["Greetings, traveller from Earth."]}, {"text": ["We come in peace."]},
                                    {"text": ["Back to normal."]}, {"text": ["Python is a language."]}])
        app, _, _ = make_app(settings, [provider])
        await app.start()
        await say(app, "Let's roleplay a conversation between an astronaut and an alien.")
        await say(app, "Who are you?")
        ended = await say(app, "end the roleplay")
        await say(app, "What is Python?")
        await app.shutdown()
        return provider, ended, app

    provider, ended, app = run(scenario())
    second = provider.calls[1]["messages"]
    assert any("Greetings, traveller" in m.get("content", "") for m in second)  # the scene continues
    assert "roleplay" in second[0]["content"].lower()
    after = provider.calls[2]["messages"]
    assert not any("traveller" in m.get("content", "") for m in after)  # and never leaks out of it
    assert "Scene over" in ended["text"] and app.working.roleplay is None


def test_voice_switch_changes_tts_profile_palette_and_persists(settings):
    async def scenario():
        app, _, events = make_app(settings, [Recording()])
        await app.start()
        first = await say(app, "switch to your female voice")
        female = app.settings.tts.voice_profile
        await say(app, "make your voice deeper")
        back = await say(app, "switch back to your original voice")
        await app.shutdown()
        return first, back, female, events, app

    first, back, female, events, app = run(scenario())
    assert female == "female" and app.settings.tts.voice_profile == "sid"
    changes = [e.data for e in events if e.type == "voice.changed"]
    assert [c["id"] for c in changes] == ["female", "female", "sid"]
    assert changes[0]["palette"][0] == "#e8114f" and changes[-1]["palette"][0] == "#ffab3d"
    overrides = json.loads(app.settings.overrides_path.read_text(encoding="utf-8"))
    assert overrides["tts"]["voice_profile"] == "sid"
    assert "female voice" in first["text"].lower()


def test_personality_is_independent_of_voice(settings):
    async def scenario():
        provider = Recording()
        app, _, _ = make_app(settings, [provider])
        await app.start()
        await say(app, "be more professional")
        await say(app, "switch to your female voice")
        await say(app, "What is the capital of France?")
        await app.shutdown()
        return provider, app

    provider, app = run(scenario())
    system = provider.calls[-1]["messages"][0]["content"]
    assert "Personality (Professional)" in system
    assert app.settings.conversation.personality == "professional" and app.settings.tts.voice_profile == "female"


def test_emergency_gets_an_immediate_instruction_without_a_model(settings):
    async def scenario():
        provider = Recording()
        app, _, _ = make_app(settings, [provider])
        await app.start()
        reply = await say(app, "I have chest pain and my left arm is numb")
        await app.shutdown()
        return reply, provider

    reply, provider = run(scenario())
    assert "Call 112" in reply["text"] and provider.calls == []


def test_personal_health_data_is_not_sent_online_without_permission(settings):
    async def scenario():
        online = Recording("freellm")
        app, _, _ = make_app(settings, [online])
        await app.start()
        refused = await say(app, "My haemoglobin is 9.8 g/dL, is that bad?")
        await say(app, "allow online health answers")
        answered = await say(app, "My haemoglobin is 9.8 g/dL, is that bad?")
        await app.shutdown()
        return refused, answered, online

    refused, answered, online = run(scenario())
    assert "allow online health answers" in refused["text"]
    assert len(online.calls) == 1 and answered["route"] == "answer"


def test_health_detectors():
    assert emergency_message("I think I'm having a heart attack")
    assert "14416" in emergency_message("I want to end my life")
    assert emergency_message("what does haemoglobin measure") is None
    assert contains_personal_health_data("my blood sugar is 180 mg/dL") and not contains_personal_health_data(
        "what is a normal blood sugar level")


def test_offline_fallback_uses_the_local_model_and_says_so(settings):
    async def scenario():
        online = Recording("freellm", fail=True)
        local = Recording("ollama", local=True, turns=[{"text": ["Local answer."]}])
        app, _, _ = make_app(settings, [online, local])
        await app.start()
        await app.pool.check_health()
        reply = await say(app, "Explain how black holes work.")
        connectivity = app.pool.connectivity()
        await app.shutdown()
        return reply, connectivity, online

    reply, connectivity, online = run(scenario())
    assert reply["text"] == "Local answer." and connectivity == "local_fallback"
    assert online.calls == []  # the benched provider isn't waited on


def test_provider_router_capabilities_and_latency(bus):
    fast, slow = Recording("freellm"), Recording("ollama", local=True)
    pool = ProviderPool({"freellm": fast, "ollama": slow}, bus)
    router = ProviderRouter(pool)
    assert router.chain_for("general_knowledge").chain == ["freellm", "ollama"]
    assert router.chain_for("health_information", local_only=True).chain == ["ollama"]
    for _ in range(3):
        pool._circuits["freellm"].record_ttft(9000)
        pool._circuits["ollama"].record_ttft(500)
    assert router.chain_for("general_conversation").chain[0] == "ollama"


def test_connectivity_local_only_when_no_online_provider_is_configured(bus):
    pool = ProviderPool({"ollama": Recording("ollama", local=True)}, bus)
    assert pool.connectivity() == "local_only"
