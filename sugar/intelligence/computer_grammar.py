"""Deterministic grammar for computer control.

"Close this tab" with a browser in front is not a question for a language
model; neither is "open Notepad and type hello" or "play Starboy on YouTube".
This grammar turns such utterances into tool calls in well under a
millisecond, using the cached desktop view ("this" = the active window) and
the app resolver. Anything ambiguous returns ``None`` and goes to the model,
which has the same tools.

All patterns are anchored to the whole (normalised) clause, so words inside
a longer sentence never trigger an action.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sugar.computer.keys import KeyParseError, parse_combo, parse_times
from sugar.intelligence.intents import DesktopView, Intent, normalize

ENGINES = (r"(?:youtube|you tube|google|github|git hub|wikipedia|wiki|stack overflow|stackoverflow|reddit|amazon|"
           r"bing|duckduckgo|google maps|maps|google images|images|npm|pypi|twitter|linkedin|netflix|flipkart|"
           r"spotify|the web|the internet|brave search)")
BROWSERS = r"(?:brave|chrome|google chrome|edge|microsoft edge|firefox|opera|vivaldi)"
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5,
             "5th": 5, "sixth": 6, "seventh": 7, "eighth": 8, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9}
_REFS = {"this", "that", "it", "this one", "that one", "the current one", "this window", "that window", "this app",
         "that app", "this application", "that application", "this program", "the current window", "the current app",
         "the window", "the app", "this thing", "that thing"}
_JUST_OPENED = re.compile(r"(?:the )?(?:app|window|program|thing|one)? ?(?:that )?i just opened|what i just opened")


def _call(name: str, tool: str, args: dict[str, Any] | None = None, text: str = "", **slots: Any) -> Intent:
    return Intent(name, {**(args or {}), **slots}, text, call=(tool, dict(args or {})))


def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(r"^(?:" + pattern + r")$")


def _engine(word: str) -> str:
    word = word.strip()
    return {"you tube": "youtube", "git hub": "github", "stackoverflow": "stack overflow", "wiki": "wikipedia",
            "the web": "google", "the internet": "google", "google maps": "maps", "google images": "images",
            "brave search": "brave"}.get(word, word)


def _target(raw: str, view: DesktopView, resolve_app: Callable[[str], Any] | None) -> str | None:
    """Normalise a spoken window target: a reference ("this"), "last_opened", or a known app/window name."""
    target = raw.strip()
    if target in _REFS:
        return target
    if _JUST_OPENED.fullmatch(target):
        return "last_opened"
    name = re.sub(r"^(?:the|my)\s+", "", target)
    name = re.sub(r"\s+(?:app|application|window|program)$", "", name)
    if not name or len(name.split()) > 5:
        return None
    if resolve_app is not None and resolve_app(name) is not None:
        return name
    if any(re.search(rf"\b{re.escape(name)}\b", title) for title in view.titles):
        return name
    return None


# ---------------------------------------------------------------------------------------------- typing

_TYPE_HEAD = re.compile(
    r"^\s*(?:(?:please|hey|ok|okay|can you|could you|now|and|then|also|just|sugar)[,\s]+)*"
    r"(?:(?:in|into|on)\s+(?:the\s+)?(?P<prefix_app>[\w .+\-']{2,30}?),?\s+)?"
    r"(?P<verb>type out|type in|type|write down|write|dictate|enter|input)\b(?P<rest>.*)$",
    re.I | re.S,
)
_SOURCE_REF = re.compile(
    r"^(?:\s*(?:out|in))?\s*(?P<ref>this|that|the|your|my|it)?\s*"
    r"(?:(?:python|javascript|js|java|c\+\+|c#|html|css|sql|bash|powershell|rust|go|typescript|ts)\s+)?"
    r"(?P<what>code|snippet|script|program|function|class|answer|reply|response|text|it|that|this)?"
    r"(?:\s+(?:out|in))?(?:\s+(?:you|that you)\s+(?:just\s+)?(?:wrote|gave me|showed me|made|generated))?"
    r"(?:\s+(?:in|into)\s+(?:the\s+)?(?P<app>[\w .+\-']{2,30}?))?\s*[.!]?$",
    re.I,
)
_GENERATIVE = re.compile(r"^(?:a|an|me|us|some|something|anything|code|program|function|script|essay|story|poem|"
                         r"letter|email|directly|what i say|whatever)\b", re.I)
_TRAILING_KEYS = re.compile(r"^(?P<body>.*?\S)\s*,?\s+(?:and\s+)?(?:then\s+)?(?:press|hit)\s+(?:the\s+)?"
                            r"(?P<keys>[a-z0-9 +]+?)(?:\s+key)?(?:\s+(?P<times>once|twice|thrice|\d{1,2} times))?\s*[.!]?$",
                            re.I | re.S)
_TARGET_SUFFIX = re.compile(r"^(?P<body>.*?\S)\s+(?:in|into|inside)\s+(?:the\s+)?(?P<app>[\w .+\-']{2,30}?)"
                            r"(?:\s+(?:window|app|tab|document|file))?\s*[.!]?$", re.I | re.S)


def parse_type(raw: str, view: DesktopView, resolve_app: Callable[[str], Any] | None) -> Intent | None:
    head = _TYPE_HEAD.match(raw.strip())
    if not head:
        return None
    verb = head.group("verb").lower()
    rest = head.group("rest")
    target = None
    prefix_app = head.group("prefix_app")
    if prefix_app:
        target = _target(prefix_app.lower(), view, resolve_app)
        if target is None:
            return None
    mode = None
    slow = re.match(r"^\s+(?:it\s+)?(?:out\s+)?(?:slowly|character by character|letter by letter)\b(?P<more>.*)$",
                    rest, re.I | re.S)
    if slow:
        mode, rest = "realtime", slow.group("more")
    source = _SOURCE_REF.match(rest)
    if source and (source.group("what") or source.group("ref") in ("it", "that", "this") or not rest.strip()):
        what = (source.group("what") or source.group("ref") or "").lower()
        if not what:
            return None
        if source.group("app"):
            target = _target(source.group("app").lower(), view, resolve_app) or target
        kind = "last_code" if what in ("code", "snippet", "script", "program", "function", "class") else "last_reply"
        args = {"source": kind, **({"target": target} if target else {}), **({"mode": mode} if mode else {})}
        return _call("type.text", "keyboard.type", args, normalize(raw))
    body = re.match(r"^(?:\s+(?:this|the following|that|out|it out))?\s*[:,\-]?\s+(?P<text>\S.*)$", rest, re.S)
    if not body:
        return None
    text = body.group("text").strip()
    separator = bool(re.match(r"^(?:\s+(?:this|the following|that))?\s*[:,\-]", rest))
    if _GENERATIVE.match(text) and not separator:
        return None
    if verb in ("write", "enter", "input") and not separator:
        # "write …" is usually a request to compose something; accept it as dictation only with a target app
        suffix = _TARGET_SUFFIX.match(text)
        if not suffix or _target(suffix.group("app").lower(), view, resolve_app) is None:
            return None
    if mode is None and re.search(r"\s+(?:slowly|character by character)\s*[.!]?$", text, re.I):
        mode = "realtime"
        text = re.sub(r"\s+(?:slowly|character by character)\s*[.!]?$", "", text, flags=re.I)
    steps: list[Intent] = []
    keys = _TRAILING_KEYS.match(text)
    if keys:
        try:
            parse_combo(keys.group("keys"))
            text = keys.group("body")
            times = parse_times(keys.group("times"))
            steps.append(_call("keyboard.press", "keyboard.press",
                               {"keys": keys.group("keys").strip(), "times": times}, normalize(raw)))
        except KeyParseError:
            pass
    if target is None:
        suffix = _TARGET_SUFFIX.match(text)
        if suffix:
            candidate = _target(suffix.group("app").lower(), view, resolve_app)
            if candidate is not None and candidate not in _REFS:
                target, text = candidate, suffix.group("body")
    text = text.strip()
    if text.startswith(("\"", "“")) and text.endswith(("\"", "”")) and len(text) > 1:
        text = text[1:-1]
    if text.endswith(".") and len(text.split()) <= 3 and not re.search(r"[,;:!?]|\.\S", text[:-1]):
        text = text[:-1]  # "Type hello." — the recogniser's full stop on a short phrase, not dictated text
    if not text:
        return None
    args = {"text": text, **({"target": target} if target else {}), **({"mode": mode} if mode else {})}
    typed = _call("type.text", "keyboard.type", args, normalize(raw))
    if steps:
        return Intent("sequence", {"steps": [typed, *steps]}, normalize(raw))
    return typed


# ---------------------------------------------------------------------------------------------- splitting

_VERBS = (r"(?:open|launch|start|close|quit|exit|kill|force|type|write|press|hit|switch|go|minimi[sz]e|maximi[sz]e|"
          r"restore|bring|search|google|play|pause|resume|stop|skip|save|select|copy|paste|undo|redo|delete|scroll|"
          r"reload|refresh|navigate|reopen|mute|unmute|turn|set|focus|show|snap|move|put|click|zoom|new tab|draw|sketch|"
          r"colou?r|fill|read|take|describe|dictate|choose|pick|tick|add|make)")
_SPLIT = re.compile(
    rf"(?:\s*[,;]\s*(?:and\s+)?(?:then\s+)?|\s+(?:and\s+then|and\s+also|and\s+after\s+that|after\s+that|and|then)\s+"
    rf"|\.\s+(?:then\s+|and\s+)?)(?={_VERBS}\b)",
    re.I,
)
_TYPE_START = re.compile(r"(?:^|[,;.]\s*|\s+(?:and\s+then|and|then)\s+)(?=(?:type|write|dictate)\b)", re.I)


def split_clauses(raw: str) -> list[str]:
    """Split "open Notepad and type hello" into clauses; a type clause keeps the rest of the sentence."""
    text = raw.strip()
    type_at = None
    for match in _TYPE_START.finditer(text):
        if match.end() > 0:
            type_at = match
            break
    head, tail = (text[: type_at.start()], text[type_at.end():]) if type_at and type_at.end() > 0 else (text, "")
    clauses = [c.strip(" ,.;") for c in _SPLIT.split(head) if c and c.strip(" ,.;")]
    if tail:
        clauses.append(tail.strip())
    return clauses


# ---------------------------------------------------------------------------------------------- single clauses

_DIALOG = [
    ("discard", _r(r"(?:no,? )?(?:don't|do not|dont) save(?: (?:it|them|that|the changes|changes))?|"
                   r"discard(?: (?:it|them|the changes|changes))?|close (?:it )?without saving|no don't|"
                   r"(?:just )?close it without saving")),
    ("save", _r(r"(?:yes,? )?save(?: (?:it|them|that|the changes|changes|first))?|yes save")),
    ("cancel", _r(r"keep (?:it )?open|don't close(?: it)?|do not close(?: it)?|go back")),
    ("confirm", _r(r"(?:yes,? )?close (?:it|them|all|everything)(?: anyway)?|yes close(?: (?:it|them|all))?|"
                   r"go ahead and close(?: it| them)?|close anyway")),
]

_NEW_TAB = _r(rf"(?:open|create|start|make|add)?(?: a| another| one more)? ?new tab(?: (?:in|on) (?P<browser>{BROWSERS}))?")
_NEW_WINDOW = _r(r"(?:open|create|start)(?: a| another)? new (?P<browser>browser )?window")
_PRIVATE = _r(r"(?:open|start)(?: a| an)?(?: new)? (?:incognito|private|in private|inprivate)(?: window| tab| browser| mode)?")
_REOPEN = _r(r"(?:reopen|re open|restore|bring back|undo close|open again)(?: the| my)?(?: last| closed| last closed|"
             r" recently closed| previous)? tab|undo close tab")
_CLOSE_ALL_TABS = _r(r"close (?:all|all the|all of the|every|all my)(?: browser| open| my)? tabs")
_CLOSE_TAB = _r(rf"close (?:this|the|the current|current|that|my)?\s*(?:{BROWSERS} |browser )?tab")
_CLOSE_NAMED_TAB = _r(r"close (?:the|my) (?P<match>.+?) tab")
_NEXT_TAB = _r(r"(?:go to |switch to |move to )?(?:the )?next tab")
_PREV_TAB = _r(r"(?:go (?:back )?to |switch (?:back )?to |move to )?(?:the )?(?:previous|prior) tab")
_INDEX_TAB = _r(r"(?:go to |switch to |open |move to )?(?:the )?(?P<ord>first|second|third|fourth|fifth|sixth|seventh|"
                r"eighth|last) tab|(?:go to |switch to )?tab (?:number )?(?P<num>\d|one|two|three|four|five|six|seven|eight)")
_NAMED_TAB = _r(r"(?:go|switch|move|jump|take me|flip)(?: back)? to (?:the|my) (?P<match>.+?) tab")
_BACK = _r(r"(?:go )?back(?: a page| one page| to the (?:previous|last) page)?|previous page")
_FORWARD = _r(r"(?:go )?forward(?: a page| one page)?")
_RELOAD = _r(r"(?:reload|refresh)(?: the| this)?(?: page| tab| site| website| it)?")
_SCROLL = _r(r"scroll (?P<dir>down|up)(?: (?:a bit|a little|more|some))?(?: (?P<n>\d+|one|two|three|four|five) "
             r"(?:times|pages|screens))?|page (?P<pdir>down|up)|(?:scroll|go|jump) to the (?P<edge>top|bottom) of the page|"
             r"scroll to the (?P<edge2>top|bottom)")
# General scrolling (any app; ui.scroll picks the scrollable area)
_SCROLL_STOP = _r(r"stop scrolling|(?:that's|thats|that is) enough scrolling|stop the scroll(?:ing)?|"
                  r"(?:ok |okay )?stop (?:the )?auto ?scroll(?:ing)?")
_SCROLL_KEEP = _r(r"(?:keep scrolling|scroll (?:continuously|automatically|non ?stop|until i say stop)|auto ?scroll)"
                  r"(?: (?P<dir>down|up|left|right))?(?: (?P<speed>slowly|slow|quickly|fast))?"
                  r"(?: (?:continuously|until i say stop))?")
_SCROLL_SLOW = _r(r"scroll (?:(?P<dir>down|up) )?(?P<speed>slowly|slow|quickly|fast)(?: (?P<dir2>down|up))?"
                  r"(?: (?:continuously|until i say stop))?")
_SCROLL_ANY = _r(r"scroll(?: the page| it| this)? (?P<dir>down|up|left|right)(?P<amt> a (?:little|bit|tiny bit|"
                 r"little bit|touch)| slightly| a lot| lots| more| further| some| a page| one page| a screen| "
                 r"(?:a )?bit more| way down| a lot more)?(?: (?P<n>\d+|one|two|three|four|five) (?:times|pages|"
                 r"screens))?(?: (?:in|inside|within|on|in the|on the) (?P<where>.+))?")
_SCROLL_EDGE = _r(r"(?:scroll|go|jump|take me) (?:all the way )?(?:to|up to|down to) the (?P<edge>top|bottom|end|start|"
                  r"beginning)(?: of (?:the |this )?(?:page|document|list|file|chat|thread|window))?")
_PAGE_KEY = _r(r"page (?P<dir>down|up)")
_SCROLL_IN = _r(r"scroll (?:in|inside|within|on|through) (?P<where>.+)")
_PICK = _r(r"(?:select|choose|pick|tick|check|uncheck|untick|toggle|open) (?:on )?(?P<target>(?:the )?(?:(?:first|"
           r"second|third|fourth|fifth|sixth|seventh|last|top|1st|2nd|3rd|4th|5th) )?(?:option|result|item|choice|"
           r"entry|check ?box|tick box|radio button|link|button|one)(?:s)?\b.*|(?:the )?.+? (?:option|check ?box|"
           r"tick box|radio button|choice|toggle|switch))")
_CHOOSE_PENDING = _r(r"(?:the |that )?(?P<ord>first|second|third|fourth|fifth|sixth|last|top|bottom|1st|2nd|3rd)(?: one)?"
                     r"(?: please)?|(?:number|option) (?P<num>one|two|three|four|five|six|\d)")
_WHERE = _r(r"where(?:'s| is| are)(?: the| my)? (?P<target>.+?)(?: on (?:the |my )?screen| in this window| here)?|"
            r"(?:find|locate|show me) the (?P<target2>.+?(?:button|box|field|bar|link|menu|tab|icon|option|checkbox))")
_READ_SCREEN = _r(r"(?:read|read out|read me|tell me)(?: what(?:'s| is| it says)?)?(?: the)? (?:text |words )?(?:in|on|of)"
                  r" (?:this|the|my)? ?(?:window|screen|page|dialog|app|document|message)|read (?:this|that|the screen|"
                  r"the window|this window|the dialog|it)(?: to me| out| aloud)?|what does (?:this|the) (?:screen|window|"
                  r"page|message|popup|pop up) say")
_WHAT_ON_SCREEN = _r(r"what(?:'s| is) (?:open )?on (?:my|the) screen(?: right now)?|what(?:'s| is| do you see) on (?:my|the) "
                     r"screen|what can you see on (?:my|the) screen|describe (?:my|the) screen|look at (?:my|the) screen")
_ASK_SCREEN = _r(r"(?P<q>what (?:error|warning|message|problem)(?: is| are)? (?:displayed|shown|showing|on (?:the |my )?screen|"
                 r"here|this)|explain what this (?:dialog|popup|pop up|window|message|prompt|box|error) (?:is )?(?:asking|"
                 r"saying|means|wants)|what (?:is|does) this (?:dialog|popup|pop up|window|message|prompt|error) "
                 r"(?:asking|saying|mean|want)|what should i do next|what do i do (?:now|next|here)|what am i looking "
                 r"at on (?:the |my )?screen|is there an error(?: on (?:the |my )?screen)?)")
_WHAT_CLICKABLE = _r(r"what can i click(?: here| on)?|what (?:buttons|options|controls) (?:are there|do (?:i|you) see|"
                     r"can (?:i|you) see)(?: here| in this window)?")
_CAMERA_ON = _r(r"(?:turn|switch) on (?:the |your |my )?(?:camera|webcam)|(?:turn|switch) (?:the |your |my )?(?:camera|"
                r"webcam) on|(?:open|start|activate|enable) (?:the |your |my )?(?:camera|webcam)")
_CAMERA_OFF = _r(r"(?:turn|switch) off (?:the |your |my )?(?:camera|webcam)|(?:turn|switch) (?:the |your |my )?(?:camera|"
                 r"webcam) off|(?:close|stop|deactivate|disable|release) (?:the |your |my )?(?:camera|webcam)")
_CAMERA_PHOTO = _r(r"(?:take|snap|capture) (?:a |another )?(?:picture|photo|selfie|snapshot)(?: (?:with|using) (?:the |your )?"
                   r"(?:camera|webcam))?")
_CAMERA_LOOK = _r(r"(?P<q>what (?:do|can) you see(?: (?:now|in front of (?:me|you)|through the camera|on the camera))?|"
                  r"(?:look|check)(?: at this)? (?:with|through|using) (?:the |your )?camera|"
                  r"describe (?:what(?:'s| is) in front of me|this object|this|the room|what you see|my surroundings)|"
                  r"(?:can you )?(?:identify|recogni[sz]e) this(?: object| thing| device)?|what is this (?:object|thing|"
                  r"device)|(?:read|what does) (?:the text on )?this (?:paper|page|note|document|label|sign)(?: say)?|"
                  r"read (?:this|the) (?:paper|page|note|document|label|sign)|what am i holding)")
_CAMERA_DEVICES = _r(r"(?:what|which|list(?: the)?) (?:cameras|webcams)(?: do (?:i|you) have| are (?:there|connected))?")

_DICTATE_START = _r(r"(?:start|begin|turn on|enable|enter|go into|switch to)(?: the)? (?:dictation|dictating|dictation mode)"
                    r"(?: mode)?(?: (?:in|into|on|for) (?:the |my )?(?P<app>.+?))?|dictation mode|take (?:a |my )?"
                    r"dictation|(?:type|write) (?:down )?what i say(?: (?:in|into) (?:the |my )?(?P<app2>.+?))?|"
                    r"(?:start|begin) (?:typing|writing) what i (?:say|dictate)")
_DICTATE_STOP = _r(r"(?:stop|end|finish|exit|quit|turn off|cancel) (?:the )?(?:dictation|dictating|writing)(?: mode)?|"
                   r"stop dictating")
_COMPOSE = _r(r"(?:write|compose|draft|type)(?: me| out)? (?P<what>(?:a|an|the|some|two|three|four|five|one|short|long)\b.+?)"
              r"(?: (?:in|into|on) (?:the |my |a )?(?:new )?(?P<app>notepad|word|wordpad|this document|the document|"
              r"this file|onenote|obsidian|vs code|visual studio code))?")
_EDIT_IN_EDITOR = _r(r"(?:go to the next line|next line|new line|new paragraph|start a new paragraph|insert a blank line|"
                     r"delete the last sentence|delete that sentence|continue(?: writing)?|keep writing|write more|"
                     r"select the (?:previous|last) paragraph|select (?:the |this )?paragraph|"
                     r"(?:add|write|insert|start) a (?:new )?(?:bullet(?: point)?|heading|numbered list)(?:[,:]? .+)?|"
                     r"make (?:this|that|it) a heading|indent (?:this|the) (?:paragraph|line)|correct the spelling|"
                     r"fix the spelling|replace .+ with .+|move the cursor to the (?:beginning|start|end)|"
                     r"go to the (?:beginning|start|end) of the (?:document|text|line))")

_ZOOM = _r(r"zoom (?P<how>in|out)|(?:reset|normal) (?:the )?zoom|reset the zoom")
_URL_WHAT = _r(r"what(?:'s| is) (?:the |this )?(?:url|link|address|web ?site|page)(?: of this page| i'm on)?|"
               r"what (?:page|site|website) (?:is this|am i on)")
_URL_COPY = _r(r"copy (?:the |this )?(?:url|link|address|page link)(?: of this page)?")
_SEARCH = [
    _r(rf"(?:search|look up|look for|find)(?: on)? (?P<engine>{ENGINES}) for (?P<query>.+)"),
    _r(rf"(?:search|look up|look for|find)(?: for)? (?P<query>.+?) (?:on|in) (?P<engine>{ENGINES})"),
    _r(rf"(?:on|in) (?P<engine>{ENGINES}),? (?:search|look up|look for|find)(?: for)? (?P<query>.+)"),
    _r(r"(?P<engine>youtube|github|wikipedia|amazon|reddit) search(?: for)? (?P<query>.+)"),
    _r(r"google (?P<query>(?!maps\b|images\b|drive\b|docs\b|chrome\b).+)"),
]
_OPEN_RESULT = _r(r"(?:open|play|click(?: on)?|show(?: me)?|go to|pick|choose|select|watch)(?: the)? (?P<rank>first|"
                  r"second|third|fourth|fifth|last|top|best|most relevant|1st|2nd|3rd|4th|5th)(?: one| result| video| "
                  r"link| search result| hit| match)?|(?:open|play|watch)(?: that| the)? (?:result|video|link)")
_CLICK = _r(r"(?:click|tap|press)(?: on)? (?:the )?(?P<name>.+?)(?: button| link| tab| icon| option)?")

_MIN_ALL = _r(r"minimi[sz]e (?:everything|all(?: the)?(?: windows)?)|show (?:me )?(?:the )?desktop|go to (?:the )?desktop")
_MINIMIZE = _r(r"minimi[sz]e (?P<target>.+)")
_MAXIMIZE = _r(r"maximi[sz]e (?P<target>.+)|make (?P<target2>this|it|that|this window|that window) (?:bigger|maximi[sz]ed|full size)")
_RESTORE = _r(r"(?:restore|unminimi[sz]e|un minimize|un minimise) (?P<target>.+)")
_BRING = _r(r"bring (?P<target>.+?) back|bring back (?P<target2>.+)|bring (?:up )?(?P<target3>.+?) (?:up|to the front)|"
            r"bring up (?P<target4>.+)")
_SWITCH_BACK = _r(r"(?:switch|go|flip|jump) back|(?:switch|go|flip|jump)(?: back)? to the (?:previous|last|other) "
                  r"(?:window|app|application|program)|alt tab")
_SWITCH = _r(r"(?:switch|go|move|jump|flip|change)(?: back| over)? to (?P<target>.+)|focus(?: on)? (?P<target2>.+)|"
             r"show (?:me )?(?P<target3>.+)")
_SNAP = _r(r"(?:snap|move|put|drag|send) (?P<target>.+?) (?:to|on) the (?P<where>left|right|top|bottom)(?: half| side|"
           r" of the screen)?|(?:center|centre) (?P<target2>.+)")
_LIST = _r(r"(?:what|which) (?:windows|apps|applications|programs) (?:are|do i have) (?:open|running)(?: right now)?|"
           r"list (?:the |my |all )?(?:open )?(?:windows|apps)|what(?:'s| is) open(?: right now)?|"
           r"show (?:me )?(?:all )?(?:the |my )?open windows")
_WHAT_ACTIVE = _r(r"what (?:app|window|program) is (?:this|open|active|in front)|what am i (?:looking at|in|on)|"
                  r"which (?:app|window) (?:is this|am i in)")
_FORCE = _r(r"(?:force (?:close|quit|kill|stop)|kill|end task(?: on| for)?|terminate) (?:the |my )?(?P<name>.+?)"
            r"(?: app| process| application| program)?")
_CLOSE = _r(r"(?:close|quit|exit|shut)(?: down)? (?P<target>.+)")
_OPEN_NEW = _r(r"(?:open|launch|start) (?:a |another )?new (?P<name>[\w .+\-']+?)(?: window| document| file| page)?")

_SHORTCUTS: list[tuple[re.Pattern[str], str, int, str]] = [
    (_r(r"select (?:all|everything)(?: the text| of it| text)?|select all text"), "ctrl+a", 1, "select all"),
    (_r(r"copy(?: (?:the )?(?:selection|selected text|highlighted text))?"), "ctrl+c", 1, "copy"),
    (_r(r"paste(?: (?:it|that|this|here|the clipboard|from the clipboard))?"), "ctrl+v", 1, "paste"),
    (_r(r"cut(?: (?:it|that|this|the selection|the selected text))?"), "ctrl+x", 1, "cut"),
    (_r(r"redo(?: (?:that|it|this))?"), "ctrl+y", 1, "redo"),
    (_r(r"save (?:it )?as|save as (?:a )?new file"), "ctrl+shift+s", 1, "save as"),
    (_r(r"new line|next line|line break|go to (?:a|the) new line"), "enter", 1, "new line"),
    (_r(r"new paragraph"), "enter", 2, "new paragraph"),
    (_r(r"(?:go|switch) full ?screen|full ?screen|make (?:it|this|the video|the window) full ?screen|"
        r"(?:exit|leave|toggle) full ?screen"), "f11", 1, "full screen"),
    (_r(r"find (?:on|in) (?:this |the )?page|search (?:this|the) page"), "ctrl+f", 1, "find"),
]
_UNDO = _r(r"undo(?: (?:that|it|this|the last (?:thing|change|edit)|my last change|the typing))?")
_SAVE = _r(r"save(?: (?:it|this|that|the file|the document|my work|file|document|changes|the changes|everything|"
           r"the notepad|this file))?")
_EDITS = [
    (_r(r"(?:delete|remove|erase)(?: the)? last line|(?:delete|remove|erase) (?:that|this|the) line|"
        r"(?:delete|remove|erase) the line"), "delete_last_line"),
    (_r(r"(?:delete|remove|erase)(?: the)? last word|(?:delete|remove|erase) (?:that|this) word"), "delete_last_word"),
    (_r(r"(?:clear|delete|erase|remove|wipe) (?:everything|all(?: of)?(?: the)? text|all of it|the whole "
        r"(?:thing|document|text|page|file)|it all)|clear (?:the )?(?:text|document)|"
        r"select all and delete(?: it)?"), "clear_all"),
    (_r(r"(?:go|jump|move)(?: the cursor)? to the (?:end|bottom)(?: of the (?:document|file|text))?"), "go_to_end"),
    (_r(r"(?:go|jump|move)(?: the cursor)? to the (?:start|beginning|top)(?: of the (?:document|file|text))?"),
     "go_to_start"),
]
_PRESS = _r(r"(?:press|hit|tap|push)(?: the| on)? (?P<keys>.+?)(?: key| keys| button)?(?: (?P<times>once|twice|thrice|"
            r"\d{1,2} times|two times|three times|four times|five times))?")

_STOP_MEDIA = _r(r"stop playing(?: the| this)?(?: music| song| video| it| track)?(?: on (?:spotify|youtube))?|"
                 r"(?:pause|stop) (?:the )?(?:video|youtube|youtube video)")
_RESUME_MEDIA = _r(r"(?:resume|continue|unpause) (?:the )?(?:video|youtube|youtube video)|play the video again")
_SKIP_AD = _r(r"skip (?:the )?ads?")
_PLAY_SOURCE = _r(r"play (?:this|that|the) (?:youtube )?(?:video|link|url)(?: (?:from|in|on) (?P<clip>(?:my |the )?clipboard))?|"
                  r"play (?:the )?(?:youtube )?(?:video|link|url) (?:from|in|on) (?:my |the )?clipboard|"
                  r"play (?:the )?(?:video|link|url|song) i copied|"
                  r"play (?:the )?(?:youtube )?(?:video|link) (?:for|at|from) (?:this|the) (?:url|link)")
_YOUTUBE_PLAY = [
    _r(r"(?:play|put on|watch|queue up|open|find)(?: me)? .+? (?:on|from|in|using) (?:youtube|you tube)"),
    _r(r"(?:on )?(?:youtube|you tube),? (?:play|put on|watch|open) .+"),
    _r(r"(?:play|open|watch|find|show me|pull up|put on)(?: me)? (?:the |a |an )?(?:official |latest |newest |new |"
       r"most recent |best )*(?:music |youtube )?(?:video|videos|vlog|trailer|music video)\b.+"),
    _r(r"(?:play|watch|put on) (?:the )?(?:latest|newest|most recent|new) .+ (?:video|upload|song|track|single)"),
    _r(r"(?:play|watch) .+?(?:'s)? (?:latest|newest|new) (?:video|upload|song|track)"),
    _r(r"(?:play|watch|put on) the (?:official )?(?:music )?video (?:for|of) .+"),
    _r(r"(?:play|put on) the official .+ (?:video|audio)"),
]


def _rank_word(word: str) -> str:
    word = word.strip()
    if word in ("top", "best", "most relevant"):
        return "best"
    return {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth", "5th": "fifth"}.get(word, word)


def match_dialog(text: str, view: DesktopView) -> Intent | None:
    if not view.has_dialog:
        return None
    if re.fullmatch(r"(?:yes|yeah|yep|yup|sure|ok|okay)(?: please| do it| go ahead)?", text):
        choice = "save" if view.dialog_kind == "save" else "confirm"
        return _call("dialog.answer", "dialog.answer", {"choice": choice}, text)
    if re.fullmatch(r"(?:no|nope|nah)(?: thanks)?", text):  # never "don't save": losing work needs those words
        return _call("dialog.answer", "dialog.answer", {"choice": "cancel"}, text)
    for choice, pattern in _DIALOG:
        if pattern.match(text):
            return _call("dialog.answer", "dialog.answer", {"choice": choice}, text)
    return None


def match_clause(text: str, raw: str, view: DesktopView, resolve_app: Callable[[str], Any] | None) -> Intent | None:
    """Computer-control rules for one normalised clause (``raw`` keeps the original casing)."""
    t = text
    # ---------------------------------------------------------------- scrolling, pointing, seeing
    found = match_paint(t, raw, view) or match_writing(t, raw, view) or match_perception(t, view)
    if found is not None:
        return found
    # ---------------------------------------------------------------- browser tabs
    match = _NEW_TAB.match(t)
    if match:
        browser = match.group("browser")
        return _call("browser.new_tab", "browser.new_tab", {"browser": _browser(browser)} if browser else {}, t)
    match = _NEW_WINDOW.match(t)
    if match and (match.group("browser") or view.active_is_browser):
        return _call("browser.open", "browser.open", {"new_window": True}, t)
    if _PRIVATE.match(t):
        return _call("browser.open", "browser.open", {"private": True}, t)
    if _REOPEN.match(t):
        return _call("browser.reopen_tab", "browser.reopen_tab", {}, t)
    if _CLOSE_ALL_TABS.match(t):
        return _call("browser.close_window", "browser.close_window", {}, t)
    if _CLOSE_TAB.match(t):
        return _call("browser.close_tab", "browser.close_tab", {}, t)
    match = _CLOSE_NAMED_TAB.match(t)
    if match and match.group("match") not in ("this", "that", "current"):
        return _call("browser.close_tab", "browser.close_tab", {"match": match.group("match")}, t)
    if _NEXT_TAB.match(t):
        return _call("browser.switch_tab", "browser.switch_tab", {"direction": "next"}, t)
    if _PREV_TAB.match(t):
        return _call("browser.switch_tab", "browser.switch_tab", {"direction": "previous"}, t)
    match = _INDEX_TAB.match(t)
    if match:
        word = match.group("ord") or match.group("num")
        index = -1 if word == "last" else int(word) if word.isdigit() else _ORDINALS.get(word, 1)
        return _call("browser.switch_tab", "browser.switch_tab", {"index": index}, t)
    match = _NAMED_TAB.match(t)
    if match:
        return _call("browser.switch_tab", "browser.switch_tab", {"match": match.group("match")}, t)
    if _BACK.match(t):
        return _call("browser.back", "browser.back", {}, t)
    if _FORWARD.match(t):
        return _call("browser.forward", "browser.forward", {}, t)
    if _RELOAD.match(t):
        return _call("browser.reload", "browser.reload", {}, t)
    match = _SCROLL.match(t)
    if match:  # (general scrolling is matched earlier by match_perception; this catches leftovers)
        edge = match.group("edge") or match.group("edge2")
        direction = edge or match.group("dir") or match.group("pdir") or "down"
        return _call("ui.scroll", "ui.scroll", {"direction": direction}, t)
    match = _ZOOM.match(t)
    if match:
        return _call("browser.zoom", "browser.zoom", {"how": match.group("how") or "reset"}, t)
    if _URL_WHAT.match(t):
        return _call("browser.current", "browser.current", {}, t)
    if _URL_COPY.match(t):
        return _call("browser.copy_url", "browser.copy_url", {}, t)
    for pattern in _SEARCH:
        match = pattern.match(t)
        if match:
            engine = _engine(match.groupdict().get("engine") or "google")
            query = match.group("query").strip()
            if query and not re.match(r"^(?:my|the|this|our) (?:files?|folders?|computer|pc|code|project)\b", query):
                name = "web.search" if engine == "google" else "browser.search"
                return _call(name, "browser.search", {"query": query, "engine": engine}, t, engine=engine)
    if view.has_search:
        match = _OPEN_RESULT.match(t)
        if match:
            rank = _rank_word(match.group("rank")) if match.group("rank") else "best"
            return _call("browser.open_result", "browser.open_result", {"rank": rank}, t)

    # ---------------------------------------------------------------- media
    if _SKIP_AD.match(t):
        return _call("ui.click", "ui.click", {"name": "Skip"}, t)
    if _STOP_MEDIA.match(t):
        return _call("media.pause", "media.pause", {}, t)
    if _RESUME_MEDIA.match(t):
        return _call("media.resume", "media.resume", {}, t)
    match = _PLAY_SOURCE.match(t)
    if match:
        source = "clipboard" if re.search(r"clipboard|copied|url|link", t) else "this"
        return _call("media.play_from", "media.play_from", {"source": source}, t)
    if any(p.match(t) for p in _YOUTUBE_PLAY):
        return _call("media.play", "media.play", {"query": raw.strip(), "platform": "youtube"}, t, platform="youtube")

    # ---------------------------------------------------------------- windows and apps
    if _MIN_ALL.match(t):
        return _call("window.minimize_all", "keyboard.press", {"keys": "win+d"}, t)
    if _SWITCH_BACK.match(t):
        return _call("window.focus", "window.focus", {"target": "previous"}, t)
    for pattern, tool in ((_MINIMIZE, "window.minimize"), (_MAXIMIZE, "window.maximize"), (_RESTORE, "window.restore")):
        match = pattern.match(t)
        if match:
            raw_target = next(g for g in match.groups() if g)
            target = _target(raw_target, view, resolve_app)
            if target is None:
                return None
            return _call(tool, tool, {"target": target}, t)
    match = _SNAP.match(t)
    if match:
        target = _target(match.group("target") or match.group("target2"), view, resolve_app)
        if target is not None:
            where = match.group("where") or "center"
            return _call("window.snap", "window.snap", {"target": target, "where": where}, t)
    if _LIST.match(t):
        return _call("window.list", "window.list", {}, t)
    if _WHAT_ACTIVE.match(t):
        return _call("desktop.context", "desktop.context", {}, t)
    match = _FORCE.match(t)
    if match:
        name = match.group("name")
        if _target(name, view, resolve_app) is not None:
            return _call("app.force_close", "app.force_close", {"name": name}, t)
    match = _CLOSE.match(t)
    if match:
        target = match.group("target").strip()
        scope = "tab" if re.search(r"\b(?:tab|page)$", target) else \
            "window" if re.search(r"\b(?:window|app|application|program)$", target) else "auto"
        if re.fullmatch(r"(?:the |my )?(?:browser|web browser)", target):
            return _call("app.close", "app.close", {"name": "browser", "scope": "app"}, t)
        if target in _REFS or re.fullmatch(r"(?:this|that|it)(?: (?:window|app|application|program|thing|one|page))?",
                                           target):
            return _call("app.close", "app.close", {"name": target.split()[0], "scope": scope}, t)
        if _JUST_OPENED.fullmatch(target):
            return _call("app.close", "app.close", {"name": "last_opened", "scope": "window"}, t)
        name = _target(target, view, resolve_app)
        if name is not None:
            return _call("app.close", "app.close", {"name": name, "scope": scope}, t)
        return None
    match = _OPEN_NEW.match(t)
    if match and resolve_app is not None and resolve_app(match.group("name")) is not None:
        return _call("app.open", "app.open", {"name": match.group("name"), "new_window": True}, t)
    match = _BRING.match(t)
    if match:
        target = _target(next(g for g in match.groups() if g), view, resolve_app)
        if target is not None:
            return _call("window.focus", "window.focus", {"target": target}, t)

    # ---------------------------------------------------------------- keyboard
    match = _PRESS.match(t)
    if match:
        try:
            parse_combo(match.group("keys"))
        except KeyParseError:
            match = None
        if match:
            return _call("keyboard.press", "keyboard.press",
                         {"keys": match.group("keys"), "times": parse_times(match.group("times"))}, t)
    if _UNDO.match(t) and view.last_domain != "coding":
        return _call("keyboard.press", "keyboard.press", {"keys": "ctrl+z"}, t, shortcut="undo")
    if _SAVE.match(t):
        return _call("keyboard.save", "keyboard.save", {}, t)
    for pattern, keys, times, label in _SHORTCUTS:
        if pattern.match(t):
            if keys == "f11" and not view.active_is_browser and "video" not in t:
                return _call("window.maximize", "window.maximize", {"target": "this"}, t)
            return _call("keyboard.press", "keyboard.press", {"keys": keys, "times": times}, t, shortcut=label)
    for pattern, operation in _EDITS:
        if pattern.match(t):
            if operation in ("go_to_end", "go_to_start") and view.active_is_browser:
                return _call("browser.scroll", "browser.scroll",
                             {"direction": "bottom" if operation == "go_to_end" else "top"}, t)
            return _call("keyboard.edit", "keyboard.edit", {"operation": operation}, t)
    match = _CLICK.match(t)
    if match:
        name = match.group("name").strip()
        if name and len(name.split()) <= 6:
            return _call("ui.click", "ui.click", {"name": name}, t)
    return None


_REGION_WORDS = {"left": "left", "right": "right", "top": "top", "bottom": "bottom", "middle": "center", "center": "center",
                 "centre": "center", "side": None}


def _scroll_where(where: str | None) -> dict[str, Any]:
    if not where:
        return {}
    where = where.strip()
    for word, region in _REGION_WORDS.items():
        if region and re.search(rf"\b{word}\b", where):
            return {"region": region}
    if re.search(r"\b(?:document|page|text|file|editor|this)\b", where):
        return {"inside": "document"}
    return {"target": re.sub(r"^(?:the|this|that|my) ", "", where)}


_AMOUNT = {" a little": "little", " a bit": "little", " a tiny bit": "little", " a little bit": "little",
           " slightly": "little", " a touch": "little", " a lot": "more", " lots": "more", " more": "more",
           " further": "more", " a page": "more", " one page": "more", " a screen": "more", " bit more": "normal",
           " a bit more": "normal", " way down": "more", " a lot more": "more", " some": "normal"}


_EDITORS = {"notepad", "wordpad", "winword", "word", "code", "notepad++", "obsidian", "onenote", "soffice"}


_DRAW = _r(r"(?:(?:can you |please )?(?:draw|sketch|doodle)|paint)(?: me| us)? (?P<subject>(?:a|an|the|some|two|three)"
           r" .+?|.+?)(?: (?P<place>beside it|next to it|to the right(?: of it)?|on the right|to the left(?: of it)?|on the"
           r" left|above it|below it|under it|underneath))?(?: (?:in|on|using) (?:ms |microsoft )?paint)?(?: please)?")
_PAINT_CLEAN = _r(r"make it (?:cleaner|neater|tidier|simpler)|clean (?:it|that|the drawing) up|tidy (?:it|that) up|"
                  r"clean up the drawing|redraw it (?:cleaner|more cleanly|neater)")
_PAINT_FILL = _r(r"(?:add|put in) (?:some )?colou?r(?: to it)?|colou?r it(?: in)?(?: (?P<c1>[a-z ]+?))?|fill it(?: in)?"
                 r"(?: with (?P<c2>[a-z ]+?))?|make it (?P<c3>red|orange|yellow|green|blue|light blue|purple|pink|brown|"
                 r"black|gray|grey|gold|turquoise|dark red|dark green)|paint it (?P<c4>[a-z ]+?)")
_PAINT_THICKER = _r(r"make the (?:outline|lines?|edges?|strokes?) thicker|thicker (?:outline|lines?)|make it bolder|"
                    r"(?:use a )?thicker (?:pencil|brush)")
_PAINT_UNDO = _r(r"(?:undo|remove|delete|erase) (?:the )?last (?:shape|drawing|thing (?:you|i) drew)|undo the last thing you drew")
_PAINT_SAVE = _r(r"save (?:this|it|that|the drawing|the picture|the image|my drawing)? ?(?:as|to|called|named) (?P<name>.+)")
_PAINT_NEW = _r(r"(?:start )?(?:a )?new (?:drawing|picture|canvas)|clear the canvas|start over|wipe the canvas")
_PLACES = {"beside it": "right", "next to it": "right", "to the right": "right", "to the right of it": "right",
           "on the right": "right", "to the left": "left", "to the left of it": "left", "on the left": "left",
           "above it": "above", "below it": "below", "under it": "below", "underneath": "below"}
_NOT_DRAWABLE = _r(r"(?:a )?(?:conclusion|conclusions|attention|breath|blank|line under .+|the curtains|the line|a bath|"
                   r"blood|water|lots|straws|cards?|a card|a gun|money|cash|fire|crowds?|comparisons?|inspiration|"
                   r"the short straw)")


def match_paint(t: str, raw: str, view: DesktopView) -> Intent | None:
    """Drawing in Paint, and follow-ups while Paint is open or in front."""
    in_paint = (view.active_app or "").lower() in ("paint", "mspaint") or view.paint_open
    match = _DRAW.fullmatch(t)
    if match:
        subject = match.group("subject").strip()
        explicit = "paint" in t.split() or in_paint or re.match(r"(?:a|an|the|some|two|three) ", subject)
        if explicit and not _NOT_DRAWABLE.fullmatch(subject) and len(subject.split()) <= 6:
            args: dict[str, Any] = {"subject": subject}
            if match.group("place"):
                args["place"] = _PLACES.get(match.group("place"), "right")
            return _call("paint.draw", "paint.draw", args, t)
    if not in_paint:
        return None
    if _PAINT_CLEAN.fullmatch(t):
        return _call("paint.clean", "paint.clean", {}, t)
    match = _PAINT_FILL.fullmatch(t)
    if match:
        colour = next((g for g in match.groups() if g), None)
        return _call("paint.fill", "paint.fill", {"colour": colour.strip()} if colour else {}, t)
    if _PAINT_THICKER.fullmatch(t):
        return _call("paint.thicker", "paint.thicker", {}, t)
    if _PAINT_UNDO.fullmatch(t):
        return _call("paint.undo", "paint.undo", {}, t)
    match = _PAINT_SAVE.fullmatch(t)
    if match:
        name = re.search(r"(?:as|to|called|named) (.+)$", raw.strip().rstrip(".!"), re.I)
        return _call("paint.save", "paint.save", {"name": (name.group(1) if name else match.group("name")).strip()}, t)
    if _PAINT_NEW.fullmatch(t):
        return _call("paint.new", "paint.new", {}, t)
    return None


def match_writing(t: str, raw: str, view: DesktopView) -> Intent | None:
    """Dictation mode, composing into an editor, and editing commands while an editor is in front."""
    match = _DICTATE_STOP.fullmatch(t)
    if match:
        return _call("dictation.stop", "dictation.stop", {}, t)
    match = _DICTATE_START.fullmatch(t)
    if match:
        app = match.group("app") or match.group("app2")
        return _call("dictation.start", "dictation.start", {"target": app} if app else {}, t)
    editor_in_front = (view.active_app or "").lower() in _EDITORS
    match = _COMPOSE.fullmatch(t)
    code = re.search(r"\b(?:function|code|script|program|class|method|regex|query|sql|algorithm|snippet)\b", t)
    if match and (match.group("app") or (editor_in_front and not code)):
        app = match.group("app")
        target = None if not app or app.startswith(("this", "the ")) else app
        prompt = re.sub(r"\s+(?:in|into|on) (?:the |my |a )?(?:new )?(?:notepad|word|wordpad|this document|the document|"
                        r"this file|onenote|obsidian|vs code|visual studio code)[.!]?$", "", raw.strip(), flags=re.I)
        return _call("dictation.write", "dictation.write", {"prompt": prompt, **({"target": target} if target else {})},
                     t)
    if editor_in_front and _EDIT_IN_EDITOR.fullmatch(t):
        return _call("dictation.edit", "dictation.edit", {"command": raw.strip()}, t)
    return None


def match_perception(t: str, view: DesktopView) -> Intent | None:
    """Scrolling, clicking by description, reading/asking about the screen, the camera."""
    if _SCROLL_STOP.fullmatch(t) or (view.scrolling and t in ("stop", "stop it", "enough", "that's enough")):
        return _call("ui.scroll_stop", "ui.scroll_stop", {}, t)
    match = _SCROLL_KEEP.fullmatch(t)
    if match:
        speed = {"slowly": "slow", "slow": "slow", "quickly": "fast", "fast": "fast"}.get(match.group("speed") or "",
                                                                                        "normal")
        return _call("ui.scroll", "ui.scroll", {"direction": match.group("dir") or "down", "continuous": True,
                                                "speed": speed}, t)
    match = _SCROLL_SLOW.fullmatch(t)
    if match:
        speed = "slow" if match.group("speed") in ("slowly", "slow") else "fast"
        direction = match.group("dir") or match.group("dir2") or "down"
        return _call("ui.scroll", "ui.scroll", {"direction": direction, "continuous": True, "speed": speed}, t)
    match = _SCROLL_EDGE.fullmatch(t)
    if match:
        edge = {"end": "bottom", "start": "top", "beginning": "top"}.get(match.group("edge"), match.group("edge"))
        return _call("ui.scroll", "ui.scroll", {"direction": edge}, t)
    match = _SCROLL_ANY.fullmatch(t)
    if match:
        args: dict[str, Any] = {"direction": match.group("dir")}
        amount = _AMOUNT.get(match.group("amt") or "", "normal")
        if amount != "normal":
            args["amount"] = amount
        count = match.group("n")
        if count:
            args["amount"] = "more"
            args["times"] = int(count) if count.isdigit() else _ORDINALS.get(count, 1)
        args.update(_scroll_where(match.group("where")))
        return _call("ui.scroll", "ui.scroll", args, t)
    match = _PAGE_KEY.fullmatch(t)
    if match:
        return _call("ui.scroll", "ui.scroll", {"direction": match.group("dir"), "amount": "more"}, t)
    match = _SCROLL_IN.fullmatch(t)
    if match:
        return _call("ui.scroll", "ui.scroll", {"direction": "down", **_scroll_where(match.group("where"))}, t)
    if view.choosing:
        match = _CHOOSE_PENDING.fullmatch(t)
        if match:
            word = match.group("ord") or match.group("num")
            ordinal = {"last": -1, "bottom": -1, "top": 1, "1st": 1, "2nd": 2, "3rd": 3}.get(word) or \
                (int(word) if word.isdigit() else _ORDINALS.get(word, 1))
            return _call("ui.choose", "ui.choose", {"ordinal": ordinal}, t)
    if _CAMERA_OFF.fullmatch(t):
        return _call("camera.off", "camera.off", {}, t)
    if _CAMERA_ON.fullmatch(t):
        return _call("camera.on", "camera.on", {}, t)
    if _CAMERA_PHOTO.fullmatch(t):
        return _call("camera.photo", "camera.photo", {}, t)
    if _CAMERA_DEVICES.fullmatch(t):
        return _call("camera.devices", "camera.devices", {}, t)
    if not view.camera_on and re.fullmatch(r"what (?:do|can) you see(?: (?:now|right now))?", t):
        return _call("screen.describe", "screen.describe", {}, t)  # the camera is off: SID's eyes are the screen
    match = _CAMERA_LOOK.fullmatch(t)
    if match and (view.camera_on or "camera" in t or "in front of me" in t or "holding" in t or "this object" in t
                  or "paper" in t or "room" in t or "surroundings" in t):
        question = None if t.startswith(("what do you see", "what can you see")) else t
        return _call("camera.look", "camera.look", {"question": question} if question else {}, t)
    if _WHAT_ON_SCREEN.fullmatch(t):
        return _call("screen.describe", "screen.describe", {}, t)
    if _READ_SCREEN.fullmatch(t):
        return _call("screen.read", "screen.read", {}, t)
    match = _ASK_SCREEN.fullmatch(t)
    if match:
        return _call("screen.ask", "screen.ask", {"question": t[:1].upper() + t[1:] + "?"}, t)
    if _WHAT_CLICKABLE.fullmatch(t):
        return _call("ui.controls", "ui.controls", {}, t)
    match = _WHERE.fullmatch(t)
    if match:
        target = (match.group("target") or match.group("target2") or "").strip()
        if target and not re.search(r"\b(?:you|i|we|they|he|she|my house|the nearest|in the world)\b", target) \
                and len(target.split()) <= 7:
            return _call("ui.where", "ui.where", {"name": target}, t)
    match = _PICK.fullmatch(t)
    if match:
        target = match.group("target").strip()
        verb = t.split(" ", 1)[0]
        action = {"tick": "toggle", "check": "toggle", "uncheck": "toggle", "untick": "toggle",
                  "toggle": "toggle"}.get(verb, "select" if verb in ("select", "choose", "pick") else "click")
        if verb == "open" and re.match(r"(?:the )?(?:first|second|third|fourth|fifth|last|top|1st|2nd|3rd) "
                                       r"(?:result|link|video)", target) and view.has_search:
            return None  # the browser's search-result opener handles this one
        args = {"name": target} if action == "click" else {"name": target, "action": action}
        return _call("ui.click", "ui.click", args, t)
    return None


def match_switch(text: str, view: DesktopView, resolve_app: Callable[[str], Any] | None) -> Intent | None:
    """"Switch to / go to / show me X" — only for running windows, tabs or known apps (projects come later)."""
    match = _SWITCH.match(text)
    if not match:
        return None
    raw_target = next(g for g in match.groups() if g).strip()
    tab = re.fullmatch(r"(?:the |my )?(?P<match>.+?) tab", raw_target)
    if tab:
        return _call("browser.switch_tab", "browser.switch_tab", {"match": tab.group("match")}, text)
    if re.fullmatch(r"(?:the )?(?:previous|last|other) (?:window|app|application|program)|previous|back", raw_target):
        return _call("window.focus", "window.focus", {"target": "previous"}, text)
    target = _target(raw_target, view, resolve_app)
    if target is None:
        return None
    return _call("window.focus", "window.focus", {"target": target}, text)


def _browser(word: str | None) -> str | None:
    if not word:
        return None
    word = word.strip()
    return {"google chrome": "chrome", "microsoft edge": "edge"}.get(word, word)
