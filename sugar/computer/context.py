"""Desktop context: what "this", "that" and "the app I just opened" mean.

A WinEvent hook reports every foreground change. Sugar keeps a most-recent
list of *external* app windows — its own UI window never counts, so saying
"type hello" while looking at Sugar still types into the editor you were in.
It also remembers what Sugar itself acted on, the last search, and any
dialog (such as "save changes?") waiting for an answer.

Reads are lock-protected and cheap: the prompt builder and the fast-path
grammar use them on the event loop without touching the OS.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sugar.computer.backend import DesktopBackend, WindowInfo

BROWSER_PROCESSES = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe",
                     "arc.exe", "chromium.exe"}


@dataclass
class ActionRecord:
    action: str
    window: WindowInfo | None
    ts: float = field(default_factory=time.time)


@dataclass
class SearchContext:
    engine: str
    query: str
    url: str
    hwnd: int | None = None
    results: list[Any] | None = None  # resolved results (YouTube videos or web links), filled lazily
    pending: Any = None  # asyncio.Future while results are being fetched
    ts: float = field(default_factory=time.time)


@dataclass
class DialogPrompt:
    hwnd: int
    app: str
    buttons: list[str]
    kind: str = "save"  # save | confirm
    ts: float = field(default_factory=time.time)


class DesktopContext:
    def __init__(self, backend: DesktopBackend, own_titles: set[str] | None = None) -> None:
        self._backend = backend
        self._own_titles = {t.lower() for t in (own_titles or {"Sugar"})}
        self._lock = threading.Lock()
        self._recent: deque[WindowInfo] = deque(maxlen=12)  # MRU external windows, newest first
        self._actions: deque[ActionRecord] = deque(maxlen=20)
        self._foreground: WindowInfo | None = None
        self._stop: Callable[[], None] | None = None
        self.last_opened: WindowInfo | None = None
        self.last_search: SearchContext | None = None
        self.pending_dialog: DialogPrompt | None = None
        self.known_video_ids: set[str] = set()

    # ------------------------------------------------------------------ tracking

    def start(self) -> None:
        if self._stop is None:
            self._stop = self._backend.watch_foreground(self._on_foreground)

    def stop(self) -> None:
        if self._stop is not None:
            self._stop()
            self._stop = None

    def is_own(self, window: WindowInfo) -> bool:
        own_pid = getattr(self._backend, "own_pid", 0)
        if own_pid and window.pid == own_pid:
            return True
        # The UI may be hosted in an Edge app window titled "Sugar".
        return window.title.strip().lower() in self._own_titles and window.process in BROWSER_PROCESSES

    def _on_foreground(self, hwnd: int) -> None:
        try:
            info = self._backend.get_window(hwnd)
        except Exception:
            return
        if info is None or not info.title:
            return
        self.observe(info)

    def observe(self, info: WindowInfo) -> None:
        with self._lock:
            self._foreground = info
            if self.is_own(info):
                return
            for existing in list(self._recent):
                if existing.hwnd == info.hwnd:
                    self._recent.remove(existing)
            self._recent.appendleft(info)

    def refresh(self) -> None:
        """Re-read the current foreground window (cheap; used before resolving references)."""
        hwnd = self._backend.foreground()
        if hwnd:
            info = self._backend.get_window(hwnd)
            if info is not None and info.title:
                self.observe(info)

    def note_action(self, action: str, window: WindowInfo | None = None, *, opened: bool = False) -> None:
        with self._lock:
            self._actions.appendleft(ActionRecord(action, window))
            if opened and window is not None:
                self.last_opened = window
            if window is not None and not self.is_own(window):
                for existing in list(self._recent):
                    if existing.hwnd == window.hwnd:
                        self._recent.remove(existing)
                self._recent.appendleft(window)

    def forget_window(self, hwnd: int) -> None:
        with self._lock:
            for existing in list(self._recent):
                if existing.hwnd == hwnd:
                    self._recent.remove(existing)
            if self.last_opened is not None and self.last_opened.hwnd == hwnd:
                self.last_opened = None

    # ------------------------------------------------------------------ queries

    def _alive(self, window: WindowInfo | None) -> WindowInfo | None:
        if window is None:
            return None
        try:
            return window if self._backend.is_window(window.hwnd) else None
        except Exception:
            return None

    def active(self) -> WindowInfo | None:
        """The window "this" refers to: the foreground app, or the last one before Sugar's own window."""
        with self._lock:
            recent = list(self._recent)
        for window in recent:
            if self._alive(window):
                return window
        return None

    def previous(self) -> WindowInfo | None:
        with self._lock:
            recent = list(self._recent)
        alive = [w for w in recent if self._alive(w)]
        return alive[1] if len(alive) > 1 else None

    def last_acted(self, within_s: float = 180.0) -> WindowInfo | None:
        with self._lock:
            actions = list(self._actions)
        for record in actions:
            if time.time() - record.ts > within_s:
                break
            if record.window is not None and self._alive(record.window):
                return record.window
        return None

    def resolve(self, reference: str | None) -> WindowInfo | None:
        reference = (reference or "this").strip().lower()
        if reference in {"last_opened", "the app i just opened", "what i just opened", "the one i just opened",
                         "the window i just opened"} or "just opened" in reference:
            return self._alive(self.last_opened) or self.last_acted()
        if reference in {"previous", "last", "the previous window", "the last window", "back", "the previous app",
                         "the last app", "previous window", "previous app"}:
            return self.previous()
        words = reference.split()
        if not words or len(words) > 3:
            return None
        nouns = {"window", "app", "application", "program", "one", "thing", "tab", "page"}
        if words[0] in {"this", "current", "active", "here"} or reference.startswith("the current"):
            if all(w in nouns | {"this", "current", "active", "here", "the"} for w in words):
                return self.active()
        if words[0] in {"that", "it"} and all(w in nouns | {"that", "it"} for w in words):
            return self.last_acted() or self.active()
        return None

    def recent_windows(self) -> list[WindowInfo]:
        with self._lock:
            return [w for w in self._recent if self._alive(w)]

    def foreground_is_own(self) -> bool:
        with self._lock:
            fg = self._foreground
        return bool(fg is not None and self.is_own(fg))

    # ------------------------------------------------------------------ for prompts and the grammar

    def active_app(self) -> str | None:
        window = self.active()
        return window.app if window else None

    def active_is_browser(self) -> bool:
        window = self.active()
        return bool(window and window.process in BROWSER_PROCESSES)

    def describe(self) -> str:
        active = self.active()
        if active is None:
            return ""
        lines = [f"Desktop — active window: {_describe_window(active)}"]
        previous = self.previous()
        if previous is not None:
            lines.append(f"Previous window: {_describe_window(previous)}")
        opened = self._alive(self.last_opened)
        if opened is not None and opened.hwnd != active.hwnd:
            lines.append(f"Last app Sugar opened: {_describe_window(opened)}")
        if self.pending_dialog is not None:
            lines.append(f"{self.pending_dialog.app} is showing a dialog with buttons: "
                         + ", ".join(self.pending_dialog.buttons[:6]))
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        active = self.active()
        return {
            "active": _window_dict(active),
            "previous": _window_dict(self.previous()),
            "last_opened": _window_dict(self._alive(self.last_opened)),
            "pending_dialog": self.pending_dialog.app if self.pending_dialog else None,
        }


def _describe_window(window: WindowInfo) -> str:
    kind = " (browser)" if window.process in BROWSER_PROCESSES else ""
    state = " (minimized)" if window.minimized else ""
    return f"{window.app}{kind} — '{window.title[:80]}'{state}"


def _window_dict(window: WindowInfo | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"hwnd": window.hwnd, "app": window.app, "title": window.title, "process": window.process}
