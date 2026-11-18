"""Drawing in Microsoft Paint — for real, with Paint's own tools and the mouse.

1. open (or focus) Paint and find the canvas in a fresh screenshot (the large white area),
2. pick Paint's tools through UI Automation (pencil/brush, colour, size, fill),
3. draw the plan's strokes with mouse drags (button held, small steps), checking before every stroke that Paint
   is still in front and that the user hasn't taken the mouse back,
4. verify that the canvas changed, and remember what was drawn ("undo the last shape", "add colour",
   "make the outline thicker", "draw a tree beside it", "make it cleaner"),
5. save through Paint's own Save dialog, never over an existing file without confirmation.

Nothing is pasted or overlaid: every line on the canvas is drawn by Paint.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.drawing import Plan, densify
from sugar.computer.guard import PointerGuard
from sugar.computer.results import ComputerActionResult

log = logging.getLogger(__name__)
R = ComputerActionResult

COLOURS = {"black": "Black", "white": "White", "red": "Red", "dark red": "Dark red", "orange": "Orange",
           "yellow": "Yellow", "green": "Green", "dark green": "Dark green", "blue": "Blue", "light blue": "Light blue",
           "turquoise": "Turquoise", "indigo": "Indigo", "purple": "Purple", "pink": "Rose", "rose": "Rose",
           "brown": "Brown", "gold": "Gold", "gray": "Gray-50%", "grey": "Gray-50%", "lime": "Lime",
           "cyan": "Turquoise", "violet": "Purple"}
SUBJECT_COLOURS = {"cat": "orange", "dog": "brown", "tree": "green", "sun": "yellow", "house": "red", "heart": "red",
                   "flower": "rose", "star": "gold", "fish": "light blue", "car": "red", "apple": "red",
                   "leaf": "green", "bird": "light blue", "smiley": "yellow", "circle": "light blue"}
SIZES = ["1px", "3px", "5px", "8px"]


@dataclass
class Drawn:
    plan: Plan  # placed in canvas coordinates (0–1)
    strokes: int  # undo steps it took
    size: str
    box: tuple[float, float, float, float]


@dataclass
class PaintSession:
    hwnd: int = 0
    shapes: list[Drawn] = field(default_factory=list)
    size: str = "3px"
    colour: str = "black"
    tool_ready: bool = False
    references: dict = field(default_factory=dict)  # subject -> Reference (for "make it cleaner")


class PaintController:
    def __init__(self, backend: DesktopBackend, computer, *, sleep=time.sleep) -> None:
        self._backend = backend
        self._computer = computer  # ComputerControl: apps, windows, interact, keyboard, window_image
        self._sleep = sleep
        self.session = PaintSession()
        self.step_px = 5.0
        self.move_delay_s = 0.004

    # ------------------------------------------------------------------ the window and canvas

    def window(self) -> WindowInfo | None:
        for window in self._computer.windows.list():
            if window.process.lower() == "mspaint.exe" and not window.minimized:
                return window
        return None

    def ensure_open(self) -> tuple[WindowInfo | None, R | None]:
        window = self.window()
        if window is None:
            opened = self._computer.apps.open("Paint")
            if not opened.success:
                return None, R.fail("paint.open", "paint", "I couldn't open Paint.", opened.error)
            for _ in range(40):
                self._sleep(0.25)
                window = self.window()
                if window is not None:
                    break
            if window is None:
                return None, R.fail("paint.open", "paint", "Paint didn't open in time.")
            self._sleep(1.0)  # let the toolbar and canvas finish laying out
        if self._backend.foreground() != window.hwnd:
            self._backend.activate(window.hwnd)
            self._sleep(0.3)
        if window.hwnd != self.session.hwnd:
            self.session = PaintSession(hwnd=window.hwnd)
        return self._backend.get_window(window.hwnd) or window, None

    def canvas(self, window: WindowInfo) -> tuple[int, int, int, int] | None:
        """Screen rectangle of the canvas: the largest plain-white rectangle in a fresh screenshot of Paint."""
        import cv2

        image = np.asarray(self._computer.window_image(window).convert("RGB"))
        white = np.all(image >= 250, axis=2).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=4)
        best = None
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            fill_ratio = area / max(1, w * h)
            if w * h > 0.08 * image.shape[0] * image.shape[1] and fill_ratio > 0.6 and (best is None or area > best[4]):
                best = (x, y, w, h, area)
        if best is None:
            return None
        x, y, w, h, _ = best
        left, top = window.rect[0], window.rect[1]
        return (left + int(x), top + int(y), left + int(x + w), top + int(y + h))

    # ------------------------------------------------------------------ tools

    def _click_named(self, window: WindowInfo, *names: str) -> bool:
        for name in names:
            result = self._computer.interact.click(name, window.hwnd)
            if result.success:
                return True
        return False

    def select_drawing_tool(self, window: WindowInfo) -> bool:
        return self._click_named(window, "Pencil", "the button labelled Pencil")

    def select_colour(self, window: WindowInfo, colour: str) -> bool:
        name = COLOURS.get(colour.lower(), colour.title())
        ok = self._click_named(window, f"the button labelled {name}", name)
        if ok:
            self.session.colour = colour
        return ok

    def select_size(self, window: WindowInfo, size: str) -> bool:
        if not self._click_named(window, "Size", "the button labelled Size"):
            return False
        self._sleep(0.3)
        ok = self._click_named(window, size, f"the item labelled {size}")
        if not ok:
            self._backend.key(0x1B, up=False)
            self._backend.key(0x1B, up=True)  # close the menu again
        else:
            self.session.size = size
        return ok

    # ------------------------------------------------------------------ drawing

    def _to_screen(self, canvas: tuple[int, int, int, int], point: tuple[float, float]) -> tuple[int, int]:
        left, top, right, bottom = canvas
        x = left + 4 + point[0] * (right - left - 8)
        y = top + 4 + point[1] * (bottom - top - 8)
        return int(round(x)), int(round(y))

    def _move(self, x: int, y: int) -> None:
        move = getattr(self._backend, "pointer_move", None) or self._backend.mouse_move
        move(x, y)

    def draw_plan(self, plan: Plan, box: tuple[float, float, float, float] | None, *, cancel=None) -> R:
        """Draw ``plan`` scaled into ``box`` (canvas-relative), or as already placed when ``box`` is None."""
        window, error = self.ensure_open()
        if error is not None:
            return error
        canvas = self.canvas(window)
        if canvas is None:
            return R.fail("paint.draw", "paint", "I can't find Paint's canvas on screen.")
        placed = plan.placed(box) if box is not None else plan
        box = box if box is not None else placed.bbox()
        before = np.asarray(self._computer.window_image(window).convert("L")).astype(np.int16)
        guard = PointerGuard(self._backend.cursor)
        home = self._backend.cursor()
        strokes = 0
        try:
            for stroke in placed.strokes:
                if cancel is not None and cancel.is_set():
                    return self._stopped(placed, strokes, box, "Stopped drawing.")
                if self._backend.foreground() != window.hwnd:
                    return self._stopped(placed, strokes, box, "Paint isn't in front any more, so I stopped drawing.")
                points = densify([self._to_screen(canvas, p) for p in stroke], self.step_px)
                if len(points) < 2:
                    continue
                self._move(*points[0])
                guard.placed(*points[0])
                self._sleep(0.01)
                self._backend.mouse_button("left", up=False)
                try:
                    for x, y in points[1:]:
                        if guard.interfered():
                            return self._stopped(placed, strokes + 1, box,
                                                 "You moved the mouse, so I stopped drawing.")
                        self._move(x, y)
                        guard.placed(x, y)
                        self._sleep(self.move_delay_s)
                finally:
                    self._backend.mouse_button("left", up=True)
                strokes += 1
                self._sleep(0.015)
        finally:
            self._backend.mouse_move(*home)
        after = np.asarray(self._computer.window_image(window).convert("L")).astype(np.int16)
        changed = float((np.abs(after - before) > 40).mean()) if after.shape == before.shape else 1.0
        self.session.shapes.append(Drawn(placed, strokes, self.session.size, placed.bbox()))
        verified = changed > 0.0005
        return R(True, "paint.draw", "paint", f"Drew {plan.subject}." if verified else
                 f"I drew {strokes} strokes, but the canvas barely changed.", verified, None,
                 {"strokes": strokes, "points": placed.points, "changed": round(changed, 4), "source": plan.source})

    def _stopped(self, placed: Plan, strokes: int, box, message: str) -> R:
        if strokes:
            self.session.shapes.append(Drawn(placed, strokes, self.session.size, placed.bbox()))
        return R.fail("paint.draw", "paint", message, "interrupted", strokes=strokes)

    # ------------------------------------------------------------------ follow-ups

    def next_box(self, place: str | None) -> tuple[float, float, float, float]:
        """Where the next drawing goes: centred at first; "beside it" to the right of the last one."""
        if not self.session.shapes or place in (None, "center"):
            return (0.2, 0.12, 0.8, 0.88) if not self.session.shapes else (0.25, 0.2, 0.75, 0.8)
        x0, y0, x1, y1 = self.session.shapes[-1].box
        width = min(0.35, x1 - x0)
        if place == "left":
            end = max(x0 - 0.03, 0.2)
            return (max(0.02, end - max(width, 0.18)), y0, end, y1)
        if place in ("above", "top"):
            return (x0, max(0.02, y0 - 0.3), x1, max(0.32, y0 - 0.02))
        if place in ("below", "bottom"):
            return (x0, min(0.68, y1 + 0.02), x1, min(0.98, y1 + 0.3))
        start = min(x1 + 0.03, 0.8)  # to the right of the last drawing, in whatever room is left
        return (start, y0, min(0.98, start + max(width, 0.18)), y1)

    def undo_last(self) -> R:
        if not self.session.shapes:
            return R.fail("paint.undo", "paint", "I haven't drawn anything here to undo.")
        window, error = self.ensure_open()
        if error is not None:
            return error
        last = self.session.shapes.pop()
        for _ in range(last.strokes):
            self._computer.keyboard.chord([0x11], ord("Z"))
            self._sleep(0.05)
        return R.ok("paint.undo", "paint", f"Removed the {last.plan.subject}.", strokes=last.strokes)

    def fill(self, colour: str | None = None) -> R:
        if not self.session.shapes:
            return R.fail("paint.fill", "paint", "Draw something first, then I can colour it in.")
        last = self.session.shapes[-1]
        if last.plan.fill_point is None:
            return R.fail("paint.fill", "paint", f"The {last.plan.subject} has no closed area I can fill.")
        window, error = self.ensure_open()
        if error is not None:
            return error
        canvas = self.canvas(window)
        if canvas is None:
            return R.fail("paint.fill", "paint", "I can't find Paint's canvas on screen.")
        colour = colour or SUBJECT_COLOURS.get(last.plan.subject.lower().split()[-1], "light blue")
        if not self._click_named(window, "Fill", "Fill with color", "the button labelled Fill"):
            return R.fail("paint.fill", "paint", "I couldn't find Paint's fill tool.")
        self.select_colour(window, colour)
        x, y = self._to_screen(canvas, last.plan.fill_point)
        home = self._backend.cursor()
        self._move(x, y)
        self._sleep(0.05)
        self._backend.mouse_button("left", up=False)
        self._backend.mouse_button("left", up=True)
        self._sleep(0.1)
        self._backend.mouse_move(*home)
        last.strokes += 1  # the fill is one more undo step of this shape
        self.select_drawing_tool(window)
        self.select_colour(window, "black")
        return R.ok("paint.fill", "paint", f"Coloured the {last.plan.subject} {colour}.")

    def thicker(self) -> R:
        if not self.session.shapes:
            return R.fail("paint.thicker", "paint", "There's no outline of mine to thicken.")
        window, error = self.ensure_open()
        if error is not None:
            return error
        index = SIZES.index(self.session.size) if self.session.size in SIZES else 1
        bigger = SIZES[min(len(SIZES) - 1, index + 1)]
        if bigger == self.session.size:
            return R.ok("paint.thicker", "paint", "That's already Paint's thickest pencil line.", verified=False)
        self.select_size(window, bigger)
        last = self.session.shapes[-1]
        outline = Plan(last.plan.subject, last.plan.strokes[:1] if last.plan.source == "reference" else
                       last.plan.strokes, last.plan.source)
        result = self.draw_plan(outline, None)  # already placed: draw over the same lines
        if result.success:
            extra = self.session.shapes.pop()
            last.strokes += extra.strokes
            return R.ok("paint.thicker", "paint", f"Made the outline thicker ({bigger}).")
        return result

    def save(self, path: Path, *, overwrite: bool = False) -> R:
        window, error = self.ensure_open()
        if error is not None:
            return error
        if path.exists() and not overwrite:
            return R.fail("paint.save", "paint", f"{path.name} already exists.", "exists", waiting_for="overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.stat().st_mtime if path.exists() else None
        self._computer.keyboard.chord([0x11, 0x10], ord("S"))  # Ctrl+Shift+S: Save as (always shows the dialog)
        dialog = None
        for _ in range(30):
            self._sleep(0.2)
            fg = self._backend.foreground()
            info = self._backend.get_window(fg)
            if info is not None and fg != window.hwnd and info.process.lower() == "mspaint.exe":
                dialog = info
                break
        if dialog is None:
            return R.fail("paint.save", "paint", "Paint's Save dialog didn't appear.")
        self._sleep(0.3)
        self._computer.keyboard.chord([0x11], ord("A"))
        if not self._computer.keyboard.type_fast(str(path), dialog.hwnd, 0.003):
            return R.fail("paint.save", "paint", "The Save dialog lost focus while I was typing the name.")
        self._backend.key(0x0D, up=False)
        self._backend.key(0x0D, up=True)
        for _ in range(30):
            self._sleep(0.2)
            fg = self._backend.foreground()
            info = self._backend.get_window(fg)
            if overwrite and info is not None and "confirm" in info.title.lower():
                self._computer.interact.click("Yes", fg)  # the user already confirmed replacing it
            if path.exists() and path.stat().st_size > 0 and path.stat().st_mtime != before:
                return R.ok("paint.save", "paint", f"Saved it as {path.name}.", path=str(path))
        return R.fail("paint.save", "paint", f"I couldn't confirm that {path.name} was saved.")
