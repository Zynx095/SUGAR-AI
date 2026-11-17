"""Request routing — SID's engineering boundary, decided locally (no router LLM call).

    CONTROL           stop / wait / sleep / repeat …              → conversation layer
    COMMAND           engineering command (project, Claude Code)  → tool, no model
    CODING_QA         programming question                        → coding model, no tools
    PROJECT_ANALYSIS  inspect a repository                        → Claude Code, read-only (plan mode)
    BUILD_EXECUTION   change a repository                         → Claude Code
    CLARIFY           engineering, but too ambiguous to execute   → SID asks what is meant
    REJECTED          not software engineering                    → fixed refusal: no model, no tools

Typed and spoken requests take the same path. The deterministic grammar
(:mod:`fastpath`) still recognises desktop commands ("open Chrome", "play…"),
but only engineering commands are executed from it; every other match is
classified like free text, so a desktop or media command can never run.
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

    def route(self, text: str) -> RouteDecision:
        intent = self._fastpath.match(text)
        if intent is not None:
            if intent.domain == "control":
                return RouteDecision(Route.CONTROL, "conversation control", intent)
            if intent.name in ENGINEERING_COMMANDS:
                return RouteDecision(Route.COMMAND, f"engineering command {intent.name}", intent)
            if intent.name in MEMORY_COMMANDS:
                subject = str(intent.slots.get("fact") or "")
                if intent.name == "memory.recall" or classify(subject).is_engineering:
                    return RouteDecision(Route.COMMAND, f"engineering memory {intent.name}", intent)
            # Any other grammar match (apps, media, browser, clock…) is classified as plain text below.

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
