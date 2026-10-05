"""Turn execution: route → act → respond.

The orchestrator never touches audio. It writes to a :class:`TurnOutput`
(implemented by the conversation layer), which streams display text to the
UI and speakable chunks to TTS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sugar.agent.executor import ToolExecutor
from sugar.agent.loop import AgentLoop, ToolRecord
from sugar.coding.sessions import CodingSessionManager
from sugar.config.settings import Settings
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.core.metrics import TurnTrace
from sugar.intelligence.context import ContextBuilder
from sugar.intelligence.fastpath import Intent
from sugar.intelligence.router import Route, RouteDecision, Router
from sugar.intelligence.working import WorkingMemory
from sugar.providers.pool import AllProvidersFailed, ProviderPool
from sugar.tools.calculator import format_number
from sugar.tools.registry import ToolResult

log = logging.getLogger(__name__)


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


# fast-path intent → (tool name, argument builder)
def _intent_call(intent: Intent, services_state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
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

    async def handle(self, text: str, out: TurnOutput, decision: RouteDecision | None = None) -> TurnSummary:
        decision = decision or self.router.route(text)
        out.trace.mark("route")
        out.trace.info["route"] = decision.route.value
        log_event("ROUTE_SELECTED", route=decision.route.value, reason=decision.reason,
                  intent=decision.intent.name if decision.intent else None)
        self._bus.publish("route.selected", route=decision.route.value, reason=decision.reason,
                          intent=decision.intent.name if decision.intent else None)
        self._working.last_user_text = text

        if decision.route == Route.COMMAND and decision.intent is not None:
            return await self._command(decision.intent, out)
        if decision.route == Route.CODING:
            return await self._coding(text, out)
        return await self._model(text, decision, out)

    # ------------------------------------------------------------------ deterministic

    async def _command(self, intent: Intent, out: TurnOutput) -> TurnSummary:
        if intent.name == "calc.evaluate":
            out.say(f"That's {format_number(intent.slots['value'])}.")
            return TurnSummary("command", tools=["calc"])
        if intent.name == "claude.connect":
            active = self._working.active_project
            if active is None:
                return await self._ask_for_project(out, "Which project should I connect Claude Code to?")
            return await self._run_tool(out, "project.open", {"name": active["name"], "open_editor": False}, "command")
        if intent.name == "coding.no_commit":
            stopped = False
            for session in self._sessions.running():
                if session.task and "commit" in session.task.lower():
                    await self._sessions.stop(session, reason="the user said not to commit")
                    stopped = True
            out.say("Stopped, nothing gets committed." if stopped else "Okay, I won't commit anything.")
            return TurnSummary("command", tools=["coding.no_commit"])
        if intent.name == "project.open" and intent.slots.get("unresolved"):
            return await self._ask_for_project(out, f"I couldn't find a project called {intent.slots.get('name')}.")
        call = _intent_call(intent, self._state)
        if call is None:
            out.say("I'm not sure how to do that yet.")
            return TurnSummary("command", ok=False)
        name, args = call
        if name == "keyboard.type":
            out.ack("Typing.")
        return await self._run_tool(out, name, args, "command")

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
            out.say(f"{question} You've got {spoken}.")
        else:
            out.say(question)
        if result.display:
            out.show(result.display)
        return TurnSummary("command", tools=["project.list"], ok=False)

    # ------------------------------------------------------------------ coding

    async def _coding(self, text: str, out: TurnOutput) -> TurnSummary:
        if self._working.active_project is None:
            recent = self._sessions.most_recent(("running", "paused", "completed", "failed", "waiting_permission"))
            if recent is None:
                return await self._ask_for_project(out, "Which project should Claude work on?")
            self._working.set_project(recent.project_name, recent.project_path)
        return await self._run_tool(out, "claude.task", {"task": text}, "coding")

    # ------------------------------------------------------------------ models

    async def _model(self, text: str, decision: RouteDecision, out: TurnOutput) -> TurnSummary:
        chain = decision.chain or self._settings.llm.chat_chain
        available = self._pool.available(chain)
        if decision.route == Route.REASONING and available and available[0].name == "claude":
            out.ack("Let me think about that.")
        messages = self._context.build(text)
        tools_used: list[str] = []

        def on_tool(record: ToolRecord) -> None:
            tools_used.append(record.name)
            if record.result.display:
                out.show(record.result.display)

        try:
            result = await self._agent.run(
                messages,
                chain=chain,
                tool_groups=decision.tool_groups,
                tool_names=decision.tool_names,
                purpose=decision.purpose,
                on_text=out.stream,
                on_tool_result=on_tool,
                trace=out.trace,
                max_steps=8 if decision.route == Route.AGENT else 4,
                model_for=self._model_overrides(decision),
            )
        except AllProvidersFailed as exc:
            log.warning("all providers failed: %s", exc)
            out.say(self._offline_message())
            self._bus.publish("error", source="llm", message=str(exc))
            return TurnSummary(decision.route.value, ok=False)
        if not result.text.strip() and tools_used:
            out.say("Done.")
        out.trace.info.update({"provider": result.provider, "model": result.model, "steps": result.steps})
        return TurnSummary(decision.route.value, result.provider, result.model, tools_used)

    def _model_overrides(self, decision: RouteDecision) -> dict[str, str]:
        llm = self._settings.llm
        if decision.route == Route.REASONING:
            return {"freellm": llm.freellm.smart_model, "ollama": llm.ollama.smart_model}
        return {}

    def _offline_message(self) -> str:
        status = self._pool.status()
        down = [name for name, info in status.items() if not info["available"] or info["last_error"]]
        if "ollama" in down and "freellm" in down:
            return "I can't reach my language models right now. FreeLLMAPI and Ollama are both down, but commands still work."
        return "I couldn't get an answer from my language models just now. Try again in a moment."

    async def answer_coding_permission(self, approved: bool, out: TurnOutput) -> TurnSummary:
        return await self._run_tool(out, "claude.approve", {"approved": approved}, "coding")
