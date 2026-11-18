"""Voice identities: the one authoritative description of how SID sounds *and* looks.

A voice identity binds a TTS profile (:mod:`voice_profile`) to its display name and to the colour palette
the particle entity and UI accents take on. ``tts.voice_profile`` holds the active identity; the backend
publishes ``voice.changed`` with the palette whenever it changes, and the frontend derives every colour
from that event — no component keeps its own hard-coded voice colours.

Palettes are ``[body, highlight, depth, warning]`` hex colours; ``tts.palettes`` overrides them per voice.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceIdentity:
    id: str
    name: str
    description: str
    gender: str  # masculine | feminine | neutral (a display label only)
    profile: str  # key in voice_profile.PROFILES
    provider: str  # TTS engine
    speaker: str  # engine speaker id
    palette: tuple[str, str, str, str]
    default_personality: str = "sid"  # a suggestion only: switching voice never changes the personality
    emotion_controls: tuple[str, ...] = ()  # none of the current engines expose emotion controls


IDENTITIES: dict[str, VoiceIdentity] = {
    "sid": VoiceIdentity(
        "sid", "SID", "Deep, masculine, synthetic — cloned from ULTRON_SID.mp3", "masculine", "sid",
        "melotts+openvoice", "EN-Newest", ("#ffab3d", "#f6cf7a", "#ff6a12", "#ff4524"), "sid"),
    "female": VoiceIdentity(
        "female", "SID (female)", "Natural, confident female voice (MeloTTS EN-Newest speaker)", "feminine",
        "female", "melotts", "EN-Newest", ("#e8114f", "#ff6fa3", "#9e0c5c", "#ffb03a"), "sid"),
    "blue": VoiceIdentity(
        "blue", "SID (blue)", "Quirky, playfully robotic — a different speaker with tuned, lively intonation",
        "neutral", "blue", "melotts", "EN-AU",
        # electric blue core, ice-blue highlight, deep cobalt depth, cyan accent
        ("#1e90ff", "#c8f4ff", "#0b2a8f", "#00e5ff"), "blue"),
    "sid_v2": VoiceIdentity(
        "sid_v2", "SID (mechanical)", "The earlier processed mechanical voice", "masculine", "sid_v2", "melotts",
        "EN-Newest", ("#d40a2a", "#ff3d55", "#6e0011", "#ff9a3c"), "cinematic"),
}


# Old ids that now mean another identity ("neutral" was the earlier third voice).
ALIASES = {"neutral": "blue"}


def canonical(identity_id: str | None) -> str:
    key = (identity_id or "sid").lower()
    key = ALIASES.get(key, key)
    return key if key in IDENTITIES else "sid"


def get_identity(identity_id: str | None) -> VoiceIdentity:
    return IDENTITIES[canonical(identity_id)]


def profile_for(setting: str | None) -> str:
    """The voice_profile key to synthesise with: an identity's profile, or a raw profile name (tools, tests)."""
    key = (setting or "sid").lower()
    if key in IDENTITIES or key in ALIASES:
        return get_identity(key).profile
    return key


def palette_for(identity_id: str | None, overrides: dict[str, list[str]] | None = None) -> list[str]:
    identity = get_identity(identity_id)
    custom = (overrides or {}).get(identity.id)
    if custom and len(custom) >= 3:
        colours = list(custom) + list(identity.palette[len(custom):])
        return colours[:4]
    return list(identity.palette)


def describe(identity_id: str | None, overrides: dict[str, list[str]] | None = None) -> dict:
    identity = get_identity(identity_id)
    return {"id": identity.id, "name": identity.name, "description": identity.description,
            "gender": identity.gender, "provider": identity.provider, "palette": palette_for(identity.id, overrides)}
