"""ComputerControl: the computer-control subsystem as one object.

Owns the backend, the desktop context and every controller, and runs all
desktop work on a single ``sugar-desktop`` thread: COM (UI Automation) and
keyboard state are per-thread, and two actions interleaving their keystrokes
would be worse than either waiting for the other. Async callers use
:meth:`run`; ``stop``/"never mind" sets a cancel flag that long typing checks
between characters.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from sugar.computer.apps import AppCatalog, ApplicationController
from sugar.computer.backend import DesktopBackend, NullDesktop
from sugar.computer.browser import BrowserController, BrowserRegistry
from sugar.computer.context import DesktopContext
from sugar.computer.keyboard import KeyboardController, TypingSettings
from sugar.computer.keys import VK_CONTROL
from sugar.computer.media import MediaController
from sugar.computer.results import ComputerActionResult
from sugar.computer.screen import ScreenController
from sugar.computer.spotify import SpotifyController
from sugar.computer.windows import WindowManager, display_name, is_browser
from sugar.computer.youtube import YouTubeSearch

if TYPE_CHECKING:
    from sugar.config.settings import Settings
    from sugar.core.events import EventBus

log = logging.getLogger(__name__)
T = TypeVar("T")

TABBED_EDITORS = {"notepad"}  # "close this" closes the document in front, like a browser tab
REFERENCES = {"this", "that", "it", "current", "active", "this window", "that window", "this app", "that app",
              "that application", "this application", "this one", "that one", "the current window", "here",
              "last_opened", "the app i just opened", "what i just opened", "the one i just opened"}


def _mentions(title: str, name: str) -> bool:
    import re

    wanted = re.sub(r"^(?:the|my)\s+", "", name.lower().strip())
    wanted = re.sub(r"\s+(?:tab|page|site|website)$", "", wanted)
    return bool(wanted) and re.search(rf"\b{re.escape(wanted)}\b", title.lower()) is not None


def create_backend(kind: str = "auto") -> DesktopBackend:
    if kind == "none" or sys.platform != "win32":
        return NullDesktop()
    from sugar.computer.native import WindowsDesktop

    return WindowsDesktop()


class ComputerControl:
    def __init__(self, settings: Settings, catalog: AppCatalog, spotify: SpotifyController, *,
                 backend: DesktopBackend | None = None, browsers: BrowserRegistry | None = None) -> None:
        cfg = settings.computer
        self.settings = cfg
        self.backend = backend if backend is not None else create_backend(cfg.backend)
        self.available = not isinstance(self.backend, NullDesktop)
        self.cancel_event = threading.Event()
        self.context = DesktopContext(self.backend, own_titles={settings.conversation.assistant_name})
        self.windows = WindowManager(self.backend, self.context, close_timeout_s=cfg.close_timeout_s)
        self.keyboard = KeyboardController(
            self.backend, self.windows, self.context,
            TypingSettings(mode=cfg.typing_mode, char_interval_s=cfg.typing_interval_ms / 1000,
                           key_gap_s=cfg.key_gap_ms / 1000, paste_threshold=cfg.paste_threshold_chars,
                           restore_clipboard=cfg.restore_clipboard, verify=cfg.verify_typing),
            self.cancel_event,
        )
        if browsers is None:
            browsers = BrowserRegistry() if self.available else BrowserRegistry([], None)
        if cfg.browser:
            browsers.prefer(cfg.browser)
        self.browsers = browsers
        self.apps = ApplicationController(self.backend, catalog, self.windows, self.context, browsers,
                                          launch_timeout_s=cfg.launch_timeout_s)
        self.browser = BrowserController(self.backend, self.windows, self.keyboard, self.context, browsers,
                                         launch_timeout_s=cfg.launch_timeout_s)
        self.screen = ScreenController(self.backend, self.windows, self.context,
                                       settings.paths.data_dir / "screenshots")
        self.youtube = YouTubeSearch(settings.secret(cfg.youtube_api_key_env), cfg.youtube_region)
        self.media = MediaController(self, spotify, self.youtube, music_platform=cfg.music_platform,
                                     pause_other_media=cfg.pause_other_media)
        self._pool: ThreadPoolExecutor | None = None
        self._unsubscribe: Callable[[], None] | None = None

    # ------------------------------------------------------------------ threading

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sugar-desktop",
                                            initializer=self.backend.thread_init)
        return self._pool

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor(), partial(fn, *args, **kwargs))

    def cancel(self) -> None:
        """Stop long-running input (typing) at the next character."""
        self.cancel_event.set()

    # ------------------------------------------------------------------ lifecycle

    def start(self, bus: EventBus | None = None) -> None:
        if not self.available:
            return
        try:
            self.context.start()
        except Exception:
            log.exception("foreground tracking failed to start")
        warm = getattr(self.backend, "uia", None)
        if warm is not None:
            self._executor().submit(warm.warm_up)  # first UI Automation use generates COM wrappers (~0.3 s)
        self._executor().submit(self.context.refresh)
        if bus is not None:
            def on_cancel(_event) -> None:
                self.cancel()

            self._unsubscribe = bus.subscribe("turn.cancelled", on_cancel)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.cancel()
        self.context.stop()
        if self._pool is not None:
            try:
                self._pool.submit(self.keyboard.release_all).result(timeout=2)
            except Exception:
                pass
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # ------------------------------------------------------------------ target resolution (worker thread)

    def close(self, name: str | None = None, scope: str = "auto") -> ComputerActionResult:
        """Close whatever "name" means right now: an app, a browser tab, or a window.

        "close this" in a browser closes the tab (the thing you're looking at); "close this window" or
        "close Brave" closes windows; "close YouTube" closes the YouTube tab when no app has that name.
        """
        self.context.refresh()
        reference = (name or "this").strip().lower()
        window = self.context.resolve(reference)
        if window is not None or reference in REFERENCES:
            if window is None:
                return ComputerActionResult.fail("window.close", None, "There's nothing to close.")
            if scope == "auto" and any(word in reference.split() for word in ("window", "app", "application")):
                scope = "window"
            if scope in ("auto", "tab") and is_browser(window):
                return self.browser.close_tab(hwnd=window.hwnd)
            if scope in ("auto", "tab") and window.app.lower() in TABBED_EDITORS:
                tabs = self.backend.tabs(window.hwnd)
                if len(tabs) > 1:  # close the document in front, not the user's other tabs
                    return self._close_editor_tab(window, len(tabs))
            return self.windows.close_window(window)
        if scope == "tab":
            return self.browser.close_tab(match=name)
        entry, _note = self.apps.resolve(name or "")
        if entry is not None and self.apps.windows_for(entry):
            return self.apps.close(name or "")
        if scope != "window":
            window, tab = self.browser.find_tab(lambda title: _mentions(title, name or ""))
            if window is not None and tab is not None:
                return self.browser.close_tab(match=name, hwnd=window.hwnd)
        if entry is not None:
            return ComputerActionResult.ok("app.close", entry.name, f"{entry.name} isn't open.", closed=0)
        matches = self.windows.find(name or "")
        if matches:
            return self.windows.close_window(matches[0])
        return ComputerActionResult.fail("window.close", name, f"I can't find {name} to close.")

    def _close_editor_tab(self, window, tab_count: int) -> ComputerActionResult:
        if self.backend.foreground() != window.hwnd and not self.backend.activate(window.hwnd):
            return ComputerActionResult.fail("window.close", window.app, "I couldn't bring it to the front.")
        self.keyboard.chord([VK_CONTROL], ord("W"))
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.08)
            if not self.backend.is_window(window.hwnd) or len(self.backend.tabs(window.hwnd)) < tab_count:
                self.context.note_action("window.close", None)
                return ComputerActionResult.ok("window.close", window.app,
                                               f"Closed the document in {display_name(window)}.")
        prompt = self.windows.detect_prompt(window)
        if prompt is not None:
            return prompt
        return ComputerActionResult.fail("window.close", window.app, "The document didn't close.")

    def focus(self, name: str | None = None) -> ComputerActionResult:
        """Bring an app, a browser tab or a window to the front ("switch to Notepad", "go to the YouTube tab")."""
        self.context.refresh()
        reference = (name or "this").strip().lower()
        window = self.context.resolve(reference)
        if window is not None:
            return self.windows.focus(window.hwnd)
        if reference in REFERENCES or reference in ("previous", "last", "back"):
            return ComputerActionResult.fail("window.focus", None, "There's no window to switch to.")
        entry, _note = self.apps.resolve(name or "")
        if entry is not None:
            windows = self.apps.windows_for(entry)
            if windows:
                return self.windows.focus(windows[0].hwnd)
        window, tab = self.browser.find_tab(lambda title: _mentions(title, name or ""))
        if window is not None and tab is not None:
            return self.browser.switch_tab(match=name, hwnd=window.hwnd)
        matches = self.windows.find(name or "")
        if matches:
            return self.windows.focus(matches[0].hwnd)
        if entry is not None:
            return self.apps.open(name or "")
        return ComputerActionResult.fail("window.focus", name, f"I can't find {name}.")

    def set_state(self, name: str | None, state: str) -> ComputerActionResult:
        self.context.refresh()
        reference = (name or "this").strip().lower()
        window = self.context.resolve(reference)
        if window is not None:
            return self.windows.set_state(window.hwnd, state)
        if reference in REFERENCES:
            return ComputerActionResult.fail(f"window.{state}", None, "There's no window for that.")
        entry, _note = self.apps.resolve(name or "")
        if entry is not None:
            windows = self.apps.windows_for(entry)
            if windows:
                return self.windows.set_state(windows[0].hwnd, state)
            return ComputerActionResult.fail(f"window.{state}", entry.name, f"{entry.name} isn't open.")
        return self.windows.set_state(name, state)

    # ------------------------------------------------------------------ for prompts / UI

    def describe(self) -> str:
        if not self.available:
            return ""
        try:
            return self.context.describe()
        except Exception:
            return ""

    def status(self) -> dict[str, Any]:
        default = self.browsers.default()
        return {
            "available": self.available,
            "browsers": [b.name for b in self.browsers.all()],
            "default_browser": default.name if default else None,
            "vision": self.screen.vision_status(),
            "youtube_search": "data-api" if self.youtube._api_key else "web",
        }
