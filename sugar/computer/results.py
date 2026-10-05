"""The structured outcome of every computer action.

A result says what was attempted, on what, and — separately — whether the
effect was *observed*. ``success`` means the action ran without an error;
``verified`` means Sugar saw the outcome (the window closed, the text is in
the editor, the page title matches). Nothing reports "done" on the strength
of having sent a keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComputerActionResult:
    success: bool
    action: str
    target: str | None = None
    details: str = ""  # short and speakable: "Typed 47 characters into Notepad."
    verified: bool = False
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, action: str, target: str | None, details: str, *, verified: bool = True,
           **data: Any) -> ComputerActionResult:
        return cls(True, action, target, details, verified, None, data)

    @classmethod
    def fail(cls, action: str, target: str | None, details: str, error: str | None = None,
             **data: Any) -> ComputerActionResult:
        return cls(False, action, target, details, False, error or details, data)

    def to_tool_result(self, *, speak: bool = True, display: str | None = None):
        from sugar.tools.registry import ToolResult

        payload = {"action": self.action, "target": self.target, "verified": self.verified, **self.data}
        return ToolResult(ok=self.success, summary=self.details, data=payload,
                          error=None if self.success else self.error, display=display, speak=speak)
