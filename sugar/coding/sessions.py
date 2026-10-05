"""Claude Code coding sessions.

One :class:`CodingSession` per project. Each task is a ``claude -p`` run that
resumes the project's Claude conversation (``--resume``), so context carries
across tasks, pauses and Sugar restarts. Session metadata is persisted to
``data/coding_sessions.json``.

Lifecycle::

    idle ──task──▶ running ──ok──▶ completed
                     │  └──error──▶ failed
                     ├──pause───▶ paused ──resume──▶ running
                     ├──stop────▶ cancelled
                     └──denied──▶ waiting_permission ──approve──▶ running

Policy enforced here (not left to the model):
  * analysis requests run in ``plan`` mode (read-only);
  * ``git commit`` / ``git push`` are disallowed unless the request asks for them;
  * actions Claude Code's own permission system denied are surfaced to the
    user and re-run only with exactly the approved tools allowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sugar.coding.claude_code import (
    ClaudeCLIInfo,
    ClaudeCodeRunner,
    ClaudeEvent,
    ClaudeRunRequest,
    ClaudeRunResult,
    find_claude_executable,
    probe_claude_cli,
)
from sugar.coding.projects import Project
from sugar.config.settings import CodingSettings
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.intelligence.response import speakable_summary

log = logging.getLogger(__name__)

VOICE_INSTRUCTIONS = """You are being driven by Sugar, the user's voice assistant. The user is speaking, not typing, and will hear a short spoken summary of your work.
- Work autonomously. Only stop to ask a question if you genuinely cannot proceed.
- Verify changes (run the relevant tests or build) before saying they work. Never claim success you have not verified; say clearly what failed.
- Do not commit, push, or rewrite git history unless this request explicitly asks for it.
- End your final message with one line starting with "SPOKEN SUMMARY:" followed by one to three short plain sentences for text-to-speech: no markdown, no code, and no file paths unless essential."""

ANALYSIS_VERBS = r"\b(?:analy[sz]e|review|inspect|investigate|explain|look (?:at|into)|check|find out|tell me|what(?:'s| is)? (?:broken|wrong)|how does|why|audit|summari[sz]e|understand|status)\b"
ACTION_VERBS = r"\b(?:fix|implement|add|change|update|refactor|build|create|write|remove|delete|rename|commit|run|install|upgrade|migrate|make|improve|clean|optimi[sz]e|set up|setup|deploy|revert|undo|continue|resume|do it|go ahead|apply)\b"
TEST_COMMAND = re.compile(r"\b(?:pytest|npm (?:run )?test|yarn test|pnpm test|jest|vitest|go test|cargo test|mvn test|gradle test|unittest|tox)\b")
BUILD_COMMAND = re.compile(r"\b(?:npm run build|yarn build|pnpm build|vite build|tsc|cargo build|go build|mvn package|gradle build|make)\b")
SUMMARY_RE = re.compile(r"^\s*\**SPOKEN SUMMARY:?\**\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def classify_mode(task: str) -> str:
    lowered = task.lower()
    if re.search(ACTION_VERBS, lowered):
        return "work"
    if re.search(ANALYSIS_VERBS, lowered):
        return "analyze"
    return "work"


def wants_commit(task: str) -> bool:
    lowered = task.lower()
    return bool(re.search(r"\bcommit\b", lowered)) and not re.search(r"\b(?:don't|do not|dont|no|without)\b[^.]*\bcommit", lowered)


def extract_spoken_summary(text: str) -> tuple[str, str]:
    """Split Claude's final message into (display text, spoken summary)."""
    matches = list(SUMMARY_RE.finditer(text))
    match = matches[-1] if matches else None  # the last summary line wins
    if match:
        spoken = match.group(1).strip().strip("*").strip()
        display = (text[: match.start()] + text[match.end():]).strip()
        return display or spoken, spoken
    return text, speakable_summary(text) or "Claude Code finished."


def denial_patterns(denials: list[dict[str, Any]]) -> list[str]:
    """Turn Claude Code permission denials into --allowedTools patterns."""
    patterns = []
    for denial in denials:
        name = denial.get("tool_name") or denial.get("name") or ""
        tool_input = denial.get("tool_input") or {}
        if name in {"Bash", "PowerShell"} and tool_input.get("command"):
            command = " ".join(str(tool_input["command"]).split())
            patterns.append(f"{name}({command})")
        elif name:
            patterns.append(name)
    return list(dict.fromkeys(patterns))


def describe_denials(denials: list[dict[str, Any]]) -> str:
    parts = []
    for denial in denials[:3]:
        name = denial.get("tool_name") or "a tool"
        command = (denial.get("tool_input") or {}).get("command")
        path = (denial.get("tool_input") or {}).get("file_path")
        if command:
            parts.append(f"run {' '.join(str(command).split())[:80]}")
        elif path:
            parts.append(f"use {name} on {Path(str(path)).name}")
        else:
            parts.append(f"use {name}")
    return ", and ".join(parts)


@dataclass
class CodingSession:
    project_name: str
    project_path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    claude_session_id: str | None = None
    status: str = "idle"
    task: str | None = None
    mode: str = "work"
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    last_summary: str | None = None
    last_result: str | None = None
    last_error: str | None = None
    cost_usd: float = 0.0
    runs: int = 0
    activity: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    pending_denials: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.activity.append(line)
        del self.activity[:-40]
        self.last_activity = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Announcer = Callable[[str, str], Awaitable[None]]  # (text, kind) -> None


class CodingSessionManager:
    def __init__(self, settings: CodingSettings, bus: EventBus, store_path: Path) -> None:
        self._settings = settings
        self._bus = bus
        self._store_path = store_path
        self._sessions: dict[str, CodingSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._runners: dict[str, ClaudeCodeRunner] = {}
        self._info: ClaudeCLIInfo | None = None
        self.on_announce: Announcer | None = None
        self.on_finished: Callable[[CodingSession, ClaudeRunResult], None] | None = None
        self._last_spoken_progress: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("sessions", []):
            try:
                session = CodingSession(**item)
            except TypeError:
                continue
            if session.status == "running":
                session.status = "paused"  # Sugar exited mid-run; the conversation can be resumed
                session.note("Interrupted when Sugar closed; resumable.")
            self._sessions[session.project_path.lower()] = session

    def save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"sessions": [s.to_dict() for s in self._sessions.values()]}
            self._store_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError:
            log.warning("could not save coding sessions", exc_info=True)

    # ------------------------------------------------------------------ queries

    async def cli(self) -> ClaudeCLIInfo | None:
        if self._info is None:
            executable = find_claude_executable(self._settings.claude_executable)
            if executable is None:
                return None
            try:
                self._info = await probe_claude_cli(executable)
            except (TimeoutError, OSError):
                log.exception("Claude CLI probe failed")
                return None
        return self._info

    def session_for(self, project: Project) -> CodingSession:
        key = project.path.lower()
        session = self._sessions.get(key)
        if session is None:
            session = CodingSession(project.name, project.path)
            self._sessions[key] = session
            self.save()
        return session

    def get(self, project_path: str) -> CodingSession | None:
        return self._sessions.get(project_path.lower())

    def all(self) -> list[CodingSession]:
        return sorted(self._sessions.values(), key=lambda s: -s.last_activity)

    def running(self) -> list[CodingSession]:
        return [s for s in self._sessions.values() if s.status == "running"]

    def most_recent(self, statuses: tuple[str, ...] = ("paused", "waiting_permission", "failed", "completed")) -> CodingSession | None:
        candidates = [s for s in self._sessions.values() if s.status in statuses]
        return max(candidates, key=lambda s: s.last_activity) if candidates else None

    def status_text(self, session: CodingSession | None) -> str:
        if session is None:
            return "There's no coding session yet."
        name = session.project_name
        if session.status == "running":
            doing = session.activity[-1] if session.activity else "getting started"
            minutes = int((time.time() - session.started_at) / 60)
            elapsed = f" for {minutes} minute{'s' if minutes != 1 else ''}" if minutes else ""
            return f"Claude is working on {name}{elapsed}. Right now: {doing.rstrip('.')}."
        if session.status == "paused":
            return f"The {name} session is paused. Say continue to resume it."
        if session.status == "waiting_permission":
            return f"Claude is waiting for your permission to {describe_denials(session.pending_denials)}."
        if session.status == "failed":
            return f"The last {name} task failed: {session.last_error or 'unknown error'}."
        if session.status == "completed" and session.last_summary:
            return f"Claude finished the last {name} task. {session.last_summary}"
        return f"Claude Code is connected to {name} and idle."

    def describe_for_context(self) -> str:
        lines = []
        for session in self.all()[:3]:
            line = f"Claude Code session for {session.project_name}: {session.status}"
            if session.task:
                line += f"; last task: {session.task[:120]}"
            if session.status == "running" and session.activity:
                line += f"; now: {session.activity[-1]}"
            elif session.last_summary:
                line += f"; result: {session.last_summary[:200]}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------ control

    async def start_task(self, project: Project, task: str, *, mode: str | None = None,
                         resume_note: str | None = None) -> CodingSession:
        session = self.session_for(project)
        if session.status == "running":
            await self.stop(session, reason="replaced by a new task")
        info = await self.cli()
        if info is None:
            raise RuntimeError("Claude Code isn't installed or couldn't be started")
        mode = mode or classify_mode(task)
        allow_commit = wants_commit(task)
        disallowed = list(self._settings.disallowed_tools)
        if not allow_commit:
            disallowed += ["Bash(git commit *)", "Bash(git push *)"]
        elif not re.search(r"\bpush\b", task.lower()):
            disallowed.append("Bash(git push *)")
        allowed = list(session.allowed_tools)
        if allow_commit:
            allowed += ["Bash(git add *)", "Bash(git commit *)"]
        prompt = task if resume_note is None else f"{resume_note}\n\n{task}".strip()
        request = ClaudeRunRequest(
            prompt=prompt,
            cwd=Path(project.path),
            session_id=session.claude_session_id,
            model=self._settings.model,
            permission_mode=(self._settings.analysis_permission_mode if mode == "analyze"
                             else self._settings.permission_mode),
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            append_system_prompt=VOICE_INSTRUCTIONS,
            effort=self._settings.effort,
            max_budget_usd=self._settings.max_budget_usd,
            name=f"Sugar: {project.name}",
        )
        session.task = task if resume_note is None else (session.task or task)
        session.mode = mode
        session.status = "running"
        session.started_at = time.time()
        session.pending_denials = []
        session.last_error = None
        session.runs += 1
        session.note(f"Started: {task[:100]}")
        self.save()
        runner = ClaudeCodeRunner(info)
        self._runners[session.id] = runner
        self._tasks[session.id] = asyncio.create_task(self._execute(session, runner, request))
        log_event("CLAUDE_SESSION_START", project=project.name, mode=mode, resume=bool(session.claude_session_id))
        self._publish(session)
        return session

    async def pause(self, session: CodingSession) -> bool:
        if session.status != "running":
            return False
        session.status = "paused"
        await self._cancel_run(session)
        session.note("Paused by the user.")
        self.save()
        self._publish(session)
        return True

    async def resume(self, session: CodingSession, instruction: str | None = None) -> CodingSession:
        project = Project(session.project_name, session.project_path)
        note = "The previous run was paused or interrupted. Continue the task where you left off."
        return await self.start_task(project, instruction or "Continue.", mode="work", resume_note=note)

    async def stop(self, session: CodingSession, reason: str = "stopped by the user") -> bool:
        if session.status not in ("running", "waiting_permission", "paused"):
            return False
        was_running = session.status == "running"
        session.status = "cancelled"
        if was_running:
            await self._cancel_run(session)
        session.pending_denials = []
        session.note(f"Stopped: {reason}.")
        self.save()
        self._publish(session)
        return True

    async def approve_pending(self, session: CodingSession) -> CodingSession | None:
        if session.status != "waiting_permission" or not session.pending_denials:
            return None
        approved = denial_patterns(session.pending_denials)
        session.allowed_tools = list(dict.fromkeys([*session.allowed_tools, *approved]))
        note = "The user approved these actions: " + "; ".join(approved) + ". Continue the task."
        project = Project(session.project_name, session.project_path)
        return await self.start_task(project, "Continue.", mode="work", resume_note=note)

    async def deny_pending(self, session: CodingSession) -> None:
        if session.status == "waiting_permission":
            session.pending_denials = []
            session.status = "completed"
            session.note("The user declined the requested action.")
            self.save()
            self._publish(session)

    async def shutdown(self) -> None:
        for session in self.running():
            await self.pause(session)

    async def _cancel_run(self, session: CodingSession) -> None:
        runner = self._runners.get(session.id)
        if runner is not None:
            await runner.cancel()
        task = self._tasks.get(session.id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()

    # ------------------------------------------------------------------ execution

    async def _execute(self, session: CodingSession, runner: ClaudeCodeRunner, request: ClaudeRunRequest) -> None:
        started = time.perf_counter()
        try:
            result = await runner.run(request, lambda event: self._on_event(session, event))
        except Exception as exc:  # the runner itself failed (not the task)
            log.exception("Claude Code run crashed")
            result = ClaudeRunResult(ok=False, error=str(exc))
        finally:
            self._runners.pop(session.id, None)
        if result.session_id:
            session.claude_session_id = result.session_id
        if result.cost_usd:
            session.cost_usd += float(result.cost_usd)
        log_event("CLAUDE_SESSION_END", project=session.project_name, ok=result.ok, cancelled=result.cancelled,
                  ms=int((time.perf_counter() - started) * 1000), cost=result.cost_usd)

        if session.status in ("paused", "cancelled"):
            self.save()
            self._publish(session)
            return  # the user asked for this; nothing to announce

        display, spoken = extract_spoken_summary(result.text or "")
        session.last_result = display
        if result.permission_denials and not result.cancelled:
            session.status = "waiting_permission"
            session.pending_denials = result.permission_denials
            what = describe_denials(result.permission_denials)
            session.last_summary = spoken
            session.note(f"Needs permission to {what}.")
            announcement = f"{spoken} Claude needs your permission to {what}. Should I allow it?"
        elif result.ok:
            session.status = "completed"
            session.last_summary = spoken
            session.note("Finished.")
            announcement = spoken
        else:
            session.status = "failed"
            session.last_error = (result.error or "unknown error")[:500]
            session.note(f"Failed: {session.last_error[:120]}")
            detail = spoken if result.text else session.last_error
            announcement = f"The {session.project_name} task failed. {detail}"
        self.save()
        self._publish(session, result_text=display)
        if self.on_finished is not None:
            self.on_finished(session, result)
        if self.on_announce is not None:
            await self.on_announce(announcement, "coding.finished")

    async def _on_event(self, session: CodingSession, event: ClaudeEvent) -> None:
        if event.kind == "init":
            session.claude_session_id = event.data.get("session_id") or session.claude_session_id
            self.save()
            return
        if event.kind == "tool_use":
            session.note(event.summary)
            payload = event.data.get("input") or {}
            path = payload.get("file_path") or payload.get("notebook_path")
            if event.data.get("name") in {"Edit", "MultiEdit", "Write", "NotebookEdit"} and path:
                session.files_touched = list(dict.fromkeys([str(path), *session.files_touched]))[:30]
            self._bus.publish("coding.progress", project=session.project_name, kind="tool",
                              text=event.summary, tool=event.data.get("name"))
            await self._maybe_speak_progress(session, event)
        elif event.kind == "text":
            text = event.data.get("text", "")
            self._bus.publish("coding.progress", project=session.project_name, kind="text", text=text[:2000])
        elif event.kind == "tool_result" and event.data.get("is_error"):
            self._bus.publish("coding.progress", project=session.project_name, kind="tool_error",
                              text="A step failed; Claude is handling it.")

    async def _maybe_speak_progress(self, session: CodingSession, event: ClaudeEvent) -> None:
        if self.on_announce is None:
            return
        command = str((event.data.get("input") or {}).get("command", ""))
        message = None
        if TEST_COMMAND.search(command):
            message = "Claude is running the tests."
        elif BUILD_COMMAND.search(command):
            message = "Claude is building the project."
        if message is None:
            return
        now = time.time()
        if now - self._last_spoken_progress.get(session.id, 0.0) < self._settings.progress_interval_s:
            return
        self._last_spoken_progress[session.id] = now
        await self.on_announce(message, "coding.progress")

    def _publish(self, session: CodingSession, result_text: str | None = None) -> None:
        payload = session.to_dict()
        payload["activity"] = session.activity[-8:]
        if result_text is not None:
            payload["result_text"] = result_text[:20000]
        self._bus.publish("coding.session", **payload)
