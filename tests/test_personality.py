"""Personality engine: personalities change what the model is told (style, length, samples, temperature, voice
delivery), follow the voice by default, can be overridden for a moment or for good, and go quiet on serious
topics."""

from __future__ import annotations

import json
import random

from conftest import run
from test_universal import Recording, make_app, say

from sugar.app.application import SIDApp
from sugar.audio.voice_profile import PROFILES
from sugar.audio.voices import IDENTITIES, canonical, get_identity, profile_for
from sugar.config.settings import load_settings
from sugar.intelligence.personality import (
    PERSONALITIES,
    PersonalityState,
    action_line,
    get_personality,
    serious_context,
    style_block,
)


def system_prompt(provider) -> str:
    return provider.calls[-1]["messages"][0]["content"]


# ---------------------------------------------------------------------------------------------- the catalogue


def test_every_required_personality_exists_with_distinct_behaviour():
    for pid in ("sid", "female", "blue", "professional", "friendly", "creative"):
        assert pid in PERSONALITIES
    blocks = {pid: style_block(p) for pid, p in PERSONALITIES.items()}
    assert len(set(blocks.values())) == len(blocks)  # no two personalities are told the same thing
    blue, pro = PERSONALITIES["blue"], PERSONALITIES["professional"]
    assert blue.humour == 3 and pro.humour == 0
    assert blue.temperature > PERSONALITIES["sid"].temperature > pro.temperature
    assert blue.pause_scale > 1.3 and pro.pause_scale < 1.0  # timing for jokes vs. brisk
    assert "Paint. An ambitious artistic direction" in style_block(blue)  # style sample, not a canned reply
    assert "no jokes" in style_block(pro).lower()


def test_serious_topics_suppress_jokes_for_any_personality():
    assert serious_context("I have chest pain", None)
    assert serious_context("what does haemoglobin measure", "health_information")
    assert serious_context("my dog died yesterday")
    assert not serious_context("open paint and draw a cat")
    block = style_block(PERSONALITIES["blue"], serious=True)
    assert "no jokes, no sarcasm" in block


def test_trait_dials_add_instructions_and_are_bounded():
    state = PersonalityState()
    for _ in range(5):
        state.nudge("sarcasm", +1)
    assert state.traits["sarcasm"] == 2
    assert "openly, playfully sarcastic" in style_block(PERSONALITIES["sid"], traits=state.traits)
    state.nudge("humour", -1)
    assert "fewer jokes" in style_block(PERSONALITIES["blue"], traits=state.traits)


def test_state_follows_the_voice_unless_overridden():
    state = PersonalityState()
    assert state.effective("sid") == "sid" and state.effective("female") == "female"
    assert state.effective("blue") == "blue"
    state.defaults["sid"] = "cinematic"
    assert state.effective("sid") == "cinematic" and state.effective("blue") == "blue"
    state.set_temporary("professional", turns=2)
    assert state.effective("blue") == "professional"
    state.turn_finished()
    assert state.effective("blue") == "professional"
    state.turn_finished()
    assert state.effective("blue") == "blue" and state.override is None


def test_action_lines_rotate_and_respect_serious_moments():
    rng = random.Random(4)
    blue, pro = PERSONALITIES["blue"], PERSONALITIES["professional"]
    lines = {action_line(blue, "open", "Opened Paint.", app="Paint", rng=rng) for _ in range(12)}
    assert len(lines) >= 3 and all("Paint" in line for line in lines)
    assert action_line(pro, "open", "Opened Paint.", app="Paint") == "Opening Paint."
    assert action_line(blue, "open", "Opened Paint.", app="Paint", serious=True) == "Opened Paint."
    assert action_line(blue, "unknown", "Did it.", app="Paint") == "Did it."


# ---------------------------------------------------------------------------------------------- voices


def test_blue_is_a_distinct_voice_with_its_own_palette():
    blue = get_identity("blue")
    assert blue.profile == "blue" and blue.default_personality == "blue"
    assert canonical("neutral") == "blue" and profile_for("neutral") == "blue"  # old setting → new voice
    profile = PROFILES["blue"]
    assert profile.model == "EN" and profile.speaker == "EN-AU"  # a different base speaker from the female voice
    assert profile.character and profile.quantize > 0 and profile.ring_depth > 0
    palettes = {i.id: i.palette for i in IDENTITIES.values()}
    assert palettes["blue"][0].lower() == "#1e90ff"  # electric blue
    assert len({p[0] for p in palettes.values()}) == len(palettes)


# ---------------------------------------------------------------------------------------------- in the app


def test_switching_to_the_blue_voice_brings_the_blue_personality(settings):
    async def scenario():
        provider = Recording()
        app, _, events = make_app(settings, [provider])
        await app.start()
        await say(app, "switch to your blue voice")
        await say(app, "What is the capital of France?")
        await app.shutdown()
        return provider, app, events

    provider, app, events = run(scenario())
    system = system_prompt(provider)
    assert "Personality (SID (blue))" in system and "suspicious bouncer" in system
    assert provider.calls[-1]["temperature"] == PERSONALITIES["blue"].temperature
    changed = [e for e in events if e.type == "personality.changed"]
    assert changed and changed[-1].data["personality"] == "blue"
    voice = [e for e in events if e.type == "voice.changed"][-1].data
    assert voice["id"] == "blue" and voice["palette"][0] == "#1e90ff"
    assert app.delivery() == (PERSONALITIES["blue"].pace, PERSONALITIES["blue"].pause_scale)


def test_be_serious_for_a_moment_lasts_three_answers(settings):
    async def scenario():
        provider = Recording()
        app, _, _ = make_app(settings, [provider])
        await app.start()
        await say(app, "switch to your blue voice")
        await say(app, "be serious for a moment")
        prompts = []
        for question in ("What is a closure?", "What is a tuple?", "What is a set?", "What is a dict?"):
            await say(app, question)
            prompts.append(system_prompt(provider))
        await app.shutdown()
        return prompts

    prompts = run(scenario())
    assert all("Personality (Professional)" in p for p in prompts[:3])
    assert "Personality (SID (blue))" in prompts[3]


def test_from_now_on_is_saved_per_voice_and_survives_a_restart(settings):
    async def first():
        app, _, _ = make_app(settings, [Recording()])
        await app.start()
        await say(app, "be friendly from now on")
        await app.shutdown()

    run(first())
    overrides = json.loads((settings.paths.data_dir / "overrides.json").read_text(encoding="utf-8"))
    assert overrides["conversation"]["personalities"] == {"sid": "friendly"}

    reloaded = load_settings(config_file=settings.paths.data_dir.parent / "missing.yaml", use_env=False,
                             data_dir=settings.paths.data_dir)
    restarted = SIDApp(reloaded, voice=False)
    assert restarted.personality.effective("sid") == "friendly"
    assert restarted.personality.effective("blue") == "blue"


def test_a_spoken_change_is_not_saved(settings):
    async def scenario():
        app, _, _ = make_app(settings, [Recording()])
        await app.start()
        await say(app, "use your funny personality")
        await app.shutdown()
        return app

    app = run(scenario())
    assert app.personality.effective("sid") == "blue"
    path = settings.paths.data_dir / "overrides.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert "personalities" not in saved.get("conversation", {})


def test_legacy_saved_personality_applies_to_the_voice_it_was_set_with(settings):
    settings.conversation.personality = "cinematic"
    settings.tts.voice_profile = "sid"
    app = SIDApp(settings, voice=False)
    assert app.personality.effective("sid") == "cinematic"
    assert app.personality.effective("blue") == "blue"


def test_health_questions_are_serious_even_in_blue_mode(settings):
    async def scenario():
        provider = Recording()
        app, _, _ = make_app(settings, [provider])
        await app.start()
        await say(app, "switch to your blue voice")
        await say(app, "Explain what haemoglobin measures in a blood test.")
        await app.shutdown()
        return provider

    provider = run(scenario())
    assert "no jokes, no sarcasm" in system_prompt(provider)
    assert provider.calls[-1]["temperature"] <= 0.4


def test_sarcasm_dial_reaches_the_model(settings):
    async def scenario():
        provider = Recording()
        app, _, _ = make_app(settings, [provider])
        await app.start()
        await say(app, "be more sarcastic")
        await say(app, "What is recursion?")
        await app.shutdown()
        return provider

    provider = run(scenario())
    assert "more sarcasm" in system_prompt(provider)


def test_personalities_change_style_for_the_same_question():
    sid = style_block(get_personality("sid"))
    blue = style_block(get_personality("blue"))
    assert "dry and rare" in sid and "Playful sarcasm" in blue
