"""The boundary between Sugar's computer-control logic and the operating system.

Controllers (windows, keyboard, apps, browser, media, screen) contain the
decisions — which window, which keys, how to verify — and call a
:class:`DesktopBackend` for every effect. ``sugar.computer.native`` implements
it with Win32, UI Automation and WinRT; the test suite implements it with an
in-memory desktop, so the logic is tested without touching a real screen.

All backend methods are synchronous and are only ever called from the single
``sugar-desktop`` worker thread (COM and keyboard state are per-thread).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str  # lower-case image name of the real app process, e.g. "brave.exe"
    class_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    minimized: bool = False
    maximized: bool = False

    @property
    def app(self) -> str:
        """Process stem: "brave", "notepad", "code"."""
        return self.process.rsplit(".", 1)[0]

    def label(self) -> str:
        return f"{self.app} '{self.title[:60]}'"


@dataclass(frozen=True)
class TabInfo:
    index: int
    name: str  # raw accessible name (may carry "- Audio playing", ". Modified." …)
    selected: bool


@dataclass(frozen=True)
class FocusInfo:
    control_type: str  # "edit", "document", "button", "pane", …
    name: str
    class_name: str


@dataclass(frozen=True)
class MediaSession:
    app_id: str  # e.g. "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "Brave"
    title: str
    artist: str
    status: str  # playing | paused | stopped | changing | closed | unknown
    is_current: bool = False
    can_next: bool = False


@dataclass(frozen=True)
class KeyChord:
    """One character on the target window's keyboard layout: a key plus the modifiers it needs."""

    vk: int
    modifiers: tuple[int, ...] = ()


class DesktopUnavailable(RuntimeError):
    """Raised by backends that cannot touch a desktop (tests, non-Windows)."""


class DesktopBackend(Protocol):
    own_pid: int

    def thread_init(self) -> None: ...

    # -- windows and processes
    def list_windows(self) -> list[WindowInfo]: ...
    def get_window(self, hwnd: int) -> WindowInfo | None: ...
    def is_window(self, hwnd: int) -> bool: ...
    def foreground(self) -> int: ...
    def activate(self, hwnd: int) -> bool: ...
    def set_state(self, hwnd: int, state: str) -> None: ...  # minimize | maximize | restore
    def close_window(self, hwnd: int) -> None: ...
    def set_rect(self, hwnd: int, left: int, top: int, width: int, height: int) -> None: ...
    def work_area(self, hwnd: int) -> tuple[int, int, int, int]: ...
    def owned_windows(self, hwnd: int) -> list[WindowInfo]: ...
    def processes(self, image: str) -> list[int]: ...
    def kill_process(self, pid: int) -> bool: ...
    def is_elevated(self, pid: int) -> bool | None: ...
    def start(self, target: str) -> None: ...
    def spawn(self, argv: list[str]) -> int: ...
    def watch_foreground(self, callback: Callable[[int], None]) -> Callable[[], None]: ...

    # -- input
    def key(self, vk: int, *, up: bool) -> bool: ...
    def unicode(self, code_unit: int, *, up: bool) -> bool: ...
    def chord_for(self, char: str, hwnd: int) -> KeyChord | None: ...
    def caps_lock(self) -> bool: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_button(self, button: str, *, up: bool) -> None: ...
    def mouse_wheel(self, clicks: int, *, horizontal: bool = False) -> None: ...
    def cursor(self) -> tuple[int, int]: ...

    # -- clipboard
    def clipboard_text(self) -> str | None: ...
    def set_clipboard_text(self, text: str) -> bool: ...
    def clipboard_snapshot(self) -> Any: ...
    def restore_clipboard(self, snapshot: Any) -> None: ...

    # -- accessibility (UI Automation)
    def tabs(self, hwnd: int) -> list[TabInfo]: ...
    def select_tab(self, hwnd: int, index: int) -> bool: ...
    def close_tab(self, hwnd: int, index: int) -> bool: ...
    def address(self, hwnd: int) -> str | None: ...
    def focused(self) -> FocusInfo | None: ...
    def focused_text(self, limit: int = 20000) -> str | None: ...
    def invoke(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> str | None: ...
    def buttons(self, hwnd: int) -> list[str]: ...
    def page_text(self, hwnd: int, limit: int = 20000) -> str | None: ...
    def describe_ui(self, hwnd: int, limit: int = 150) -> list[dict[str, Any]]: ...

    # -- media sessions (Windows SMTC)
    def media_sessions(self) -> list[MediaSession]: ...
    def media_command(self, app_id: str, action: str) -> bool: ...

    # -- screen
    def screenshot(self, path: Path, hwnd: int | None = None) -> Path: ...


class NullDesktop:
    """A backend for runs without desktop access: every action fails clearly."""

    own_pid = 0

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise DesktopUnavailable("computer control is not available in this run")

        return unavailable

    def thread_init(self) -> None:
        return None

    def watch_foreground(self, callback: Callable[[int], None]) -> Callable[[], None]:
        return lambda: None

    def list_windows(self) -> list[WindowInfo]:
        return []

    def foreground(self) -> int:
        return 0

    def get_window(self, hwnd: int) -> WindowInfo | None:
        return None

    def is_window(self, hwnd: int) -> bool:
        return False
