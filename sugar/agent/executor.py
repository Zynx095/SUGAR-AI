"""Tool execution: validation → permission → timeout/cancellation → events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from sugar.agent.permissions import PermissionManager
from sugar.core.events import EventBus
from sugar.core.logging import log_event
from sugar.tools.registry import ToolRegistry, ToolResult, ToolValidationError, current_origin, validate_arguments

log = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, permissions: PermissionManager, bus: EventBus) -> None:
        self.registry = registry
        self.permissions = permissions
        self._bus = bus
        self.on_result: Callable[[str, dict[str, Any], ToolResult], None] | None = None

    async def execute(self, name: str, args: dict[str, Any] | None, *, origin: str = "model") -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult.failure(f"I don't have a tool called {name}.", f"unknown tool {name}")
        try:
            clean = validate_arguments(tool.parameters, args or {})
        except ToolValidationError as exc:
            return ToolResult.failure(f"Invalid arguments for {tool.name}: {exc}", str(exc))

        allowed = await self.permissions.authorize(tool, clean, origin)
        if not allowed:
            log_event("TOOL_DENIED", tool=tool.name, origin=origin)
            self._bus.publish("tool.denied", name=tool.name, action=tool.action_text(clean))
            return ToolResult(ok=False, summary="Okay, I won't do that.", error="permission denied by user")

        started = time.perf_counter()
        log_event("TOOL_START", tool=tool.name, origin=origin, args=_preview(clean))
        self._bus.publish("tool.start", name=tool.name, action=tool.action_text(clean),
                          level=tool.level_for(clean).label, origin=origin)
        token = current_origin.set(origin)
        try:
            result = await asyncio.wait_for(tool.handler(clean), timeout=tool.timeout_s)
        except TimeoutError:
            result = ToolResult.failure(f"{tool.name} timed out after {tool.timeout_s:.0f} seconds.", "timeout")
        except asyncio.CancelledError:
            self._bus.publish("tool.complete", name=tool.name, ok=False, summary="cancelled", ms=_ms(started))
            raise
        except Exception as exc:  # a tool bug must not crash the turn
            log.exception("tool %s failed", tool.name)
            result = ToolResult.failure(f"{tool.name} failed: {exc}", repr(exc))
        finally:
            current_origin.reset(token)

        elapsed = _ms(started)
        log_event("TOOL_COMPLETE", tool=tool.name, ok=result.ok, ms=elapsed, summary=result.summary)
        self._bus.publish("tool.complete", name=tool.name, ok=result.ok, summary=result.summary,
                          display=result.display, ms=elapsed)
        if self.on_result is not None:
            self.on_result(tool.name, clean, result)
        return result


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _preview(args: dict[str, Any]) -> str:
    text = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
    return text[:200]
