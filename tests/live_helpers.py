"""Helpers for real-machine computer-control checks (tests/test_desktop_live.py, benchmarks/).

Everything here acts only on windows it creates: a throw-away Tk text window,
a temporary file opened in Notepad, and a separate browser window. Before any
input it checks that nobody is playing a full-screen game, and it puts the
user's foreground window back afterwards.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sugar.computer.apps import AppCatalog
from sugar.computer.engine import ComputerControl
from sugar.computer.spotify import SpotifyController
from sugar.config.settings import load_settings

GAMES = {"valorant-win64-shipping.exe", "valorant.exe", "league of legends.exe", "cs2.exe", "r5apex.exe",
         "fortniteclient-win64-shipping.exe", "gta5.exe", "minecraft.exe", "javaw.exe", "rocketleague.exe",
         "overwatch.exe", "dota2.exe", "eldenring.exe", "cod.exe"}

TK_TARGET = r'''
import os, sys, tkinter as tk
out = sys.argv[1]
root = tk.Tk(); root.title(sys.argv[2]); root.geometry("640x320+160+160")
text = tk.Text(root, undo=False); text.pack(fill="both", expand=True); text.focus_set()
def poll():
    if os.path.exists(out + ".req"):
        os.remove(out + ".req")
        with open(out + ".tmp", "w", encoding="utf-8", newline="") as f:
            f.write(text.get("1.0", "end-1c"))
        os.replace(out + ".tmp", out)
    root.after(40, poll)
root.after(40, poll)
root.mainloop()
'''


def make_computer() -> ComputerControl:
    settings = load_settings()
    catalog = AppCatalog(settings.paths.data_dir / "apps.json")
    computer = ComputerControl(settings, catalog, SpotifyController(settings))
    computer.context.start()
    return computer


def busy_reason(computer: ComputerControl) -> str | None:
    """Why the desktop shouldn't be touched right now (a game or full-screen app in front), or None."""
    hwnd = computer.backend.foreground()
    window = computer.backend.get_window(hwnd)
    if window is None:
        return None
    if window.process in GAMES:
        return f"{window.process} is in the foreground"
    user32 = ctypes.windll.user32
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    left, top, right, bottom = window.rect
    if left <= 0 and top <= 0 and right >= width and bottom >= height and window.class_name not in (
            "Progman", "WorkerW"):
        return f"a full-screen window ({window.app}) is in the foreground"
    return None


class TkTarget:
    """A throw-away text window in its own process, whose content can be read back exactly."""

    def __init__(self, title: str = "Sugar Typing Target") -> None:
        self.title = title
        folder = Path(tempfile.mkdtemp(prefix="sugar-live-"))
        self.script = folder / "target.py"
        self.script.write_text(TK_TARGET, encoding="utf-8")
        self.dump = folder / "content.txt"
        self.process = subprocess.Popen([sys.executable, str(self.script), str(self.dump), title])
        self.hwnd = 0

    def wait(self, computer: ComputerControl, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for window in computer.backend.list_windows():
                if window.title == self.title and window.pid == self.process.pid:
                    self.hwnd = window.hwnd
                    return self.hwnd
            time.sleep(0.05)
        raise TimeoutError("the Tk target window didn't appear")

    def text(self, timeout: float = 3.0) -> str:
        if self.dump.exists():
            self.dump.unlink()
        Path(str(self.dump) + ".req").touch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.dump.exists():
                return self.dump.read_text(encoding="utf-8")
            time.sleep(0.03)
        raise TimeoutError("the Tk target didn't report its content")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ForegroundKeeper:
    """Remember the user's foreground window and give it back afterwards."""

    def __init__(self, computer: ComputerControl) -> None:
        self.computer = computer
        self.original = computer.backend.foreground()

    def restore(self) -> None:
        if self.original and self.computer.backend.is_window(self.original):
            self.computer.backend.activate(self.original)


def temp_text_file(prefix: str = "sugar-live") -> Path:
    handle, name = tempfile.mkstemp(prefix=prefix + "-", suffix=".txt")
    os.close(handle)
    return Path(name)
