"""Request routing — SID's engineering boundary, decided locally (no router LLM call).

    CONTROL           stop / wait / sleep / repeat …              → conversation layer
    COMMAND           engineering command (project, Claude Code)  → tool, no model
    CODING_QA         programming question                        → coding model, no tools
    PROJECT_ANALYSIS  inspect a repository                        → Claude Code, read-only (plan mode)
    BUILD_EXECUTION   change a repository                         → Claude Code
    DESKTOP           operate the PC (apps, windows, browser,     → deterministic tool, or the agent model
                      YouTube/Spotify, files, terminal)              with the full computer toolset
    CLARIFY           engineering, but too ambiguous to execute   → SID asks what is meant
    REJECTED          neither engineering nor operating the PC    → fixed refusal: no model, no tools

Typed and spoken requests take the same path. Desktop control
(``conversation.desktop_control``, on by default) lets the deterministic grammar run desktop and media
commands ("open Chrome", "play…", "search YouTube for…") and sends free-form computer operations to the
agent. Small talk and general-knowledge questions are still refused. Risky actions keep going through
the permission manager: destructive ones are always confirmed.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sugar.config.settings import Settings
from sugar.intelligence.domain import Classification, EngineeringIntent, RequestContext, classify
from sugar.intelligence.fastpath import FastPath, Intent, normalize
from sugar.intelligence.working import WorkingMemory


class Route(StrEnum):
    CONTROL = "control"
    COMMAND = "command"
    CODING_QA = "coding_qa"
    PROJECT_ANALYSIS = "project_analysis"
    BUILD_EXECUTION = "build_execution"
    DESKTOP = "desktop"
    CLARIFY = "clarify"
    REJECTED = "rejected"


EXECUTION_ROUTES = frozenset({Route.PROJECT_ANALYSIS, Route.BUILD_EXECUTION})


@dataclass
class RouteDecision:
    route: Route
    reason: str
    intent: Intent | None = None
    chain: list[str] | None = None
    tool_groups: set[str] | None = None  # None/empty: the model gets no tools at all
    tool_names: set[str] | None = None
    purpose: str = "chat"
    classification: Classification | None = None
    project: Any | None = None  # a project the request itself names ("…in Jiva")

    @property
    def category(self) -> str | None:
        return self.classification.intent.value if self.classification else None


# Fast-path intents SID executes directly. Everything else the grammar knows (apps, media, browser,
# volume, clock, typing…) belongs to the old general assistant and is never run.
ENGINEERING_COMMANDS = frozenset({
    "claude.status", "claude.pause", "claude.resume", "claude.stop", "claude.connect", "coding.no_commit",
    "git.diff", "project.list", "project.open",
})
MEMORY_COMMANDS = frozenset({"memory.remember", "memory.forget", "memory.recall"})

COMPUTER_GROUPS = {"agent", "chat", "computer"}

# "type lofi beats in youtube", "search spotify for daft punk", "spotify search interstellar"
_MEDIA_SEARCH = re.compile(
    r"^(?:type|search|search for|look up|find|put)\s+(?P<q1>.+?)\s+(?:in|into|on)\s+(?:the\s+)?"
    r"(?P<t1>youtube|you tube|yt|spotify)(?:\s+search)?(?:\s+bar)?$|"
    r"^(?:search|type in|type into|open)\s+(?:the\s+)?(?P<t2>youtube|you tube|yt|spotify)(?:\s+search)?"
    r"\s+(?:for\s+)?(?P<q2>.+)$|"
    r"^(?P<t3>youtube|you tube|yt|spotify)\s+search(?:\s+for)?\s+(?P<q3>.+)$"
)
# Imperatives that operate the computer even when the grammar can't parse the details
# ("open a new notepad and start writing a Fibonacci function", "close the tab with the error").
_COMPUTER_VERB = re.compile(
    r"^(?:open|close|quit|exit|kill|launch|type|press|hit|click|double.click|right.click|scroll|switch|"
    r"minimi[sz]e|maximi[sz]e|restore|focus|reload|refresh|paste|undo|redo|snap|resize|navigate|play|pause|"
    r"resume|skip|save|bring up|bring back|go to|search (?:youtube|google|github|spotify|for)|watch|mute|"
    r"unmute|shut ?down|restart|lock|turn (?:on|off|up|down)|set|change|install|uninstall|download|"
    r"empty|show|hide|enable|disable|connect|disconnect|run|start|create|make|delete|rename|move|copy|find|"
    r"organi[sz]e|sort|clean up|clear|zip|unzip|extract|compress|print|send|check|tell me|how much|"
    r"how many|what(?:'s| is) (?:my|the)|list|kill|end|free up|update|scan)\b"
)
_COMPUTER_NOUN = re.compile(
    r"\b(?:notepad|tab|tabs|window|windows|browser|chrome|brave|edge|firefox|youtube|spotify|screen|desktop|"
    r"clipboard|keyboard|mouse|cursor|vs code|explorer|file explorer|this page|the page|website|web page|video|"
    r"song|music|playlist|app|application|word document|text box|search bar|address bar|files?|folders?|"
    r"directory|downloads|documents|pictures|wallpaper|volume|brightness|wifi|wi-fi|bluetooth|settings|"
    r"recycle bin|task manager|terminal|powershell|command prompt|cmd|discord|whatsapp|steam|computer|pc|laptop|"
    r"system|screenshot|drive|drives|disk|disks|storage|space|battery|cpu|ram|memory usage|processes|"
    r"process|network|ip address|wifi password|startup apps|updates|drivers?|printer)\b"
)

# Places on the PC that are never a code repository: requests about them are desktop work even when they use
# build-like verbs ("move the files in my downloads folder into a new folder").
_PERSONAL_PLACE = re.compile(
    r"\b(?:downloads?|documents|desktop|pictures|photos|music folder|videos folder|recycle bin|my computer|"
    r"this pc|onedrive|wallpaper|start menu|taskbar|system tray|control panel|settings app)\b"
)

_STATUS = re.compile(
    r"^(?:are you (?:done|finished)(?: yet)?|is it (?:done|finished|ready)(?: yet)?|(?:what's|what is) the status|"
    r"status(?: update| report)?|how(?:'s| is) (?:it|the task|the build|the work) going|where are we|"
    r"how far along are you|what are you (?:doing|working on)|progress(?: report)?|any progress)$"
)
_DEEP = re.compile(
    r"\b(?:think (?:hard|carefully|deeply|it through)|in detail|step by step|deep dive|pros and cons|trade-?offs?|"
    r"compare|comparison|design (?:a|an|the)|architecture|evaluate|critique|prove|derivation)\b"
)
_NAMED_PROJECT = re.compile(
    r"\b(?:in|on|for|inside|within|to) (?:the |my |our )?(?P<name>[\w][\w .\-]{1,40}?)"
    r"(?: project| repo| repository| codebase| app)?(?:[.?!]|$)"
)


class Router:
    def __init__(self, settings: Settings, fastpath: FastPath, working: WorkingMemory,
                 coding_active: Callable[[], bool] = lambda: False) -> None:
        self._settings = settings
        self._fastpath = fastpath
        self._working = working
        self._coding_active = coding_active

    # ------------------------------------------------------------------ context

    def _recent_coding(self, within_s: float = 900) -> bool:
        action = self._working.last_action
        return bool(action and action.domain == "coding" and time.time() - action.ts < within_s)

    def named_project(self, text: str) -> Any | None:
        """A known project the sentence names ("fix the login bug in Jiva"), or None."""
        resolve = self._fastpath.resolve_project
        if resolve is None:
            return None
        plain = normalize(text)
        for match in _NAMED_PROJECT.finditer(plain):
            name = match.group("name").strip()
            if name in {"it", "this", "that", "here", "there", "python", "react", "javascript", "typescript"}:
                continue
            try:
                project = resolve(name)
            except Exception:
                project = None
            if project is not None:
                return project
        return None

    def project_reply(self, text: str) -> Any | None:
        """The user answering "which project?" with just a name ("Jiva", "the portfolio one")."""
        resolve = self._fastpath.resolve_project
        if resolve is None:
            return None
        plain = normalize(text)
        if len(plain.split()) > 6:
            return None
        plain = re.sub(r"^(?:in |use |it's |its |it is |the one called |try )", "", plain)
        try:
            return resolve(plain)
        except Exception:
            return None

    # ------------------------------------------------------------------ routing

    @property
    def desktop_enabled(self) -> bool:
        return bool(self._settings.conversation.desktop_control)

    def _desktop_agent(self, reason: str) -> RouteDecision:
        return RouteDecision(Route.DESKTOP, reason, chain=self._settings.llm.chat_chain,
                             tool_groups=set(COMPUTER_GROUPS), purpose="agent")

    def route(self, text: str) -> RouteDecision:
        plain_text = normalize(text)
        if self.desktop_enabled:
            search = _MEDIA_SEARCH.match(plain_text)
            if search:
                target = next(v for k, v in search.groupdict().items() if k.startswith("t") and v)
                query = next(v for k, v in search.groupdict().items() if k.startswith("q") and v).strip()
                platform = "spotify" if target == "spotify" else "youtube"
                intent = Intent("media.search", {"platform": platform, "query": query}, plain_text,
                                call=("media.search", {"platform": platform, "query": query}))
                return RouteDecision(Route.DESKTOP, f"{platform} search", intent)
        intent = self._fastpath.match(text)
        if intent is not None:
            if intent.domain == "control":
                return RouteDecision(Route.CONTROL, "conversation control", intent)
            if intent.name in ENGINEERING_COMMANDS:
                return RouteDecision(Route.COMMAND, f"engineering command {intent.name}", intent)
            if intent.name in MEMORY_COMMANDS:
                subject = str(intent.slots.get("fact") or "")
                if intent.name == "memory.recall" or classify(subject).is_engineering or self.desktop_enabled:
                    return RouteDecision(Route.COMMAND, f"memory {intent.name}", intent)
            if self.desktop_enabled:
                return RouteDecision(Route.DESKTOP, f"desktop command {intent.name}", intent)
            # Desktop control off: any other grammar match is classified as plain text below.

        plain = normalize(text)
        if _STATUS.match(plain):
            return RouteDecision(Route.COMMAND, "status question", Intent("claude.status", {}, plain))

        project = self.named_project(text)
        context = RequestContext(
            has_project=self._working.active_project is not None,
            recent_coding=self._recent_coding() or self._coding_active(),
            project_named=project is not None,
        )
        classification = classify(text, context)
        category = classification.intent

        personal = bool(_PERSONAL_PLACE.search(plain)) and project is None
        if (self.desktop_enabled and (personal or category not in (EngineeringIntent.BUILD_EXECUTION,
                                                                   EngineeringIntent.PROJECT_ANALYSIS))
                and not classification.courtesy and project is None
                and (_COMPUTER_VERB.search(plain) and _COMPUTER_NOUN.search(plain)
                     or re.match(r"^(?:open|launch|close|play|pause|type|press|click|switch to|minimi[sz]e|"
                                 r"maximi[sz]e|go to|search youtube|search spotify)\b", plain))):
            return self._desktop_agent("computer operation")

        if category == EngineeringIntent.NON_ENGINEERING:
            return RouteDecision(Route.REJECTED, classification.reason, classification=classification)
        if classification.ambiguous:
            return RouteDecision(Route.CLARIFY, classification.reason, classification=classification,
                                 project=project)
        if category == EngineeringIntent.CODING_QA:
            deep = bool(_DEEP.search(plain)) or len(plain.split()) >= 35
            llm = self._settings.llm
            return RouteDecision(Route.CODING_QA, classification.reason,
                                 chain=llm.reasoning_chain if deep else llm.chat_chain,
                                 tool_groups=None, purpose="reasoning" if deep else "chat",
                                 classification=classification)
        route = Route.PROJECT_ANALYSIS if category == EngineeringIntent.PROJECT_ANALYSIS else Route.BUILD_EXECUTION
        return RouteDecision(route, classification.reason, classification=classification, project=project)
