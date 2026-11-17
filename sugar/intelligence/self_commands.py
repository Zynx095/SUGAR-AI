"""Commands about SID itself: voice, personality, roleplay and health-data authorisation.

"Switch to your female voice", "change your voice back", "speak more slowly", "make your voice deeper",
"use a softer voice", "be more professional", "end the roleplay", "allow online health answers".
Each becomes an intent that maps straight to a tool (no model), so the change is real and immediate.
"""

from __future__ import annotations

import re

from sugar.intelligence.intents import Intent, normalize

VOICE_ALIASES = {
    "female": "female", "woman": "female", "womans": "female", "woman's": "female", "girl": "female",
    "lady": "female", "feminine": "female", "her": "female",
    "original": "sid", "default": "sid", "normal": "sid", "usual": "sid", "old": "sid", "masculine": "sid",
    "male": "sid", "man": "sid", "deep": "sid", "sid": "sid", "ultron": "sid", "own": "sid", "real": "sid",
    "neutral": "neutral", "androgynous": "neutral", "calm": "neutral",
    "mechanical": "sid_v2", "robotic": "sid_v2", "robot": "sid_v2", "machine": "sid_v2",
}
PERSONALITY_ALIASES = {
    "default": "sid", "normal": "sid", "usual": "sid", "sid": "sid", "original": "sid", "regular": "sid",
    "cinematic": "cinematic", "movie": "cinematic", "dramatic": "cinematic", "villain": "cinematic",
    "friendly": "friendly", "casual": "friendly", "warm": "friendly", "nice": "friendly", "chill": "friendly",
    "professional": "professional", "formal": "professional", "business": "professional", "serious": "professional",
    "creative": "creative", "imaginative": "creative", "playful": "creative", "expressive": "creative",
}

_VOICE_WORDS = "|".join(sorted((re.escape(k) for k in VOICE_ALIASES), key=len, reverse=True))
_PERSONALITY_WORDS = "|".join(sorted((re.escape(k) for k in PERSONALITY_ALIASES), key=len, reverse=True))

_SET_VOICE = re.compile(
    rf"^(?:(?:switch|change|go|set|turn)(?: back)?(?: over)? (?:to|into) |use |try |give me |speak (?:in|with) )"
    rf"(?:your |the |a |an )?(?P<v>{_VOICE_WORDS}) (?:voice|sounding voice)(?: again| please| now)?$|"
    rf"^(?P<v2>{_VOICE_WORDS}) voice(?: please| now)?$"
)
_RESET_VOICE = re.compile(
    r"^(?:change|switch|set|turn|put) (?:your |the )?voice back(?: to (?:normal|the original|your original voice))?$|"
    r"^(?:go|switch|change) back to (?:your |the )?(?:original|old|normal|usual|default) voice$|"
    r"^(?:use|bring back) (?:your )?(?:original|old|normal|usual|default) voice(?: again)?$|^reset your voice$"
)
_ADJUST = [
    ("speed", -0.08, re.compile(r"^(?:speak|talk|go)(?: a bit| a little| much)? (?:more )?(?:slowly|slower)$|"
                                r"^slow (?:down|it down)(?: please)?$|^speak more slowly$|^not so fast$")),
    ("speed", +0.08, re.compile(r"^(?:speak|talk|go)(?: a bit| a little)? (?:more )?(?:quickly|faster)$|"
                                r"^speed up(?: your voice| your speech)?$|^talk faster$")),
    ("pitch", -1.5, re.compile(r"^(?:make|can you make) (?:your )?voice (?:a bit |a little |much )?(?:deeper|lower)$|"
                               r"^(?:lower|deepen) your (?:voice|pitch)$|^(?:sound|speak) deeper$|^deeper voice$")),
    ("pitch", +1.5, re.compile(r"^(?:make|can you make) (?:your )?voice (?:a bit |a little )?(?:higher|lighter)$|"
                               r"^raise your (?:voice|pitch)$|^(?:sound|speak) higher$|^higher voice$")),
    ("volume", -0.2, re.compile(r"^(?:use|try) a (?:softer|quieter|gentler) voice$|^(?:speak|talk) (?:more )?"
                                r"(?:softly|softer|quietly|quieter|gently)$|^softer voice$|^be quieter$")),
    ("volume", +0.2, re.compile(r"^(?:speak|talk) (?:more )?(?:loudly|louder|up)$|^use a louder voice$")),
]
_PERSONALITY = re.compile(
    rf"^(?:(?:switch|change|go|set)(?: back)? to |use |be |become |turn on |activate |enable )"
    rf"(?:your |the |a |an |more )?(?P<p>{_PERSONALITY_WORDS})(?: personality| mode| persona| style)?(?: please| now)?$|"
    rf"^(?P<p2>{_PERSONALITY_WORDS}) (?:personality|mode|persona)(?: please| now)?$"
)
_END_ROLEPLAY = re.compile(r"^(?:end|stop|exit|quit|leave|finish|break) (?:the |this |our )?(?:roleplay|role play|"
                           r"role-play|scene|story|character)$|^(?:back to normal|out of character|ooc|drop the act|"
                           r"you can stop pretending|stop pretending)$")
_HEALTH_ALLOW = re.compile(r"^(?:allow|use|enable|permit)(?: using)? (?:online|cloud|external|internet) "
                           r"(?:models? |ai )?(?:for )?(?:my )?health(?: questions| answers| data)?(?: (?:always|from now on))?$|"
                           r"^(?:allow|enable) online health answers(?: always)?$")
_HEALTH_REVOKE = re.compile(r"^(?:stop|don'?t|do not) (?:using|use|send(?:ing)?) (?:online|cloud|external) "
                            r"(?:models? |ai )?(?:for |with )?(?:my )?health(?: questions| data| answers)?$")


def parse(text: str) -> Intent | None:
    plain = normalize(text)
    if not plain:
        return None
    match = _SET_VOICE.match(plain)
    if match:
        word = match.group("v") or match.group("v2")
        voice = VOICE_ALIASES[word]
        return Intent("voice.set", {"voice": voice}, plain, call=("voice.set", {"voice": voice}))
    if _RESET_VOICE.match(plain):
        return Intent("voice.set", {"voice": "sid"}, plain, call=("voice.set", {"voice": "sid", "reset": True}))
    for setting, delta, pattern in _ADJUST:
        if pattern.match(plain):
            return Intent("voice.adjust", {setting: delta}, plain,
                          call=("voice.adjust", {"setting": setting, "delta": delta}))
    match = _PERSONALITY.match(plain)
    if match:
        personality = PERSONALITY_ALIASES[match.group("p") or match.group("p2")]
        return Intent("personality.set", {"personality": personality}, plain,
                      call=("personality.set", {"personality": personality}))
    if _END_ROLEPLAY.match(plain):
        return Intent("roleplay.end", {}, plain, call=("roleplay.end", {}))
    if _HEALTH_ALLOW.match(plain):
        always = bool(re.search(r"\b(?:always|from now on)\b", plain))
        return Intent("health.authorize", {}, plain, call=("health.authorize", {"allow": True, "persist": always}))
    if _HEALTH_REVOKE.match(plain):
        return Intent("health.authorize", {}, plain, call=("health.authorize", {"allow": False, "persist": True}))
    return None
