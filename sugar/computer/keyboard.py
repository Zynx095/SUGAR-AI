"""Keyboard control: real-time typing, fast paste, shortcuts and editing commands.

How a character reaches an app matters. Measured on Windows 11 Notepad, input
injected in one batch is processed with the keyboard state of *later*
events: Shift arrives released (")" becomes "0") and Unicode packets repeat
("Hello gggar"). So every key event is sent on its own with a short gap, and
characters go through the target window's own keyboard layout as real key
presses (scan codes included), falling back to Unicode packets only for
characters the layout can't produce (é, ✓, emoji) or that sit on dead keys.

Before every character Sugar checks that the target window still has focus;
if the user switches windows mid-sentence, typing stops instead of spilling
into whatever is now in front.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.context import DesktopContext
from sugar.computer.keys import (
    GLOBAL_KEYS,
    MODIFIERS,
    VK_BACK,
    VK_CONTROL,
    VK_END,
    VK_HOME,
    VK_LEFT,
    VK_RETURN,
    VK_SHIFT,
    VK_TAB,
    describe_combo,
    parse_combo,
)
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import WindowManager, display_name

R = ComputerActionResult

CODE_EDITORS = {"code", "cursor", "pycharm64", "idea64", "webstorm64", "devenv", "studio64", "sublime_text",
                "notepad++", "zed", "windowsterminal", "powershell", "pwsh", "cmd"}


@dataclass
class TypingSettings:
    mode: str = "auto"  # auto | realtime | paste
    char_interval_s: float = 0.006  # pause between characters in real-time mode
    key_gap_s: float = 0.004  # pause between individual key events
    unicode_interval_s: float = 0.02  # extra pause after a Unicode packet
    paste_threshold: int = 300  # auto mode pastes text longer than this
    restore_clipboard: bool = True
    verify: bool = True


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class KeyboardController:
    def __init__(self, backend: DesktopBackend, windows: WindowManager, context: DesktopContext,
                 settings: TypingSettings | None = None, cancel: threading.Event | None = None,
                 sleep=time.sleep) -> None:
        self._backend = backend
        self._windows = windows
        self._context = context
        self.settings = settings or TypingSettings()
        self._cancel = cancel or threading.Event()
        self._sleep = sleep
        self._held: list[int] = []

    # ------------------------------------------------------------------ targeting

    def focus_target(self, target: str | int | None, action: str) -> tuple[WindowInfo | None, R | None]:
        window = self._windows.resolve(target)
        if window is None:
            what = f"a {target} window" if target and str(target).lower() not in {"this", "that", "it"} else "a window"
            return None, R.fail(action, str(target) if target else None, f"I can't find {what} to type into.")
        if self._backend.foreground() != window.hwnd:
            if not self._backend.activate(window.hwnd):
                return None, R.fail(action, window.app, f"I couldn't bring {display_name(window)} to the front.",
                                    "focus refused")
            self._sleep(0.12)  # let the app move keyboard focus into its editor
        elevated = self._backend.is_elevated(window.pid) if hasattr(self._backend, "is_elevated") else None
        self_elevated = getattr(self._backend, "self_elevated", lambda: False)()
        if elevated and not self_elevated:
            return None, R.fail(action, window.app, f"{display_name(window)} is running as administrator, so "
                                "Windows blocks my keystrokes. Run Sugar as administrator to control it.",
                                "target process is elevated")
        return window, None

    # ------------------------------------------------------------------ primitives

    def _gap(self) -> None:
        self._sleep(self.settings.key_gap_s)

    def _tap(self, vk: int) -> None:
        self._backend.key(vk, up=False)
        self._gap()
        self._backend.key(vk, up=True)
        self._gap()

    def chord(self, modifiers: list[int], vk: int | None) -> None:
        pressed: list[int] = []
        try:
            for modifier in modifiers:
                self._backend.key(modifier, up=False)
                pressed.append(modifier)
                self._gap()
            if vk is not None:
                self._tap(vk)
        finally:
            for modifier in reversed(pressed):
                self._backend.key(modifier, up=True)
                self._gap()

    def _type_char(self, char: str, hwnd: int, caps: bool) -> None:
        if char == "\n":
            self._tap(VK_RETURN)
            return
        if char == "\t":
            self._tap(VK_TAB)
            return
        chord = self._backend.chord_for(char, hwnd) if char.isprintable() else None
        if chord is not None:
            modifiers = list(chord.modifiers)
            if caps and char.isalpha() and char.lower() != char.upper():  # Caps Lock flips Shift for letters
                if VK_SHIFT in modifiers:
                    modifiers.remove(VK_SHIFT)
                else:
                    modifiers.insert(0, VK_SHIFT)
            self.chord(modifiers, chord.vk)
            return
        encoded = char.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[i:i + 2], "little")
            self._backend.unicode(unit, up=False)
            self._gap()
            self._backend.unicode(unit, up=True)
            self._gap()
        self._sleep(self.settings.unicode_interval_s)

    # ------------------------------------------------------------------ typing

    def _read_back(self) -> str | None:
        if not self.settings.verify:
            return None
        try:
            text = self._backend.focused_text(200000)
        except Exception:
            return None
        return _normalize(text) if text is not None else None

    def type_text(self, text: str, *, target: str | int | None = None, mode: str | None = None,
                  interval_ms: float | None = None) -> R:
        text = _normalize(text)
        if not text:
            return R.fail("keyboard.type", None, "There's nothing to type.")
        self._cancel.clear()
        window, error = self.focus_target(target, "keyboard.type")
        if error is not None:
            return error
        assert window is not None
        chosen = (mode or self.settings.mode).lower()
        if chosen not in ("realtime", "paste"):
            multiline_code = "\n" in text and window.app.lower() in CODE_EDITORS
            chosen = "paste" if len(text) > self.settings.paste_threshold or multiline_code else "realtime"
        before = self._read_back()
        if chosen == "paste":
            typed, problem = self._paste(text, window)
        else:
            interval = self.settings.char_interval_s if interval_ms is None else max(0.0, interval_ms / 1000)
            typed, problem = self._type_realtime(text, window, interval)
        name = display_name(window)
        self._context.note_action("keyboard.type", window)
        if problem == "focus":
            return R.fail("keyboard.type", window.app, f"I stopped typing after {typed} of {len(text)} characters "
                          f"because {name} lost focus.", "target lost focus", chars=typed, mode=chosen)
        if problem == "cancelled":
            return R.fail("keyboard.type", window.app, f"Stopped typing after {typed} of {len(text)} characters.",
                          "cancelled", chars=typed, mode=chosen)
        if problem:
            return R.fail("keyboard.type", window.app, f"I couldn't type into {name}.", problem, chars=typed)
        after = self._read_back()
        verified = after is not None and (after.count(text) > (before or "").count(text) or
                                          after.endswith(text) and before != after)
        details = f"Typed {len(text)} characters into {name}."
        return R(True, "keyboard.type", window.app, details, verified, None,
                 {"chars": len(text), "mode": chosen, "hwnd": window.hwnd, "read_back": after is not None})

    def _type_realtime(self, text: str, window: WindowInfo, interval: float) -> tuple[int, str | None]:
        caps = bool(self._backend.caps_lock())
        typed = 0
        for char in text:
            if self._cancel.is_set():
                return typed, "cancelled"
            if self._backend.foreground() != window.hwnd:
                return typed, "focus"
            self._type_char(char, window.hwnd, caps)
            typed += 1
            if interval:
                self._sleep(interval)
        return typed, None

    def type_fast(self, text: str, hwnd: int, gap_s: float = 0.0015) -> bool:
        """Short machine text (URLs, file names) into a field that handles fast input, e.g. a browser omnibox."""
        saved = self.settings.key_gap_s
        self.settings.key_gap_s = gap_s
        try:
            for char in text:
                if self._backend.foreground() != hwnd:
                    return False
                self._type_char(char, hwnd, False)
            return True
        finally:
            self.settings.key_gap_s = saved

    def tap(self, vk: int) -> None:
        self._tap(vk)

    def _paste(self, text: str, window: WindowInfo) -> tuple[int, str | None]:
        snapshot = self._backend.clipboard_snapshot() if self.settings.restore_clipboard else None
        try:
            if not self._backend.set_clipboard_text(text) or self._backend.clipboard_text() != text:
                return 0, "the clipboard was busy"
            if self._backend.foreground() != window.hwnd:
                return 0, "focus"
            self.chord([VK_CONTROL], ord("V"))
            self._sleep(0.3 + min(1.5, len(text) / 50000))  # the app reads the clipboard asynchronously
            return len(text), None
        finally:
            if snapshot is not None:
                self._backend.restore_clipboard(snapshot)

    def write(self, text: str, *, target: str | int | None = None) -> R:
        """Fast path for bulk text: clipboard paste, still focused, verified and clipboard-restoring."""
        return self.type_text(text, target=target, mode="paste")

    # ------------------------------------------------------------------ keys and shortcuts

    def press(self, keys: str | list[str], *, target: str | int | None = None, times: int = 1) -> R:
        vks = parse_combo(keys)
        label = describe_combo(vks)
        modifiers = [vk for vk in vks if vk in MODIFIERS]
        main = next((vk for vk in vks if vk not in MODIFIERS), None)
        times = max(1, min(50, int(times)))
        if all(vk in GLOBAL_KEYS for vk in vks):
            for _ in range(times):
                self.chord(modifiers, main)
            return R.ok("keyboard.press", None, f"Pressed {label}.", keys=label)
        window, error = self.focus_target(target, "keyboard.press")
        if error is not None:
            return error
        assert window is not None
        for _ in range(times):
            if self._backend.foreground() != window.hwnd:
                return R.fail("keyboard.press", window.app, f"{display_name(window)} lost focus, so I stopped.",
                              "target lost focus")
            self.chord(modifiers, main)
            self._sleep(0.03)
        self._context.note_action("keyboard.press", window)
        suffix = "" if times == 1 else " twice" if times == 2 else f" {times} times"
        return R.ok("keyboard.press", window.app, f"Pressed {label}{suffix}.", keys=label, times=times,
                    hwnd=window.hwnd)

    def hotkey(self, *keys: str, target: str | int | None = None) -> R:
        return self.press(list(keys), target=target)

    def hold(self, keys: str | list[str]) -> list[int]:
        """Press and keep holding (release with :meth:`release`); returns the held keys."""
        for vk in parse_combo(keys):
            if vk not in self._held:
                self._backend.key(vk, up=False)
                self._held.append(vk)
                self._gap()
        return list(self._held)

    def release(self, keys: str | list[str] | None = None) -> None:
        targets = parse_combo(keys) if keys else list(self._held)
        for vk in reversed(targets):
            if vk in self._held:
                self._backend.key(vk, up=True)
                self._held.remove(vk)
                self._gap()

    def release_all(self) -> None:
        self.release(None)

    # ------------------------------------------------------------------ editing commands

    def edit(self, operation: str, *, target: str | int | None = None) -> R:
        action = f"keyboard.{operation}"
        window, error = self.focus_target(target, action)
        if error is not None:
            return error
        assert window is not None
        before = self._read_back()
        name = display_name(window)
        if operation == "delete_last_line":
            self.chord([VK_CONTROL], VK_END)
            if before is not None:
                stripped = before.rstrip("\n")
                trailing = len(before) - len(stripped)
                last_line = stripped.rsplit("\n", 1)[-1]
                has_previous = "\n" in stripped
                count = trailing + len(last_line) + (1 if has_previous else 0)
                if count <= 400:
                    for _ in range(count):
                        self.chord([VK_SHIFT], VK_LEFT)
                    if count:
                        self._tap(VK_BACK)
                else:
                    self.chord([VK_SHIFT], VK_HOME)
                    self._tap(VK_BACK)
                    self._tap(VK_BACK)
                expected = stripped.rsplit("\n", 1)[0] if has_previous else ""
            else:
                self.chord([VK_SHIFT], VK_HOME)
                self._tap(VK_BACK)
                self._tap(VK_BACK)
                expected = None
            details = f"Deleted the last line in {name}."
        elif operation == "delete_last_word":
            self.chord([VK_CONTROL], VK_BACK)
            expected = None
            details = "Deleted the last word."
        elif operation == "clear_all":
            self.chord([VK_CONTROL], ord("A"))
            self._tap(0x2E)
            expected = ""
            details = f"Cleared everything in {name}."
        elif operation == "select_all":
            self.chord([VK_CONTROL], ord("A"))
            expected = None
            details = "Selected everything."
        elif operation == "go_to_end":
            self.chord([VK_CONTROL], VK_END)
            expected = None
            details = "At the end."
        elif operation == "go_to_start":
            self.chord([VK_CONTROL], VK_HOME)
            expected = None
            details = "At the top."
        else:
            return R.fail(action, window.app, f"I don't know how to {operation.replace('_', ' ')}.")
        self._sleep(0.1)
        after = self._read_back()
        verified = expected is not None and after is not None and after.rstrip("\n") == expected.rstrip("\n")
        self._context.note_action(action, window)
        return R(True, action, window.app, details, verified, None, {"hwnd": window.hwnd})

    def save(self, *, target: str | int | None = None) -> R:
        window, error = self.focus_target(target, "keyboard.save")
        if error is not None:
            return error
        assert window is not None
        name = display_name(window)
        self.chord([VK_CONTROL], ord("S"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self._sleep(0.15)
            front = self._backend.foreground()
            if front and front != window.hwnd:
                dialog = self._backend.get_window(front)
                if dialog is not None and dialog.pid == window.pid and "save" in dialog.title.lower():
                    self._context.note_action("keyboard.save", window)
                    return R(True, "keyboard.save", window.app, f"{name} is asking where to save it. Tell me a "
                             "file name, or pick a folder.", False, None, {"waiting_for": "save_dialog"})
            fresh = self._backend.get_window(window.hwnd)
            if fresh is not None and not fresh.title.lstrip().startswith(("*", "●")) and _saved_tab(self._backend,
                                                                                                      window.hwnd):
                self._context.note_action("keyboard.save", fresh)
                return R.ok("keyboard.save", window.app, f"Saved in {name}.", hwnd=window.hwnd)
        self._context.note_action("keyboard.save", window)
        return R(True, "keyboard.save", window.app, f"Pressed save in {name}.", False, None, {"hwnd": window.hwnd})


def _saved_tab(backend: DesktopBackend, hwnd: int) -> bool:
    """Editors with tabs (Notepad) mark them Modified/Unmodified; without tabs, trust the title."""
    try:
        tabs = backend.tabs(hwnd)
    except Exception:
        return True
    selected = next((t for t in tabs if t.selected), None)
    if selected is None:
        return True
    name = selected.name.lower()
    return "unmodified" in name or "modified" not in name
