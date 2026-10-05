"""An in-memory Windows desktop for testing computer control without touching the real one.

It models just enough behaviour to make the controllers' decisions observable:
windows with focus and min/max state, editors that receive keystrokes (with
Shift, Caps Lock, selection, Backspace, Ctrl shortcuts), Chromium-like browser
windows with tabs, an omnibox and history, "save changes?" prompts, a
clipboard with formats, processes, and media sessions.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sugar.computer.backend import FocusInfo, KeyChord, MediaSession, TabInfo, WindowInfo
from sugar.computer.keys import (
    VK_BACK,
    VK_CONTROL,
    VK_DELETE,
    VK_END,
    VK_HOME,
    VK_LEFT,
    VK_MENU,
    VK_RETURN,
    VK_SHIFT,
    VK_TAB,
)

_ids = itertools.count(1000)


@dataclass
class Tab:
    title: str
    url: str
    history: list[tuple[str, str]] = field(default_factory=list)
    forward: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FakeWindow:
    title: str
    process: str
    pid: int
    hwnd: int = field(default_factory=lambda: next(_ids))
    class_name: str = ""
    kind: str = "editor"  # editor | browser | other
    text: str = ""
    selection: tuple[int, int] | None = None  # (start, end) in text
    caret: int | None = None
    unsaved: bool = False
    file_backed: bool = False
    tabs: list[Tab] = field(default_factory=list)
    selected: int = 0
    omnibox: str | None = None  # text while the address bar has focus
    dialog: list[str] | None = None  # buttons of a modal "save changes?" prompt
    dialog_scope: str = "window"  # what answering it closes: the window or the current tab
    minimized: bool = False
    maximized: bool = False
    rect: tuple[int, int, int, int] = (100, 100, 900, 700)
    closed_tabs: list[Tab] = field(default_factory=list)

    def info(self) -> WindowInfo:
        title = self.title
        if self.kind == "browser" and self.tabs:
            title = f"{self.tabs[self.selected].title} - {self.process.split('.')[0].title()}"
        elif self.kind == "editor" and self.app_title:
            title = ("*" if self.unsaved else "") + self.app_title
        return WindowInfo(self.hwnd, title, self.pid, self.process, self.class_name, self.rect, self.minimized,
                          self.maximized)

    @property
    def app_title(self) -> str:
        return self.title


def browser_window(process: str = "brave.exe", tabs: list[tuple[str, str]] | None = None, pid: int = 500) -> FakeWindow:
    items = [Tab(t, u) for t, u in (tabs or [("New Tab", "brave://newtab")])]
    return FakeWindow("", process, pid, kind="browser", class_name="Chrome_WidgetWin_1", tabs=items)


class FakeDesktop:
    own_pid = 1

    def __init__(self) -> None:
        self.windows: dict[int, FakeWindow] = {}
        self.z: list[int] = []
        self.fg: int = 0
        self.refuse_focus: set[int] = set()
        self.clipboard: list[tuple[int, bytes]] = []
        self.caps = False
        self.held: set[int] = set()
        self.pending_surrogate: int | None = None
        self.launchers: dict[str, Any] = {}
        self.launched: list[str] = []
        self.sessions: list[MediaSession] = []
        self.media_log: list[tuple[str, str]] = []
        self.mouse: list[tuple] = []
        self.key_log: list[tuple[int, bool]] = []
        self.watchers: list[Any] = []
        self.elevated_pids: set[int] = set()
        self.killed: list[int] = []
        self.page_loader = None  # callable(url) -> title

    # ------------------------------------------------------------------ setup helpers

    def add(self, window: FakeWindow, *, focus: bool = True) -> FakeWindow:
        self.windows[window.hwnd] = window
        self.z.insert(0, window.hwnd)
        if focus:
            self._focus(window.hwnd)
        return window

    def _focus(self, hwnd: int) -> None:
        self.fg = hwnd
        if hwnd in self.z:
            self.z.remove(hwnd)
            self.z.insert(0, hwnd)
        for callback in self.watchers:
            callback(hwnd)

    def window_by_title(self, fragment: str) -> FakeWindow:
        return next(w for w in self.windows.values() if fragment.lower() in w.info().title.lower())

    # ------------------------------------------------------------------ DesktopBackend: windows

    def thread_init(self) -> None:
        return None

    def list_windows(self) -> list[WindowInfo]:
        return [self.windows[h].info() for h in self.z if h in self.windows]

    def get_window(self, hwnd: int) -> WindowInfo | None:
        window = self.windows.get(hwnd)
        return window.info() if window else None

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def foreground(self) -> int:
        return self.fg

    def activate(self, hwnd: int) -> bool:
        if hwnd not in self.windows or hwnd in self.refuse_focus:
            return False
        self.windows[hwnd].minimized = False
        self._focus(hwnd)
        return True

    def set_state(self, hwnd: int, state: str) -> None:
        window = self.windows[hwnd]
        if state == "minimize":
            window.minimized, window.maximized = True, False
            if self.fg == hwnd:
                others = [h for h in self.z if h != hwnd and not self.windows[h].minimized]
                self.fg = others[0] if others else 0
        elif state == "maximize":
            window.minimized, window.maximized = False, True
            self._focus(hwnd)
        elif state == "restore":
            window.minimized, window.maximized = False, False

    def close_window(self, hwnd: int) -> None:
        window = self.windows.get(hwnd)
        if window is None:
            return
        if window.unsaved and window.kind == "editor":
            window.dialog = ["Save", "Don't save", "Cancel"]
            return
        self._remove(hwnd)

    def _close_document(self, window: FakeWindow) -> None:
        if len(window.tabs) > 1:
            window.tabs.pop()
            window.text = ""
        else:
            self._remove(window.hwnd)

    def _remove(self, hwnd: int) -> None:
        self.windows.pop(hwnd, None)
        if hwnd in self.z:
            self.z.remove(hwnd)
        if self.fg == hwnd:
            self.fg = self.z[0] if self.z else 0

    def set_rect(self, hwnd: int, left: int, top: int, width: int, height: int) -> None:
        window = self.windows[hwnd]
        window.maximized = window.minimized = False
        window.rect = (left, top, left + width, top + height)

    def work_area(self, hwnd: int) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1040)

    def owned_windows(self, hwnd: int) -> list[WindowInfo]:
        return []

    def processes(self, image: str) -> list[int]:
        return sorted({w.pid for w in self.windows.values() if w.process == image.lower()})

    def kill_process(self, pid: int) -> bool:
        self.killed.append(pid)
        for hwnd in [h for h, w in self.windows.items() if w.pid == pid]:
            self._remove(hwnd)
        return True

    def is_elevated(self, pid: int) -> bool | None:
        return pid in self.elevated_pids

    def self_elevated(self) -> bool:
        return False

    def start(self, target: str) -> None:
        self.launched.append(target)
        factory = self.launchers.get(target)
        if factory is not None:
            self.add(factory())

    def spawn(self, argv: list[str]) -> int:
        self.launched.append(" ".join(argv))
        factory = self.launchers.get(argv[0])
        if factory is not None:
            window = factory()
            urls = [a for a in argv[1:] if not a.startswith("-")]
            if urls and window.kind == "browser":
                window.tabs = [Tab(self._title_for(urls[0]), urls[0])]
            self.add(window)
            return window.pid
        return 0

    def watch_foreground(self, callback):
        self.watchers.append(callback)
        return lambda: self.watchers.remove(callback) if callback in self.watchers else None

    # ------------------------------------------------------------------ DesktopBackend: input

    def chord_for(self, char: str, hwnd: int) -> KeyChord | None:
        if char.isascii() and char.isalpha():
            return KeyChord(ord(char.upper()), (VK_SHIFT,) if char.isupper() else ())
        if char.isdigit():
            return KeyChord(ord(char))
        if char == " ":
            return KeyChord(0x20)
        symbols = {"!": (0x31, True), "@": (0x32, True), "(": (0x39, True), ")": (0x30, True), ".": (0xBE, False),
                   ",": (0xBC, False), ":": (0xBA, True), '"': (0xDE, True), "-": (0xBD, False)}
        if char in symbols:
            vk, shift = symbols[char]
            return KeyChord(vk, (VK_SHIFT,) if shift else ())
        return None

    def caps_lock(self) -> bool:
        return self.caps

    def unicode(self, code_unit: int, *, up: bool) -> bool:
        if up:
            return True
        if 0xD800 <= code_unit <= 0xDBFF:
            self.pending_surrogate = code_unit
            return True
        if 0xDC00 <= code_unit <= 0xDFFF and self.pending_surrogate is not None:
            char = (bytes([self.pending_surrogate & 0xFF, self.pending_surrogate >> 8, code_unit & 0xFF,
                           code_unit >> 8])).decode("utf-16-le")
            self.pending_surrogate = None
        else:
            char = chr(code_unit)
        self._insert(char)
        return True

    def key(self, vk: int, *, up: bool) -> bool:
        self.key_log.append((vk, up))
        if vk in (VK_SHIFT, VK_CONTROL, VK_MENU, 0x5B):
            (self.held.discard if up else self.held.add)(vk)
            return True
        if up:
            return True
        window = self.windows.get(self.fg)
        if window is None:
            return True
        ctrl, shift, alt = VK_CONTROL in self.held, VK_SHIFT in self.held, VK_MENU in self.held
        if window.kind == "browser":
            self._browser_key(window, vk, ctrl, shift, alt)
        else:
            self._editor_key(window, vk, ctrl, shift)
        return True

    def _char_for(self, vk: int, shift: bool) -> str | None:
        if 0x41 <= vk <= 0x5A:
            upper = shift != self.caps
            return chr(vk) if upper else chr(vk).lower()
        shifted = {0x31: "!", 0x32: "@", 0x39: "(", 0x30: ")", 0xBA: ":", 0xDE: '"'}
        plain = {0xBE: ".", 0xBC: ",", 0xBD: "-", 0x20: " "}
        if shift and vk in shifted:
            return shifted[vk]
        if 0x30 <= vk <= 0x39:
            return chr(vk)
        return plain.get(vk)

    def _insert(self, text: str) -> None:
        window = self.windows.get(self.fg)
        if window is None:
            return
        if window.kind == "browser":
            if window.omnibox is not None:
                window.omnibox += text
            return
        if window.dialog:
            return
        start, end = window.selection or (len(window.text), len(window.text))
        window.text = window.text[:start] + text + window.text[end:]
        window.selection = None
        window.unsaved = True

    def _editor_key(self, window: FakeWindow, vk: int, ctrl: bool, shift: bool) -> None:
        if window.dialog:
            return
        if ctrl and vk == ord("A"):
            window.selection = (0, len(window.text))
        elif ctrl and vk == ord("V"):
            text = self.clipboard_text()
            if text:
                self._insert(text)
        elif ctrl and vk == ord("C"):
            if window.selection:
                self.set_clipboard_text(window.text[window.selection[0]:window.selection[1]])
        elif ctrl and vk == ord("S"):
            if window.file_backed:
                window.unsaved = False
            else:
                self.add(FakeWindow("Save as", window.process, window.pid, kind="other"))
        elif ctrl and vk == ord("N"):
            window.tabs.append(Tab("Untitled", ""))
            window.text = ""
        elif ctrl and vk == ord("W"):
            if window.unsaved:
                window.dialog, window.dialog_scope = ["Save", "Don't save", "Cancel"], "tab"
            else:
                self._close_document(window)
        elif ctrl and vk == ord("Z"):
            window.text = ""
        elif ctrl and vk == VK_END:
            window.selection = None
            window.caret = len(window.text)
        elif ctrl and vk == VK_BACK:
            stripped = window.text.rstrip()
            cut = stripped.rfind(" ") + 1
            window.text = stripped[:cut]
        elif shift and vk == VK_HOME:
            line_start = window.text.rfind("\n") + 1
            window.selection = (line_start, len(window.text))
        elif shift and vk == VK_LEFT:
            start, end = window.selection or (len(window.text), len(window.text))
            window.selection = (max(0, start - 1), end)
        elif vk == VK_BACK:
            if window.selection and window.selection[0] != window.selection[1]:
                start, end = window.selection
                window.text = window.text[:start] + window.text[end:]
                window.selection = None
            else:
                window.text = window.text[:-1]
            window.unsaved = True
        elif vk == VK_DELETE:
            if window.selection:
                start, end = window.selection
                window.text = window.text[:start] + window.text[end:]
                window.selection = None
        elif vk == VK_RETURN:
            self._insert("\n")
        elif vk == VK_TAB:
            self._insert("\t")
        else:
            char = self._char_for(vk, shift)
            if char is not None and not ctrl:
                self._insert(char)

    def _title_for(self, url: str) -> str:
        if self.page_loader is not None:
            return self.page_loader(url)
        host = url.split("//")[-1].split("/")[0].removeprefix("www.")
        return host.split(".")[0].title()

    def _browser_key(self, window: FakeWindow, vk: int, ctrl: bool, shift: bool, alt: bool) -> None:
        tab = window.tabs[window.selected] if window.tabs else None
        if window.omnibox is not None and not ctrl and not alt:
            if vk == VK_RETURN:
                url = window.omnibox
                window.omnibox = None
                if tab is not None:
                    tab.history.append((tab.title, tab.url))
                    tab.forward.clear()
                    tab.url = url.split("//")[-1]
                    tab.title = self._title_for(url)
                return
            if vk == VK_DELETE:
                return
            char = self._char_for(vk, shift)
            if char is not None:
                window.omnibox += char
            return
        if ctrl and shift and vk == ord("T"):
            if window.closed_tabs:
                window.tabs.append(window.closed_tabs.pop())
                window.selected = len(window.tabs) - 1
        elif ctrl and vk == ord("T"):
            window.tabs.append(Tab("New Tab", "brave://newtab"))
            window.selected = len(window.tabs) - 1
            window.omnibox = ""
        elif ctrl and vk == ord("W"):
            self.close_tab(window.hwnd, window.selected)
        elif ctrl and vk == ord("L"):
            window.omnibox = ""
        elif ctrl and shift and vk == VK_TAB:
            window.selected = (window.selected - 1) % len(window.tabs)
        elif ctrl and vk == VK_TAB:
            window.selected = (window.selected + 1) % len(window.tabs)
        elif ctrl and ord("1") <= vk <= ord("8"):
            window.selected = min(len(window.tabs) - 1, vk - ord("1"))
        elif ctrl and vk == ord("9"):
            window.selected = len(window.tabs) - 1
        elif alt and vk == 0x25 and tab is not None and tab.history:
            tab.forward.append((tab.title, tab.url))
            tab.title, tab.url = tab.history.pop()
        elif alt and vk == 0x27 and tab is not None and tab.forward:
            tab.history.append((tab.title, tab.url))
            tab.title, tab.url = tab.forward.pop()

    def mouse_move(self, x: int, y: int) -> None:
        self.mouse.append(("move", x, y))

    def mouse_button(self, button: str, *, up: bool) -> None:
        self.mouse.append(("up" if up else "down", button))

    def mouse_wheel(self, clicks: int, *, horizontal: bool = False) -> None:
        self.mouse.append(("wheel", clicks, horizontal))

    def cursor(self) -> tuple[int, int]:
        return (0, 0)

    # ------------------------------------------------------------------ clipboard

    def clipboard_text(self) -> str | None:
        for fmt, data in self.clipboard:
            if fmt == 13:
                return data.decode("utf-16-le").rstrip("\x00")
        return None

    def set_clipboard_text(self, text: str) -> bool:
        self.clipboard = [(13, text.encode("utf-16-le") + b"\x00\x00")]
        return True

    def clipboard_snapshot(self) -> list[tuple[int, bytes]]:
        return list(self.clipboard)

    def restore_clipboard(self, snapshot: list[tuple[int, bytes]]) -> None:
        self.clipboard = list(snapshot)

    # ------------------------------------------------------------------ accessibility

    def tabs(self, hwnd: int) -> list[TabInfo]:
        window = self.windows.get(hwnd)
        if window is None:
            return []
        if window.kind == "browser":
            return [TabInfo(i, t.title + (" - Audio playing" if "YouTube" in t.title and i == window.selected else ""),
                            i == window.selected) for i, t in enumerate(window.tabs)]
        return [TabInfo(i, f"{t.title}. {'Modified' if window.unsaved else 'Unmodified'}.", i == len(window.tabs) - 1)
                for i, t in enumerate(window.tabs)]

    def select_tab(self, hwnd: int, index: int) -> bool:
        window = self.windows.get(hwnd)
        if window is None or not 0 <= index < len(window.tabs):
            return False
        window.selected = index
        return True

    def close_tab(self, hwnd: int, index: int) -> bool:
        window = self.windows.get(hwnd)
        if window is None or not 0 <= index < len(window.tabs):
            return False
        window.closed_tabs.append(window.tabs.pop(index))
        if not window.tabs:
            self._remove(hwnd)
            return True
        window.selected = min(window.selected, len(window.tabs) - 1)
        return True

    def address(self, hwnd: int) -> str | None:
        window = self.windows.get(hwnd)
        if window is None or window.kind != "browser" or not window.tabs:
            return None
        return window.tabs[window.selected].url

    def focused(self) -> FocusInfo | None:
        window = self.windows.get(self.fg)
        if window is None:
            return None
        return FocusInfo("document" if window.kind == "editor" else "pane", window.title, "")

    def focused_text(self, limit: int = 20000) -> str | None:
        window = self.windows.get(self.fg)
        if window is None or window.kind != "editor":
            return None
        return window.text.replace("\n", "\r")[-limit:]  # like RichEdit: paragraphs end with CR

    def invoke(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> str | None:
        window = self.windows.get(hwnd)
        if window is None:
            return None
        if window.dialog and name in window.dialog:
            scope, window.dialog = window.dialog_scope, None
            if name in ("Save", "Don't save"):
                window.unsaved = False
                if scope == "tab":
                    self._close_document(window)
                else:
                    self._remove(hwnd)
            return name
        if window.kind == "browser" and name.lower() == "skip":
            return "Skip Ad"
        return None

    def element_center(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> tuple[int, int] | None:
        return (400, 300) if hwnd in self.windows else None

    def buttons(self, hwnd: int) -> list[str]:
        window = self.windows.get(hwnd)
        return list(window.dialog) if window and window.dialog else ["Minimize", "Close"]

    def page_text(self, hwnd: int, limit: int = 20000) -> str | None:
        window = self.windows.get(hwnd)
        return window.tabs[window.selected].title if window and window.tabs else None

    def describe_ui(self, hwnd: int, limit: int = 150) -> list[dict[str, Any]]:
        return [{"type": "button", "name": "Close", "rect": [0, 0, 10, 10], "offscreen": False}]

    # ------------------------------------------------------------------ media

    def media_sessions(self) -> list[MediaSession]:
        return list(self.sessions)

    def media_command(self, app_id: str, action: str) -> bool:
        self.media_log.append((app_id, action))
        for i, session in enumerate(self.sessions):
            if session.app_id == app_id:
                status = {"pause": "paused", "play": "playing", "stop": "stopped"}.get(action, session.status)
                title = session.title + " (next)" if action == "next" else session.title
                self.sessions[i] = MediaSession(session.app_id, title, session.artist, status, session.is_current,
                                                session.can_next)
                return True
        return False

    # ------------------------------------------------------------------ screen

    def screenshot(self, path: Path, hwnd: int | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake")
        return path
