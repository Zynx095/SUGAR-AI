"""Browser control for the browser the user actually uses (any Chromium browser, Firefox basics).

Sugar does not start a separate automation browser: it drives the window on
screen. Tabs and the address bar are read and operated through UI Automation
(each Chromium tab exposes its title, selection state and a Close button);
navigation and tab shortcuts go through the keyboard engine, always into a
focused, verified browser window; every action is checked afterwards by
re-reading the tab list, address bar or title.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass

from sugar.computer.backend import DesktopBackend, TabInfo, WindowInfo
from sugar.computer.context import BROWSER_PROCESSES, DesktopContext, SearchContext
from sugar.computer.keyboard import KeyboardController
from sugar.computer.keys import VK_CONTROL, VK_DELETE, VK_MENU, VK_RETURN, VK_SHIFT, VK_TAB
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import WindowManager

R = ComputerActionResult


@dataclass(frozen=True)
class BrowserInfo:
    key: str  # brave | chrome | edge | firefox | opera | vivaldi
    name: str
    exe: str
    process: str
    chromium: bool = True


KNOWN_BROWSERS = {
    "brave": ("Brave", "brave.exe", True, ("BraveHTML", "BraveSoftware")),
    "chrome": ("Google Chrome", "chrome.exe", True, ("ChromeHTML", "Google Chrome")),
    "edge": ("Microsoft Edge", "msedge.exe", True, ("MSEdgeHTM", "Microsoft Edge")),
    "firefox": ("Firefox", "firefox.exe", False, ("FirefoxURL", "Firefox")),
    "opera": ("Opera", "opera.exe", True, ("Opera",)),
    "vivaldi": ("Vivaldi", "vivaldi.exe", True, ("VivaldiHTM", "Vivaldi")),
}
SPOKEN_BROWSERS = {
    "brave": "brave", "brave browser": "brave", "chrome": "chrome", "google chrome": "chrome",
    "chrome browser": "chrome", "edge": "edge", "microsoft edge": "edge", "edge browser": "edge",
    "firefox": "firefox", "mozilla": "firefox", "mozilla firefox": "firefox", "opera": "opera",
    "vivaldi": "vivaldi",
}

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "github": "https://github.com/search?q={q}&type=repositories",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "brave": "https://search.brave.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "amazon": "https://www.amazon.in/s?k={q}",
    "spotify": "https://open.spotify.com/search/{p}",
    "maps": "https://www.google.com/maps/search/{p}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "npm": "https://www.npmjs.com/search?q={q}",
    "pypi": "https://pypi.org/search/?q={q}",
    "x": "https://x.com/search?q={q}",
    "twitter": "https://x.com/search?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
}
ENGINE_ALIASES = {
    "the web": "google", "web": "google", "online": "google", "the internet": "google", "internet": "google",
    "you tube": "youtube", "yt": "youtube", "git hub": "github", "stackoverflow": "stack overflow",
    "google maps": "maps", "google images": "images", "ddg": "duckduckgo", "brave search": "brave",
    "wiki": "wikipedia", "twitter": "x",
}

_TAB_SUFFIX = re.compile(
    r"(?:\s+-\s+(?:audio playing|playing audio|audio muted|muted|pinned|network error|crashed|"
    r"(?:high )?memory usage\b.*|using (?:camera|microphone|your camera|your microphone).*|"
    r"recording.*|sharing.*|bluetooth.*|connected to .*|paused|loading)|"
    r"\.\s*(?:un)?modified\.?)$",
    re.I,
)
_BROWSER_TITLE_SUFFIX = re.compile(
    r"\s+[-—]\s+(?:brave|google chrome|chrome|microsoft​? ?edge|mozilla firefox|firefox|opera|vivaldi)$",
    re.I,
)
_EDGE_PROFILE = re.compile(r"\s+-\s+(?:personal|work|profile \d+)$", re.I)  # "Inbox - Personal - Microsoft Edge"
_NEW_TAB_TITLES = {"new tab", "untitled", "about:blank", "start page", "new tab page", ""}


def clean_tab_name(name: str) -> str:
    previous = None
    name = name.replace("​", "").strip()
    while previous != name:
        previous = name
        name = _TAB_SUFFIX.sub("", name).strip()
    return name


def page_title(window_title: str) -> str:
    title = window_title.replace("​", "").strip()
    stripped = _BROWSER_TITLE_SUFFIX.sub("", title).strip()
    if stripped != title and re.search(r"edge$", title, re.I):
        stripped = _EDGE_PROFILE.sub("", stripped).strip()
    return stripped


def is_blank(title: str, url: str | None) -> bool:
    t = clean_tab_name(title).lower()
    u = (url or "").strip().lower()
    return t in _NEW_TAB_TITLES or u in ("", "about:blank") or u.startswith(
        ("chrome://newtab", "brave://newtab", "edge://newtab", "about:newtab", "about:home"))


def search_url(engine: str, query: str) -> str:
    engine = ENGINE_ALIASES.get(engine.lower().strip(), engine.lower().strip())
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])
    return template.format(q=urllib.parse.quote_plus(query), p=urllib.parse.quote(query))


def normalize_engine(engine: str | None) -> str:
    if not engine:
        return "google"
    engine = engine.lower().strip()
    engine = ENGINE_ALIASES.get(engine, engine)
    return engine if engine in SEARCH_ENGINES else "google"


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) and not url.lower().startswith(("about:", "mailto:")):
        url = "https://" + url
    return url


def _bare(url: str) -> str:
    """URL without scheme, "www." or a trailing slash, for comparing with what the address bar shows."""
    bare = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I)
    return bare.removeprefix("www.").rstrip("/").lower()


def _host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(normalize_url(url)).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


class BrowserRegistry:
    """Installed browsers and the default one, from the Windows registry."""

    def __init__(self, browsers: list[BrowserInfo] | None = None, default_key: str | None = None) -> None:
        if browsers is None:
            browsers, default_key = _detect()
        self._browsers = {b.key: b for b in browsers}
        self._default = default_key if default_key in self._browsers else next(iter(self._browsers), None)

    def all(self) -> list[BrowserInfo]:
        return list(self._browsers.values())

    def get(self, key: str | None) -> BrowserInfo | None:
        return self._browsers.get(key or "")

    def default(self) -> BrowserInfo | None:
        return self._browsers.get(self._default or "")

    def prefer(self, key: str | None) -> None:
        if key in self._browsers:
            self._default = key

    @staticmethod
    def match(spoken: str) -> str | None:
        spoken = spoken.lower().strip()
        spoken = re.sub(r"^(?:the|my)\s+", "", spoken)
        return SPOKEN_BROWSERS.get(spoken)

    @staticmethod
    def display(key: str) -> str:
        return {"chrome": "Chrome", "edge": "Edge"}.get(key, KNOWN_BROWSERS.get(key, (key.title(),))[0])

    def for_process(self, process: str) -> BrowserInfo | None:
        return next((b for b in self._browsers.values() if b.process == process.lower()), None)


def _detect() -> tuple[list[BrowserInfo], str | None]:
    import os

    try:
        import winreg
    except ImportError:
        return [], None
    found: dict[str, BrowserInfo] = {}

    def add(key: str, exe: str) -> None:
        exe = exe.strip().strip('"')
        if key in found or not exe or not os.path.exists(exe):
            return
        name, process, chromium, _ = KNOWN_BROWSERS[key]
        found[key] = BrowserInfo(key, name, exe, process, chromium)

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Clients\StartMenuInternet") as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    sub = winreg.EnumKey(root, i)
                    try:
                        with winreg.OpenKey(root, sub + r"\shell\open\command") as command:
                            exe = winreg.QueryValue(command, None)
                    except OSError:
                        continue
                    exe_path = exe.split('"')[1] if exe.startswith('"') else exe.split(" ")[0]
                    for key, (_name, process, _chromium, _ids) in KNOWN_BROWSERS.items():
                        if os.path.basename(exe_path).lower() == process:
                            add(key, exe_path)
        except OSError:
            continue
        for key, (_name, process, _chromium, _ids) in KNOWN_BROWSERS.items():
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{process}") as k:
                    add(key, winreg.QueryValue(k, None))
            except OSError:
                pass
    default = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\https\UserChoice") as choice:
            prog_id = winreg.QueryValueEx(choice, "ProgId")[0]
        default = next((k for k, (_n, _p, _c, ids) in KNOWN_BROWSERS.items()
                        if any(prog_id.startswith(i) for i in ids)), None)
    except OSError:
        pass
    ordered = sorted(found.values(), key=lambda b: (b.key != default, b.key))
    return ordered, default


class BrowserController:
    def __init__(self, backend: DesktopBackend, windows: WindowManager, keyboard: KeyboardController,
                 context: DesktopContext, registry: BrowserRegistry, *, sleep=time.sleep,
                 launch_timeout_s: float = 12.0) -> None:
        self._backend = backend
        self._windows = windows
        self._keyboard = keyboard
        self._context = context
        self.registry = registry
        self._sleep = sleep
        self._launch_timeout = launch_timeout_s

    # ------------------------------------------------------------------ finding the browser window

    def windows(self, browser: str | None = None) -> list[WindowInfo]:
        info = self.registry.get(browser) if browser else None
        processes = {info.process} if info else BROWSER_PROCESSES
        recency = {w.hwnd: i for i, w in enumerate(self._context.recent_windows())}
        found = [w for w in self._windows.list() if w.process in processes]
        found.sort(key=lambda w: recency.get(w.hwnd, 99))
        return found

    def window(self, browser: str | None = None) -> WindowInfo | None:
        """The browser window "this tab" refers to: the active window if it's a browser, else the latest one."""
        self._context.refresh()
        active = self._context.active()
        info = self.registry.get(browser) if browser else None
        if active is not None and active.process in BROWSER_PROCESSES and (info is None or active.process == info.process):
            return active
        candidates = self.windows(browser)
        if candidates:
            return candidates[0]
        if browser is None:
            default = self.registry.default()
            if default is not None:
                candidates = self.windows(default.key)
                return candidates[0] if candidates else None
        return None

    def _focused_window(self, browser: str | None, action: str) -> tuple[WindowInfo | None, R | None]:
        window = self.window(browser)
        if window is None:
            name = self.registry.display(browser) if browser else "A browser"
            return None, R.fail(action, browser, f"{name} isn't open." if browser else "No browser window is open.")
        if self._backend.foreground() != window.hwnd and not self._backend.activate(window.hwnd):
            return None, R.fail(action, window.app, "I couldn't bring the browser to the front.", "focus refused")
        return window, None

    # ------------------------------------------------------------------ reading state

    def tabs(self, window: WindowInfo) -> list[TabInfo]:
        try:
            return self._backend.tabs(window.hwnd)
        except Exception:
            return []

    def current(self, browser: str | None = None) -> R:
        window = self.window(browser)
        if window is None:
            return R.fail("browser.current", browser, "No browser window is open.")
        url = self._safe_address(window)
        title = page_title(window.title)
        tabs = self.tabs(window)
        return R.ok("browser.current", window.app, f"You're on {title}." if title else "That's a blank tab.",
                    title=title, url=url, tabs=len(tabs), hwnd=window.hwnd)

    def _safe_address(self, window: WindowInfo) -> str | None:
        try:
            return self._backend.address(window.hwnd)
        except Exception:
            return None

    def _state(self, hwnd: int) -> tuple[str, str | None, int]:
        info = self._backend.get_window(hwnd)
        title = page_title(info.title) if info else ""
        return title, (self._backend.address(hwnd) if info else None), len(self._backend.tabs(hwnd)) if info else 0

    # ------------------------------------------------------------------ opening

    def open(self, browser: str | None = None, url: str | None = None, *, new_window: bool = False,
             private: bool = False) -> R:
        """Bring up a browser window (launching it if needed), optionally on a URL."""
        info = self.registry.get(browser) if browser else self.registry.default()
        if info is None:
            requested = self.registry.display(browser) if browser else "a browser"
            fallback = self.registry.default()
            if fallback is None:
                return R.fail("browser.open", browser, f"I can't find {requested} on this computer.")
            info, note = fallback, f"{requested} isn't installed, so I used {fallback.name}. "
        else:
            note = ""
        existing = self.windows(info.key)
        if existing and not new_window and not private:
            window = existing[0]
            if not self._backend.activate(window.hwnd):
                return R.fail("browser.open", info.name, f"{info.name} is open, but it wouldn't come to the front.")
            self._context.note_action("browser.open", window, opened=True)
            if url:
                result = self.navigate(url, new_tab=True, browser=info.key)
                result.details = note + result.details
                return result
            return R.ok("browser.open", info.name, f"{note}Switched to {info.name}.", hwnd=window.hwnd)
        before = {w.hwnd for w in self._backend.list_windows()}
        argv = [info.exe]
        if new_window or existing:
            argv.append("--new-window" if info.chromium else "-new-window")
        if private:
            argv.append("--incognito" if info.key == "chrome" else "--inprivate" if info.key == "edge"
                        else "-private-window" if info.key == "firefox" else "--incognito")
        if url:
            argv.append(normalize_url(url))
        try:
            self._backend.spawn(argv)
        except Exception as exc:
            return R.fail("browser.open", info.name, f"{info.name} wouldn't start.", str(exc))
        deadline = time.monotonic() + self._launch_timeout
        while time.monotonic() < deadline:
            self._sleep(0.1)
            fresh = [w for w in self.windows(info.key) if w.hwnd not in before]
            if fresh:
                window = fresh[0]
                if self._backend.foreground() != window.hwnd:
                    self._backend.activate(window.hwnd)
                self._context.note_action("browser.open", window, opened=True)
                what = "a private window" if private else "a new window" if (new_window or existing) else info.name
                details = f"{note}Opened {what}." if what != info.name else f"{note}{info.name}'s open."
                return R.ok("browser.open", info.name, details, hwnd=window.hwnd)
        return R(True, "browser.open", info.name, f"{note}I started {info.name}, but no window showed up yet.",
                 False, None, {})

    # ------------------------------------------------------------------ tabs

    def new_tab(self, url: str | None = None, *, browser: str | None = None) -> R:
        window, error = self._focused_window(browser, "browser.new_tab")
        if error is not None:
            return self.open(browser, url) if "isn't open" in error.details or "No browser" in error.details else error
        assert window is not None
        before = len(self.tabs(window))
        self._keyboard.chord([VK_CONTROL], ord("T"))
        deadline = time.monotonic() + 2.0
        verified = False
        while time.monotonic() < deadline:
            self._sleep(0.05)
            count = len(self.tabs(window))
            if before and count > before:
                verified = True
                break
            info = self._backend.get_window(window.hwnd)
            if not before and info is not None and is_blank(page_title(info.title), None):
                verified = True
                break
        self._context.note_action("browser.new_tab", window)
        if url:
            return self.navigate(url, browser=browser)
        return R(True, "browser.new_tab", window.app, "New tab's open." if verified else
                 "I pressed new tab, but I can't see it.", verified, None, {"hwnd": window.hwnd})

    def _match_tab(self, tabs: list[TabInfo], match: str) -> TabInfo | None:
        wanted = match.lower().strip()
        wanted = re.sub(r"^(?:the|my)\s+", "", wanted)
        wanted = re.sub(r"\s+tab$", "", wanted)
        best: tuple[int, TabInfo] | None = None
        for tab in tabs:
            name = clean_tab_name(tab.name).lower()
            score = 0
            if name == wanted:
                score = 100
            elif re.search(rf"\b{re.escape(wanted)}\b", name):
                score = 80 - min(30, len(name) // 10)
            elif wanted in name:
                score = 50
            if score and (best is None or score > best[0] or (score == best[0] and tab.selected)):
                best = (score, tab)
        return best[1] if best else None

    def close_tab(self, match: str | None = None, *, browser: str | None = None, hwnd: int | None = None) -> R:
        window = self._backend.get_window(hwnd) if hwnd else self.window(browser)
        if window is None:
            return R.fail("browser.close_tab", browser, "No browser window is open.")
        tabs = self.tabs(window)
        if match:
            target = self._match_tab(tabs, match)
            if target is None:
                return R.fail("browser.close_tab", window.app, f"I don't see a {match} tab.")
        else:
            target = next((t for t in tabs if t.selected), None)
        label = clean_tab_name(target.name) if target else page_title(window.title)
        if target is not None and self._backend.close_tab(window.hwnd, target.index):
            method = "uia"
        else:
            if target is not None and not target.selected:
                if not self._backend.select_tab(window.hwnd, target.index):
                    return R.fail("browser.close_tab", window.app, f"I couldn't switch to the {label} tab.")
            if self._backend.foreground() != window.hwnd and not self._backend.activate(window.hwnd):
                return R.fail("browser.close_tab", window.app, "I couldn't bring the browser to the front.")
            self._keyboard.chord([VK_CONTROL], ord("W"))
            method = "keys"
        deadline = time.monotonic() + 2.0
        verified = False
        while time.monotonic() < deadline:
            self._sleep(0.05)
            if not self._backend.is_window(window.hwnd):
                verified = True
                self._context.forget_window(window.hwnd)
                break
            remaining = self.tabs(window)
            if tabs and len(remaining) < len(tabs):
                verified = True
                break
        self._context.note_action("browser.close_tab", window if self._backend.is_window(window.hwnd) else None)
        if not verified:
            return R(True, "browser.close_tab", window.app, f"I tried to close the {label} tab, but it's still "
                     "there.", False, None, {"method": method})
        return R.ok("browser.close_tab", window.app, f"Closed the {label} tab." if label else "Closed the tab.",
                    method=method, title=label)

    def switch_tab(self, *, direction: str | None = None, index: int | None = None, match: str | None = None,
                   browser: str | None = None, hwnd: int | None = None) -> R:
        if hwnd:
            window = self._backend.get_window(hwnd)
            focused = window is not None and (self._backend.foreground() == hwnd or self._backend.activate(hwnd))
            error = None if focused else R.fail("browser.switch_tab", browser, "I couldn't bring the browser to the front.")
        else:
            window, error = self._focused_window(browser, "browser.switch_tab")
        if error is not None:
            return error
        assert window is not None
        tabs = self.tabs(window)
        if match:
            target = self._match_tab(tabs, match)
            if target is None:
                return R.fail("browser.switch_tab", window.app, f"I don't see a {match} tab.")
            if not self._backend.select_tab(window.hwnd, target.index):
                return R.fail("browser.switch_tab", window.app, f"I couldn't switch to the {match} tab.")
        elif index is not None:
            if index == -1:
                self._keyboard.chord([VK_CONTROL], ord("9"))
            elif 1 <= index <= 8:
                self._keyboard.chord([VK_CONTROL], ord(str(index)))
            elif tabs and 1 <= index <= len(tabs):
                self._backend.select_tab(window.hwnd, index - 1)
            else:
                return R.fail("browser.switch_tab", window.app, f"There's no tab {index}.")
        elif direction == "previous":
            self._keyboard.chord([VK_CONTROL, VK_SHIFT], VK_TAB)
        else:
            self._keyboard.chord([VK_CONTROL], VK_TAB)
        self._sleep(0.15)
        fresh = self._backend.get_window(window.hwnd) or window
        self._context.note_action("browser.switch_tab", fresh)
        title = page_title(fresh.title)
        return R.ok("browser.switch_tab", window.app, f"On {title}." if title else "Switched tabs.",
                    verified=fresh.title != window.title or bool(match), title=title)

    def reopen_tab(self, *, browser: str | None = None) -> R:
        return self._shortcut("browser.reopen_tab", [VK_CONTROL, VK_SHIFT], ord("T"), "Reopened the last tab.",
                              browser, expect="more_tabs")

    def back(self, *, browser: str | None = None) -> R:
        return self._shortcut("browser.back", [VK_MENU], 0x25, "Went back.", browser, expect="title")

    def forward(self, *, browser: str | None = None) -> R:
        return self._shortcut("browser.forward", [VK_MENU], 0x27, "Went forward.", browser, expect="title")

    def reload(self, *, browser: str | None = None) -> R:
        return self._shortcut("browser.reload", [], 0x74, "Reloaded the page.", browser, expect=None)

    def close_window(self, *, browser: str | None = None) -> R:
        window = self.window(browser)
        if window is None:
            return R.fail("browser.close_window", browser, "No browser window is open.")
        return self._windows.close_window(window)

    def zoom(self, how: str, *, browser: str | None = None) -> R:
        key = {"in": 0xBB, "out": 0xBD, "reset": ord("0")}[how]
        details = {"in": "Zoomed in.", "out": "Zoomed out.", "reset": "Reset the zoom."}[how]
        return self._shortcut("browser.zoom", [VK_CONTROL], key, details, browser, expect=None)

    def _shortcut(self, action: str, modifiers: list[int], vk: int, details: str, browser: str | None,
                  expect: str | None) -> R:
        window, error = self._focused_window(browser, action)
        if error is not None:
            return error
        assert window is not None
        before_title = window.title
        before_tabs = len(self.tabs(window)) if expect == "more_tabs" else 0
        self._keyboard.chord(modifiers, vk)
        verified = expect is None
        deadline = time.monotonic() + 2.5
        while expect and time.monotonic() < deadline:
            self._sleep(0.08)
            fresh = self._backend.get_window(window.hwnd)
            if expect == "title" and fresh is not None and fresh.title != before_title:
                verified = True
                break
            if expect == "more_tabs" and len(self.tabs(window)) > before_tabs:
                verified = True
                break
        self._context.note_action(action, window)
        if expect and not verified:
            details = details.rstrip(".") + ", but nothing seemed to change."
        return R(True, action, window.app, details, verified, None, {"hwnd": window.hwnd})

    # ------------------------------------------------------------------ navigation

    def navigate(self, url: str, *, new_tab: bool = False, browser: str | None = None, timeout_s: float = 12.0,
                 reuse_blank: bool = True) -> R:
        url = normalize_url(url)
        window = self.window(browser)
        if window is None:
            return self.open(browser, url)
        if self._backend.foreground() != window.hwnd and not self._backend.activate(window.hwnd):
            return R.fail("browser.navigate", window.app, "I couldn't bring the browser to the front.")
        if new_tab:
            current_url = self._safe_address(window)
            blank = reuse_blank and is_blank(page_title(window.title), current_url)
            if not blank:
                before = len(self.tabs(window))
                self._keyboard.chord([VK_CONTROL], ord("T"))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and before and len(self.tabs(window)) <= before:
                    self._sleep(0.05)
                self._sleep(0.1)
        before_title = (self._backend.get_window(window.hwnd) or window).title
        self._keyboard.chord([VK_CONTROL], ord("L"))
        self._sleep(0.08)
        # Typed, not pasted, so the user's clipboard stays untouched.
        if not self._keyboard.type_fast(url, window.hwnd):
            return R.fail("browser.navigate", window.app, "The browser lost focus while I was typing the address.")
        self._keyboard.tap(VK_DELETE)  # drop any inline autocomplete suggestion
        self._keyboard.tap(VK_RETURN)
        verified, title, address = self.wait_for_page(window, url, before_title, timeout_s)
        self._context.note_action("browser.navigate", self._backend.get_window(window.hwnd) or window)
        host = _host(url)
        if not verified:
            return R(True, "browser.navigate", window.app, f"I opened {host}, but the page hasn't finished loading.",
                     False, None, {"url": url, "address": address, "title": title, "hwnd": window.hwnd})
        return R.ok("browser.navigate", window.app, f"Opened {title or host}.", url=url, address=address,
                    title=title, hwnd=window.hwnd)

    def wait_for_page(self, window: WindowInfo, url: str, before_title: str, timeout_s: float) -> tuple[bool, str, str | None]:
        """Wait until the address bar shows the target host and the title has changed."""
        host = _host(url)
        target = _bare(url)
        deadline = time.monotonic() + timeout_s
        title, address = "", None
        same_since: float | None = None
        while time.monotonic() < deadline:
            self._sleep(0.15)
            info = self._backend.get_window(window.hwnd)
            if info is None:
                return False, "", None
            title = page_title(info.title)
            address = self._safe_address(window)
            host_ok = bool(address) and host and host in (address or "").lower()
            if host_ok and info.title != before_title and title and not title.lower().startswith(host):
                return True, title, address
            if address and _bare(address) == target and title and not title.lower().startswith(host):
                # Re-opening the page that's already showing: the title won't change, so give it a moment.
                same_since = same_since or time.monotonic()
                if time.monotonic() - same_since > 0.6:
                    return True, title, address
        return bool(address and host and host in address.lower()), title, address

    def search(self, query: str, engine: str | None = None, *, browser: str | None = None) -> R:
        engine_key = normalize_engine(engine)
        url = search_url(engine_key, query)
        window = self.window(browser)
        if window is None:
            result = self.open(browser, url)
        else:
            result = self.navigate(url, new_tab=True, browser=browser)
        self._context.last_search = SearchContext(engine_key, query, url, hwnd=(result.data or {}).get("hwnd"))
        engine_name = {"youtube": "YouTube", "github": "GitHub", "stack overflow": "Stack Overflow"}.get(
            engine_key, engine_key.title())
        if result.success:
            result.action = "browser.search"
            result.details = f"Here are the {engine_name} results for {query}."
        result.data.update({"engine": engine_key, "query": query})
        return result

    def scroll(self, direction: str = "down", amount: int = 1, *, browser: str | None = None) -> R:
        window, error = self._focused_window(browser, "browser.scroll")
        if error is not None:
            return error
        assert window is not None
        keys = {"down": 0x22, "up": 0x21, "top": 0x24, "bottom": 0x23}
        vk = keys.get(direction, 0x22)
        for _ in range(1 if direction in ("top", "bottom") else max(1, min(20, amount))):
            self._keyboard.tap(vk)
            self._sleep(0.05)
        self._context.note_action("browser.scroll", window)
        words = {"down": "Scrolled down.", "up": "Scrolled up.", "top": "At the top.", "bottom": "At the bottom."}
        return R.ok("browser.scroll", window.app, words.get(direction, "Scrolled."), verified=False)

    def click(self, name: str, *, browser: str | None = None) -> R:
        window = self.window(browser)
        if window is None:
            return R.fail("browser.click", browser, "No browser window is open.")
        before = window.title
        clicked = self._backend.invoke(window.hwnd, name)
        if clicked is None:
            return R.fail("browser.click", window.app, f"I couldn't find {name} on the page.")
        self._sleep(0.4)
        fresh = self._backend.get_window(window.hwnd) or window
        self._context.note_action("browser.click", fresh)
        return R(True, "browser.click", window.app, f"Clicked {clicked[:60]}.", fresh.title != before, None,
                 {"element": clicked})

    def page_text(self, *, browser: str | None = None, limit: int = 20000) -> tuple[WindowInfo | None, str | None, str | None]:
        window = self.window(browser)
        if window is None:
            return None, None, None
        return window, self._safe_address(window), self._backend.page_text(window.hwnd, limit)

    def find_tab(self, predicate, *, browser: str | None = None) -> tuple[WindowInfo | None, TabInfo | None]:
        for window in [w for w in [self.window(browser)] if w] + self.windows(browser):
            for tab in self.tabs(window):
                if predicate(clean_tab_name(tab.name)):
                    return window, tab
        return None, None

    # ------------------------------------------------------------------ video pages

    def open_video(self, url: str, *, browser: str | None = None) -> R:
        """Open a video, reusing a YouTube tab when there is one (so two videos don't play at once)."""
        window = self.window(browser)
        if window is None:
            result = self.open(browser, url)
            if result.success and "hwnd" not in result.data:
                fresh = self.window(browser)
                if fresh is not None:
                    result.data["hwnd"] = fresh.hwnd
            return result
        tabs = self.tabs(window)
        youtube = [t for t in tabs if re.search(r"\byoutube\b", clean_tab_name(t.name), re.I)]
        if youtube:
            target = next((t for t in youtube if t.selected), youtube[0])
            if not target.selected:
                self._backend.select_tab(window.hwnd, target.index)
                self._sleep(0.15)
            return self.navigate(url, new_tab=False, browser=browser, timeout_s=4.0)
        return self.navigate(url, new_tab=True, browser=browser, timeout_s=4.0)

    def tab_state(self, hwnd: int | None) -> tuple[str, str | None] | None:
        """(window title, address bar) of a browser window, for verification polling."""
        window = self._backend.get_window(hwnd) if hwnd else self.window()
        if window is None:
            return None
        return window.title, self._safe_address(window)

    def youtube_next(self, *, browser: str | None = None) -> R:
        """Next video on YouTube when its media session offers no "next" (Shift+N in the player)."""
        window, tab = self.find_tab(lambda name: bool(re.search(r"\byoutube\b", name, re.I)), browser=browser)
        if window is None or tab is None:
            return R.fail("media.next", "youtube", "I can't find the YouTube tab.")
        if not tab.selected:
            self._backend.select_tab(window.hwnd, tab.index)
        if self._backend.foreground() != window.hwnd and not self._backend.activate(window.hwnd):
            return R.fail("media.next", "youtube", "I couldn't bring the browser to the front.")
        before = window.title
        self._keyboard.chord([VK_SHIFT], ord("N"))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            self._sleep(0.2)
            fresh = self._backend.get_window(window.hwnd)
            if fresh is not None and fresh.title != before:
                return R.ok("media.next", "youtube", f"Skipped to {page_title(fresh.title)}.")
        return R(True, "media.next", "youtube", "Skipped.", False, None, {})
