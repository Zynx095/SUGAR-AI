"""Permission policy for tool execution.

Policy lives here, in code — never in a prompt. Model output, file contents,
web pages and Claude Code output can *request* actions but cannot change the
level an action requires or approve it.

Rules:
  * level <= ``auto_approve_level`` (default NON_DESTRUCTIVE) runs immediately;
  * tools listed in ``trusted_tools`` run immediately (deliberate opt-in);
  * a SENSITIVE action the user asked for directly (fast path) runs, because
    the spoken command is itself explicit; the same action proposed by a
    model needs confirmation;
  * DESTRUCTIVE actions always need confirmation.

Confirmation is asynchronous: a :class:`PermissionRequest` is published, the
conversation layer asks out loud, and the user's "yes"/"no" (or a UI click)
resolves it. Unanswered requests are denied after a timeout.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sugar.config.settings import PermissionSettings
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.tools.registry import PermissionLevel, Tool

log = logging.getLogger(__name__)
_ids = itertools.count(1)


@dataclass
class PermissionRequest:
    tool: str
    action: str
    level: PermissionLevel
    origin: str
    id: str = field(default_factory=lambda: f"perm-{next(_ids)}")
    created: float = field(default_factory=time.time)
    future: asyncio.Future[bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "action": self.action,
                "level": self.level.label, "origin": self.origin}


class PermissionManager:
    def __init__(self, settings: PermissionSettings, bus: EventBus) -> None:
        self._settings = settings
        self._bus = bus
        self._lock = asyncio.Lock()
        self._pending: PermissionRequest | None = None
        self.on_request: Callable[[PermissionRequest], Awaitable[None]] | None = None

    @property
    def pending(self) -> PermissionRequest | None:
        return self._pending

    def requires_confirmation(self, tool: Tool, level: PermissionLevel, origin: str) -> bool:
        if tool.name in self._settings.trusted_tools:
            return False
        if level <= self._settings.auto_approve_level:
            return False
        if origin == "user" and level < PermissionLevel.DESTRUCTIVE:
            return False
        return True

    async def authorize(self, tool: Tool, args: dict[str, Any], origin: str) -> bool:
        level = tool.level_for(args)
        if not self.requires_confirmation(tool, level, origin):
            return True
        async with self._lock:  # one spoken question at a time
            loop = asyncio.get_running_loop()
            request = PermissionRequest(tool.name, tool.action_text(args), level, origin, future=loop.create_future())
            self._pending = request
            log_event("PERMISSION_REQUEST", tool=tool.name, action=request.action, level=level.label)
            self._bus.publish("permission.request", **request.to_dict())
            try:
                if self.on_request is not None:
                    await self.on_request(request)
                approved = await asyncio.wait_for(
                    asyncio.shield(request.future), timeout=self._settings.confirmation_timeout_s
                )
            except TimeoutError:
                approved = False
                self._bus.publish("permission.expired", id=request.id)
            finally:
                self._pending = None
            log_event("PERMISSION_RESOLVED", tool=tool.name, approved=approved)
            self._bus.publish("permission.resolved", id=request.id, approved=approved)
            return approved

    def resolve(self, approved: bool, request_id: str | None = None) -> bool:
        request = self._pending
        if request is None or request.future is None or request.future.done():
            return False
        if request_id is not None and request_id != request.id:
            return False
        request.future.set_result(approved)
        return True

    def cancel_pending(self) -> None:
        self.resolve(False)
