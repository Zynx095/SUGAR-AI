"""Remember / recall / forget — explicit, inspectable long-term memory."""

from __future__ import annotations

import re
from typing import Any

from sugar.memory.store import project_scope
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

_PREFERENCE = re.compile(r"\b(?:i (?:prefer|like|love|hate|don't like|dislike|always|never|usually)|my favou?rite|call me)\b",
                         re.IGNORECASE)


def _first_person_to_second(text: str) -> str:
    """'I prefer Python' → 'User prefers Python' for a neutral stored fact."""
    text = text.strip().rstrip(".")
    swaps = [(r"^i am\b", "User is"), (r"^i'm\b", "User is"), (r"^i\b", "User"), (r"\bmy\b", "the user's"),
             (r"\bme\b", "the user")]
    for pattern, replacement in swaps:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"^User (prefer|like|love|hate|dislike|want|need|use)\b", lambda m: f"User {m.group(1)}s", text)
    return text[0].upper() + text[1:] if text else text


def register(registry: ToolRegistry, services: ToolServices) -> None:
    memory = services.memory

    async def remember(args: dict[str, Any]) -> ToolResult:
        fact = args["fact"].strip()
        scope_name = args.get("scope", "user")
        scope = "user"
        if scope_name == "project" and services.working.active_project:
            scope = project_scope(services.working.active_project["path"])
        kind = "preference" if _PREFERENCE.search(fact) else "fact"
        stored = memory.remember(_first_person_to_second(fact), scope=scope, kind=kind)
        services.bus.publish("memory.changed", action="add", id=stored.id)
        return ToolResult(True, "Got it, I'll remember that.", data={"id": stored.id, "content": stored.content})

    async def recall(args: dict[str, Any]) -> ToolResult:
        query = (args.get("query") or "").strip()
        scopes = ["user"]
        if services.working.active_project:
            scopes.append(project_scope(services.working.active_project["path"]))
        items = memory.search(query, scopes, limit=8) if query else memory.list("user", limit=12)
        if not items:
            return ToolResult(True, "I don't have anything saved about that.", data={"memories": []})
        listing = "\n".join(f"- {m.content}" for m in items)
        return ToolResult(True, f"I remember {len(items)} thing{'s' if len(items) != 1 else ''}.",
                          data={"memories": [m.content for m in items]}, display=listing)

    async def forget(args: dict[str, Any]) -> ToolResult:
        removed = memory.forget(args["fact"], ["user"] + (
            [project_scope(services.working.active_project["path"])] if services.working.active_project else []))
        if removed is None:
            return ToolResult(True, "I didn't have that saved.", data={"removed": None})
        services.bus.publish("memory.changed", action="delete", id=removed.id)
        return ToolResult(True, "Forgotten.", data={"removed": removed.content})

    groups = frozenset({"agent", "chat"})
    registry.register(Tool("memory.remember", "Save a fact or preference the user wants remembered long-term.",
                           params(["fact"], fact={"type": "string"},
                                  scope={"type": "string", "enum": ["user", "project"], "default": "user"}),
                           remember, PermissionLevel.NON_DESTRUCTIVE, 5, groups))
    registry.register(Tool("memory.recall", "Look up what Sugar remembers (optionally about a topic).",
                           params(query={"type": "string"}), recall, PermissionLevel.READ, 5, groups))
    registry.register(Tool("memory.forget", "Delete a remembered fact that matches the description.",
                           params(["fact"], fact={"type": "string"}), forget, PermissionLevel.NON_DESTRUCTIVE, 5,
                           groups, describe=lambda a: f"forget {a.get('fact')}"))
