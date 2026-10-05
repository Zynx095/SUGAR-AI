"""Deterministic command grammar.

Obvious commands ("open Chrome", "pause", "volume 50", "close this tab",
"open Notepad and type hello") never touch a language model: they are matched
here in well under a millisecond and executed directly.

Unlike the old ``"type" in text`` checks, every pattern is anchored to the
*whole* utterance (or to one clause of a compound command) after politeness
and wake words are stripped, so "write me a function" or "what time
complexity does quicksort have" can never trigger typing or the clock.
Anything not matched goes to the router and, from there, to a model that
can only act through permission-checked tools.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sugar.intelligence import computer_grammar as cg
from sugar.intelligence.intents import DesktopView, Intent, normalize

__all__ = ["DesktopView", "FastPath", "Intent", "normalize"]


@dataclass
class _Rule:
    name: str
    pattern: re.Pattern[str]
    accept: Callable[[dict[str, str]], dict[str, Any] | None] | None = None


def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(r"^(?:" + pattern + r")$")


_SITES = {
    "youtube": "https://www.youtube.com", "github": "https://github.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
    "stack overflow": "https://stackoverflow.com", "stackoverflow": "https://stackoverflow.com",
    "linkedin": "https://www.linkedin.com", "twitter": "https://x.com", "x": "https://x.com",
    "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com", "whatsapp": "https://web.whatsapp.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "amazon": "https://www.amazon.in", "wikipedia": "https://www.wikipedia.org",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com", "outlook": "https://outlook.live.com",
}
_NOT_SONGS = {"music", "some music", "something", "a song", "song", "it", "that", "this", "the music", "spotify",
              "anything", "something good", "a game", "game", "with me", "a video", "the video", "the next song",
              "the previous song", "next song", "previous song", "youtube", "a youtube video", "videos"}
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100, "max": 100, "maximum": 100, "full": 100, "half": 50,
}


def _level(slots: dict[str, str]) -> dict[str, Any] | None:
    raw = slots["level"].strip()
    value = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
    if value is None or not 0 <= value <= 100:
        return None
    return {"level": value}


def _song(slots: dict[str, str]) -> dict[str, Any] | None:
    query = slots["query"].strip()
    if query in _NOT_SONGS or len(query) < 2 or len(query.split()) > 12:
        return None
    if re.search(r"\b(game|games|movie|role|part|along|around|with you|with me|dead|fair|nice|catch|tag|cards|"
                 r"chess|hide and seek)\b", query):
        return None
    if re.match(r"^(?:a|an|any|another|some|something|one of)\s+(?:song|songs|track|video|tune|music|playlist|album|"
                r"good|nice|random|new|popular|hindi|tamil|english)\b", query):
        return None  # "a song by a Hindi artist": a choice for the model to make, not a search string
    return {"query": query}


_NOT_PLACES = {"and", "should", "will", "do", "is", "it", "my", "the", "next", "this", "that", "here"}


def _weather(slots: dict[str, str]) -> dict[str, Any] | None:
    location = slots.get("location")
    if location and set(location.split()) & _NOT_PLACES:
        return None  # "…in Chennai and should I carry an umbrella" is a question for the model
    return {"location": location} if location else {}


def _search(slots: dict[str, str]) -> dict[str, Any] | None:
    query = slots["query"].strip()
    local = (r"^(?:my|the|this|our)\s+(?:files?|folders?|computer|pc|code|codebase|project|repo|drive|"
             r"documents|downloads|desktop)\b")
    if re.search(local, query) or re.search(r"\bin (?:my|the|this) (?:project|code|repo|codebase|files)\b", query):
        return None  # a local search, not a web search
    return {"query": query}


def _site(slots: dict[str, str]) -> dict[str, Any] | None:
    site = slots["site"].strip()
    browser = None
    in_browser = re.match(r"^(?P<site>.+?) (?:on|in|using|with) (?P<browser>brave|chrome|google chrome|edge|"
                          r"microsoft edge|firefox)(?: browser)?$", site)
    if in_browser:
        site, browser = in_browser.group("site"), in_browser.group("browser").replace("google ", "").replace("microsoft ", "")
    if site in _SITES:
        url = _SITES[site]
    elif re.fullmatch(r"[\w\-]+(?:\.[\w\-]+)*\.(?:com|org|net|io|dev|ai|app|in|co|gov|edu|me|tv|so|gg|xyz)(?:/\S*)?", site):
        url = "https://" + site
    else:
        return None
    return {"url": url, "name": site, **({"browser": browser} if browser else {})}


_RULES_FIRST: list[_Rule] = [
    # ------------------------------------------------------------ conversation control
    _Rule("control.stop", _r(r"stop|stop it|stop talking|stop that|be quiet|quiet|shut up|shush|enough|"
                             r"cancel|cancel (?:that|it)|never ?mind|forget it|nothing|that's all|that is all|"
                             r"stop stop|ok stop|okay stop")),
    _Rule("control.wait", _r(r"wait|wait wait|wait (?:a )?(?:sec|second|minute|moment)|hold on|hang on|"
                             r"one (?:sec|second|moment)|just a (?:sec|second)|wait stop|wait,? stop|hold up")),
    _Rule("control.sleep", _r(r"go to sleep|sleep|stand ?by|stop listening|that's all for now|goodbye|good night|"
                              r"bye|bye bye|see you|go away")),
    _Rule("control.privacy", _r(r"mute (?:yourself|the mic|the microphone|microphone|mic|your mic)|privacy mode|"
                                r"turn off (?:the |your )?(?:mic|microphone)|stop recording")),
    _Rule("control.repeat", _r(r"repeat that|say that again|what did you say|come again|repeat|pardon|sorry what")),
    # ------------------------------------------------------------ coding control (before media "pause")
    _Rule("claude.pause", _r(r"(?:pause|hold|freeze) (?:the )?(?:coding session|coding|claude(?: code)?|session)")),
    _Rule("claude.stop", _r(r"(?:stop|cancel|kill|end|abort) (?:the )?(?:coding task|coding session|claude(?:'s)? task|"
                            r"claude(?: code)?|current task|task)")),
    _Rule("claude.resume", _r(r"(?:resume|continue|unpause|restart) (?:the )?(?:coding session|coding|claude(?: code)?|"
                              r"previous coding session|last coding session|session)|"
                              r"(?:continue|resume|pick up|carry on) (?:from )?where (?:we|you) (?:left off|stopped)")),
    _Rule("claude.status", _r(r"what(?:'s| is) claude (?:doing|working on|up to)|how(?:'s| is) claude (?:doing|getting on)|"
                              r"claude status|(?:what(?:'s| is) the )?status of (?:the )?(?:coding session|claude|task)|"
                              r"is claude (?:done|finished)(?: yet)?|what is claude doing right now|"
                              r"what(?:'s| is) the coding status")),
    _Rule("claude.connect", _r(r"(?:connect|attach|start|open|launch|hook up) claude(?: code)?"
                               r"(?: (?:to|on|for|in) (?:this|the|my) (?:project|repo|repository|codebase)| here)?")),
    _Rule("coding.no_commit", _r(r"(?:don't|do not|dont) commit(?: (?:yet|it|that|anything|the changes))?|no commits?|"
                                 r"hold off on (?:the )?commit(?:ting)?")),
    _Rule("git.diff", _r(r"show me what changed|what changed|what did (?:you|claude) change|"
                         r"show (?:me )?the (?:diff|changes|git diff)|what are the changes")),
    _Rule("project.list", _r(r"(?:show|list|what are|tell me)(?: me)?(?: all)? (?:my|the) projects|"
                             r"what projects do i have|which projects do i have|my projects")),
]

_RULES: list[_Rule] = [
    # ------------------------------------------------------------ media
    _Rule("media.pause", _r(r"pause|pause (?:the )?(?:music|song|track|spotify|playback|it)|"
                            r"stop (?:the )?(?:music|song|playback|spotify)")),
    _Rule("media.resume", _r(r"resume|play|unpause|resume (?:the )?(?:music|song|playback|spotify)|"
                             r"play (?:the |some )?music|continue (?:the )?(?:music|song)|play spotify")),
    _Rule("media.next", _r(r"next|skip|next (?:song|track|video)|skip (?:this )?(?:song|track|one|video)?|"
                           r"play (?:the )?next (?:song|track|one|video)")),
    _Rule("media.previous", _r(r"previous|previous (?:song|track|video)|go back (?:a|one) (?:song|track)|"
                               r"play (?:the )?previous (?:song|track|one)|last (?:song|track)|"
                               r"play (?:the )?last (?:song|track)")),
    _Rule("media.current", _r(r"what(?:'s| is) (?:this|the) (?:song|track|video)|what song is (?:this|playing)|"
                              r"what(?:'s| is) playing(?: now| right now)?|who sings this|"
                              r"what am i listening to|what song is this|what video is this")),
    _Rule("media.play", _r(r"(?:play|put on|queue up)(?: me)? (?P<query>.+?)(?P<spotify> on spotify)?"), _song),
    # ------------------------------------------------------------ volume
    _Rule("volume.set", _r(r"(?:set |turn |change )?(?:the )?(?:system )?volume (?:to |at )?(?P<level>\d{1,3}|\w+)"
                           r"(?: percent| %|%)?"), _level),
    _Rule("volume.up", _r(r"volume up|turn (?:it|the volume|the sound) up|louder|increase (?:the )?volume|"
                          r"raise (?:the )?volume|turn up (?:the )?volume")),
    _Rule("volume.down", _r(r"volume down|turn (?:it|the volume|the sound) down|quieter|softer|"
                            r"lower (?:the )?volume|decrease (?:the )?volume|turn down (?:the )?volume")),
    _Rule("volume.mute", _r(r"mute|mute (?:the )?(?:sound|volume|audio|computer|speakers|pc)")),
    _Rule("volume.unmute", _r(r"unmute|unmute (?:the )?(?:sound|volume|audio|computer|speakers|pc)")),
    # ------------------------------------------------------------ time, date, weather
    _Rule("system.time", _r(r"what(?:'s| is) the time(?: now| right now)?|what time is it(?: now| right now)?|"
                            r"tell me the time|time|current time|the time|do you know what time it is|time check")),
    _Rule("system.date", _r(r"what(?:'s| is) (?:the )?(?:date|day)(?: today)?|what day is (?:it|today)|"
                            r"what(?:'s| is) today(?:'s date)?|today's date|what date is it")),
    _Rule("weather.current", _r(r"(?:what(?:'s| is) the weather(?: like)?|how(?:'s| is) the weather|weather|"
                                r"what(?:'s| is) it like outside|is it (?:going to )?rain(?:ing)?|"
                                r"do i need an umbrella|how hot is it|how cold is it|what's the temperature)"
                                r"(?: (?:today|now|outside|tomorrow|right now))?"
                                r"(?: (?:in|at|for) (?P<location>(?!(?:the|a)\b)[a-z][\w.'-]*(?: [a-z][\w.'-]*){0,2}?))?"
                                r"(?: (?:today|now|tomorrow|right now|outside))?"), _weather),
    # ------------------------------------------------------------ browser & web
    _Rule("browser.open", _r(r"(?:open|launch|start)(?: up)?(?: the| my| a)? (?:browser|web browser|internet)")),
    _Rule("browser.site", _r(r"(?:open|go to|navigate to|visit|load|pull up|bring up) (?P<site>[\w .\-/]+)"), _site),
    _Rule("web.search", _r(r"(?:search|look up|search the web for|search online for|search google for|"
                           r"search the internet for|do a search for|search for|look up online)"
                           r" (?:for )?(?P<query>.+?)(?: on (?:the web|google|the internet|online))?"), _search),
    # ------------------------------------------------------------ screen & clipboard
    _Rule("screen.capture", _r(r"(?:take|grab|capture)(?: a| the)? (?:screen ?shot|screen capture|screen)|screenshot|"
                               r"screen ?shot")),
    _Rule("clipboard.read", _r(r"what(?:'s| is) (?:in|on) (?:my|the) clipboard|read (?:my|the) clipboard|"
                               r"what did i copy")),
    _Rule("clipboard.copy_last", _r(r"copy (?:that|this|it)|copy (?:your|the) (?:answer|response|last answer|reply)")),
    # ------------------------------------------------------------ memory
    _Rule("memory.recall", _r(r"what do you (?:remember|know) about me|what have you remembered|what do you remember|"
                              r"what are my preferences")),
    _Rule("memory.remember", _r(r"(?:remember|remember that|note that|keep in mind that|don't forget that|"
                                r"make a note that|make a note) (?P<fact>.{3,})")),
    _Rule("memory.forget", _r(r"(?:forget|forget that|forget about|delete the memory) (?P<fact>.{3,})")),
]

_APP_RE = _r(r"(?:open|launch|start|run|fire up|bring up|load|pull up)(?: up)?(?: the| my)? "
             r"(?P<name>[\w .+\-']+?)(?: app| application| program)?")
_CLOSE_RE = _r(r"(?:close|quit|exit|kill|shut down|shut)(?: down)?(?: the| my)? "
               r"(?P<name>[\w .+\-']+?)(?: app| application| window| program)?")
_PROJECT_RE = _r(r"(?:open|switch to|switch over to|use|load|go to|work on|start working on|jump to|"
                 r"change to|move to|connect to)(?: my| the| our)? (?P<name>.+?)"
                 r"(?: project| repo| repository| folder| codebase| app)?")
_PROJECT_PATH_RE = re.compile(r"(?:in|at|from|on)\s+(?P<path>[a-z]\s*(?:colon|drive|:)\b.+)$")
_CALC_HINT = re.compile(r"\d|\b(?:plus|minus|times|divided|percent|squared|cubed|square root|power)\b")


class FastPath:
    """Matches whole utterances (or each clause of a compound command) against the grammar.

    Resolvers are injected so this module stays free of OS code:
      * ``resolve_app(name)`` → app entry or None
      * ``resolve_project(name)`` → project or None
      * ``evaluate_math(text)`` → number or None
      * ``desktop()`` → :class:`DesktopView` (cached facts about the active window)
    """

    def __init__(
        self,
        resolve_app: Callable[[str], Any | None] | None = None,
        resolve_project: Callable[[str], Any | None] | None = None,
        evaluate_math: Callable[[str], float | None] | None = None,
        desktop: Callable[[], DesktopView] | None = None,
    ) -> None:
        self.resolve_app = resolve_app
        self.resolve_project = resolve_project
        self.evaluate_math = evaluate_math
        self.desktop = desktop

    def _view(self) -> DesktopView:
        if self.desktop is None:
            return DesktopView()
        try:
            return self.desktop()
        except Exception:
            return DesktopView()

    def match(self, raw_text: str) -> Intent | None:
        text = normalize(raw_text)
        if not text:
            return None
        view = self._view()
        single = self._match_single(raw_text, text, view)
        if single is not None:
            return single
        clauses = cg.split_clauses(raw_text)
        if len(clauses) < 2:
            return None
        steps: list[Intent] = []
        for clause in clauses:
            step = self._match_single(clause, normalize(clause), view)
            if step is None or step.domain in ("control", "claude", "coding", "project", "memory"):
                return None
            steps.extend(step.slots["steps"] if step.name == "sequence" else [step])
        return Intent("sequence", {"steps": steps}, text)

    def _match_single(self, raw_text: str, text: str, view: DesktopView) -> Intent | None:
        if not text:
            return None
        typed = cg.parse_type(raw_text, view, self.resolve_app)
        if typed is not None:
            return typed

        for rule in _RULES_FIRST:
            intent = self._apply(rule, text)
            if intent is not None:
                return intent

        dialog = cg.match_dialog(text, view)
        if dialog is not None:
            return dialog
        computer = cg.match_clause(text, raw_text, view, self.resolve_app)
        if computer is not None:
            return computer

        for rule in _RULES:
            intent = self._apply(rule, text, raw_text)
            if intent is not None:
                return intent

        switch = cg.match_switch(text, view, self.resolve_app)
        if switch is not None:
            return switch

        project = self._match_project(text)
        if project is not None:
            return project

        app = _APP_RE.match(text)
        if app and self.resolve_app is not None:
            resolved = self.resolve_app(app.group("name"))
            if resolved is not None:
                return Intent("app.open", {"app": resolved, "name": app.group("name")}, text)

        close = _CLOSE_RE.match(text)
        if close and self.resolve_app is not None:
            resolved = self.resolve_app(close.group("name"))
            if resolved is not None:
                return Intent("app.close", {"app": resolved, "name": close.group("name")}, text)

        if self.evaluate_math is not None and _CALC_HINT.search(text):
            expression = re.sub(r"^(?:what(?:'s| is)|calculate|compute|how much is|solve|evaluate)\s+", "", text)
            value = self.evaluate_math(expression)
            if value is not None:
                return Intent("calc.evaluate", {"expression": expression, "value": value}, text)
        return None

    @staticmethod
    def _apply(rule: _Rule, text: str, raw_text: str = "") -> Intent | None:
        match = rule.pattern.match(text)
        if not match:
            return None
        slots = {k: v for k, v in match.groupdict().items() if v is not None}
        if rule.accept is not None:
            accepted = rule.accept(slots)
            if accepted is None:
                return None
            slots = accepted
        intent = Intent(rule.name, slots, text)
        if rule.name == "media.play":
            platform = "spotify" if match.groupdict().get("spotify") else "auto"
            intent.call = ("media.play", {"query": (raw_text or text).strip(), "platform": platform})
        elif rule.name == "browser.site":
            args = {"url": slots["url"], "new_tab": True}
            if slots.get("browser"):
                args["browser"] = slots["browser"]
            intent.call = ("browser.open_url", args)
        elif rule.name == "web.search":
            intent.call = ("browser.search", {"query": slots["query"], "engine": "google"})
        return intent

    def _match_project(self, text: str) -> Intent | None:
        spoken_path = _PROJECT_PATH_RE.search(text)
        if spoken_path and re.search(r"\b(?:project|work|open|code|folder|repo)\b", text):
            return Intent("project.open", {"spoken_path": spoken_path.group("path")}, text)
        match = _PROJECT_RE.match(text)
        if not match:
            return None
        name = match.group("name").strip()
        says_project = bool(re.search(r"\b(?:project|repo|repository|codebase)\b", text))
        if self.resolve_project is not None:
            project = self.resolve_project(name)
            if project is not None:
                return Intent("project.open", {"project": project, "name": name}, text)
        if says_project:
            return Intent("project.open", {"name": name, "unresolved": True}, text)
        return None

    def is_command(self, text: str) -> bool:
        intent = self.match(text)
        if intent is None or intent.name in {"memory.remember", "type.text", "web.search", "browser.search"}:
            return False
        if intent.name == "sequence":
            return not any(step.name == "type.text" for step in intent.slots["steps"])
        return True
