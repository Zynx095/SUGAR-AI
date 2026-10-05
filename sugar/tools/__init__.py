"""Tool registry assembly."""

from __future__ import annotations

from typing import Any

from sugar.tools import coding_tools, desktop, filesystem, media, memory_tools, terminal, web
from sugar.tools.calculator import evaluate_spoken_math, format_number
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices


def _register_calculator(registry: ToolRegistry) -> None:
    async def calculate(args: dict[str, Any]) -> ToolResult:
        value = evaluate_spoken_math(args["expression"])
        if value is None:
            return ToolResult.failure("I couldn't work that out as arithmetic.")
        return ToolResult(True, f"That's {format_number(value)}.", data={"value": value})

    registry.register(Tool("calc.evaluate", "Evaluate an arithmetic expression exactly (use for any calculation).",
                           params(["expression"], expression={"type": "string"}), calculate,
                           PermissionLevel.READ, 5, frozenset({"agent", "chat"})))


def build_registry(services: ToolServices) -> ToolRegistry:
    registry = ToolRegistry()
    for module in (desktop, web, media, filesystem, terminal, memory_tools, coding_tools):
        module.register(registry, services)
    _register_calculator(registry)
    return registry


__all__ = ["ToolRegistry", "ToolServices", "build_registry"]
