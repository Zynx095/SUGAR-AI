"""Screen: capture, inspect, and pointer actions — plus the seam for a vision model.

``inspect`` describes the foreground window through UI Automation (names,
roles and positions of its controls), which is enough to click things by
name without "seeing" pixels. Pixel-level understanding — comparing two UI
versions, finding an element that has no accessible name, checking a visual
result — needs a vision model. :class:`VisionProvider` is that seam; until a
real provider is configured, :class:`UnavailableVision` answers honestly
that it can't see, and nothing pretends otherwise.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sugar.computer.backend import DesktopBackend
from sugar.computer.context import DesktopContext
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import WindowManager, display_name

R = ComputerActionResult


class VisionProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def describe(self, image: Path, question: str | None = None) -> str: ...

    async def compare(self, before: Path, after: Path, question: str | None = None) -> str: ...

    async def locate(self, image: Path, description: str) -> tuple[int, int] | None: ...


class UnavailableVision:
    """No vision model is configured. Every call says so instead of guessing."""

    name = "none"
    reason = "no vision model is configured"

    @property
    def available(self) -> bool:
        return False

    async def describe(self, image: Path, question: str | None = None) -> str:
        raise NotImplementedError(self.reason)

    async def compare(self, before: Path, after: Path, question: str | None = None) -> str:
        raise NotImplementedError(self.reason)

    async def locate(self, image: Path, description: str) -> tuple[int, int] | None:
        raise NotImplementedError(self.reason)


class ScreenController:
    def __init__(self, backend: DesktopBackend, windows: WindowManager, context: DesktopContext, folder: Path,
                 vision: VisionProvider | None = None, sleep=time.sleep) -> None:
        self._backend = backend
        self._windows = windows
        self._context = context
        self._folder = folder
        self.vision: VisionProvider = vision or UnavailableVision()
        self._sleep = sleep
        self.current: Path | None = None
        self.previous: Path | None = None

    def capture(self, target: str | int | None = None, *, window_only: bool = False) -> R:
        window = self._windows.resolve(target) if (window_only or target) else None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = self._folder / f"screenshot-{stamp}.png"
        try:
            self._backend.screenshot(path, window.hwnd if window else None)
        except Exception as exc:
            return R.fail("screen.capture", None, "I couldn't take the screenshot.", str(exc))
        self.previous, self.current = self.current, path
        what = display_name(window) if window else "the screen"
        return R.ok("screen.capture", what, f"Got a screenshot of {what}.", path=str(path),
                    previous=str(self.previous) if self.previous else None)

    def inspect(self, target: str | int | None = None, limit: int = 120) -> R:
        window = self._windows.resolve(target)
        if window is None:
            return R.fail("screen.inspect", None, "There's no window to look at.")
        try:
            elements = self._backend.describe_ui(window.hwnd, limit)
        except Exception as exc:
            return R.fail("screen.inspect", window.app, f"I couldn't read {display_name(window)}'s controls.", str(exc))
        focus = self._backend.focused()
        return R.ok("screen.inspect", window.app, f"{display_name(window)} has {len(elements)} named controls.",
                    window={"app": window.app, "title": window.title, "rect": list(window.rect)},
                    focused={"type": focus.control_type, "name": focus.name} if focus else None,
                    elements=elements)

    def _point(self, x: int | None, y: int | None, element: str | None, target: str | int | None) -> tuple[int, int] | None:
        if element:
            window = self._windows.resolve(target)
            if window is None:
                return None
            center = getattr(self._backend, "element_center", None)
            return center(window.hwnd, element) if center else None
        if x is None or y is None:
            return None
        return int(x), int(y)

    def click(self, x: int | None = None, y: int | None = None, *, element: str | None = None,
              target: str | int | None = None, button: str = "left", double: bool = False) -> R:
        point = self._point(x, y, element, target)
        if point is None:
            what = f"'{element}'" if element else "that spot"
            return R.fail("screen.click", None, f"I can't find {what} on screen.")
        window = self._windows.resolve(target) if target else None
        if window is not None and self._backend.foreground() != window.hwnd:
            self._backend.activate(window.hwnd)
        self._backend.mouse_move(*point)
        self._sleep(0.03)
        for _ in range(2 if double else 1):
            self._backend.mouse_button(button, up=False)
            self._sleep(0.02)
            self._backend.mouse_button(button, up=True)
            self._sleep(0.06)
        verb = {"left": "Clicked", "right": "Right-clicked", "middle": "Middle-clicked"}[button]
        if double:
            verb = "Double-clicked"
        label = f" {element}" if element else f" at {point[0]}, {point[1]}"
        return R(True, "screen.click", element, f"{verb}{label}.", False, None, {"x": point[0], "y": point[1]})

    def scroll(self, clicks: int, *, x: int | None = None, y: int | None = None, horizontal: bool = False) -> R:
        if x is not None and y is not None:
            self._backend.mouse_move(int(x), int(y))
        self._backend.mouse_wheel(int(clicks), horizontal=horizontal)
        direction = ("right" if clicks > 0 else "left") if horizontal else ("up" if clicks > 0 else "down")
        return R(True, "screen.scroll", None, f"Scrolled {direction}.", False, None, {"clicks": clicks})

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, steps: int = 12) -> R:
        self._backend.mouse_move(int(x1), int(y1))
        self._sleep(0.03)
        self._backend.mouse_button("left", up=False)
        try:
            for i in range(1, steps + 1):
                self._backend.mouse_move(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
                self._sleep(0.015)
        finally:
            self._backend.mouse_button("left", up=True)
        return R(True, "screen.drag", None, "Dragged it.", False, None, {"from": [x1, y1], "to": [x2, y2]})

    def vision_status(self) -> dict[str, Any]:
        return {"provider": self.vision.name, "available": self.vision.available}
