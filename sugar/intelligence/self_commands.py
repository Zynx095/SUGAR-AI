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
    "neutral": "blue", "androgynous": "blue", "calm": "blue", "blue": "blue", "quirky": "blue",
    "funny": "blue", "third": "blue", "cyan": "blue", "red": "female", "crimson": "female", "amber": "sid",
    "orange": "sid",
    "mechanical": "sid_v2", "robotic": "sid_v2", "robot": "sid_v2", "machine": "sid_v2",
}
PERSONALITY_ALIASES = {
    # "normal"/"usual"/"default" mean: back to the voice's own personality (see RESET_WORDS)
    "default": "reset", "normal": "reset", "usual": "reset", "regular": "reset", "own": "reset",
    "sid": "sid", "original": "sid", "classic": "sid",
    "cinematic": "cinematic", "movie": "cinematic", "villain": "cinematic", "epic": "cinematic",
    "friendly": "friendly", "casual": "friendly", "warm": "friendly", "nice": "friendly", "chill": "friendly",
    "kind": "friendly",
    "professional": "professional", "formal": "professional", "business": "professional", "serious": "professional",
    "creative": "creative", "imaginative": "creative", "expressive": "creative", "artistic": "creative",
    "blue": "blue", "funny": "blue", "quirky": "blue", "silly": "blue", "playful": "blue", "goofy": "blue",
    "dramatic": "blue", "robot": "blue", "robotic": "blue", "comedian": "blue", "comedy": "blue",
    "female": "female", "feminine": "female", "red": "female",
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
    rf"^(?:(?:switch|change|go|set|get)(?: back)?(?: over)? (?:to|into) |use |be |become |turn on |activate |enable |"
    rf"(?:speak|talk|act|answer|respond) (?:more )?(?:like |in |with )?(?:a |an |your )?)"
    rf"(?:your |the |a |an |more |a bit more |a little more |a little |a bit |slightly )?(?P<p>{_PERSONALITY_WORDS})"
    rf"(?:ly)?(?: personality| mode| persona| style| self| side| version| sid| tone| voice personality)?$|"
    rf"^(?P<p2>{_PERSONALITY_WORDS}) (?:personality|mode|persona)$"
)
# "for a moment" → a short override; "from now on" → saved as this voice's default
_FOR_A_MOMENT = re.compile(r"\s*,?\s*(?:for (?:a |one )?(?:moment|minute|second|bit|while|sec)|for now|"
                           r"for the next (?:few|couple of) (?:minutes|answers|replies|questions)|just this once|"
                           r"temporarily)$")
_FROM_NOW_ON = re.compile(r"\s*,?\s*(?:from now on|permanently|by default|for good|always|forever|"
                          r"and (?:keep|make) (?:it|that) (?:that way|your default|the default)|"
                          r"and save (?:it|that)|as (?:your|the) default)$")
_TRAIT = [
    ("sarcasm", +1, re.compile(r"^(?:be|get|act|sound) (?:a (?:bit|little) |even |much )?more sarcastic$|"
                               r"^(?:more|extra) sarcasm(?: please)?$|^(?:turn|crank|dial) up the sarcasm$|"
                               r"^be sarcastic$")),
    ("sarcasm", -1, re.compile(r"^(?:be )?less sarcastic$|^(?:less|no more|stop the|enough) sarcasm$|"
                               r"^(?:turn|tone|dial) down the sarcasm$|^stop being sarcastic$|^be sincere$")),
    ("humour", +1, re.compile(r"^(?:be|get) (?:a (?:bit|little) |even |much )?(?:more funny|funnier)$|"
                              r"^(?:more|tell more) jokes(?: please)?$|^(?:turn|crank|dial) up the (?:humou?r|jokes)$|"
                              r"^lighten up$")),
    ("humour", -1, re.compile(r"^(?:fewer|less|no more) jokes(?: please)?$|^stop (?:joking|making jokes)(?: around)?$|"
                              r"^(?:turn|tone|dial) down the (?:humou?r|jokes)$|^be less funny$|^less humou?r$")),
    ("brevity", +1, re.compile(r"^(?:be (?:more )?(?:concise|brief)|(?:give me |use )?shorter (?:answers|replies)|"
                               r"keep (?:it|your answers) (?:short|brief)|talk less|less detail)(?: please)?$")),
    ("brevity", -1, re.compile(r"^(?:give me |use )?(?:longer|more detailed|fuller) (?:answers|replies)(?: please)?$|"
                               r"^(?:more detail|explain more|go into more detail)(?: please)?$|^be more detailed$")),
]
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
    for trait, delta, pattern in _TRAIT:
        if pattern.match(plain):
            return Intent("personality.trait", {trait: delta}, plain,
                          call=("personality.trait", {"trait": trait, "delta": delta}))
    core, turns, persist = plain, None, False
    moment = _FOR_A_MOMENT.search(core)
    if moment:
        core, turns = core[: moment.start()].strip(" ,"), 3
    forever = _FROM_NOW_ON.search(core)
    if forever:
        core, persist = core[: forever.start()].strip(" ,"), True
    match = _PERSONALITY.match(core)
    if match:
        personality = PERSONALITY_ALIASES[match.group("p") or match.group("p2")]
        if personality == "reset":
            return Intent("personality.reset", {}, plain, call=("personality.reset", {"persist": persist}))
        args: dict = {"personality": personality, "persist": persist}
        if turns:
            args["turns"] = turns
        return Intent("personality.set", {"personality": personality}, plain, call=("personality.set", args))
    if _END_ROLEPLAY.match(plain):
        return Intent("roleplay.end", {}, plain, call=("roleplay.end", {}))
    if _HEALTH_ALLOW.match(plain):
        always = bool(re.search(r"\b(?:always|from now on)\b", plain))
        return Intent("health.authorize", {}, plain, call=("health.authorize", {"allow": True, "persist": always}))
    if _HEALTH_REVOKE.match(plain):
        return Intent("health.authorize", {}, plain, call=("health.authorize", {"allow": False, "persist": True}))
    return None
