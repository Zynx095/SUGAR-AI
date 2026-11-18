"""Command provenance: who asked for an action, and what content the decision was exposed to.

Every tool call carries a :class:`Provenance`. Only the user's own words — a typed or spoken command —
can authorise automatic execution. Anything SID reads from the outside world (web pages, search results,
files, documents, e-mail, tool and Claude Code output) is *untrusted*: once a model has seen such content
in a turn, the turn is tainted, and every action the model proposes afterwards carries that taint. The
taint cannot be laundered: summarising, paraphrasing or re-typing the content inside model-generated tool
arguments does not remove it, because it is tracked per turn, not per string. Argument-level matches
(a command that literally appears in a fetched page) are recorded as well, for the audit trail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Origin(StrEnum):
    DIRECT_USER_TEXT = "direct_user_text"
    DIRECT_USER_VOICE = "direct_user_voice"
    MODEL_GENERATED = "model_generated"
    WEB_PAGE = "web_page"
    WEB_SEARCH_RESULT = "web_search_result"
    LOCAL_FILE = "local_file"
    EMAIL_CONTENT = "email_content"
    DOCUMENT_CONTENT = "document_content"
    TOOL_OUTPUT = "tool_output"
    SCREEN_CONTENT = "screen_content"  # text read off the screen (UI Automation, OCR) or a vision model's account
    CAMERA_IMAGE = "camera_image"  # what the camera sees (a printed note can say anything)
    SYSTEM_EVENT = "system_event"


DIRECT = frozenset({Origin.DIRECT_USER_TEXT, Origin.DIRECT_USER_VOICE})
UNTRUSTED = frozenset({Origin.WEB_PAGE, Origin.WEB_SEARCH_RESULT, Origin.LOCAL_FILE, Origin.EMAIL_CONTENT,
                       Origin.DOCUMENT_CONTENT, Origin.TOOL_OUTPUT, Origin.SCREEN_CONTENT, Origin.CAMERA_IMAGE})

# Tools whose *results* bring outside content into the model's context, and what kind of content it is.
UNTRUSTED_OUTPUT: dict[str, Origin] = {
    "web.fetch": Origin.WEB_PAGE,
    "web.lookup": Origin.WEB_SEARCH_RESULT,
    "browser.read_page": Origin.WEB_PAGE,
    "browser.current": Origin.WEB_PAGE,
    "browser.search": Origin.WEB_SEARCH_RESULT,
    "youtube.search": Origin.WEB_SEARCH_RESULT,
    "media.search": Origin.WEB_SEARCH_RESULT,
    "filesystem.read": Origin.LOCAL_FILE,
    "filesystem.search": Origin.LOCAL_FILE,
    "filesystem.list": Origin.LOCAL_FILE,
    "clipboard.read": Origin.DOCUMENT_CONTENT,
    "screen.inspect": Origin.SCREEN_CONTENT,
    "screen.look": Origin.SCREEN_CONTENT,
    "screen.read": Origin.SCREEN_CONTENT,
    "screen.ask": Origin.SCREEN_CONTENT,
    "screen.describe": Origin.SCREEN_CONTENT,
    "ui.where": Origin.SCREEN_CONTENT,
    "ui.controls": Origin.SCREEN_CONTENT,
    "camera.look": Origin.CAMERA_IMAGE,
    "paint.reference": Origin.WEB_SEARCH_RESULT,
    "paint.draw": Origin.WEB_SEARCH_RESULT,
    "terminal.run": Origin.TOOL_OUTPUT,
    "git.diff": Origin.LOCAL_FILE,
    "claude.status": Origin.TOOL_OUTPUT,
    "window.list": Origin.TOOL_OUTPUT,
    "desktop.context": Origin.TOOL_OUTPUT,
}


@dataclass
class Provenance:
    """The provenance of one action: who asked, and what untrusted content the asker had seen."""

    origin: Origin
    tainted_by: set[Origin] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)  # e.g. "web.fetch https://…" (for confirmations/audit)
    echoes_untrusted: bool = False  # an argument appears verbatim in untrusted content

    @property
    def direct(self) -> bool:
        return self.origin in DIRECT and not self.tainted_by

    @property
    def tainted(self) -> bool:
        return bool(self.tainted_by) or self.origin in UNTRUSTED

    def describe(self) -> str:
        if self.direct:
            return "your own request"
        if self.tainted:
            kinds = ", ".join(sorted(o.value.replace("_", " ") for o in (self.tainted_by or {self.origin})))
            where = f" ({self.sources[-1]})" if self.sources else ""
            return f"a model action after reading untrusted content: {kinds}{where}"
        return "a model action on your request"

    def to_dict(self) -> dict:
        return {"origin": self.origin.value, "tainted_by": sorted(o.value for o in self.tainted_by),
                "sources": self.sources[-3:], "echoes_untrusted": self.echoes_untrusted}


def user_origin(source: str) -> Origin:
    return Origin.DIRECT_USER_VOICE if source == "voice" else Origin.DIRECT_USER_TEXT


class TurnProvenance:
    """Tracks one turn: the user's request plus every piece of untrusted content the model has seen."""

    def __init__(self, user: Origin = Origin.DIRECT_USER_TEXT) -> None:
        self.user = user
        self.tainted_by: set[Origin] = set()
        self.sources: list[str] = []
        self._untrusted_text: list[str] = []

    def observe(self, tool: str, args: dict, output: str) -> None:
        """Record a tool result entering the model's context."""
        kind = UNTRUSTED_OUTPUT.get(tool)
        if kind is None:
            return
        self.tainted_by.add(kind)
        target = args.get("url") or args.get("path") or args.get("query") or args.get("command") or ""
        self.sources.append(f"{tool} {str(target)[:80]}".strip())
        self._untrusted_text.append(output[:50_000])

    def for_model_action(self, args: dict) -> Provenance:
        return Provenance(Origin.MODEL_GENERATED, set(self.tainted_by), list(self.sources),
                          echoes_untrusted=self._echoes(args))

    def _echoes(self, args: dict) -> bool:
        if not self._untrusted_text:
            return False
        haystack = _squash("\n".join(self._untrusted_text))
        for value in args.values():
            text = _squash(str(value))
            if len(text) >= 12 and text in haystack:
                return True
        return False


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()
