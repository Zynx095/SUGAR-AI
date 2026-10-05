"""Claude Code CLI integration.

This drives the real ``claude`` executable — never an API call dressed up as
Claude Code. Verified against Claude Code 2.1.x:

* ``claude -p`` reads the prompt from stdin; the prompt is always piped,
  which avoids Windows command-line quoting and length limits and the CLI's
  3 s wait for stdin.
* ``--output-format stream-json --verbose`` emits one JSON object per line:
  ``system/init`` (session_id, model, tools, permissionMode), ``assistant``
  (thinking / text / tool_use blocks), ``user`` (tool results),
  ``rate_limit_event`` and a final ``result`` (``is_error``, ``result``,
  ``session_id``, ``total_cost_usd``, ``num_turns``, ``permission_denials``).
* ``--resume <session_id>`` continues a conversation; that is how Sugar
  pauses and resumes coding sessions across runs and restarts.

Supported flags and permission modes are probed from ``claude --help`` so
nothing is passed that the installed version does not understand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sugar.core.processes import background_creationflags, clean_child_env, kill_process_tree

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024  # tool results can be large single lines


def find_claude_executable(configured: str | None = None) -> str | None:
    candidates: list[str | None] = [
        configured,
        shutil.which("claude"),
        shutil.which("claude-code"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@dataclass
class ClaudeCLIInfo:
    executable: str
    version: str
    flags: frozenset[str]
    permission_modes: frozenset[str]

    def supports(self, flag: str) -> bool:
        return flag in self.flags

    def resolve_permission_mode(self, wanted: str | None) -> str | None:
        if not wanted:
            return None
        if wanted in self.permission_modes:
            return wanted
        # "auto" (classifier-approved actions) is newer than "acceptEdits".
        for fallback in ("acceptEdits", "default"):
            if fallback in self.permission_modes:
                log.warning("Claude CLI lacks permission mode %r; using %r", wanted, fallback)
                return fallback
        return None


_info_cache: dict[str, ClaudeCLIInfo] = {}


async def probe_claude_cli(executable: str, timeout_s: float = 30.0) -> ClaudeCLIInfo:
    if executable in _info_cache:
        return _info_cache[executable]

    async def run(*args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            executable, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_child_env(),
            creationflags=background_creationflags(),
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        return out.decode("utf-8", "replace")

    version_text = await run("--version")
    help_text = await run("--help")
    version_match = re.search(r"\d+\.\d+\.\d+", version_text)
    flags = frozenset(re.findall(r"(--[a-z][a-z0-9-]+)", help_text))
    modes: set[str] = set()
    mode_block = re.search(r"--permission-mode <mode>(.*?)(?:\n\s*--)", help_text, re.S)
    if mode_block:
        modes.update(re.findall(r'"([A-Za-z]+)"', mode_block.group(1)))
    info = ClaudeCLIInfo(
        executable=executable,
        version=version_match.group(0) if version_match else "unknown",
        flags=flags,
        permission_modes=frozenset(modes),
    )
    _info_cache[executable] = info
    return info


# --------------------------------------------------------------------------- events


@dataclass
class ClaudeEvent:
    kind: str  # init | text | thinking | tool_use | tool_result | delta | result | rate_limit | other
    data: dict[str, Any]
    summary: str = ""


def _short_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/")
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) > 2 else text


def describe_tool_use(name: str, payload: dict[str, Any]) -> str:
    """One short, speakable line describing what Claude Code is doing."""
    path = payload.get("file_path") or payload.get("path") or payload.get("notebook_path")
    if name == "Read":
        return f"Reading {_short_path(path)}"
    if name in {"Edit", "MultiEdit", "NotebookEdit"}:
        return f"Editing {_short_path(path)}"
    if name == "Write":
        return f"Writing {_short_path(path)}"
    if name in {"Bash", "PowerShell"}:
        command = " ".join(str(payload.get("command", "")).split())
        description = payload.get("description")
        return f"Running {description or command[:70]}"
    if name == "Grep":
        return f"Searching for '{str(payload.get('pattern', ''))[:40]}'"
    if name == "Glob":
        return f"Finding files {str(payload.get('pattern', ''))[:40]}"
    if name in {"WebFetch", "WebSearch"}:
        return f"Looking up {str(payload.get('query') or payload.get('url') or '')[:60]}"
    if name in {"TodoWrite", "TaskCreate", "TaskUpdate"}:
        return "Updating its plan"
    if name in {"Task", "Agent"}:
        return f"Delegating: {str(payload.get('description', ''))[:60]}"
    return f"Using {name}"


def parse_stream_line(line: str) -> list[ClaudeEvent]:
    line = line.strip()
    if not line:
        return []
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return [ClaudeEvent("other", {"raw": line[:500]})]
    kind = data.get("type")
    if kind == "system" and data.get("subtype") == "init":
        return [ClaudeEvent("init", data, f"Claude Code {data.get('claude_code_version', '')} started")]
    if kind == "assistant":
        events = []
        for block in (data.get("message") or {}).get("content") or []:
            block_type = block.get("type")
            if block_type == "text" and block.get("text", "").strip():
                events.append(ClaudeEvent("text", {"text": block["text"]}))
            elif block_type == "tool_use":
                name = block.get("name", "tool")
                payload = block.get("input") or {}
                events.append(
                    ClaudeEvent("tool_use", {"id": block.get("id"), "name": name, "input": payload},
                                describe_tool_use(name, payload))
                )
            elif block_type == "thinking":
                events.append(ClaudeEvent("thinking", {}))
        return events
    if kind == "user":
        events = []
        content = (data.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(
                        ClaudeEvent("tool_result", {"id": block.get("tool_use_id"),
                                                    "is_error": bool(block.get("is_error"))})
                    )
        return events
    if kind == "stream_event":
        event = data.get("event") or {}
        delta = event.get("delta") or {}
        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return [ClaudeEvent("delta", {"text": delta.get("text", "")})]
        return []
    if kind == "result":
        return [ClaudeEvent("result", data)]
    if kind == "rate_limit_event":
        return [ClaudeEvent("rate_limit", data)]
    return [ClaudeEvent("other", {"type": kind, "subtype": data.get("subtype")})]


# --------------------------------------------------------------------------- runner


@dataclass
class ClaudeRunRequest:
    prompt: str
    cwd: Path
    session_id: str | None = None  # resume this conversation
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    append_system_prompt: str | None = None
    system_prompt: str | None = None
    effort: str | None = None
    max_budget_usd: float | None = None
    tools: str | None = None  # "" disables built-in tools (plain LLM use)
    partial_messages: bool = False
    persist_session: bool = True
    isolated: bool = False  # skip user settings, hooks and MCP servers (plain LLM use)
    name: str | None = None


@dataclass
class ClaudeRunResult:
    ok: bool
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    cancelled: bool = False
    stderr_tail: str = ""
    model: str | None = None


class ClaudeCodeRunner:
    """Runs one ``claude -p`` invocation and streams its events."""

    def __init__(self, info: ClaudeCLIInfo) -> None:
        self.info = info
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_requested = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def build_args(self, request: ClaudeRunRequest) -> list[str]:
        info = self.info
        args = [info.executable, "-p", "--output-format", "stream-json", "--verbose"]
        if request.session_id:
            args += ["--resume", request.session_id]
        if request.model:
            args += ["--model", request.model]
        mode = info.resolve_permission_mode(request.permission_mode)
        if mode:
            args += ["--permission-mode", mode]
        if request.effort and info.supports("--effort"):
            args += ["--effort", request.effort]
        if request.max_budget_usd and info.supports("--max-budget-usd"):
            args += ["--max-budget-usd", f"{request.max_budget_usd:.2f}"]
        if request.system_prompt is not None:
            args += ["--system-prompt", request.system_prompt]
        if request.append_system_prompt and info.supports("--append-system-prompt"):
            args += ["--append-system-prompt", request.append_system_prompt]
        if request.tools is not None and info.supports("--tools"):
            args += ["--tools", request.tools]
        if request.partial_messages and info.supports("--include-partial-messages"):
            args.append("--include-partial-messages")
        if not request.persist_session and info.supports("--no-session-persistence"):
            args.append("--no-session-persistence")
        if request.isolated:
            if info.supports("--strict-mcp-config"):
                args.append("--strict-mcp-config")
            if info.supports("--setting-sources"):
                args += ["--setting-sources", "project"]
        if request.name and info.supports("--name"):
            args += ["--name", request.name]
        # Variadic options go last so nothing after them is swallowed.
        if request.allowed_tools:
            args += ["--allowedTools", *request.allowed_tools]
        if request.disallowed_tools:
            args += ["--disallowedTools", *request.disallowed_tools]
        return args

    async def run(
        self,
        request: ClaudeRunRequest,
        on_event: Callable[[ClaudeEvent], Any] | None = None,
    ) -> ClaudeRunResult:
        if self.running:
            raise RuntimeError("this runner is already running a task")
        args = self.build_args(request)
        started = time.perf_counter()
        log.info("starting Claude Code in %s (resume=%s, mode=%s)", request.cwd,
                 bool(request.session_id), request.permission_mode)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(request.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_child_env(),
                creationflags=background_creationflags(),
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            return ClaudeRunResult(ok=False, error=f"could not start Claude Code: {exc}")
        self._process = process
        self._cancel_requested = False

        stderr_chunks: list[str] = []

        async def drain_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk.decode("utf-8", "replace"))
                if len(stderr_chunks) > 50:
                    del stderr_chunks[:25]

        stderr_task = asyncio.create_task(drain_stderr())
        result: ClaudeRunResult | None = None
        session_id = request.session_id
        model: str | None = None
        texts: list[str] = []
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(request.prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                for event in parse_stream_line(raw.decode("utf-8", "replace")):
                    if event.kind == "init":
                        session_id = event.data.get("session_id") or session_id
                        model = event.data.get("model")
                    elif event.kind == "text":
                        texts.append(event.data["text"])
                    elif event.kind == "result":
                        data = event.data
                        session_id = data.get("session_id") or session_id
                        is_error = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
                        result = ClaudeRunResult(
                            ok=not is_error,
                            text=str(data.get("result") or ("\n".join(texts) if texts else "")),
                            session_id=session_id,
                            cost_usd=data.get("total_cost_usd"),
                            num_turns=data.get("num_turns"),
                            duration_ms=data.get("duration_ms"),
                            permission_denials=list(data.get("permission_denials") or []),
                            error=(str(data.get("result") or data.get("subtype")) if is_error else None),
                            model=model,
                        )
                    if on_event is not None:
                        outcome = on_event(event)
                        if asyncio.iscoroutine(outcome):
                            await outcome
            await process.wait()
        except asyncio.CancelledError:
            await kill_process_tree(process)
            stderr_task.cancel()
            raise
        finally:
            self._process = None
        await asyncio.gather(stderr_task, return_exceptions=True)
        stderr_tail = "".join(stderr_chunks)[-2000:]

        if result is None:
            if self._cancel_requested:
                result = ClaudeRunResult(ok=False, text="\n".join(texts), session_id=session_id,
                                         error="cancelled", cancelled=True, model=model)
            else:
                lines = stderr_tail.strip().splitlines()
                message = lines[-1] if lines else f"exit code {process.returncode}"
                result = ClaudeRunResult(ok=False, text="\n".join(texts), session_id=session_id,
                                         error=f"Claude Code ended without a result: {message}", model=model)
        result.stderr_tail = stderr_tail
        if result.duration_ms is None:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    async def cancel(self) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return False
        self._cancel_requested = True
        await kill_process_tree(process)
        return True
