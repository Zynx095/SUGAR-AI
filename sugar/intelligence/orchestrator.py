"""Turn execution: classify → route → act → respond.

The orchestrator never touches audio. It writes to a :class:`TurnOutput`
(implemented by the conversation layer), which streams display text to the
UI and speakable chunks to TTS.

Enforcement lives here, in code:
  * REJECTED requests get a fixed refusal — no language model, no tool, no Claude Code;
  * CODING_QA requests reach a model with *no tools*, so an answer can't act on anything;
  * only PROJECT_ANALYSIS / BUILD_EXECUTION start Claude Code, and only with an explicit
    project chosen in this session (or named in the request) — never one inferred from history;
  * destructive requests go through the permission manager before Claude Code starts.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sugar.agent.executor import ToolExecutor
from sugar.agent.loop import AgentLoop, ToolRecord
from sugar.coding.projects import extract_target_path, path_allowed
from sugar.coding.sessions import CodingSessionManager
from sugar.config.settings import Settings
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.core.metrics import TurnTrace
from sugar.intelligence.context import ContextBuilder
from sugar.intelligence.domain import REFUSAL
from sugar.intelligence.fastpath import Intent
from sugar.intelligence.router import Route, RouteDecision, Router
from sugar.intelligence.working import WorkingMemory
from sugar.providers.pool import AllProvidersFailed, ProviderPool
from sugar.tools.registry import ToolResult

log = logging.getLogger(__name__)

PENDING_TTL_S = 300.0

QA_INSTRUCTIONS = """This is a programming question. Answer it directly from your knowledge as SID.
- Lead with the answer in one or two spoken sentences, then put code, commands or longer detail in markdown.
- Use fenced code blocks with a language tag for any code.
- If the question depends on code you have not been shown, say what you need to see instead of guessing.
- You have no tools in this turn and you cannot change files; never claim to have run, edited or tested anything."""

_NEW_PROJECT = re.compile(
    r"\b(?:create|build|make|scaffold|start|set ?up|bootstrap|initiali[sz]e|generate|develop)\b.*\b"
    r"(?:new )?(?:app|application|website|site|web ?app|project|api|service|cli|game|bot|tool|dashboard|"
    r"extension|library|package|backend|frontend|server|script)\b"
)
_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|wipe|erase|purge|nuke|drop|destroy|truncate)\b.*\b(?:repo|repository|project|"
    r"all (?:the )?files|every file|everything|database|tables?|branch(?:es)?|folder|directory|directories|"
    r"node_modules|history|commits?|data)\b|"
    r"\b(?:force[- ]push|push --force|reset --hard|git clean|rewrite (?:the )?history|squash all|"
    r"format (?:the )?(?:drive|disk))\b"
)
_COURTESY_REPLIES = {
    "question about SID": ("I'm SID. I answer programming questions, analyse repositories and build software "
                           "through Claude Code. Name a project and a task."),
    "thanks": "Acknowledged.",
    "greeting": "Ready. Name the engineering task.",
}


class TurnOutput(Protocol):
    trace: TurnTrace

    def stream(self, delta: str) -> None: ...

    def say(self, text: str) -> None: ...

    def show(self, markdown: str) -> None: ...

    def ack(self, text: str) -> None: ...


@dataclass
class TurnSummary:
    route: str
    provider: str | None = None
    model: str | None = None
    tools: list[str] = field(default_factory=list)
    ok: bool = True
    category: str | None = None


@dataclass
class PendingTask:
    """An engineering request waiting for the user to say which project it belongs to."""

    text: str
    route: Route
    created: float = field(default_factory=time.monotonic)

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.created < PENDING_TTL_S


def is_destructive_request(text: str) -> bool:
    return bool(_DESTRUCTIVE.search(text.lower()))


def _command_call(intent: Intent) -> tuple[str, dict[str, Any]] | None:
    """Engineering fast-path intent → (tool, arguments)."""
    simple = {"project.list": "project.list", "claude.status": "claude.status", "claude.pause": "claude.pause",
              "claude.resume": "claude.resume", "claude.stop": "claude.stop", "git.diff": "git.diff",
              "memory.recall": "memory.recall"}
    if intent.name in simple:
        return simple[intent.name], {}
    slots = intent.slots
    if intent.name == "memory.remember":
        return "memory.remember", {"fact": slots["fact"]}
    if intent.name == "memory.forget":
        return "memory.forget", {"fact": slots["fact"]}
    if intent.name == "project.open":
        if slots.get("project") is not None:
            project = slots["project"]
            return "project.open", {"name": project.name, "path": getattr(project, "path", None)}
        if slots.get("spoken_path"):
            return "project.open", {"path": slots["spoken_path"]}
        return "project.open", {"name": slots.get("name", "")}
    return None


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        router: Router,
        pool: ProviderPool,
        agent: AgentLoop,
        executor: ToolExecutor,
        context: ContextBuilder,
        working: WorkingMemory,
        sessions: CodingSessionManager,
        shared_state: dict[str, Any],
    ) -> None:
        self._settings = settings
        self._bus = bus
        self.router = router
        self._pool = pool
        self._agent = agent
        self._executor = executor
        self._context = context
        self._working = working
        self._sessions = sessions
        self._state = shared_state
        self.pending: PendingTask | None = None

    async def handle(self, text: str, out: TurnOutput, decision: RouteDecision | None = None) -> TurnSummary:
        # Answering "which project?" with a bare name resumes the request that was waiting for it.
        if decision is None and self.pending is not None and self.pending.fresh:
            project = self.router.project_reply(text)
            if project is not None:
                return await self._resume_pending(project, out)

        decision = decision or self.router.route(text)
        out.trace.mark("route")
        out.trace.info["route"] = decision.route.value
        out.trace.info["category"] = decision.category
        intent_name = decision.intent.name if decision.intent else None
        log_event("ROUTE_SELECTED", route=decision.route.value, reason=decision.reason, intent=intent_name,
                  category=decision.category)
        self._bus.publish("route.selected", route=decision.route.value, reason=decision.reason, intent=intent_name,
                          category=decision.category)
        self._working.last_user_text = text

        if decision.route == Route.REJECTED:
            return self._reject(decision, out)
        if decision.route == Route.CLARIFY:
            return self._clarify(decision, out)
        if decision.route == Route.COMMAND and decision.intent is not None:
            return await self._command(decision.intent, out)
        if decision.route in (Route.PROJECT_ANALYSIS, Route.BUILD_EXECUTION):
            return await self._engineering(text, decision, out)
        if decision.route == Route.CODING_QA:
            return await self._answer(text, decision, out)
        # CONTROL is handled by the conversation layer; reaching here means it was submitted directly.
        out.say(REFUSAL)
        return TurnSummary(decision.route.value, ok=False)

    # ------------------------------------------------------------------ boundary

    def _reject(self, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        classification = decision.classification
        reply = REFUSAL
        if classification is not None and classification.courtesy:
            plain = self._working.last_user_text.lower().strip(" .!?")
            if classification.reason == "question about SID":
                reply = _COURTESY_REPLIES["question about SID"]
            elif plain.startswith(("thank", "cheers")):
                reply = _COURTESY_REPLIES["thanks"]
            else:
                reply = _COURTESY_REPLIES["greeting"]
        log_event("REQUEST_REJECTED", reason=decision.reason)
        self._bus.publish("route.rejected", reason=decision.reason)
        out.say(reply)
        return TurnSummary("rejected", ok=classification is not None and classification.courtesy,
                           category="non_engineering")

    def _clarify(self, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        if self._working.active_project is None and decision.project is None:
            out.say("There's no engineering task in context. State the change and the project it applies to.")
        else:
            out.say("That's ambiguous. State the exact change you want before I touch the code.")
        return TurnSummary("clarify", ok=False, category=decision.category)

    # ------------------------------------------------------------------ deterministic commands

    async def _command(self, intent: Intent, out: TurnOutput) -> TurnSummary:
        if intent.name == "claude.connect":
            active = self._working.active_project
            if active is None:
                return await self._ask_for_project(out, "Which project should I connect Claude Code to?")
            return await self._run_tool(out, "project.open",
                                        {"name": active["name"], "path": active["path"], "open_editor": False},
                                        "command")
        if intent.name == "coding.no_commit":
            stopped = False
            for session in self._sessions.running():
                if session.task and "commit" in session.task.lower():
                    await self._sessions.stop(session, reason="the user said not to commit")
                    stopped = True
            out.say("Stopped. Nothing gets committed." if stopped else "Understood. Nothing gets committed.")
            return TurnSummary("command", tools=["coding.no_commit"])
        if intent.name == "project.open" and intent.slots.get("unresolved"):
            return await self._ask_for_project(out, f"I can't find a project called {intent.slots.get('name')}.")
        call = _command_call(intent)
        if call is None:
            out.say(REFUSAL)
            return TurnSummary("command", ok=False)
        name, args = call
        summary = await self._run_tool(out, name, args, "command")
        if name == "project.open" and summary.ok and self.pending is not None and self.pending.fresh:
            pending, self.pending = self.pending, None
            return await self._engineering(pending.text, RouteDecision(pending.route, "resumed after project choice"),
                                           out, announce_project=False)
        return summary

    async def _run_tool(self, out: TurnOutput, name: str, args: dict[str, Any], route: str) -> TurnSummary:
        out.trace.mark("tool_start")
        result: ToolResult = await self._executor.execute(name, args, origin="user")
        out.trace.mark("tool_done")
        if result.speak or not result.ok:
            out.say(result.summary)
        if result.display:
            out.show(result.display)
        return TurnSummary(route, tools=[name], ok=result.ok)

    async def _ask_for_project(self, out: TurnOutput, question: str) -> TurnSummary:
        result = await self._executor.execute("project.list", {}, origin="user")
        names = (result.data or {}).get("projects", [])[:4] if result.ok else []
        if names:
            spoken = ", ".join(p["name"] for p in names)
            out.say(f"{question} Known projects include {spoken}.")
        else:
            out.say(question)
        return TurnSummary("clarify", tools=["project.list"], ok=False)

    # ------------------------------------------------------------------ Claude Code

    async def _engineering(self, text: str, decision: RouteDecision, out: TurnOutput, *,
                           announce_project: bool = True) -> TurnSummary:
        analysis = decision.route == Route.PROJECT_ANALYSIS
        project, answered = await self._target_project(text, decision, out)
        if answered:
            return TurnSummary("clarify", ok=False, category=decision.category)
        if project is None:
            self.pending = PendingTask(text, decision.route)
            if not analysis and _NEW_PROJECT.search(text.lower()):
                out.say("Name the target directory and the core requirements. "
                        "I'll establish the project structure before implementation.")
                return TurnSummary("clarify", ok=False, category=decision.category)
            question = ("Which project should I inspect?" if analysis else "Which project does this apply to?")
            summary = await self._ask_for_project(out, question)
            summary.category = decision.category
            return summary
        self.pending = None
        args = {"task": text, "project": project["name"], "path": project["path"],
                "mode": "analyze" if analysis else "work"}
        if not analysis and is_destructive_request(text):
            args["destructive"] = True  # raises the permission level: SID asks before Claude Code starts
        summary = await self._run_tool(out, "claude.task", args, decision.route.value)
        summary.category = decision.category
        return summary

    async def _target_project(self, text: str, decision: RouteDecision,
                              out: TurnOutput) -> tuple[dict[str, str] | None, bool]:
        """The project this request applies to — explicit, never inferred from history.

        Returns ``(project, answered)``; ``answered`` means SID already told the user why it can't proceed.
        """
        if decision.project is not None:
            named = decision.project
            return {"name": named.name, "path": named.path}, False
        target = extract_target_path(text)
        if target is not None:
            if not path_allowed(target, list(self._settings.permissions.allowed_roots)):
                roots = ", ".join(str(r) for r in self._settings.permissions.allowed_roots)
                out.say(f"{target} is outside the directories I'm permitted to modify. Allowed roots: {roots}.")
                return None, True
            result = await self._executor.execute("project.open", {"path": str(target), "open_editor": False,
                                                                   "create": True}, origin="user")
            if not result.ok:
                out.say(result.summary)
                return None, True
            data = (result.data or {}).get("project") or {}
            return {"name": data.get("name", Path(target).name), "path": data.get("path", str(target))}, False
        return self._working.active_project, False

    async def _resume_pending(self, project: Any, out: TurnOutput) -> TurnSummary:
        pending, self.pending = self.pending, None
        assert pending is not None
        result = await self._executor.execute("project.open", {"name": project.name, "path": project.path,
                                                               "open_editor": False}, origin="user")
        if not result.ok:
            out.say(result.summary)
            return TurnSummary("command", tools=["project.open"], ok=False)
        log_event("PENDING_RESUMED", project=project.name)
        return await self._engineering(pending.text, RouteDecision(pending.route, "resumed after project choice"), out)

    # ------------------------------------------------------------------ programming questions

    async def _answer(self, text: str, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        chain = decision.chain or self._settings.llm.chat_chain
        messages = self._context.build(text, instructions=QA_INSTRUCTIONS)
        tools_used: list[str] = []

        def on_tool(record: ToolRecord) -> None:  # never called: the QA path has no tools
            tools_used.append(record.name)

        try:
            result = await self._agent.run(
                messages,
                chain=chain,
                tool_groups=None,
                tool_names=None,
                purpose=decision.purpose,
                on_text=out.stream,
                on_tool_result=on_tool,
                trace=out.trace,
                max_steps=1,
                model_for=self._model_overrides(decision),
            )
        except AllProvidersFailed as exc:
            log.warning("all providers failed: %s", exc)
            out.say(self._offline_message())
            self._bus.publish("error", source="llm", message=str(exc))
            return TurnSummary(decision.route.value, ok=False, category=decision.category)
        out.trace.info.update({"provider": result.provider, "model": result.model, "steps": result.steps})
        return TurnSummary(decision.route.value, result.provider, result.model, tools_used,
                           category=decision.category)

    def _model_overrides(self, decision: RouteDecision) -> dict[str, str]:
        llm = self._settings.llm
        if decision.purpose == "reasoning":
            return {"freellm": llm.freellm.smart_model, "ollama": llm.ollama.smart_model}
        return {}

    def _offline_message(self) -> str:
        status = self._pool.status()
        down = [name for name, info in status.items() if not info["available"] or info["last_error"]]
        if "ollama" in down and "freellm" in down:
            return ("My language models are unreachable. FreeLLMAPI and Ollama are both down. "
                    "Claude Code tasks and project commands still work.")
        return "No model answered. Try again in a moment."

    async def answer_coding_permission(self, approved: bool, out: TurnOutput) -> TurnSummary:
        return await self._run_tool(out, "claude.approve", {"approved": approved}, "command")
