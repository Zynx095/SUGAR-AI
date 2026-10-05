"""Window management: find, focus, minimise, maximise, restore, close, snap.

Windows are not processes. A process can own many windows (every Brave
window), no windows (background helpers) or windows of another app's frame
(Store apps inside ApplicationFrameHost). Everything here works on *windows*,
found by app name, title, process or a spoken reference ("this", "that"),
and each action is checked afterwards against the real window state.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.context import BROWSER_PROCESSES, DesktopContext, DialogPrompt
from sugar.computer.results import ComputerActionResult

R = ComputerActionResult

# Spoken app names → process stems. Anything not listed still matches by its own process name or title.
APP_PROCESSES: dict[str, tuple[str, ...]] = {
    "brave": ("brave",), "chrome": ("chrome",), "google chrome": ("chrome",), "edge": ("msedge",),
    "microsoft edge": ("msedge",), "firefox": ("firefox",), "opera": ("opera",), "vivaldi": ("vivaldi",),
    "notepad": ("notepad",), "vs code": ("code",), "vscode": ("code",), "visual studio code": ("code",),
    "code": ("code",), "terminal": ("windowsterminal",), "windows terminal": ("windowsterminal",),
    "powershell": ("powershell", "pwsh"), "command prompt": ("cmd",), "cmd": ("cmd",),
    "file explorer": ("explorer",), "explorer": ("explorer",), "files": ("explorer",),
    "spotify": ("spotify",), "discord": ("discord",), "whatsapp": ("whatsapp", "whatsapp.root"),
    "telegram": ("telegram",), "slack": ("slack",), "zoom": ("zoom",), "teams": ("ms-teams", "teams"),
    "word": ("winword",), "excel": ("excel",), "powerpoint": ("powerpnt",), "outlook": ("outlook", "olk"),
    "calculator": ("calculatorapp", "calculator"), "settings": ("systemsettings",), "task manager": ("taskmgr",),
    "paint": ("mspaint",), "snipping tool": ("snippingtool",), "photos": ("photos",), "obs": ("obs64",),
    "steam": ("steam", "steamwebhelper"), "vlc": ("vlc",), "postman": ("postman",), "claude": ("claude",),
    "freellmapi": ("freellmapi",), "riot client": ("riot client", "riotclientux"), "overwolf": ("overwolf",),
    "fxsound": ("fxsound",), "docker": ("docker desktop",), "pycharm": ("pycharm64",), "android studio": ("studio64",),
    "visual studio": ("devenv",), "cursor": ("cursor",), "github desktop": ("githubdesktop",),
}
# Windows Sugar must never close or kill.
PROTECTED_PROCESSES = {"explorer.exe", "dwm.exe", "winlogon.exe", "csrss.exe", "lsass.exe", "services.exe",
                       "svchost.exe", "smss.exe", "wininit.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
                       "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"}
# Classes of File Explorer windows (explorer.exe also owns the taskbar and desktop).
EXPLORER_WINDOW_CLASSES = {"CabinetWClass", "ExploreWClass"}

_SAVE_WORDS = re.compile(r"^(?:save|&save|yes)$", re.I)
_DISCARD_WORDS = re.compile(r"^(?:don'?t save|do not save|discard|no|close without saving|don't save changes)$", re.I)
_CANCEL_WORDS = re.compile(r"^(?:cancel|keep editing|go back)$", re.I)
_CONFIRM_WORDS = re.compile(r"^(?:close all|close tabs|close|ok|yes|quit|leave|exit|end task)$", re.I)


def _norm(text: str) -> str:
    text = text.lower().replace("​", "").replace("&", "and")
    text = re.sub(r"[^a-z0-9 .+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Match:
    window: WindowInfo
    score: float


class WindowManager:
    def __init__(self, backend: DesktopBackend, context: DesktopContext, *, close_timeout_s: float = 4.0,
                 sleep=time.sleep) -> None:
        self._backend = backend
        self._context = context
        self._close_timeout = close_timeout_s
        self._sleep = sleep

    # ------------------------------------------------------------------ discovery

    def list(self) -> list[WindowInfo]:
        return [w for w in self._backend.list_windows() if not self._context.is_own(w)]

    @staticmethod
    def stems_for(name: str) -> tuple[str, ...]:
        query = _norm(name)
        query = re.sub(r"^(?:the|my)\s+", "", query)
        query = re.sub(r"\s+(?:app|application|program|window|browser)$", "", query)
        return APP_PROCESSES.get(query, (query.replace(" ", ""), query))

    def find(self, name: str, windows: list[WindowInfo] | None = None) -> list[WindowInfo]:
        """Windows matching a spoken app name or title, best first (recently used breaks ties)."""
        query = _norm(name)
        query = re.sub(r"^(?:the|my)\s+", "", query)
        query = re.sub(r"\s+(?:app|application|program|window)$", "", query)
        if not query:
            return []
        stems = self.stems_for(query)
        candidates = windows if windows is not None else self.list()
        recency = {w.hwnd: i for i, w in enumerate(self._context.recent_windows())}
        matches: list[Match] = []
        for window in candidates:
            if window.process == "explorer.exe" and window.class_name not in EXPLORER_WINDOW_CLASSES:
                continue
            app = window.app.lower()
            title = _norm(window.title)
            score = 0.0
            if app in stems:
                score = 100
            elif len(app) >= 3 and any(app.startswith(stem) or stem.startswith(app) for stem in stems if len(stem) >= 3):
                score = 85
            elif title.endswith(" - " + query) or title.endswith(" " + query):
                score = 70
            elif re.search(rf"\b{re.escape(query)}\b", title):
                score = 50
            elif len(query) >= 4 and difflib.SequenceMatcher(None, query, app).ratio() >= 0.8:
                score = 40
            if score:
                matches.append(Match(window, score - recency.get(window.hwnd, 50) * 0.01))
        matches.sort(key=lambda m: m.score, reverse=True)
        return [m.window for m in matches]

    def resolve(self, target: str | int | None) -> WindowInfo | None:
        if target is None or target == "":
            self._context.refresh()
            return self._context.active()
        if isinstance(target, int):
            return self._backend.get_window(target)
        reference = self._context.resolve(target)
        if reference is not None:
            return reference
        if target.strip().lower() in {"this", "that", "it", "current", "active"}:
            return None
        found = self.find(target)
        return found[0] if found else None

    # ------------------------------------------------------------------ actions

    def focus(self, target: str | int | None, *, action: str = "focus") -> R:
        window = self.resolve(target)
        if window is None:
            return R.fail(action, str(target), f"I can't find a {target} window." if target else
                          "There's no window to switch to.")
        if self._backend.activate(window.hwnd):
            fresh = self._backend.get_window(window.hwnd) or window
            self._context.note_action(action, fresh)
            return R.ok(action, fresh.app, f"Switched to {display_name(fresh)}.", hwnd=fresh.hwnd)
        return R.fail(action, window.app, f"Windows wouldn't let me bring {display_name(window)} to the front.",
                      "SetForegroundWindow refused", hwnd=window.hwnd)

    def set_state(self, target: str | int | None, state: str) -> R:
        action = f"window.{state}"
        window = self.resolve(target)
        if window is None:
            return R.fail(action, str(target), f"I can't find a {target} window." if target else
                          "There's no window for that.")
        self._backend.set_state(window.hwnd, state)
        deadline = time.monotonic() + 1.5
        fresh = window
        while time.monotonic() < deadline:
            fresh = self._backend.get_window(window.hwnd) or window
            if (state == "minimize" and fresh.minimized) or (state == "maximize" and fresh.maximized) or (
                    state == "restore" and not fresh.minimized and not fresh.maximized):
                break
            self._sleep(0.05)
        verified = (state == "minimize" and fresh.minimized) or (state == "maximize" and fresh.maximized) or (
            state == "restore" and not fresh.minimized)
        if state in ("restore", "maximize"):
            self._backend.activate(window.hwnd)
        self._context.note_action(action, fresh)
        past = {"minimize": "Minimized", "maximize": "Maximized", "restore": "Restored"}[state]
        if not verified:
            return R(True, action, window.app, f"I asked {display_name(window)} to {state}, but it didn't change.",
                     False, None, {"hwnd": window.hwnd})
        return R.ok(action, window.app, f"{past} {display_name(window)}.", hwnd=window.hwnd)

    def close(self, target: str | int | None) -> R:
        window = self.resolve(target)
        if window is None:
            return R.fail("window.close", str(target), f"I can't find a {target} window." if target else
                          "There's no window to close.")
        return self.close_window(window)

    def close_window(self, window: WindowInfo, *, timeout_s: float | None = None) -> R:
        name = display_name(window)
        if window.process in PROTECTED_PROCESSES and window.class_name not in EXPLORER_WINDOW_CLASSES:
            return R.fail("window.close", window.app, f"I won't close {name}; Windows needs it.")
        self._backend.close_window(window.hwnd)
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self._close_timeout)
        checked_prompt = False
        started = time.monotonic()
        while time.monotonic() < deadline:
            if not self._backend.is_window(window.hwnd):
                self._context.forget_window(window.hwnd)
                self._context.note_action("window.close", None)
                if self._context.pending_dialog and self._context.pending_dialog.hwnd == window.hwnd:
                    self._context.pending_dialog = None
                return R.ok("window.close", window.app, f"Closed {name}.", hwnd=window.hwnd)
            if not checked_prompt and time.monotonic() - started > 0.7:
                checked_prompt = True
                prompt = self.detect_prompt(window)
                if prompt is not None:
                    return prompt
            self._sleep(0.08)
        prompt = self.detect_prompt(window)
        if prompt is not None:
            return prompt
        return R.fail("window.close", window.app, f"{name} didn't close. Say \"force close {window.app}\" to end it.",
                      "window still open after WM_CLOSE", hwnd=window.hwnd)

    def detect_prompt(self, window: WindowInfo) -> R | None:
        try:
            buttons = self._backend.buttons(window.hwnd)
        except Exception:
            return None
        name = display_name(window)
        has_save = any(_SAVE_WORDS.match(b) for b in buttons) and any(_DISCARD_WORDS.match(b) for b in buttons)
        has_confirm = any(_CONFIRM_WORDS.match(b) for b in buttons) and any(_CANCEL_WORDS.match(b) for b in buttons)
        if has_save:
            self._context.pending_dialog = DialogPrompt(window.hwnd, name, buttons, "save")
            return R(True, "window.close", window.app,
                     f"{name} has unsaved changes. Should I save them, not save, or cancel?", False, None,
                     {"hwnd": window.hwnd, "waiting_for": "save_prompt", "buttons": buttons[:8]})
        if has_confirm:
            self._context.pending_dialog = DialogPrompt(window.hwnd, name, buttons, "confirm")
            return R(True, "window.close", window.app,
                     f"{name} is asking to confirm closing. Should I go ahead or cancel?", False, None,
                     {"hwnd": window.hwnd, "waiting_for": "confirmation", "buttons": buttons[:8]})
        return None

    def answer_dialog(self, choice: str) -> R:
        prompt = self._context.pending_dialog
        if prompt is None or not self._backend.is_window(prompt.hwnd):
            self._context.pending_dialog = None
            return R.fail("dialog.answer", None, "There's no question waiting for an answer.")
        patterns = {"save": _SAVE_WORDS, "discard": _DISCARD_WORDS, "cancel": _CANCEL_WORDS, "confirm": _CONFIRM_WORDS}
        pattern = patterns.get(choice)
        button = next((b for b in prompt.buttons if pattern and pattern.match(b)), None)
        if button is None and choice == "confirm" and prompt.kind == "save":
            button = next((b for b in prompt.buttons if _SAVE_WORDS.match(b)), None)
        if button is None:
            return R.fail("dialog.answer", prompt.app, f"I couldn't find a {choice} button.")
        invoked = self._backend.invoke(prompt.hwnd, button, ("button",))
        if invoked is None:
            for owned in self._backend.owned_windows(prompt.hwnd):
                invoked = self._backend.invoke(owned.hwnd, button, ("button",))
                if invoked:
                    break
        if invoked is None:
            return R.fail("dialog.answer", prompt.app, f"I couldn't press {button}.")
        self._context.pending_dialog = None
        if choice == "cancel":
            return R.ok("dialog.answer", prompt.app, f"Cancelled. {prompt.app} stays open.", button=button)
        owner = self._backend.get_window(prompt.hwnd)
        deadline = time.monotonic() + self._close_timeout
        while time.monotonic() < deadline:
            self._sleep(0.1)
            if not self._backend.is_window(prompt.hwnd):
                self._context.forget_window(prompt.hwnd)
                verb = "Saved and closed" if choice == "save" else "Closed"
                return R.ok("dialog.answer", prompt.app, f"{verb} {prompt.app}.", button=button)
            front = self._backend.get_window(self._backend.foreground())
            if (choice == "save" and front is not None and owner is not None and front.hwnd != owner.hwnd
                    and front.pid == owner.pid and "save" in front.title.lower()):
                return R(True, "dialog.answer", prompt.app, f"{prompt.app} is asking where to save it. Tell me a "
                         "file name, or pick a folder.", False, None, {"button": button, "waiting_for": "save_dialog"})
            remaining = self._backend.buttons(prompt.hwnd)
            if not any(_SAVE_WORDS.match(b) for b in remaining) or not any(_DISCARD_WORDS.match(b) for b in remaining):
                # The prompt is gone but the window stays: a tab closed and others remain.
                verb = "Saved and closed" if choice == "save" else "Closed"
                return R.ok("dialog.answer", prompt.app, f"{verb} the document in {prompt.app}.", button=button)
        return R(True, "dialog.answer", prompt.app, f"Pressed {button}.", False, None, {"button": button})

    def snap(self, target: str | int | None, where: str) -> R:
        window = self.resolve(target)
        if window is None:
            return R.fail("window.snap", str(target), "There's no window to move.")
        left, top, right, bottom = self._backend.work_area(window.hwnd)
        width, height = right - left, bottom - top
        layouts = {
            "left": (left, top, width // 2, height), "right": (left + width // 2, top, width - width // 2, height),
            "top": (left, top, width, height // 2), "bottom": (left, top + height // 2, width, height - height // 2),
            "center": (left + width // 6, top + height // 8, width * 2 // 3, height * 3 // 4),
        }
        if where == "maximize":
            return self.set_state(window.hwnd, "maximize")
        if where not in layouts:
            return R.fail("window.snap", window.app, f"I don't know how to put a window {where}.")
        x, y, w, h = layouts[where]
        self._backend.set_rect(window.hwnd, x, y, w, h)
        self._sleep(0.15)
        fresh = self._backend.get_window(window.hwnd) or window
        fl, ft, fr, fb = fresh.rect
        verified = abs(fl - x) <= 12 and abs(ft - y) <= 12 and abs((fr - fl) - w) <= 24
        self._context.note_action("window.snap", fresh)
        details = f"Moved {display_name(window)} to the {where}." if where != "center" else \
            f"Centered {display_name(window)}."
        return R(True, "window.snap", window.app, details, verified, None, {"hwnd": window.hwnd})

    def move_resize(self, target: str | int | None, x: int | None, y: int | None, width: int | None,
                    height: int | None) -> R:
        window = self.resolve(target)
        if window is None:
            return R.fail("window.move", str(target), "There's no window to move.")
        left, top, right, bottom = window.rect
        nx, ny = x if x is not None else left, y if y is not None else top
        nw, nh = width if width is not None else right - left, height if height is not None else bottom - top
        if nw < 120 or nh < 80:
            return R.fail("window.move", window.app, "That's too small for a window.")
        self._backend.set_rect(window.hwnd, nx, ny, nw, nh)
        self._sleep(0.15)
        fresh = self._backend.get_window(window.hwnd) or window
        verified = abs(fresh.rect[0] - nx) <= 12 and abs((fresh.rect[2] - fresh.rect[0]) - nw) <= 24
        self._context.note_action("window.move", fresh)
        return R(True, "window.move", window.app, f"Moved {display_name(window)}.", verified, None,
                 {"hwnd": window.hwnd})

    def summary(self) -> R:
        windows = self.list()
        names: list[str] = []
        for window in windows:
            label = display_name(window)
            if label not in names:
                names.append(label)
        if not names:
            return R.ok("window.list", None, "No app windows are open.", windows=[])
        spoken = ", ".join(names[:7]) + (f" and {len(names) - 7} more" if len(names) > 7 else "")
        listing = [{"app": w.app, "title": w.title, "hwnd": w.hwnd, "minimized": w.minimized} for w in windows]
        return R.ok("window.list", None, f"Open: {spoken}.", windows=listing)


_DISPLAY_NAMES = {
    "brave": "Brave", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "notepad": "Notepad",
    "code": "VS Code", "windowsterminal": "Terminal", "explorer": "File Explorer", "spotify": "Spotify",
    "discord": "Discord", "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook",
    "calculatorapp": "Calculator", "systemsettings": "Settings", "taskmgr": "Task Manager", "mspaint": "Paint",
    "cmd": "Command Prompt", "powershell": "PowerShell", "pwsh": "PowerShell", "freellmapi": "FreeLLMAPI",
    "whatsapp": "WhatsApp", "ms-teams": "Teams", "obs64": "OBS", "steam": "Steam", "vlc": "VLC",
}


def display_name(window: WindowInfo) -> str:
    name = _DISPLAY_NAMES.get(window.app.lower())
    if name:
        return name
    title = window.title.strip()
    for separator in (" - ", " — ", " | "):
        if separator in title:
            return title.rsplit(separator, 1)[-1].strip() or window.app
    return title or window.app


def is_browser(window: WindowInfo | None) -> bool:
    return bool(window and window.process in BROWSER_PROCESSES)
