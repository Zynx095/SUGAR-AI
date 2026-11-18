"""Click, select and find things on screen by description — the visual interaction pipeline.

1. the active app's window (dialogs it owns first),
2. a fresh UI Automation snapshot of its elements,
3. resolve the description to exactly one element (:mod:`sugar.computer.elements`),
4. act through UI Automation patterns (no mouse) — or a mouse click at the element's *current* centre,
5. verify: the element's state changed, focus moved, a window appeared, or the UI changed.

When accessibility data has no match, the words on screen are read with OCR and, failing that, a vision model
is asked where the target is; both work from a screenshot taken right then, never from old coordinates.
Several equally good matches → SID asks which one (and "the first one" answers it); none → it says so.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.context import DesktopContext
from sugar.computer.elements import Choice, Element, choose, describe_position, parse_target
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import WindowManager, display_name

R = ComputerActionResult

ACTIONS = ("click", "select", "toggle", "focus", "expand")


@dataclass
class PendingChoice:
    window: WindowInfo
    candidates: list[Element]
    action: str
    at: float

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.at < 60


class InteractionController:
    def __init__(self, backend: DesktopBackend, windows: WindowManager, context: DesktopContext, *,
                 ocr: Callable[[WindowInfo], list[Element]] | None = None,
                 locate: Callable[[WindowInfo, str], tuple[int, int] | None] | None = None,
                 sleep=time.sleep) -> None:
        self._backend = backend
        self._windows = windows
        self._context = context
        self.ocr = ocr
        self.locate = locate
        self._sleep = sleep
        self.pending: PendingChoice | None = None

    # ------------------------------------------------------------------ finding

    def _window(self, target: str | int | None) -> WindowInfo | None:
        window = self._windows.resolve(target)
        if window is None or self._context.is_own(window):
            window = self._context.active()
        return window

    def _scopes(self, window: WindowInfo) -> list[WindowInfo]:
        """Dialogs the window owns come first: "click Save" in a save prompt means the prompt's button."""
        owned = []
        try:
            owned = [w for w in self._backend.owned_windows(window.hwnd) if w.visible]
        except Exception:
            pass
        return [*owned, window]

    def _snapshot(self, window: WindowInfo) -> list[Element]:
        try:
            return list(self._backend.snapshot(window.hwnd))
        except Exception:
            return []

    def find(self, phrase: str, target: str | int | None = None) -> tuple[WindowInfo | None, Choice]:
        window = self._window(target)
        if window is None:
            return None, Choice(None, reason="no window")
        spec = parse_target(phrase)
        for scope in self._scopes(window):
            elements = self._snapshot(scope)
            choice = choose(elements, spec, scope.rect)
            if choice.element is not None or choice.ambiguous:
                return scope, choice
        if self.ocr is not None and spec.text:
            try:
                words = self.ocr(window)
            except Exception:
                words = []
            choice = choose(words, spec, window.rect)
            if choice.element is not None or choice.ambiguous:
                return window, choice
        if self.locate is not None:
            try:
                point = self.locate(window, phrase)
            except Exception:
                point = None
            if point is not None:
                x, y = point
                element = Element(-1, spec.text or phrase, "unknown", (x - 2, y - 2, x + 2, y + 2), source="vision")
                return window, Choice(element)
        return window, Choice(None, reason="no match")

    # ------------------------------------------------------------------ acting

    def _signature(self, window: WindowInfo) -> str:
        elements = self._snapshot(window)[:400]
        text = "|".join(f"{e.kind}:{e.name}:{e.toggled}:{e.selected}:{e.expanded}" for e in elements)
        return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()

    def _mouse_click(self, x: int, y: int, double: bool = False) -> None:
        before = self._backend.cursor()
        self._backend.mouse_move(x, y)
        self._sleep(0.03)
        for _ in range(2 if double else 1):
            self._backend.mouse_button("left", up=False)
            self._sleep(0.02)
            self._backend.mouse_button("left", up=True)
            self._sleep(0.05)
        self._backend.mouse_move(*before)  # give the pointer back where the user left it

    def click(self, phrase: str, target: str | int | None = None, action: str = "click") -> R:
        """Find the described element and activate it, then check that something happened."""
        window, choice = self.find(phrase, target)
        if window is None:
            return R.fail("ui.click", None, "There's no window to click in.")
        app = display_name(window)
        if choice.ambiguous:
            self.pending = PendingChoice(window, choice.candidates[:6], action, time.monotonic())
            listed = self._list(choice.candidates[:4], window)
            return R(False, "ui.click", window.app, f"I see {len(choice.candidates)} matches in {app}: {listed}. "
                     "Which one?", False, None, {"ambiguous": [e.to_dict() for e in choice.candidates[:6]],
                                                 "waiting_for": "choice"})
        if choice.element is None:
            more = f" {choice.reason}." if choice.reason.startswith("only") else ""
            return R.fail("ui.click", window.app, f"I can't find {phrase} in {app}.{more}")
        return self._activate(window, choice.element, action)

    def choose_pending(self, ordinal: int) -> R:
        """Answer to "which one?": "the first one", "the second", "the last one"."""
        pending, self.pending = self.pending, None
        if pending is None or not pending.fresh:
            return R.fail("ui.click", None, "There's nothing waiting for a choice.")
        index = ordinal - 1 if ordinal > 0 else len(pending.candidates) + ordinal
        if not 0 <= index < len(pending.candidates):
            return R.fail("ui.click", pending.window.app, f"There were only {len(pending.candidates)}.")
        wanted = pending.candidates[index]
        # Re-find it in a fresh snapshot (the window may have moved): same name, kind and nearest position.
        fresh = [e for e in self._snapshot(pending.window) if e.name == wanted.name and e.kind == wanted.kind]
        if not fresh:
            return R.fail("ui.click", pending.window.app, f"{wanted.label} is gone.")
        cx, cy = wanted.center
        element = min(fresh, key=lambda e: abs(e.center[0] - cx) + abs(e.center[1] - cy))
        return self._activate(pending.window, element, pending.action)

    def _activate(self, window: WindowInfo, element: Element, action: str) -> R:
        app = display_name(window)
        label = element.label
        before_sig = self._signature(window) if element.source == "uia" else ""
        before_fg = self._backend.foreground()
        before_windows = {w.hwnd for w in self._backend.list_windows()}
        current = self._backend.get_window(window.hwnd)
        if current is not None and current.rect != window.rect:
            # The window moved since it was resolved: re-find the element so no stale coordinate is used.
            return self.click(element.name or label, window.hwnd, action)
        method = None
        if element.source == "uia" and element.id >= 0:
            if action == "focus" or element.kind in ("edit", "combobox", "document") and action == "click":
                method = self._backend.act(window.hwnd, element.id, "focus")
            else:
                wanted = {"select": "select", "toggle": "toggle", "expand": "expand"}.get(action, "click")
                method = self._backend.act(window.hwnd, element.id, wanted)
                if method == "focus" and element.kind not in ("edit", "combobox"):
                    method = None  # focus alone doesn't click a text label: use the mouse
        if method is None:
            if self._backend.foreground() != window.hwnd:
                self._backend.activate(window.hwnd)
                self._sleep(0.15)
            x, y = element.center
            self._mouse_click(x, y)
            method = "mouse"
        self._context.note_action("ui.click", window)
        verified, how = self._verify(window, element, before_sig, before_fg, before_windows, action)
        verb = {"select": "Selected", "toggle": "Toggled", "focus": "Put the cursor in", "expand": "Opened"}.get(
            action, "Clicked")
        if element.kind == "checkbox" and element.toggled is not None:
            verb = "Unchecked" if element.toggled else "Checked"
        if element.kind in ("edit", "combobox") and action == "click":
            verb = "Put the cursor in"
        summary = f"{verb} {label}." if verified else f"{verb} {label}, but nothing visibly changed in {app}."
        return R(True, "ui.click", window.app, summary, verified, None,
                 {"element": element.to_dict(), "method": method, "verified_by": how, "source": element.source})

    def _verify(self, window: WindowInfo, element: Element, before_sig: str, before_fg: int,
                before_windows: set[int], action: str) -> tuple[bool, str]:
        for _ in range(4):
            self._sleep(0.15)
            if element.source == "uia" and element.id >= 0:
                try:
                    state = self._backend.element_state(window.hwnd, element.id) or {}
                except Exception:
                    state = {}
                if element.toggled is not None and state.get("toggled") is not None \
                        and state["toggled"] != element.toggled:
                    return True, "toggle state"
                if state.get("selected") and not element.selected:
                    return True, "selection"
                if element.expanded is not None and state.get("expanded") is not None \
                        and state["expanded"] != element.expanded:
                    return True, "expanded"
                if state.get("focused") and element.kind in ("edit", "combobox", "document"):
                    return True, "focus"
            if self._backend.foreground() != before_fg:
                return True, "foreground window"
            if {w.hwnd for w in self._backend.list_windows()} != before_windows:
                return True, "windows"
            if before_sig and self._signature(window) != before_sig:
                return True, "interface changed"
        return False, "no visible change"

    # ------------------------------------------------------------------ describing

    def _list(self, elements: list[Element], window: WindowInfo) -> str:
        parts = [f"{e.label} {describe_position(e, window.rect)}" for e in elements]
        return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else ""

    def where(self, phrase: str, target: str | int | None = None) -> R:
        window, choice = self.find(phrase, target)
        if window is None:
            return R.fail("ui.where", None, "There's no window to look in.")
        app = display_name(window)
        if choice.ambiguous:
            return R.ok("ui.where", window.app, f"There are {len(choice.candidates)}: "
                        f"{self._list(choice.candidates[:4], window)} of {app}.",
                        candidates=[e.to_dict() for e in choice.candidates[:6]])
        if choice.element is None:
            return R.fail("ui.where", window.app, f"I can't see {phrase} in {app}.")
        element = choice.element
        source = {"ocr": " (read from the screen)", "vision": " (by looking at the screen)"}.get(element.source, "")
        return R.ok("ui.where", window.app, f"{element.label[:1].upper() + element.label[1:]} is "
                    f"{describe_position(element, window.rect)} of {app}{source}.", element=element.to_dict())

    def controls(self, target: str | int | None = None, limit: int = 40) -> R:
        window = self._window(target)
        if window is None:
            return R.fail("ui.controls", None, "There's no window to look at.")
        elements = [e for e in self._snapshot(window) if e.name.strip() and not e.offscreen and e.area > 0
                    and e.kind in ("button", "link", "checkbox", "radiobutton", "tabitem", "menuitem", "edit",
                                   "combobox", "listitem", "splitbutton")]
        names = []
        for element in elements:
            if element.label not in names:
                names.append(element.label)
        app = display_name(window)
        if not names:
            return R.ok("ui.controls", window.app, f"{app} doesn't expose any named controls.", controls=[])
        spoken = ", ".join(names[:8])
        return R.ok("ui.controls", window.app, f"In {app} I can see {spoken}" + (" and more." if len(names) > 8
                                                                                  else "."),
                    controls=names[:limit])
