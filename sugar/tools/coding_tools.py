"""Projects, Claude Code sessions and git inspection as tools."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

from sugar.coding.projects import Project, resolve_spoken_path
from sugar.core.processes import CREATE_NO_WINDOW
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices


def _git(path: str, *args: str, timeout: int = 15) -> tuple[int, str]:
    try:
        completed = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout,
                                   encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr if completed.returncode else "")


def register(registry: ToolRegistry, services: ToolServices) -> None:
    projects = services.projects
    sessions = services.sessions
    working = services.working

    def find_project(name: str | None) -> Project | None:
        if name:
            project = projects.resolve(name)
            if project is None:
                path = resolve_spoken_path(name)
                if path is not None:
                    project = projects.add(path)
            return project
        active = working.active_project
        if active:
            return projects.resolve(active["name"]) or Project(active["name"], active["path"])
        return None

    async def list_projects(args: dict[str, Any]) -> ToolResult:
        if not projects.all():
            await asyncio.to_thread(projects.discover)
        items = projects.all()
        names = [p.name for p in items]
        working.offer_options(names[:6])
        listing = "\n".join(f"{i + 1}. **{p.name}** — `{p.path}`" for i, p in enumerate(items))
        spoken_names = ", ".join(names[:5])
        more = f", and {len(names) - 5} more" if len(names) > 5 else ""
        return ToolResult(True, f"You've got {len(names)} projects: {spoken_names}{more}.",
                          data={"projects": [p.to_dict() for p in items]}, display=listing)

    async def open_project(args: dict[str, Any]) -> ToolResult:
        project = find_project(args.get("name") or args.get("path"))
        if project is None:
            names = projects.names()[:5]
            working.offer_options(names)
            return ToolResult.failure(f"I couldn't find a project called {args.get('name') or args.get('path')}.",
                                      data={"known": names})
        working.set_project(project.name, project.path)
        projects.mark_opened(project)
        services.bus.publish("project.active", name=project.name, path=project.path, stack=project.stack)
        notes = []
        if args.get("open_editor", True):
            entry = services.apps.resolve("vs code")
            executable = services.apps.executable_for(entry) if entry else None
            if executable:
                await asyncio.to_thread(subprocess.Popen, [executable, project.path], close_fds=True,
                                        creationflags=CREATE_NO_WINDOW)
            else:
                notes.append("VS Code isn't available right now, so I didn't open an editor.")
        cli = await sessions.cli()
        session = sessions.session_for(project)
        connected = "Claude Code is connected." if cli else "Claude Code isn't installed, so coding tasks won't work."
        working.record_action("coding", f"opened project {project.name}", tool="project.open",
                              args={"path": project.path})
        summary = " ".join([f"{project.name} is open.", connected, *notes])
        return ToolResult(True, summary, data={"project": project.to_dict(), "session": session.status,
                                               "claude_code": cli.version if cli else None})

    async def delegate(args: dict[str, Any]) -> ToolResult:
        project = find_project(args.get("project"))
        if project is None:
            working.offer_options(projects.names()[:5])
            return ToolResult.failure("Which project should Claude work on?")
        if working.active_project is None or working.active_project["path"] != project.path:
            working.set_project(project.name, project.path)
        task = args["task"].strip()
        try:
            session = await sessions.start_task(project, task, mode=args.get("mode"))
        except RuntimeError as exc:
            return ToolResult.failure(str(exc))
        working.current_task = task
        working.record_action("coding", f"asked Claude Code to: {task[:120]}", tool="claude.task",
                              args={"project": project.name})
        verb = "looking into it" if session.mode == "analyze" else "on it"
        return ToolResult(True, f"Claude's {verb} in {project.name}.",
                          data={"project": project.name, "mode": session.mode, "status": session.status})

    async def status(args: dict[str, Any]) -> ToolResult:
        project = find_project(args.get("project"))
        session = sessions.get(project.path) if project else (sessions.running() or [sessions.most_recent()])[0]
        text = sessions.status_text(session)
        return ToolResult(True, text, data=session.to_dict() if session else None)

    async def pause(args: dict[str, Any]) -> ToolResult:
        running = sessions.running()
        if not running:
            return ToolResult(True, "Nothing's running right now.")
        for session in running:
            await sessions.pause(session)
        return ToolResult(True, "Paused. Say continue when you want Claude to pick it back up.")

    async def resume(args: dict[str, Any]) -> ToolResult:
        project = find_project(args.get("project"))
        session = sessions.get(project.path) if project else sessions.most_recent(("paused", "failed", "completed"))
        if session is None:
            return ToolResult.failure("There's no coding session to resume.")
        if session.status == "running":
            return ToolResult(True, "Claude's already working on it.")
        await sessions.resume(session, args.get("instruction"))
        working.set_project(session.project_name, session.project_path)
        return ToolResult(True, f"Resuming {session.project_name}.", data={"project": session.project_name})

    async def stop(args: dict[str, Any]) -> ToolResult:
        stopped = [s for s in sessions.all() if s.status in ("running", "waiting_permission")]
        for session in stopped:
            await sessions.stop(session)
        return ToolResult(True, "Stopped." if stopped else "Nothing's running.", data={"stopped": len(stopped)})

    async def approve(args: dict[str, Any]) -> ToolResult:
        waiting = [s for s in sessions.all() if s.status == "waiting_permission"]
        if not waiting:
            return ToolResult(True, "Claude isn't waiting on anything.")
        if args.get("approved", True):
            await sessions.approve_pending(waiting[0])
            return ToolResult(True, "Allowed. Claude's continuing.")
        await sessions.deny_pending(waiting[0])
        return ToolResult(True, "Okay, I told Claude not to.")

    async def diff(args: dict[str, Any]) -> ToolResult:
        project = find_project(args.get("project"))
        if project is None:
            return ToolResult.failure("Which project? There isn't an active one.")
        if not (Path(project.path) / ".git").exists():
            return ToolResult.failure(f"{project.name} isn't a git repository.")
        _, porcelain = await asyncio.to_thread(_git, project.path, "status", "--porcelain")
        changed = [line[3:].strip() for line in porcelain.splitlines() if line.strip()]
        if not changed:
            return ToolResult(True, f"Nothing has changed in {project.name} since the last commit.", data={"files": []})
        _, stat = await asyncio.to_thread(_git, project.path, "diff", "HEAD", "--shortstat")
        _, patch = await asyncio.to_thread(_git, project.path, "diff", "HEAD")
        added = re.search(r"(\d+) insertion", stat)
        removed = re.search(r"(\d+) deletion", stat)
        names = [Path(f.split(" -> ")[-1]).name for f in changed]
        spoken_files = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        summary = f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed: {spoken_files}."
        if added or removed:
            summary += f" {added.group(1) if added else 0} lines added, {removed.group(1) if removed else 0} removed."
        working.note_files(changed)
        return ToolResult(True, summary, data={"files": changed, "stat": stat.strip()},
                          display="```diff\n" + patch[:15000] + ("\n…" if len(patch) > 15000 else "") + "\n```")

    coding = frozenset({"agent", "chat", "coding"})
    registry.register(Tool("project.list", "List the user's code projects.", params(), list_projects,
                           PermissionLevel.READ, 30, coding))
    registry.register(Tool("project.open", "Make a project active (and open it in the editor). Accepts a name or a spoken path.",
                           params(name={"type": "string"}, path={"type": "string"},
                                  open_editor={"type": "boolean", "default": True}),
                           open_project, PermissionLevel.NON_DESTRUCTIVE, 30, coding,
                           describe=lambda a: f"open the project {a.get('name') or a.get('path')}"))
    registry.register(Tool("claude.task", "Delegate software work on a project to Claude Code (runs in the background): "
                           "analysis, debugging, fixes, features, tests, refactors, commits.",
                           params(["task"], task={"type": "string", "description": "the full request in the user's words"},
                                  project={"type": "string"},
                                  mode={"type": "string", "enum": ["analyze", "work"]}),
                           delegate, PermissionLevel.NON_DESTRUCTIVE, 60, coding,
                           describe=lambda a: f"have Claude Code {a.get('task')}"))
    registry.register(Tool("claude.status", "What Claude Code is doing (or did last) on a project.",
                           params(project={"type": "string"}), status, PermissionLevel.READ, 10, coding))
    registry.register(Tool("claude.pause", "Pause the running Claude Code task (resumable).", params(), pause,
                           PermissionLevel.NON_DESTRUCTIVE, 15, coding))
    registry.register(Tool("claude.resume", "Resume a paused or interrupted Claude Code session.",
                           params(project={"type": "string"}, instruction={"type": "string"}), resume,
                           PermissionLevel.NON_DESTRUCTIVE, 30, coding))
    registry.register(Tool("claude.stop", "Stop the running Claude Code task.", params(), stop,
                           PermissionLevel.NON_DESTRUCTIVE, 15, coding))
    registry.register(Tool("claude.approve", "Answer Claude Code's pending permission request.",
                           params(approved={"type": "boolean", "default": True}), approve,
                           PermissionLevel.NON_DESTRUCTIVE, 30, coding))
    registry.register(Tool("git.diff", "Summarise uncommitted changes in a project and show the diff.",
                           params(project={"type": "string"}), diff, PermissionLevel.READ, 30, coding))
