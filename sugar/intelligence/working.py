"""Working memory: what "it", "that", "the project" and "the last error" refer to.

The model gets a compact description of this state with every request, and
the deterministic fast path uses it directly ("open the second one",
"go back", "undo that"). It is persisted to ``data/working.json`` so a restart
does not forget the active project.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ORDINALS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4, "sixth": 5, "6th": 5, "last": -1,
}
CARDINALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


@dataclass
class LastAction:
    domain: str  # app | browser | media | coding | typing | file | system | memory
    description: str
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    ts: float = field(default_factory=time.time)


@dataclass
class WorkingMemory:
    active_project: dict[str, Any] | None = None  # {"name", "path"}
    current_task: str | None = None
    last_user_text: str = ""
    last_assistant_text: str = ""
    last_action: LastAction | None = None
    last_error: str | None = None
    last_options: list[str] = field(default_factory=list)
    last_files: list[str] = field(default_factory=list)
    topic: str | None = None
    _path: Path | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: Path) -> WorkingMemory:
        memory = cls()
        memory._path = path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                memory.active_project = data.get("active_project")
                memory.current_task = data.get("current_task")
                memory.last_error = data.get("last_error")
                memory.last_files = data.get("last_files", [])[:10]
            except (OSError, json.JSONDecodeError):
                pass
        return memory

    def save(self) -> None:
        if self._path is None:
            return
        data = {
            "active_project": self.active_project,
            "current_task": self.current_task,
            "last_error": self.last_error,
            "last_files": self.last_files[:10],
        }
        try:
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ updates

    def set_project(self, name: str, path: str) -> None:
        self.active_project = {"name": name, "path": path}
        self.save()

    def record_action(self, domain: str, description: str, *, tool: str | None = None,
                      args: dict[str, Any] | None = None, ok: bool = True, error: str | None = None) -> None:
        self.last_action = LastAction(domain, description, tool, args or {}, ok)
        if not ok and error:
            self.last_error = error
        self.save()

    def offer_options(self, options: list[str]) -> None:
        self.last_options = list(options)[:10]

    def note_files(self, files: list[str]) -> None:
        merged = [f for f in files if f] + [f for f in self.last_files if f not in files]
        self.last_files = merged[:10]

    # ------------------------------------------------------------------ resolution

    def resolve_option(self, text: str) -> str | None:
        """"the second one" / "number 3" → the option Sugar last listed."""
        if not self.last_options:
            return None
        lowered = text.lower()
        match = re.search(r"\b(?:number|option)\s+(\d+|" + "|".join(CARDINALS) + r")\b", lowered)
        if match:
            value = match.group(1)
            index = (int(value) if value.isdigit() else CARDINALS[value]) - 1
            return self.last_options[index] if 0 <= index < len(self.last_options) else None
        for word, index in ORDINALS.items():
            if re.search(rf"\b{word}\b", lowered):
                try:
                    return self.last_options[index]
                except IndexError:
                    return None
        return None

    def describe(self) -> str:
        lines = []
        if self.active_project:
            lines.append(f"Active project: {self.active_project['name']} ({self.active_project['path']})")
        if self.current_task:
            lines.append(f"Current task: {self.current_task}")
        if self.last_action:
            status = "ok" if self.last_action.ok else "failed"
            lines.append(f"Last action ({self.last_action.domain}, {status}): {self.last_action.description}")
        if self.last_error:
            lines.append(f"Last error: {self.last_error[:300]}")
        if self.last_files:
            lines.append("Recently touched files: " + ", ".join(self.last_files[:5]))
        if self.last_options:
            numbered = "; ".join(f"{i + 1}) {o}" for i, o in enumerate(self.last_options[:6]))
            lines.append(f"Options Sugar last listed: {numbered}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_path", None)
        return data
