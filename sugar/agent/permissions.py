"""Permission policy for tool execution.

Policy lives here, in code — never in a prompt. Model output, file contents,
web pages and Claude Code output can *request* actions but cannot change the
level an action requires or approve it.

Decisions come from :mod:`sugar.security.policy` (provenance + action risk):
  * credential access is denied;
  * after the model has read untrusted content (web, files, search results, tool output) only
    read-only actions run unasked; low-risk/modifying ones are confirmed; anything riskier is denied;
  * the user's own commands run up to modifications; riskier ones are confirmed;
  * the model acting on a direct request runs read-only and low-risk actions, modifications only with
    ``auto_approve_level`` >= 2, and always confirms unknown, network, destructive and privileged ones.

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
from sugar.security.policy import Decision, Verdict, decide
from sugar.security.provenance import Origin, Provenance
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
        self.last_verdict: Verdict | None = None

    @property
    def pending(self) -> PermissionRequest | None:
        return self._pending

    def verdict(self, tool: Tool, args: dict[str, Any], provenance: Provenance) -> Verdict:
        return decide(tool, args, provenance, auto_approve_level=self._settings.auto_approve_level,
                      trusted_tools=tuple(self._settings.trusted_tools))

    def requires_confirmation(self, tool: Tool, level: PermissionLevel, origin: str) -> bool:
        """Compatibility helper: whether an action at ``level`` would need a confirmation."""
        provenance = Provenance(Origin.DIRECT_USER_TEXT if origin == "user" else Origin.MODEL_GENERATED)
        probe = _LevelProbe(tool.name, level)
        return self.verdict(probe, {}, provenance).decision != Decision.ALLOW

    async def authorize(self, tool: Tool, args: dict[str, Any], origin: str,
                        provenance: Provenance | None = None) -> bool:
        if provenance is None:
            provenance = Provenance(Origin.DIRECT_USER_TEXT if origin == "user" else Origin.MODEL_GENERATED)
        verdict = self.verdict(tool, args, provenance)
        self.last_verdict = verdict
        log_event("PERMISSION_VERDICT", tool=tool.name, decision=verdict.decision.value, risk=verdict.risk.label,
                  reason=verdict.reason, provenance=provenance.to_dict())
        self._bus.publish("security.verdict", tool=tool.name, decision=verdict.decision.value,
                          risk=verdict.risk.label, reason=verdict.reason, provenance=provenance.to_dict())
        if verdict.decision == Decision.ALLOW:
            return True
        if verdict.decision == Decision.DENY:
            return False
        level = tool.level_for(args)
        action = tool.action_text(args)
        if provenance.tainted:
            action = f"{action} (suggested after reading untrusted content: {provenance.describe()})"
        async with self._lock:  # one spoken question at a time
            loop = asyncio.get_running_loop()
            request = PermissionRequest(tool.name, action, level, origin, future=loop.create_future())
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

    async def confirm(self, action: str, tool: str = "sid.confirm") -> bool:
        """Ask the user a yes/no question about ``action`` through the normal confirmation flow."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            request = PermissionRequest(tool, action, PermissionLevel.SENSITIVE, "user", future=loop.create_future())
            self._pending = request
            self._bus.publish("permission.request", **request.to_dict())
            try:
                if self.on_request is not None:
                    await self.on_request(request)
                approved = await asyncio.wait_for(asyncio.shield(request.future),
                                                  timeout=self._settings.confirmation_timeout_s)
            except TimeoutError:
                approved = False
                self._bus.publish("permission.expired", id=request.id)
            finally:
                self._pending = None
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


@dataclass
class _LevelProbe:
    """A stand-in tool with a fixed level, for :meth:`PermissionManager.requires_confirmation`."""

    name: str
    level: PermissionLevel

    def level_for(self, args: dict[str, Any]) -> PermissionLevel:
        return self.level
