"""Tools that change SID itself: voice identity and its palette, voice adjustments, personality, roleplay, and
permission to send personal health information to online models. All changes persist (data/overrides.json)
and take effect immediately — no restart."""

from __future__ import annotations

from typing import Any

from sugar.audio.voices import ALIASES, IDENTITIES, canonical, describe
from sugar.config.settings import save_override
from sugar.core.logging import log_event
from sugar.intelligence.personality import PERSONALITIES
from sugar.tools.registry import PermissionLevel, Tool, ToolResult, params

SELF = frozenset({"agent", "chat", "self"})
LIMITS = {"speed": (0.7, 1.4), "pitch": (-6.0, 6.0), "volume": (0.3, 1.5)}


def voice_state(app) -> dict[str, Any]:
    tts = app.settings.tts
    state = describe(tts.voice_profile, tts.palettes)
    state.update({"speed": round(tts.speed, 2), "pitch_offset": round(tts.pitch_offset, 1),
                  "volume": round(tts.volume, 2)})
    state.update(personality_state(app))
    return state


def personality_state(app) -> dict[str, Any]:
    state = app.personality.describe(canonical(app.settings.tts.voice_profile))
    return {"personality": state["personality"], "personality_name": state["name"],
            "personality_default": state["default"], "personality_temporary": state["temporary"],
            "traits": state["traits"]}


def publish_personality(app, reason: str) -> None:
    state = personality_state(app)
    app.bus.publish("personality.changed", reason=reason, name=state["personality_name"], **state)
    log_event("PERSONALITY_CHANGED", reason=reason, personality=state["personality"],
              temporary=state["personality_temporary"], traits=state["traits"])


def publish_voice(app, reason: str) -> None:
    state = voice_state(app)
    app.bus.publish("voice.changed", reason=reason, **state)
    log_event("VOICE_CHANGED", voice=state["id"], reason=reason, speed=state["speed"],
              pitch_offset=state["pitch_offset"], volume=state["volume"])


def _set(app, key: str, value: Any) -> None:
    section, name = key.split(".", 1)
    setattr(getattr(app.settings, section), name, value)
    save_override(app.settings, key, value)


def register_self_tools(app) -> None:
    registry = app.registry

    async def set_voice(args: dict[str, Any]) -> ToolResult:
        voice = str(args["voice"]).lower()
        voice = ALIASES.get(voice, voice)
        previous_personality = app.personality.effective(canonical(app.settings.tts.voice_profile))
        if voice not in IDENTITIES:
            return ToolResult.failure(f"I don't have a voice called {voice}. I have: "
                                      + ", ".join(i.name for i in IDENTITIES.values()) + ".")
        _set(app, "tts.voice_profile", voice)
        if args.get("reset"):
            _set(app, "tts.pitch_offset", 0.0)
        app.synth._cache.clear()
        # A voice brings its matching personality (or the one pinned to it) — unless the user picked one this
        # session ("be professional"), which stays: voice and personality are independent.
        publish_voice(app, "voice.set")
        if app.personality.effective(voice) != previous_personality:
            publish_personality(app, "voice.set")
        identity = IDENTITIES[voice]
        lines = {"female": "Switched to my female voice.", "sid": "This is my original voice.",
                 "blue": "Blue mode. Systems quirky. Sarcasm calibrated.",
                 "sid_v2": "Switched to my mechanical voice."}
        return ToolResult(True, lines.get(voice, f"Switched to {identity.name}."), data=voice_state(app))

    async def adjust_voice(args: dict[str, Any]) -> ToolResult:
        setting = str(args["setting"])
        delta = float(args["delta"])
        low, high = LIMITS[setting]
        key = {"speed": "tts.speed", "pitch": "tts.pitch_offset", "volume": "tts.volume"}[setting]
        current = float(getattr(app.settings.tts, key.split(".", 1)[1]))
        value = round(min(high, max(low, current + delta)), 2)
        if value == current:
            edge = "fastest" if delta > 0 else "slowest"
            if setting != "speed":
                edge = ("highest" if delta > 0 else "deepest") if setting == "pitch" else ("loudest" if delta > 0
                                                                                             else "softest")
            return ToolResult(True, f"That's already my {edge} setting.", data=voice_state(app))
        _set(app, key, value)
        if setting == "volume":
            app.player.volume = value
        app.synth._cache.clear()
        publish_voice(app, f"voice.adjust.{setting}")
        words = {("speed", True): "Speaking a little faster.", ("speed", False): "Speaking more slowly.",
                 ("pitch", True): "Raising my voice a little.", ("pitch", False): "Lowering my voice.",
                 ("volume", True): "Speaking up.", ("volume", False): "Speaking more softly."}
        return ToolResult(True, words[(setting, delta > 0)], data=voice_state(app))

    async def set_personality(args: dict[str, Any]) -> ToolResult:
        personality = str(args["personality"]).lower()
        if personality not in PERSONALITIES:
            return ToolResult.failure("I don't have that personality. I have: "
                                      + ", ".join(p.name for p in PERSONALITIES.values()) + ".")
        voice = canonical(app.settings.tts.voice_profile)
        persist = bool(args.get("persist"))
        if persist:  # this voice's saved default
            app.personality.defaults[voice] = personality
            app.personality.clear_temporary()
            _set(app, "conversation.personalities", dict(app.personality.defaults))
        else:
            turns = args.get("turns")
            app.personality.set_temporary(personality, int(turns) if turns else None)
        app.synth._cache.clear()
        publish_personality(app, "personality.set")
        lines = {"sid": "Back to my original self.", "cinematic": "Very well.",
                 "friendly": "Sure thing, I'll keep it friendly.", "professional": "Understood. Professional mode.",
                 "creative": "Let's get creative.", "female": "Alright, this is me being myself.",
                 "blue": "Blue personality engaged. Brace yourself for excellence."}
        line = lines[personality]
        if persist:
            line += " That's my default for this voice now."
        elif args.get("turns"):
            line += " For the next few answers."
        return ToolResult(True, line, data=personality_state(app))

    async def reset_personality(args: dict[str, Any]) -> ToolResult:
        voice = canonical(app.settings.tts.voice_profile)
        app.personality.clear_temporary()
        app.personality.traits.clear()
        if args.get("persist") and voice in app.personality.defaults:
            app.personality.defaults.pop(voice)
            _set(app, "conversation.personalities", dict(app.personality.defaults))
        app.synth._cache.clear()
        publish_personality(app, "personality.reset")
        return ToolResult(True, "Back to normal.", data=personality_state(app))

    async def personality_trait(args: dict[str, Any]) -> ToolResult:
        trait, delta = str(args["trait"]), int(args["delta"])
        value = app.personality.nudge(trait, 1 if delta > 0 else -1)
        publish_personality(app, f"trait.{trait}")
        lines = {("sarcasm", True): "Sarcasm levels rising. This will go wonderfully.",
                 ("sarcasm", False): "Less sarcasm. Sincerely.",
                 ("humour", True): "I'll be funnier.", ("humour", False): "Fewer jokes. Got it.",
                 ("brevity", True): "Shorter answers.", ("brevity", False): "I'll go into more detail."}
        return ToolResult(True, lines[(trait, delta > 0)], data={"trait": trait, "value": value})

    async def end_roleplay(args: dict[str, Any]) -> ToolResult:
        if app.working.roleplay is None:
            return ToolResult(True, "We're not in a roleplay.")
        app.working.roleplay = None
        app.bus.publish("roleplay.ended")
        return ToolResult(True, "Scene over. I'm back to myself.")

    async def authorize_health(args: dict[str, Any]) -> ToolResult:
        allow = bool(args.get("allow", True))
        app.router.health_authorized = allow
        if args.get("persist") or not allow:
            _set(app, "conversation.health_external", allow)
        log_event("HEALTH_AUTHORIZATION", allow=allow, persist=bool(args.get("persist")))
        if allow:
            return ToolResult(True, "Okay. Your health questions can go to online models"
                              + (" from now on." if args.get("persist") else " for this session."))
        return ToolResult(True, "Okay. Your personal health details stay on this computer.")

    registry.register(Tool("voice.set", "Switch SID's voice (and its matching colours): sid, female, blue, sid_v2.",
                           params(["voice"], voice={"type": "string", "enum": sorted(IDENTITIES)},
                                  reset={"type": "boolean", "default": False}),
                           set_voice, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("voice.adjust", "Adjust SID's speaking speed, pitch or volume by a delta.",
                           params(["setting", "delta"], setting={"type": "string", "enum": ["speed", "pitch", "volume"]},
                                  delta={"type": "number"}),
                           adjust_voice, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("personality.set", "Switch SID's personality (" + ", ".join(sorted(PERSONALITIES))
                           + "). persist=true saves it as this voice's default; otherwise it lasts this session "
                           "(or `turns` replies).",
                           params(["personality"], personality={"type": "string", "enum": sorted(PERSONALITIES)},
                                  persist={"type": "boolean", "default": False}, turns={"type": "integer"}),
                           set_personality, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("personality.reset", "Drop temporary personality changes and trait nudges.",
                           params(persist={"type": "boolean", "default": False}), reset_personality,
                           PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("personality.trait", "Nudge a trait for this session: sarcasm, humour or brevity, by +1/-1.",
                           params(["trait", "delta"], trait={"type": "string", "enum": ["sarcasm", "humour", "brevity"]},
                                  delta={"type": "integer"}), personality_trait,
                           PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("roleplay.end", "End the current roleplay.", params(), end_roleplay,
                           PermissionLevel.READ, 5, SELF))
    registry.register(Tool("health.authorize", "Allow or stop sending personal health information to online models.",
                           params(allow={"type": "boolean", "default": True}, persist={"type": "boolean",
                                                                                       "default": False}),
                           authorize_health, PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"self"})))
