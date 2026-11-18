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
import uuid
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
from sugar.intelligence.health import emergency_message
from sugar.intelligence.personality import action_line, serious_context
from sugar.intelligence.router import Route, RouteDecision, Router
from sugar.intelligence.working import WorkingMemory
from sugar.providers.pool import AllProvidersFailed, ProviderPool
from sugar.security.provenance import user_origin
from sugar.tools.registry import ToolResult

log = logging.getLogger(__name__)

PENDING_TTL_S = 300.0

QA_INSTRUCTIONS = """This is a programming question. Answer it directly from your knowledge as SID.
- Lead with the answer in one or two spoken sentences, then put code, commands or longer detail in markdown.
- Use fenced code blocks with a language tag for any code.
- If the question depends on code you have not been shown, say what you need to see instead of guessing.
- You have no tools in this turn and you cannot change files; never claim to have run, edited or tested anything."""

DESKTOP_INSTRUCTIONS = """This request operates the user's Windows PC. You control it through your tools: apps, windows,
keyboard and mouse, the browser (on the user's own screen), YouTube and Spotify (media.search types a search
into YouTube or Spotify; media.play finds and plays), files, the clipboard, screenshots and PowerShell
(terminal.run). Use as few tool calls as possible, check results, and report what actually happened.
Risky actions are confirmed with the user by the system; don't ask again yourself. Never invent links."""

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
    context: str | None = None  # context layer the turn belongs to (e.g. "roleplay:1a2b3c4d")


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
    if intent.call is not None:  # self-commands (voice, personality, roleplay…) carry their tool call
        return intent.call[0], dict(intent.call[1])
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


# Desktop/media grammar intent → (tool, arguments). Engineering intents use _command_call.
def _desktop_call(intent: Intent, services_state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if intent.call is not None:
        return intent.call[0], dict(intent.call[1])
    slots = intent.slots
    simple = {
        "system.time": "system.time", "system.date": "system.date", "browser.open": "browser.open",
        "media.pause": "media.pause", "media.resume": "media.resume", "media.next": "media.next",
        "media.previous": "media.previous", "media.current": "media.now_playing", "screen.capture": "screen.capture",
        "clipboard.read": "clipboard.read", "memory.recall": "memory.recall", "project.list": "project.list",
        "claude.status": "claude.status", "claude.pause": "claude.pause", "claude.resume": "claude.resume",
        "claude.stop": "claude.stop", "git.diff": "git.diff",
    }
    name = intent.name
    if name in simple:
        return simple[name], {}
    if name == "weather.current":
        return "weather.current", ({"location": slots["location"]} if slots.get("location") else {})
    if name == "app.open":  # the spoken name, so a substitution ("Chrome isn't installed…") can be explained
        return "app.open", {"name": slots.get("name") or slots["app"].name}
    if name == "app.close":
        return "app.close", {"name": slots.get("name") or slots["app"].name}
    if name == "browser.site":
        return "browser.open_url", {"url": slots["url"]}
    if name == "web.search":
        return "browser.search", {"query": slots["query"], "engine": "google"}
    if name == "media.play":
        return "media.play", {"query": slots["query"]}
    if name == "volume.set":
        return "volume.set", {"level": slots["level"]}
    if name == "volume.up":
        return "volume.change", {"delta": 10}
    if name == "volume.down":
        return "volume.change", {"delta": -10}
    if name == "volume.mute":
        return "volume.mute", {"muted": True}
    if name == "volume.unmute":
        return "volume.mute", {"muted": False}
    if name == "type.text":
        return "keyboard.type", {"text": slots["text"]}
    if name == "clipboard.copy_last":
        return "clipboard.write", {"text": services_state.get("last_reply", "")}
    if name == "memory.remember":
        return "memory.remember", {"fact": slots["fact"]}
    if name == "memory.forget":
        return "memory.forget", {"fact": slots["fact"]}
    if name == "project.open":
        if slots.get("project") is not None:
            return "project.open", {"name": slots["project"].name}
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

    personality = None  # PersonalityState, set by the app ("for a moment" overrides count down per answer)

    async def handle(self, text: str, out: TurnOutput, decision: RouteDecision | None = None) -> TurnSummary:
        summary = await self._handle(text, out, decision)
        if self.personality is not None and summary.route in ("answer", "desktop", "coding_qa"):
            self.personality.turn_finished()
        return summary

    def _style(self, text: str, category: str | None) -> tuple[bool, float | None]:
        """(serious?, temperature) for a model call under the current personality."""
        serious = serious_context(text, category)
        personality, _ = self._context.personality()
        temperature = min(personality.temperature, 0.4) if serious else personality.temperature
        return serious, temperature

    def _flavour(self, name: str, args: dict[str, Any], result: ToolResult) -> str:
        """The personality's way of acknowledging a finished desktop action (plain summary otherwise)."""
        kind = {"app.open": "open", "app.close": "close"}.get(name)
        if not kind or not result.ok:
            return result.summary
        data = result.data if isinstance(result.data, dict) else {}
        app = str(data.get("target") or args.get("name") or "").strip()
        if not app or len(app) > 40 or app.lower() in ("this", "that", "it", "last_opened"):
            return result.summary
        if not re.match(r"^(?:Opened|Opening|Switched|Closed|Closing|Focused)\b", result.summary):
            return result.summary  # something other than a plain success ("save changes?") — say it as is
        personality, _ = self._context.personality()
        return action_line(personality, kind, result.summary, app=app[:1].upper() + app[1:])

    async def _handle(self, text: str, out: TurnOutput, decision: RouteDecision | None = None) -> TurnSummary:
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
                          category=decision.category, mode=decision.mode, chain=decision.chain,
                          local_only=decision.local_only)
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
        if decision.route == Route.ANSWER:
            return await self._general(text, decision, out)
        if decision.route == Route.DESKTOP:
            if decision.intent is not None:
                return await self._desktop(decision.intent, out)
            return await self._operate(text, decision, out)
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
        if decision.project is not None and decision.reason == "change not specified":
            project = decision.project
            self._working.set_project(project.name, project.path)
            self._bus.publish("project.active", name=project.name, path=project.path,
                              stack=getattr(project, "stack", []))
            out.say(f"I'll work in {project.name}. What should I change?")
            out.show(f"Project directory: `{project.path}`")
            return TurnSummary("clarify", ok=True, category=decision.category)
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
            out.say(self._flavour(name, args, result))
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

    # ------------------------------------------------------------------ desktop control

    async def _desktop(self, intent: Intent, out: TurnOutput) -> TurnSummary:
        """A desktop/media command the grammar fully understood: run it directly, no model."""
        if intent.name == "calc.evaluate":
            from sugar.tools.calculator import format_number

            out.say(f"That's {format_number(intent.slots['value'])}.")
            return TurnSummary("desktop", tools=["calc"])
        if intent.name == "sequence":
            return await self._sequence(intent.slots["steps"], out)
        if intent.name == "app.missing":
            name = str(intent.slots.get("name", "")).strip()
            out.say(f"I can't find an app called {name[:1].upper() + name[1:]} on this PC. If it's installed "
                    "somewhere unusual, tell me where, or I can look for it online.")
            return TurnSummary("desktop", ok=False)
        call = _desktop_call(intent, self._state)
        if call is None:
            return await self._operate(intent.text, RouteDecision(Route.DESKTOP, "grammar fallback",
                                                                chain=self._settings.llm.chat_chain,
                                                                tool_groups={"agent", "chat", "computer"},
                                                                purpose="agent"), out)
        name, args = call
        if name == "keyboard.type":
            out.ack("Typing.")
        return await self._run_tool(out, name, args, "desktop")

    async def _sequence(self, steps: list[Intent], out: TurnOutput) -> TurnSummary:
        """Compound commands ("open Notepad and type hello"): run each step, stop at the first failure."""
        tools: list[str] = []
        spoke = False
        out.trace.mark("tool_start")
        for step in steps:
            call = _desktop_call(step, self._state)
            if call is None:
                out.say("I can't do part of that.")
                return TurnSummary("desktop", tools=tools, ok=False)
            name, args = call
            if name == "keyboard.type":
                out.ack("Typing.")
            result: ToolResult = await self._executor.execute(name, args, origin="user")
            tools.append(name)
            if result.display:
                out.show(result.display)
            if not result.ok:
                out.say(result.summary)
                out.trace.mark("tool_done")
                return TurnSummary("desktop", tools=tools, ok=False)
            if result.speak:
                out.say(self._flavour(name, args, result))
                spoke = True
            if isinstance(result.data, dict) and result.data.get("waiting_for"):
                break  # an app asked a question ("save changes?"); let the user answer first
        out.trace.mark("tool_done")
        if not spoke:
            out.say("Done.")
        return TurnSummary("desktop", tools=tools)

    async def _operate(self, text: str, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        """Free-form computer operation: a model with the full computer toolset (permission-checked)."""
        serious, temperature = self._style(text, decision.category)
        messages = self._context.build(text, instructions=DESKTOP_INSTRUCTIONS, serious=serious)
        tools_used: list[str] = []

        def on_tool(record: ToolRecord) -> None:
            tools_used.append(record.name)
            if record.result.display:
                out.show(record.result.display)

        try:
            result = await self._agent_loop_run(messages, decision, out, on_tool, temperature)
        except AllProvidersFailed as exc:
            log.warning("all providers failed: %s", exc)
            out.say(self._offline_message())
            self._bus.publish("error", source="llm", message=str(exc))
            return TurnSummary("desktop", ok=False)
        if not result.text.strip() and tools_used:
            out.say("Done.")
        out.trace.info.update({"provider": result.provider, "model": result.model, "steps": result.steps})
        return TurnSummary("desktop", result.provider, result.model, tools_used)

    async def _agent_loop_run(self, messages, decision: RouteDecision, out: TurnOutput, on_tool, temperature=None):
        return await self._agent.run(
            messages,
            chain=decision.chain or self._settings.llm.chat_chain,
            tool_groups=decision.tool_groups or {"agent", "chat", "computer"},
            tool_names=None,
            purpose="agent",
            on_text=out.stream,
            on_tool_result=on_tool,
            trace=out.trace,
            max_steps=10,
            user_origin=user_origin(getattr(out, "source", "text")),
            temperature=temperature,
        )

    # ------------------------------------------------------------------ programming questions

    async def _general(self, text: str, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        """Any non-engineering request: a model chosen for the category, with the category's tools only."""
        universal = decision.universal
        category = decision.category
        if universal is not None and universal.flags.get("emergency"):
            message = emergency_message(text) or ""
            log_event("HEALTH_EMERGENCY", category=category)
            out.say(message)
            return TurnSummary("answer", category=category)
        if decision.local_only and not decision.chain:
            out.say("That includes your personal health information, and no local model is running, so I'd have "
                    "to send it to an online model. Say \"allow online health answers\" if that's okay, or ask it "
                    "without the personal details.")
            return TurnSummary("answer", ok=False, category=category)
        context_tag = None
        instructions = decision.instructions
        if category == "roleplay":
            roleplay = self._working.roleplay
            if roleplay is None:
                roleplay = {"id": uuid.uuid4().hex[:8], "premise": text[:500], "started": time.time()}
                self._working.roleplay = roleplay
                self._bus.publish("roleplay.started", id=roleplay["id"])
            context_tag = f"roleplay:{roleplay['id']}"
            instructions += f"\nThe roleplay's premise, as the user set it up: {roleplay['premise']}"
        if decision.local_only:
            instructions += ("\nThe user's personal health information stays on this computer: you are a local "
                             "model. Be especially careful about what you are unsure of.")
        serious, temperature = self._style(text, category)
        messages = self._context.build(text, instructions=instructions or None, context_tag=context_tag,
                                       serious=serious)
        tools_used: list[str] = []

        def on_tool(record: ToolRecord) -> None:
            tools_used.append(record.name)
            if record.result.display and category != "research":
                out.show(record.result.display)

        try:
            result = await self._agent.run(
                messages,
                chain=decision.chain or self._settings.llm.chat_chain,
                tool_groups=decision.tool_groups,
                tool_names=decision.tool_names,
                purpose=decision.purpose,
                on_text=out.stream,
                on_tool_result=on_tool,
                trace=out.trace,
                max_steps=max(1, decision.max_steps),
                model_for=self._model_overrides(decision),
                user_origin=user_origin(getattr(out, "source", "text")),
                temperature=temperature,
            )
        except AllProvidersFailed as exc:
            log.warning("all providers failed: %s", exc)
            out.say(self._offline_message())
            self._bus.publish("error", source="llm", message=str(exc))
            return TurnSummary("answer", ok=False, category=category, context=context_tag)
        if not result.text.strip() and tools_used:
            out.say("Done.")
        out.trace.info.update({"provider": result.provider, "model": result.model, "steps": result.steps})
        return TurnSummary("answer", result.provider, result.model, tools_used, category=category,
                           context=context_tag)

    async def _answer(self, text: str, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        chain = decision.chain or self._settings.llm.chat_chain
        serious, temperature = self._style(text, decision.category)
        messages = self._context.build(text, instructions=QA_INSTRUCTIONS, serious=serious)
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
                temperature=temperature,
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
        if self._pool.connectivity() == "offline":
            return ("No language model is reachable right now: the online models and the local fallback are all "
                    "down. Commands, desktop control and Claude Code still work.")
        if "ollama" in down and "freellm" in down:
            return ("My language models are unreachable. FreeLLMAPI and Ollama are both down. "
                    "Claude Code tasks and project commands still work.")
        return "No model answered. Try again in a moment."

    async def answer_coding_permission(self, approved: bool, out: TurnOutput) -> TurnSummary:
        return await self._run_tool(out, "claude.approve", {"approved": approved}, "command")
