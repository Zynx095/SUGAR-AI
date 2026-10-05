"""Running shell commands — always SENSITIVE.

Commands proposed by a model are confirmed out loud before they run (the
confirmation repeats the exact command). They run in PowerShell with a
timeout, output is captured and truncated, and the process tree is killed
if the turn is cancelled.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from sugar.core.processes import background_creationflags, clean_child_env, kill_process_tree
from sugar.tools.filesystem import PathPolicy, PathPolicyError
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

OUTPUT_LIMIT = 8000


def register(registry: ToolRegistry, services: ToolServices) -> None:
    async def run(args: dict[str, Any]) -> ToolResult:
        command = args["command"].strip()
        timeout = min(int(args.get("timeout", 120)), 600)
        project = services.working.active_project
        default_cwd = project["path"] if project else str(Path.home())
        try:
            cwd = PathPolicy(services.settings.permissions.allowed_roots).resolve(args.get("cwd") or default_cwd)
        except PathPolicyError as exc:
            return ToolResult.failure(f"I can't run commands there: {exc}.")
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", command,
            cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, env=clean_child_env(), creationflags=background_creationflags(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            await kill_process_tree(process)
            return ToolResult.failure(f"The command timed out after {timeout} seconds.", "timeout")
        except asyncio.CancelledError:
            await kill_process_tree(process)
            raise
        text = output.decode("utf-8", errors="replace")
        if len(text) > OUTPUT_LIMIT:
            text = text[: OUTPUT_LIMIT // 2] + "\n…[output truncated]…\n" + text[-OUTPUT_LIMIT // 2:]
        ok = process.returncode == 0
        elapsed = round(time.perf_counter() - started, 1)
        services.working.record_action("terminal", f"ran `{command[:80]}`", tool="terminal.run",
                                       args={"command": command}, ok=ok,
                                       error=None if ok else text.strip().splitlines()[-1:][0] if text.strip() else None)
        summary = "The command finished." if ok else f"The command failed with exit code {process.returncode}."
        return ToolResult(ok, summary, data={"exit_code": process.returncode, "output": text, "seconds": elapsed,
                                             "cwd": str(cwd)},
                          display=f"```\n$ {command}\n{text.strip()[:3000]}\n```")

    registry.register(Tool(
        "terminal.run",
        "Run a PowerShell command (default folder: the active project) and return its output.",
        params(["command"], command={"type": "string"}, cwd={"type": "string"},
               timeout={"type": "integer", "default": 120, "minimum": 1, "maximum": 600}),
        run, PermissionLevel.SENSITIVE, 620, frozenset({"agent"}),
        describe=lambda a: f"run `{a.get('command')}`" + (f" in {os.path.basename(a['cwd'])}" if a.get("cwd") else ""),
    ))
