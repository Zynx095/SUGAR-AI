"""Scrolling in any app: the right pane, the right amount, continuous until "stop scrolling".

The target is a scrollable element from a fresh UI Automation snapshot of the app in front, not "the main
window": a region ("in the left panel"), the document around the cursor ("inside this document"), or the
biggest scrollable area. Scrolling goes through UIA's ScrollPattern (no mouse, no focus change); apps
without it get mouse-wheel input at the target's centre, with the pointer put back afterwards.

Amounts: "a little" ≈ two wheel notches, plain "scroll down" ≈ five, "more"/"a lot" a page, "to the bottom".
Continuous scrolling runs on its own thread until "stop scrolling", the end of the content, 2 minutes, or
the user touching the mouse or keyboard.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.context import DesktopContext
from sugar.computer.elements import Element, _in_region
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import WindowManager, display_name

R = ComputerActionResult

LARGE_BACK, SMALL_BACK, NONE, LARGE_ON, SMALL_ON = 0, 1, 2, 3, 4
AMOUNTS = {"little": (SMALL_ON, 2, 2), "normal": (SMALL_ON, 5, 5), "more": (LARGE_ON, 1, 10),
           "page": (LARGE_ON, 1, 10)}  # amount → (UIA step, repeats, wheel notches)
SPEEDS = {"slow": 0.45, "normal": 0.22, "fast": 0.1}  # seconds between small steps when continuous


class ScrollController:
    def __init__(self, backend: DesktopBackend, windows: WindowManager, context: DesktopContext,
                 idle: Callable[[], float] | None = None, sleep=time.sleep) -> None:
        self._backend = backend
        self._windows = windows
        self._context = context
        self._idle = idle  # seconds since the last input (to notice the user taking over)
        self._sleep = sleep
        self._continuous: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_target: tuple[int, int] | None = None  # (hwnd, element id) of the last scroll

    # ------------------------------------------------------------------ choosing the target

    def _window(self, target: str | int | None) -> WindowInfo | None:
        window = self._windows.resolve(target) if target is not None else None
        if window is None or self._context.is_own(window):
            window = self._context.active()
        return window

    def _scrollables(self, window: WindowInfo) -> list[Element]:
        try:
            elements = self._backend.snapshot(window.hwnd)
        except Exception:
            return []
        return [e for e in elements if "scroll" in e.patterns and e.area > 0 and not e.offscreen]

    def target(self, window: WindowInfo, *, region: str | None = None, inside: str | None = None,
               horizontal: bool = False) -> Element | None:
        candidates = self._scrollables(window)
        usable = []
        for element in candidates:
            info = self._backend.scroll_info(window.hwnd, element.id) or {}
            if info.get("horizontal" if horizontal else "vertical"):
                usable.append(element)
        if region:
            usable = [e for e in usable if _in_region(e, region, window.rect)]
        if inside:
            try:
                focused = self._backend.focused_scrollable(window.hwnd)
            except Exception:
                focused = None
            if focused is not None:
                match = [e for e in usable if e.rect == tuple(focused)]
                if match:
                    return match[0]
        return max(usable, key=lambda e: e.area) if usable else None

    # ------------------------------------------------------------------ one-off scrolling

    def scroll(self, direction: str = "down", amount: str = "normal", *, region: str | None = None,
               inside: str | None = None, target: str | int | None = None) -> R:
        self.stop_continuous()
        window = self._window(target)
        if window is None:
            return R.fail("ui.scroll", None, "There's nothing in front to scroll.")
        app = display_name(window)
        horizontal = direction in ("left", "right")
        element = self.target(window, region=region, inside=inside, horizontal=horizontal)
        where = f" the {region} side of {app}" if region else f" {app}"
        if element is not None:
            before = self._backend.scroll_info(window.hwnd, element.id) or {}
            key = "h_percent" if horizontal else "v_percent"
            if direction in ("top", "bottom"):
                ok = self._backend.scroll(window.hwnd, element.id, v_percent=0.0 if direction == "top" else 100.0)
            else:
                step, repeats, _ = AMOUNTS.get(amount, AMOUNTS["normal"])
                forward = direction in ("down", "right")
                step = step if forward else {SMALL_ON: SMALL_BACK, LARGE_ON: LARGE_BACK}[step]
                ok = False
                for _ in range(repeats):
                    kwargs = {"horizontal": step} if horizontal else {"vertical": step}
                    ok = self._backend.scroll(window.hwnd, element.id, **kwargs) or ok
                    self._sleep(0.03)
            after = self._backend.scroll_info(window.hwnd, element.id) or {}
            if ok:
                self.last_target = (window.hwnd, element.id)
                self._context.note_action("ui.scroll", window)
                moved = before.get(key) != after.get(key)
                at_edge = after.get(key) in (0.0, 100.0)
                if not moved and at_edge:
                    edge = {"down": "bottom", "up": "top", "bottom": "bottom", "top": "top", "left": "left edge",
                            "right": "right edge"}[direction]
                    return R.ok("ui.scroll", window.app, f"Already at the {edge}.", verified=True,
                                percent=after.get(key))
                words = {"top": "At the top", "bottom": "At the bottom"}.get(direction, f"Scrolled {direction}")
                return R.ok("ui.scroll", window.app, f"{words}{'' if direction in ('top', 'bottom') else ''}.",
                            verified=moved, percent=after.get(key), method="uia", region=region)
        # No ScrollPattern: the mouse wheel over the target area (or keyboard for top/bottom).
        return self._wheel(window, direction, amount, region, where)

    def _wheel(self, window: WindowInfo, direction: str, amount: str, region: str | None, where: str) -> R:
        left, top, right, bottom = window.rect
        x = {"left": left + (right - left) // 4, "right": right - (right - left) // 4}.get(region or "",
                                                                                          (left + right) // 2)
        y = {"top": top + (bottom - top) // 4, "bottom": bottom - (bottom - top) // 4}.get(region or "",
                                                                                          (top + bottom) // 2)
        if self._backend.foreground() != window.hwnd:
            self._backend.activate(window.hwnd)
            self._sleep(0.12)
        if direction in ("top", "bottom"):
            from sugar.computer.keys import VK_CONTROL

            vk = 0x24 if direction == "top" else 0x23
            self._backend.key(VK_CONTROL, up=False)
            self._backend.key(vk, up=False)
            self._backend.key(vk, up=True)
            self._backend.key(VK_CONTROL, up=True)
            self._context.note_action("ui.scroll", window)
            return R.ok("ui.scroll", window.app, f"At the {direction}.", verified=False, method="keys")
        _, _, notches = AMOUNTS.get(amount, AMOUNTS["normal"])
        before = self._backend.cursor()
        self._backend.mouse_move(x, y)
        self._sleep(0.02)
        sign = 1 if direction in ("up", "left") else -1  # wheel: positive scrolls up / left
        self._backend.mouse_wheel(sign * notches, horizontal=direction in ("left", "right"))
        self._sleep(0.02)
        self._backend.mouse_move(*before)
        self._context.note_action("ui.scroll", window)
        return R.ok("ui.scroll", window.app, f"Scrolled {direction}.", verified=False, method="wheel")

    # ------------------------------------------------------------------ continuous

    @property
    def scrolling(self) -> bool:
        return self._continuous is not None and self._continuous.is_alive()

    def start_continuous(self, direction: str = "down", speed: str = "normal", *, region: str | None = None,
                         target: str | int | None = None, limit_s: float = 120.0) -> R:
        self.stop_continuous()
        window = self._window(target)
        if window is None:
            return R.fail("ui.scroll", None, "There's nothing in front to scroll.")
        element = self.target(window, region=region, horizontal=direction in ("left", "right"))
        if element is None:
            return R.fail("ui.scroll", window.app, f"{display_name(window)} has no area I can scroll smoothly.")
        self._stop.clear()
        gap = SPEEDS.get(speed, SPEEDS["normal"])
        step = SMALL_ON if direction in ("down", "right") else SMALL_BACK
        key = "h_percent" if direction in ("left", "right") else "v_percent"
        horizontal = direction in ("left", "right")
        idle_at_start = self._idle() if self._idle else None

        def run() -> None:
            started = time.monotonic()
            # COM is per thread: this worker initialises its own apartment.
            try:
                self._backend.thread_init()
            except Exception:
                pass
            while not self._stop.is_set() and time.monotonic() - started < limit_s:
                kwargs = {"horizontal": step} if horizontal else {"vertical": step}
                if not self._backend.scroll(window.hwnd, element.id, **kwargs):
                    break
                info = self._backend.scroll_info(window.hwnd, element.id) or {}
                if info.get(key) in ((100.0,) if step in (SMALL_ON,) else (0.0,)):
                    break  # reached the end
                if self._idle is not None and idle_at_start is not None:
                    elapsed = time.monotonic() - started
                    if self._idle() < min(elapsed, 0.5) and elapsed > 0.6:
                        break  # the user touched the mouse or keyboard: they've taken over
                self._stop.wait(gap)

        self._continuous = threading.Thread(target=run, name="sid-scroll", daemon=True)
        self._continuous.start()
        self.last_target = (window.hwnd, element.id)
        self._context.note_action("ui.scroll", window)
        pace = {"slow": "slowly", "fast": "quickly"}.get(speed, "")
        return R.ok("ui.scroll", window.app, f"Scrolling {direction} {pace}. Say stop scrolling when you want."
                    .replace("  ", " "), verified=True, continuous=True)

    def stop_continuous(self) -> bool:
        running = self.scrolling
        self._stop.set()
        if self._continuous is not None:
            self._continuous.join(timeout=1.0)
        self._continuous = None
        return running

    def stop(self) -> R:
        if self.stop_continuous():
            return R.ok("ui.scroll_stop", None, "Stopped scrolling.")
        return R.ok("ui.scroll_stop", None, "I wasn't scrolling.", verified=False)
