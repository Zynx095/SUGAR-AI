"""Request routing — decided locally in microseconds (no router LLM call).

    CONTROL    stop / wait / sleep / repeat …               → handled by the conversation layer
    COMMAND    deterministic fast-path intent               → tool, no model
    CODING     repository work on a project                 → Claude Code
    AGENT      multi-step computer operation                → tool-calling model (agent toolset)
    REASONING  explicitly deep / long analysis              → Claude, then FreeLLMAPI
    CHAT       everything else                              → FreeLLMAPI (light toolset), Ollama offline

The old design paid for an extra Ollama round-trip on every request just to
pick a model. Here the cheap, unambiguous signals are used directly, and
ambiguous requests go to CHAT, whose model can still escalate by calling
tools (including ``claude.task``).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum

from sugar.config.settings import Settings
from sugar.intelligence.fastpath import FastPath, Intent
from sugar.intelligence.working import WorkingMemory


class Route(StrEnum):
    CONTROL = "control"
    COMMAND = "command"
    CODING = "coding"
    AGENT = "agent"
    REASONING = "reasoning"
    CHAT = "chat"


@dataclass
class RouteDecision:
    route: Route
    reason: str
    intent: Intent | None = None
    chain: list[str] | None = None
    tool_groups: set[str] | None = None
    tool_names: set[str] | None = None  # further restricts the groups (small prompt = faster first token)
    purpose: str = "chat"


# Conversation gets a deliberately small toolset: every schema costs prompt tokens and latency, and the
# obvious commands (pause, volume, next track…) never reach a model anyway.
CHAT_TOOLS = {
    "weather.current", "web.lookup", "browser.search", "app.open", "app.close", "keyboard.type", "media.play",
    "media.pause", "media.now_playing", "memory.remember", "memory.recall", "calc.evaluate", "claude.task",
    "claude.status", "project.open",
}


_CODING_WORK = re.compile(
    r"\b(?:fix(?:es|ed)?|debug|implement|refactor|build|compile|deploy|lint|test(?:s|ing)?|commit|merge|rebase|"
    r"analy[sz]e|review|investigate|add (?:a |an )?(?:feature|endpoint|page|component|test|button|route|api)|"
    r"write (?:a |the )?(?:test|function|component|endpoint|migration)|run (?:it|the (?:tests?|app|server|build|project))|"
    r"make the (?:ui|page|app|design|frontend|backend|layout|animation)|clean(?:er)? up|optimi[sz]e|"
    r"what(?:'s| is) (?:broken|wrong|failing)|why (?:is|did|does) (?:it|the|this|that) (?:\w+ )?(?:fail|crash|break)|"
    r"broken|failing|crash(?:es|ing)?|bug|error|stack ?trace|exception|undo (?:that|it|the changes?)|revert)\b"
)
_REPO_REFERENCE = re.compile(
    r"\b(?:the|this|my|our) (?:project|repo|repository|code|codebase|app|application|frontend|backend|ui|tests?|"
    r"build|server|api|bug|error|issue|branch|changes|websocket|auth(?:entication)?|database|component|page|"
    r"hero section|animation|layout|function|file)\b|"
    r"\b(?:fix (?:it|that|this|them|all|both)|do it|run it|ship it|build it|test it|fix that too|go ahead|"
    r"commit (?:it|that|them|the changes|everything))\b"
)
_AGENT = re.compile(
    r"\b(?:files?|folders?|directory|directories|terminal|command line|powershell|cmd|screenshot|clipboard|desktop|"
    r"downloads folder|documents folder|rename|move (?:the|my|this|that)|delete|copy (?:the|my|this|that) file|"
    r"install|uninstall|run (?:the )?command|and then|then open|after that|on my screen|what's on (?:my|the) screen)\b"
)
_REASONING = re.compile(
    r"\b(?:think (?:hard|carefully|deeply|it through)|in detail|step by step|deep dive|pros and cons|trade-?offs?|"
    r"architecture|design (?:a|an|the)|compare|comparison|analy[sz]e|research|plan (?:out|for)|strategy|"
    r"explain (?:how|why) .{20,}|evaluate|critique|prove|derivation)\b"
)


class Router:
    def __init__(self, settings: Settings, fastpath: FastPath, working: WorkingMemory,
                 coding_active: callable = lambda: False) -> None:
        self._settings = settings
        self._fastpath = fastpath
        self._working = working
        self._coding_active = coding_active

    def _recent_coding(self, within_s: float = 900) -> bool:
        action = self._working.last_action
        return bool(action and action.domain == "coding" and time.time() - action.ts < within_s)

    def route(self, text: str) -> RouteDecision:
        intent = self._fastpath.match(text)
        if intent is not None:
            if intent.domain == "control":
                return RouteDecision(Route.CONTROL, "fast-path control", intent)
            return RouteDecision(Route.COMMAND, f"fast-path {intent.name}", intent)

        lowered = text.lower()
        chat_chain = self._settings.llm.chat_chain
        has_project = self._working.active_project is not None
        coding_context = has_project or self._coding_active() or self._recent_coding()

        if coding_context and _CODING_WORK.search(lowered) and (
            _REPO_REFERENCE.search(lowered) or self._recent_coding() or len(lowered.split()) <= 8
        ):
            return RouteDecision(Route.CODING, "coding request with a project in context")
        if coding_context and _REPO_REFERENCE.search(lowered) and self._recent_coding():
            return RouteDecision(Route.CODING, "follow-up to coding work")

        if _AGENT.search(lowered):
            return RouteDecision(Route.AGENT, "computer operation", chain=chat_chain,
                                 tool_groups={"agent", "chat"}, purpose="agent")

        words = len(lowered.split())
        if _REASONING.search(lowered) or words >= 35:
            return RouteDecision(Route.REASONING, "explicitly deep or long request",
                                 chain=self._settings.llm.reasoning_chain, tool_groups=None, purpose="reasoning")

        return RouteDecision(Route.CHAT, "conversation", chain=chat_chain, tool_groups={"agent", "chat"},
                             tool_names=CHAT_TOOLS, purpose="chat")
