"""Is the user at the keyboard? Used before and during automation that moves the real mouse.

* :func:`idle_seconds` — time since the last physical or injected input (``GetLastInputInfo``).
* :func:`fullscreen_foreground` — a full-screen app (a game, a video) is in front: never automate over it.
* :class:`PointerGuard` — while SID drives the mouse (drawing in Paint), notices the user taking it back
  (the cursor is somewhere SID didn't put it) so the action stops instead of fighting the user.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


def idle_seconds() -> float:
    if os.name != "nt":
        return 1e9

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def fullscreen_foreground() -> str | None:
    """The process name of a full-screen window in front (games, full-screen video), else None."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    monitor = user32.MonitorFromWindow(hwnd, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    m = info.rcMonitor
    if not (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom):
        return None
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, 256)
    if class_name.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return None
    from sugar.computer import win32

    try:
        _, pid = win32.window_pid(hwnd)
        return win32.process_image(pid) or "a full-screen app"
    except Exception:
        return "a full-screen app"


class PointerGuard:
    """Detects the user moving the mouse while SID is moving it."""

    def __init__(self, cursor, tolerance: int = 6) -> None:
        self._cursor = cursor  # () -> (x, y)
        self._tolerance = tolerance
        self.expected: tuple[int, int] | None = None

    def placed(self, x: int, y: int) -> None:
        self.expected = (x, y)

    def interfered(self) -> bool:
        if self.expected is None:
            return False
        x, y = self._cursor()
        return abs(x - self.expected[0]) > self._tolerance or abs(y - self.expected[1]) > self._tolerance
