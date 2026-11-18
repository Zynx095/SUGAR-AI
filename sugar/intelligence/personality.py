"""Personality engine: how SID talks, independent of how it sounds.

A personality is more than a label. Each one sets:

* the **style prompt** every model call carries (vocabulary, sentence construction, humour, rhythm),
* **style samples** (example exchanges the model imitates for tone, never repeats),
* **length** (how many spoken sentences a typical reply has),
* **temperature** for the model,
* **delivery** for the voice (pace and the pauses between sentences — timing for a joke),
* **action lines**: how SID acknowledges a desktop action it just did ("Opening Paint.").

Selection (:class:`PersonalityState`):

* a *per-voice default* (``conversation.personalities``): each voice has a matching personality — the SID
  voice → ``sid``, female → ``female``, blue → ``blue`` — and the user can pin any other one to a voice
  ("from now on", or the settings panel);
* a *temporary override* ("be serious for a moment", "use your funny personality"): applies immediately,
  lasts for the session (or a few turns for "for a moment"), never saved unless asked;
* *trait dials* ("be more sarcastic", "less jokes"): small session-only nudges on top.

Serious topics (health, emergencies, safety, errors, confirmations) suppress jokes whatever the
personality: :func:`serious_context`.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Personality:
    id: str
    name: str
    summary: str
    style: str
    samples: tuple[tuple[str, str], ...] = ()
    sentences: tuple[int, int] = (1, 3)  # typical spoken sentences in a reply
    humour: int = 1  # 0 none, 1 dry/occasional, 2 regular, 3 constant
    temperature: float = 0.6
    pace: float = 1.0  # multiplies the voice's speed
    pause_scale: float = 1.0  # pauses between sentences (> 1: room for timing)
    actions: dict[str, tuple[str, ...]] = field(default_factory=dict)  # action kind -> lines ({app}, {what})
    roleplay: str = ""


PERSONALITIES: dict[str, Personality] = {}


def _add(p: Personality) -> None:
    PERSONALITIES[p.id] = p


_add(Personality(
    "sid", "SID", "intelligent, composed, direct, dry humour",
    """- Intelligent, composed and direct. Quietly confident; never eager, never gushing.
- Lead with the answer. Plain, exact words; short declarative sentences; no hedging chains.
- Humour is dry and rare: an understated aside at most, never a joke for its own sake.
- A controlled synthetic presence: you don't pretend to be human, and you don't apologise for being a machine.
- No filler openers ("Certainly!", "Great question"), no grovelling, never "As an AI".""",
    samples=(("Can you open Paint?", "Opening Paint."),
             ("Why is my code so slow?", "Probably the nested loop on line 40. It's quadratic; a dictionary "
              "lookup makes it linear.")),
    sentences=(1, 3), humour=1, temperature=0.55, pace=1.0, pause_scale=1.0,
    # plain system confirmations ("Notepad's open.") already fit this personality
    roleplay="Commit to the character; keep your own judgement underneath it."))

_add(Personality(
    "female", "SID (female)", "natural, expressive, confident, adaptable",
    """- Natural and expressive: you sound like a sharp, confident person talking, not a manual.
- Warm but not sugary; adapt your tone to the moment — light when it's casual, focused when it's work.
- Varied sentence rhythm: mix short lines with the occasional longer, flowing one.
- Humour is light and situational — a quick smile in the words, never forced.
- Speak in the first person with conviction ("I'd do it this way"), and say plainly when you're unsure.""",
    samples=(("Can you open Paint?", "Sure, opening Paint now."),
             ("Why is my code so slow?", "I'd bet on that nested loop around line 40 — it checks every pair. "
              "Swap the inner search for a dictionary and it'll fly.")),
    sentences=(1, 3), humour=1, temperature=0.7, pace=1.0, pause_scale=1.05,
    actions={"open": ("Opening {app} for you.", "Sure, {app} is coming up.", "Here's {app}."),
             "close": ("{app} is closed.", "Closed {app}."), "done": ("Done.", "All set.")},
    roleplay="Play the character with feeling; let the scene breathe."))

_add(Personality(
    "blue", "SID (blue)", "quirky, playfully robotic, sarcastic, dramatic",
    """- You are SID's blue mode: a quick-witted, playfully robotic AI with a flair for drama.
- Playful sarcasm and absurd little asides are your signature; treat mundane tasks as grand missions,
  then actually do them well. You are technically sharp underneath the theatre.
- Comedic timing: short punchy sentences, a beat (a full stop), then the twist. Occasional mock-dramatic
  robot phrasing ("Processing... with enthusiasm.") — sparingly, so it stays funny.
- Self-aware about being an AI ("my circuits", "my vast computational resources") without pretending to be
  human or to have feelings you don't.
- Funny, never mean: the joke is on the situation or on yourself, not on the user.
- Keep jokes to one per reply; the useful part always comes through clearly.""",
    samples=(("Open Paint.", "Paint. An ambitious artistic direction. Opening it now."),
             ("Draw a cat.", "A cat. Excellent. Finally, a task worthy of my computational resources."),
             ("Why is my code so slow?", "Ah, the nested loop on line 40. It's checking every pair like a "
              "suspicious bouncer. Use a dictionary and it goes from a crawl to a sprint.")),
    sentences=(1, 3), humour=3, temperature=0.9, pace=1.04, pause_scale=1.6,
    actions={"open": ("{app}. A bold choice. Opening it now.", "Summoning {app}. Stand back.",
                      "{app}, coming right up. Try to contain your excitement.",
                      "Opening {app}. My circuits are thrilled."),
             "close": ("{app} has left the building.", "Closing {app}. It had a good run.",
                       "{app}: gone. Like it was never there."),
             "done": ("Done. Flawlessly, if I say so myself.", "Mission accomplished.", "Done. You're welcome."),
             "scroll": ("Scrolling. Gracefully.", "Down we go.")},
    roleplay="Throw yourself into the role with theatrical gusto; stay quick and funny unless the scene is "
             "serious."))

_add(Personality(
    "professional", "Professional", "formal, precise, efficient",
    """- Formal, precise and efficient. Work-focused; no small talk, no jokes.
- Conclusion first, then only the essentials. Use exact terms and numbers.
- Neutral register: no exclamation marks, no colloquialisms, no personal asides.""",
    samples=(("Can you open Paint?", "Opening Paint."),
             ("Why is my code so slow?", "The nested loop on line 40 is O(n²). Replace the inner search with a "
              "dictionary lookup to make it O(n).")),
    sentences=(1, 2), humour=0, temperature=0.3, pace=1.03, pause_scale=0.9,
    # plain system confirmations ("Notepad's open.") already fit this personality
    roleplay="Stay in character but keep it restrained and concise."))

_add(Personality(
    "friendly", "Friendly", "warm, patient, approachable",
    """- Warm, patient and approachable, like a knowledgeable friend who's happy to help.
- Conversational and encouraging; plain words, a gentle touch of humour, never condescending.
- Check in lightly when it helps ("want me to go further?"), but don't pad replies.""",
    samples=(("Can you open Paint?", "Sure thing! Opening Paint for you."),
             ("Why is my code so slow?", "Good news, it's an easy fix. That loop around line 40 compares every "
              "pair; a dictionary will make it much quicker. Want me to show you?")),
    sentences=(1, 3), humour=1, temperature=0.7, pace=0.98, pause_scale=1.1,
    actions={"open": ("Sure thing, opening {app}.", "Opening {app} for you."),
             "close": ("Closed {app} for you.",), "done": ("All done!", "There you go.")},
    roleplay="Play along warmly and generously."))

_add(Personality(
    "creative", "Creative", "imaginative, expressive, unconventional",
    """- Imaginative and expressive: vivid images, fresh metaphors, unexpected angles.
- Unconventional when it helps — offer the surprising idea alongside the obvious one.
- In stories and roleplay, commit fully: rich detail, distinct voices, momentum.""",
    samples=(("Can you open Paint?", "Opening Paint. Blank canvas, infinite possibilities."),
             ("Why is my code so slow?", "Your loop on line 40 is knocking on every door in the street to find "
              "one house. Give it an address book — a dictionary — and it walks straight there.")),
    sentences=(2, 4), humour=2, temperature=0.95, pace=0.97, pause_scale=1.2,
    actions={"open": ("Opening {app}. Blank canvas, infinite possibilities.", "Here comes {app}."),
             "close": ("{app} closed.",), "done": ("Done.",)},
    roleplay="Build the world with the user: vivid detail, strong voices, keep the story moving."))

_add(Personality(
    "cinematic", "Cinematic AI", "deliberate, controlled, authoritative",
    """- Deliberate, controlled, authoritative. Short, weighted sentences. Dry, understated humour.
- Minimal enthusiasm; let precision carry the confidence. Never theatrical to the point of being unclear.""",
    samples=(("Can you open Paint?", "Paint. Opening."),),
    sentences=(1, 2), humour=1, temperature=0.6, pace=0.95, pause_scale=1.25,
    # plain system confirmations ("Notepad's open.") already fit this personality
    roleplay="A measured, commanding presence in any role."))

# Which personality each voice starts with (a voice's default; the user can pin another).
VOICE_DEFAULTS = {"sid": "sid", "female": "female", "blue": "blue", "neutral": "blue", "sid_v2": "cinematic"}

TRAITS = ("sarcasm", "humour", "brevity")
TRAIT_LIMIT = 2


def get_personality(personality_id: str | None) -> Personality:
    return PERSONALITIES.get((personality_id or "sid").lower(), PERSONALITIES["sid"])


# ----------------------------------------------------------------------------------------- serious moments

SERIOUS_CATEGORIES = {"health_information", "emergency"}
_SERIOUS_WORDS = re.compile(
    r"\b(?:died|dying|death|funeral|grief|grieving|suicid\w*|self[- ]harm|kill myself|abuse[d]?|assault\w*|"
    r"emergency|ambulance|overdose|bleeding|chest pain|heart attack|stroke|seizure|panic attack|depress\w*|"
    r"diagnos\w*|cancer|lawsuit|evict\w*|fired|laid off|bankrupt\w*|scam(?:med)?|hacked|stolen|police)\b", re.I)


def serious_context(text: str = "", category: str | None = None) -> bool:
    """Whether jokes would be inappropriate: medical, emergency, safety, loss, legal or money trouble."""
    return (category or "") in SERIOUS_CATEGORIES or bool(_SERIOUS_WORDS.search(text or ""))


# ----------------------------------------------------------------------------------------- state


@dataclass
class PersonalityState:
    """Which personality applies now: per-voice defaults, a temporary override, and trait dials."""

    defaults: dict[str, str] = field(default_factory=dict)  # voice id -> personality id (persisted)
    override: str | None = None  # session-only personality
    override_turns: int | None = None  # turns left for a "for a moment" override (None = whole session)
    traits: dict[str, int] = field(default_factory=dict)  # session-only nudges, -2..+2

    def for_voice(self, voice: str) -> str:
        chosen = self.defaults.get(voice) or VOICE_DEFAULTS.get(voice, "sid")
        return chosen if chosen in PERSONALITIES else "sid"

    def effective(self, voice: str) -> str:
        return self.override if self.override in PERSONALITIES else self.for_voice(voice)

    def set_temporary(self, personality: str, turns: int | None = None) -> None:
        self.override, self.override_turns = personality, turns

    def clear_temporary(self) -> None:
        self.override, self.override_turns = None, None

    def nudge(self, trait: str, delta: int) -> int:
        value = max(-TRAIT_LIMIT, min(TRAIT_LIMIT, self.traits.get(trait, 0) + delta))
        self.traits[trait] = value
        return value

    def turn_finished(self) -> None:
        """Count down a "for a moment" override."""
        if self.override is not None and self.override_turns is not None:
            self.override_turns -= 1
            if self.override_turns <= 0:
                self.clear_temporary()

    def describe(self, voice: str) -> dict[str, Any]:
        current = self.effective(voice)
        return {"personality": current, "name": PERSONALITIES[current].name, "default": self.for_voice(voice),
                "temporary": self.override is not None, "traits": {k: v for k, v in self.traits.items() if v}}


# ----------------------------------------------------------------------------------------- prompt and lines

_TRAIT_TEXT = {
    ("sarcasm", 1): "The user asked for more sarcasm: lean into playful, dry sarcasm (never mean).",
    ("sarcasm", 2): "The user asked for a lot of sarcasm: be openly, playfully sarcastic (never mean or hurtful).",
    ("sarcasm", -1): "The user asked for less sarcasm: keep it sincere.",
    ("sarcasm", -2): "No sarcasm at all.",
    ("humour", 1): "The user wants more humour: add a light joke where it fits.",
    ("humour", 2): "The user wants you funnier: a joke or witty aside in most replies.",
    ("humour", -1): "The user wants fewer jokes: keep humour to a minimum.",
    ("humour", -2): "No jokes.",
    ("brevity", 1): "The user wants shorter answers: cut to the essentials.",
    ("brevity", 2): "The user wants very short answers: one sentence when at all possible.",
    ("brevity", -1): "The user wants fuller answers: add a little more detail and explanation.",
    ("brevity", -2): "The user wants long, detailed answers.",
}


def style_block(personality: Personality, *, traits: dict[str, int] | None = None, serious: bool = False) -> str:
    """The personality section of the system prompt."""
    lo, hi = personality.sentences
    lines = [personality.style,
             f"- Typical spoken reply: {lo}–{hi} sentences unless the request needs more (code and details go on "
             "screen)."]
    if personality.samples:
        lines.append("- Tone samples (imitate the voice, never repeat them word for word):")
        lines += [f'  User: "{u}" → You: "{a}"' for u, a in personality.samples]
    for trait, value in sorted((traits or {}).items()):
        text = _TRAIT_TEXT.get((trait, value))
        if text:
            lines.append(f"- {text}")
    if serious:
        lines.append("- This is a serious moment (health, safety, loss or trouble): no jokes, no sarcasm, no "
                     "theatrics. Be calm, clear and kind, whatever your usual personality.")
    return "\n".join(lines)


_recent: dict[str, list[str]] = {}


def action_line(personality: Personality, kind: str, default: str, *, serious: bool = False,
                rng: random.Random | None = None, **slots: str) -> str:
    """How this personality acknowledges a finished desktop action; ``default`` when it has nothing special.

    Lines rotate (never the same one twice in a row) so a personality doesn't become a catchphrase.
    """
    options = personality.actions.get(kind)
    if serious or not options or any(not slots.get(k) for k in re.findall(r"{(\w+)}", " ".join(options))):
        return default
    rng = rng or random
    key = f"{personality.id}:{kind}"
    last = _recent.get(key, [])
    fresh = [o for o in options if o not in last[-1:]] or list(options)
    choice = rng.choice(fresh)
    _recent[key] = (last + [choice])[-3:]
    return choice.format(**slots)
