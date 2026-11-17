"""Tools that change SID itself: voice identity and its palette, voice adjustments, personality, roleplay, and
permission to send personal health information to online models. All changes persist (data/overrides.json)
and take effect immediately — no restart."""

from __future__ import annotations

from typing import Any

from sugar.audio.voices import IDENTITIES, describe
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
                  "volume": round(tts.volume, 2), "personality": app.settings.conversation.personality})
    return state


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
        if voice not in IDENTITIES:
            return ToolResult.failure(f"I don't have a voice called {voice}. I have: "
                                      + ", ".join(i.name for i in IDENTITIES.values()) + ".")
        _set(app, "tts.voice_profile", voice)
        if args.get("reset"):
            _set(app, "tts.pitch_offset", 0.0)
        app.synth._cache.clear()
        publish_voice(app, "voice.set")
        identity = IDENTITIES[voice]
        lines = {"female": "Switched to my female voice.", "sid": "This is my original voice.",
                 "neutral": "Switched to my neutral voice.", "sid_v2": "Switched to my mechanical voice."}
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
        _set(app, "conversation.personality", personality)
        app.bus.publish("personality.changed", personality=personality, name=PERSONALITIES[personality].name)
        log_event("PERSONALITY_CHANGED", personality=personality)
        lines = {"sid": "Back to my usual self.", "cinematic": "Very well.", "friendly": "Sure thing, I'll keep it "
                 "friendly.", "professional": "Understood. Professional mode.", "creative": "Let's get creative."}
        return ToolResult(True, lines[personality], data={"personality": personality})

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

    registry.register(Tool("voice.set", "Switch SID's voice (and its matching colours): sid, female, neutral, sid_v2.",
                           params(["voice"], voice={"type": "string", "enum": sorted(IDENTITIES)},
                                  reset={"type": "boolean", "default": False}),
                           set_voice, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("voice.adjust", "Adjust SID's speaking speed, pitch or volume by a delta.",
                           params(["setting", "delta"], setting={"type": "string", "enum": ["speed", "pitch", "volume"]},
                                  delta={"type": "number"}),
                           adjust_voice, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("personality.set", "Switch SID's personality: sid, cinematic, friendly, professional, "
                           "creative.", params(["personality"], personality={"type": "string",
                                                                              "enum": sorted(PERSONALITIES)}),
                           set_personality, PermissionLevel.NON_DESTRUCTIVE, 10, SELF))
    registry.register(Tool("roleplay.end", "End the current roleplay.", params(), end_roleplay,
                           PermissionLevel.READ, 5, SELF))
    registry.register(Tool("health.authorize", "Allow or stop sending personal health information to online models.",
                           params(allow={"type": "boolean", "default": True}, persist={"type": "boolean",
                                                                                       "default": False}),
                           authorize_health, PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"self"})))
