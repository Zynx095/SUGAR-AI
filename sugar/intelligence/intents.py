"""Intent type and utterance normalisation shared by the command grammars."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_LEADING_FILLER = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|yo|alright|right|now|and|also|then|actually|just|please|"
    r"can you|could you|would you|will you|can you please|could you please|would you mind|"
    r"i want you to|i need you to|i'd like you to|i would like you to|go ahead and|let's|lets)\s+)+"
)
_TRAILING_FILLER = re.compile(
    r"(?:\s+(?:please|for me|now|right now|thanks|thank you|real quick|quickly))+$"
)


def normalize(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"[^\w\s'%.:+\-*/×÷^()?]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,!?")
    for _ in range(3):
        stripped = _LEADING_FILLER.sub("", t)
        stripped = _TRAILING_FILLER.sub("", stripped).strip(" .,!?")
        if stripped == t:
            break
        t = stripped
    return t


@dataclass
class Intent:
    name: str
    slots: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    call: tuple[str, dict[str, Any]] | None = None  # (tool, arguments) when the intent maps straight to a tool

    @property
    def domain(self) -> str:
        return self.name.split(".", 1)[0]


@dataclass
class DesktopView:
    """What the grammar may know about the desktop — a cheap, cached snapshot (no OS calls)."""

    active_app: str | None = None  # process stem of the window "this" refers to, e.g. "brave"
    active_is_browser: bool = False
    has_dialog: bool = False  # an app is asking "save changes?"
    dialog_kind: str | None = None  # save | confirm
    has_search: bool = False  # a recent browser search exists ("open the first result")
    last_domain: str | None = None  # domain of Sugar's last action: typing, coding, browser…
    titles: tuple[str, ...] = ()  # lower-case titles of recent app windows
