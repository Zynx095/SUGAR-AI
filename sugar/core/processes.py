"""Child-process helpers shared by tools and the Claude Code integration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

# Variables Claude Code sets for its own child processes. If Sugar itself was
# launched from a Claude Code terminal they must not leak into the sessions
# Sugar starts, or the CLI treats them as nested sessions.
_CLAUDE_SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID")
_CLAUDE_SESSION_PREFIX = "CLAUDE_CODE_"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def clean_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _CLAUDE_SESSION_VARS and not key.startswith(_CLAUDE_SESSION_PREFIX)
    }
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def background_creationflags() -> int:
    """Flags that keep console children invisible and killable as a group on Windows."""
    if sys.platform == "win32":
        return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    return 0


async def kill_process_tree(process: asyncio.subprocess.Process, grace_s: float = 2.0) -> None:
    """Terminate a child and everything it spawned.

    On Windows ``Process.terminate`` only kills the direct child, which would
    leave e.g. a test runner started by Claude Code running, so ``taskkill /T``
    is used for the tree.
    """
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
    except TimeoutError:
        process.kill()
        await process.wait()
