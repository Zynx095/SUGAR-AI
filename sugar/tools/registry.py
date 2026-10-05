"""Tool definitions and registry.

Every capability Sugar has that touches the computer is a :class:`Tool` with a
JSON-schema signature, a permission level, a timeout and an async handler.
The same registry serves three callers: the deterministic fast path, the LLM
agent loop (via OpenAI-style function schemas) and the UI.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

# Who asked for the running tool call: "user" (a spoken/typed command matched by the fast path) or
# "model". Set by the executor; tools that must treat model-written input with suspicion read it.
current_origin: ContextVar[str] = ContextVar("current_origin", default="model")


class PermissionLevel(IntEnum):
    READ = 0  # inspect only
    NON_DESTRUCTIVE = 1  # open apps, create files, edit code, media
    SENSITIVE = 2  # run commands/scripts, install, system settings, overwrite
    DESTRUCTIVE = 3  # delete, wipe, security changes

    @property
    def label(self) -> str:
        return self.name.replace("_", "-").lower()


@dataclass
class ToolResult:
    ok: bool
    summary: str  # short outcome, safe to speak ("Opened Chrome.")
    data: Any = None  # structured payload for the model / UI
    error: str | None = None
    display: str | None = None  # longer markdown for the screen
    speak: bool = True  # whether the summary should be spoken by fast paths

    def for_model(self, limit: int = 6000) -> str:
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.error:
            payload["error"] = self.error
        if self.data is not None:
            payload["data"] = self.data
        text = json.dumps(payload, default=str, ensure_ascii=False)
        if len(text) > limit:
            text = text[: limit - 40] + '…", "truncated": true}'
        return text

    @classmethod
    def failure(cls, summary: str, error: str | None = None, **kwargs: Any) -> ToolResult:
        return cls(ok=False, summary=summary, error=error or summary, **kwargs)


Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]
LevelFn = Callable[[dict[str, Any]], PermissionLevel]


@dataclass
class Tool:
    name: str  # dotted, e.g. "filesystem.read"
    description: str
    parameters: dict[str, Any]
    handler: Handler
    level: PermissionLevel | LevelFn = PermissionLevel.READ
    timeout_s: float = 30.0
    groups: frozenset[str] = frozenset({"agent"})
    describe: Callable[[dict[str, Any]], str] | None = None  # for confirmation prompts

    @property
    def llm_name(self) -> str:
        return self.name.replace(".", "__")

    def level_for(self, args: dict[str, Any]) -> PermissionLevel:
        return self.level(args) if callable(self.level) else self.level

    def action_text(self, args: dict[str, Any]) -> str:
        if self.describe:
            try:
                return self.describe(args)
            except Exception:
                pass
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{self.name}({rendered})"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolValidationError(ValueError):
    pass


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Check model-supplied arguments against a (flat) JSON schema.

    Supports the subset Sugar's tools use: ``type``, ``required``, ``enum``,
    ``default``, ``minimum``/``maximum`` and array ``items.type``. Numbers that
    arrive as strings are coerced; unknown keys are dropped.
    """
    if not isinstance(args, dict):
        raise ToolValidationError("arguments must be an object")
    properties: dict[str, Any] = schema.get("properties", {})
    clean: dict[str, Any] = {}
    for name, spec in properties.items():
        if name not in args or args[name] is None:
            if "default" in spec:
                clean[name] = spec["default"]
            continue
        value = args[name]
        expected = spec.get("type")
        if expected in ("integer", "number") and isinstance(value, str):
            try:
                value = int(value) if expected == "integer" else float(value)
            except ValueError as exc:
                raise ToolValidationError(f"{name} must be a {expected}") from exc
        if expected == "boolean" and isinstance(value, str):
            value = value.strip().lower() in {"true", "yes", "1"}
        if expected and not isinstance(value, _TYPES.get(expected, (object,))):
            raise ToolValidationError(f"{name} must be a {expected}")
        if expected == "integer" and isinstance(value, bool):
            raise ToolValidationError(f"{name} must be an integer")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolValidationError(f"{name} must be one of {spec['enum']}")
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolValidationError(f"{name} must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolValidationError(f"{name} must be <= {spec['maximum']}")
        if expected == "array" and "items" in spec:
            item_type = spec["items"].get("type")
            if item_type and not all(isinstance(v, _TYPES.get(item_type, (object,))) for v in value):
                raise ToolValidationError(f"{name} items must be {item_type}")
        clean[name] = value
    missing = [name for name in schema.get("required", []) if name not in clean]
    if missing:
        raise ToolValidationError(f"missing required argument(s): {', '.join(missing)}")
    return clean


def params(required: list[str] | None = None, **properties: dict[str, Any]) -> dict[str, Any]:
    """Shorthand for building a flat object schema."""
    return {"type": "object", "properties": properties, "required": required or []}


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} registered twice")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name) or self._tools.get(name.replace("__", "."))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self, groups: set[str] | None = None, names: set[str] | None = None) -> list[Tool]:
        selected = [
            t for t in self._tools.values()
            if (groups is None or t.groups & groups) and (names is None or t.name in names)
        ]
        return sorted(selected, key=lambda t: t.name)

    def schemas(self, groups: set[str] | None = None, names: set[str] | None = None) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools(groups, names)]
