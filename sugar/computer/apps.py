"""Applications: finding them by spoken name, launching, focusing and closing them.

The catalog is built from ``Get-StartApps`` (every Start-menu app, including
Store apps, with its AppUserModelID) plus a few built-ins, cached in
``data/apps.json`` and refreshed in the background. Apps are launched through
``shell:AppsFolder\\<AppID>``, which works for desktop and Store apps alike
and needs no hard-coded install paths.

:class:`ApplicationController` adds the window side: "open Notepad" focuses
a running Notepad instead of stacking copies, waits for a new window when it
does launch, and "close Notepad" closes Notepad's *windows* politely (the
app can still ask to save) rather than killing a process image.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.context import DesktopContext
from sugar.computer.results import ComputerActionResult
from sugar.computer.windows import PROTECTED_PROCESSES, WindowManager, display_name
from sugar.core.processes import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from sugar.computer.browser import BrowserRegistry

log = logging.getLogger(__name__)
R = ComputerActionResult


@dataclass
class AppEntry:
    name: str
    target: str  # AppUserModelID, executable, or URI
    kind: str = "aumid"  # aumid | exe | uri
    processes: list[str] = field(default_factory=list)


BUILTINS = [
    AppEntry("Task Manager", "taskmgr.exe", "exe", ["Taskmgr.exe"]),
    AppEntry("Command Prompt", "cmd.exe", "exe", ["cmd.exe"]),
    AppEntry("PowerShell", "powershell.exe", "exe", ["powershell.exe"]),
    AppEntry("File Explorer", "explorer.exe", "exe", []),
    AppEntry("Settings", "ms-settings:", "uri", ["SystemSettings.exe"]),
    AppEntry("Control Panel", "control.exe", "exe", []),
    AppEntry("Snipping Tool", "ms-screenclip:", "uri", []),
]

# spoken alias -> canonical display name fragment
ALIASES = {
    "chrome": "google chrome", "google": "google chrome", "vs code": "visual studio code",
    "vscode": "visual studio code", "code": "visual studio code", "visual studio code": "visual studio code",
    "v s code": "visual studio code", "edge": "microsoft edge", "terminal": "terminal",
    "windows terminal": "terminal", "cmd": "command prompt", "command prompt": "command prompt",
    "powershell": "powershell", "explorer": "file explorer", "file explorer": "file explorer",
    "files": "file explorer", "my files": "file explorer", "this pc": "file explorer",
    "calculator": "calculator", "calc": "calculator", "settings": "settings", "task manager": "task manager",
    "word": "word", "excel": "excel", "powerpoint": "powerpoint", "outlook": "outlook", "teams": "teams",
    "claude": "claude", "claude desktop": "claude", "spotify": "spotify", "discord": "discord",
    "brave": "brave", "notepad": "notepad", "postman": "postman", "docker": "docker desktop",
    "paint": "paint", "ms paint": "paint", "microsoft paint": "paint", "mspaint": "paint", "windows paint": "paint",
    "windows settings": "settings", "system settings": "settings", "pc settings": "settings",
    "the control panel": "control panel", "taskmgr": "task manager",
    "windows explorer": "file explorer", "camera app": "camera", "the camera app": "camera",
    "snipping tool": "snipping tool", "screenshot tool": "snipping tool", "clock": "clock", "alarms": "clock",
    "photos": "photos", "media player": "media player", "store": "microsoft store",
    "microsoft store": "microsoft store", "visual studio": "visual studio",
}

# Windows Settings pages: "open Bluetooth settings", "open the display settings", "settings for sound".
SETTINGS_PAGES = {
    "bluetooth": "bluetooth", "devices": "bluetooth", "display": "display", "screen": "display",
    "brightness": "display", "night light": "nightlight", "sound": "sound", "audio": "sound", "volume": "sound",
    "wifi": "network-wifi", "wi fi": "network-wifi", "network": "network", "internet": "network",
    "vpn": "network-vpn", "airplane mode": "network-airplanemode", "windows update": "windowsupdate",
    "update": "windowsupdate", "updates": "windowsupdate", "apps": "appsfeatures", "installed apps": "appsfeatures",
    "default apps": "defaultapps", "startup apps": "startupapps", "startup": "startupapps",
    "battery": "batterysaver", "power": "powersleep", "power and battery": "powersleep",
    "personalization": "personalization", "personalisation": "personalization", "background": "personalization-background",
    "wallpaper": "personalization-background", "themes": "themes", "colors": "colors", "colours": "colors",
    "lock screen": "lockscreen", "taskbar": "taskbar", "notifications": "notifications", "focus": "focus",
    "privacy": "privacy", "camera": "privacy-webcam", "camera privacy": "privacy-webcam",
    "microphone": "privacy-microphone", "microphone privacy": "privacy-microphone", "location": "privacy-location",
    "accounts": "accounts", "your info": "yourinfo", "sign in": "signinoptions", "sign in options": "signinoptions",
    "date and time": "dateandtime", "time": "dateandtime", "language": "regionlanguage", "region": "regionlanguage",
    "keyboard": "typing", "typing": "typing", "mouse": "mousetouchpad", "touchpad": "devices-touchpad",
    "printers": "printers", "storage": "storagesense", "about": "about", "system": "system",
    "multitasking": "multitasking", "clipboard": "clipboard", "remote desktop": "remotedesktop",
    "gaming": "gaming-gamebar", "game mode": "gaming-gamemode", "accessibility": "easeofaccess",
    "narrator": "easeofaccess-narrator", "magnifier": "easeofaccess-magnifier", "mobile hotspot": "network-mobilehotspot",
    "proxy": "network-proxy", "ethernet": "network-ethernet", "recovery": "recovery", "activation": "activation",
    "windows security": "windowsdefender", "security": "windowsdefender", "backup": "backup",
    "graphics": "display-advancedgraphics", "notifications and actions": "notifications",
}
_SETTINGS_RE = re.compile(r"^(?:the )?(?:windows )?(?P<page>[a-z ]+?) settings(?: page)?$|"
                          r"^settings (?:for|of|on) (?:the )?(?P<page2>[a-z ]+)$")


def settings_page(spoken: str) -> AppEntry | None:
    """An ms-settings: page for "bluetooth settings", "the display settings", "settings for sound"."""
    match = _SETTINGS_RE.match(spoken)
    if not match:
        return None
    page = (match.group("page") or match.group("page2") or "").strip()
    uri = SETTINGS_PAGES.get(page)
    if uri is None:
        return None
    title = page[:1].upper() + page[1:]
    return AppEntry(f"{title} settings", f"ms-settings:{uri}", "uri", ["SystemSettings.exe"])

# canonical name fragment -> process image names (for matching windows and force-closing)
PROCESSES = {
    "google chrome": ["chrome.exe"], "visual studio code": ["Code.exe"], "microsoft edge": ["msedge.exe"],
    "spotify": ["Spotify.exe"], "discord": ["Discord.exe"], "brave": ["brave.exe"], "notepad": ["notepad.exe"],
    "terminal": ["WindowsTerminal.exe"], "calculator": ["CalculatorApp.exe"], "postman": ["Postman.exe"],
    "claude": ["claude.exe"], "word": ["WINWORD.EXE"], "excel": ["EXCEL.EXE"], "powerpoint": ["POWERPNT.EXE"],
    "outlook": ["OUTLOOK.EXE", "olk.exe"], "teams": ["ms-teams.exe"], "docker desktop": ["Docker Desktop.exe"],
    "paint": ["mspaint.exe"],
}
NEVER_CLOSE = {p.lower() for p in PROTECTED_PROCESSES}
BROWSER_WORDS = {"browser", "web browser", "the browser", "my browser", "internet", "the internet", "web"}
# Apps that open a fresh document (new tab) when asked to "open" while already running, so dictated text
# never lands in a document the user already had open.
NEW_DOCUMENT_KEYS = {"notepad": ["ctrl", "n"]}


def _norm(text: str) -> str:
    text = text.lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AppCatalog:
    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path
        self._entries: list[AppEntry] = list(BUILTINS)
        self._lock = threading.Lock()
        self.loaded_at = 0.0
        self._load_cache()

    # ------------------------------------------------------------------ discovery

    def _load_cache(self) -> None:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            entries = [AppEntry(**item) for item in data.get("apps", [])]
            if entries:
                with self._lock:
                    self._entries = entries
                self.loaded_at = data.get("ts", 0.0)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    def refresh(self) -> int:
        """Re-scan Start-menu apps (blocking, ~1–2 s)."""
        if os.name != "nt":
            return len(self._entries)
        try:
            output = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
            ).stdout
            items = json.loads(output) if output.strip() else []
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            log.warning("could not list Start-menu apps: %s", exc)
            return len(self._entries)
        if isinstance(items, dict):
            items = [items]
        entries = list(BUILTINS)
        seen = {_norm(e.name) for e in entries}
        for item in items:
            name, app_id = item.get("Name"), item.get("AppID")
            if not name or not app_id or _norm(name) in seen:
                continue
            seen.add(_norm(name))
            canonical = next((c for c in PROCESSES if c in _norm(name)), None)
            entries.append(AppEntry(name, app_id, "aumid", PROCESSES.get(canonical, [])))
        for name, exe in self._registered_executables():
            if _norm(name) not in seen:
                seen.add(_norm(name))
                entries.append(AppEntry(name, exe, "exe", [Path(exe).name]))
        with self._lock:
            self._entries = entries
        self.loaded_at = time.time()
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps({"ts": self.loaded_at, "apps": [asdict(e) for e in entries]}),
                                        encoding="utf-8")
        except OSError:
            pass
        return len(entries)

    @staticmethod
    def _registered_executables() -> list[tuple[str, str]]:
        """Apps registered outside the Start menu: "App Paths" in the registry and execution aliases
        (%LOCALAPPDATA%\\Microsoft\\WindowsApps). Lower priority than Start-menu names."""
        found: list[tuple[str, str]] = []
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    root = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
                except OSError:
                    continue
                with root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        key_name = winreg.EnumKey(root, i)
                        try:
                            with winreg.OpenKey(root, key_name) as key:
                                target = str(winreg.QueryValueEx(key, "")[0]).strip('"')
                        except OSError:
                            continue
                        if target.lower().endswith(".exe") and Path(target).exists():
                            found.append((Path(key_name).stem, target))
        except ImportError:
            pass
        aliases = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
        if aliases.is_dir():
            for exe in aliases.glob("*.exe"):
                found.append((exe.stem, str(exe)))
        return found

    def names(self) -> list[str]:
        with self._lock:
            return [e.name for e in self._entries]

    def add(self, entry: AppEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    # ------------------------------------------------------------------ resolution

    def resolve(self, spoken: str) -> AppEntry | None:
        query = _norm(spoken)
        query = re.sub(r"^(?:the|my|a|an|a new)\s+", "", query)
        query = re.sub(r"\s+(?:app|application|program|window)$", "", query)
        if not query or len(query) > 40:
            return None
        page = settings_page(query)
        if page is not None:
            return page
        found = self._resolve(query)
        if found is None and query.endswith(" browser") and query != "web browser":
            found = self._resolve(query[: -len(" browser")])  # "brave browser" → Brave
        return found

    def _resolve(self, query: str) -> AppEntry | None:
        target = ALIASES.get(query, query)
        with self._lock:
            entries = list(self._entries)
        by_name = {_norm(e.name): e for e in entries}
        if target in by_name:
            return by_name[target]
        # Prefix / containment ("chrome" → "Google Chrome"), preferring the shortest name.
        contained = [e for n, e in by_name.items() if re.search(rf"\b{re.escape(target)}\b", n)]
        if contained:
            return min(contained, key=lambda e: len(e.name))
        close = difflib.get_close_matches(target, list(by_name), n=1, cutoff=0.82)
        return by_name[close[0]] if close else None

    # ------------------------------------------------------------------ launching

    def executable_for(self, entry: AppEntry) -> str | None:
        """Best effort: find an .exe for apps that need command-line arguments."""
        name = _norm(entry.name)
        if "visual studio code" in name:
            for candidate in (shutil.which("code"), shutil.which("code.cmd"),
                              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                              r"C:\Program Files\Microsoft VS Code\Code.exe"):
                if candidate and Path(candidate).exists():
                    return candidate
            target = self._shortcut_target("Visual Studio Code")
            if target and Path(target).exists():
                return target
            return None
        if entry.kind == "exe":
            return shutil.which(entry.target) or entry.target
        return None

    @staticmethod
    def _shortcut_target(name: str) -> str | None:
        roots = [Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
                 Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
        for root in roots:
            for link in root.rglob(f"{name}.lnk"):
                script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SUGAR_LNK);"
                          "Write-Output $s.TargetPath")
                try:
                    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                            env={**os.environ, "SUGAR_LNK": str(link)}, capture_output=True,
                                            text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
                    target = result.stdout.strip()
                    if target:
                        return target
                except (subprocess.SubprocessError, OSError):
                    continue
        return None

    def launch(self, entry: AppEntry, arguments: list[str] | None = None, backend: DesktopBackend | None = None) -> None:
        if arguments:
            executable = self.executable_for(entry)
            if executable is None:
                raise RuntimeError(f"can't pass arguments to {entry.name}: executable not found")
            if backend is not None:
                backend.spawn([executable, *arguments])
            else:
                subprocess.Popen([executable, *arguments], creationflags=CREATE_NO_WINDOW, close_fds=True)
            return
        if entry.kind == "aumid":
            target = f"shell:AppsFolder\\{entry.target}"
            backend.start(target) if backend is not None else os.startfile(target)
        elif entry.kind == "uri":
            backend.start(entry.target) if backend is not None else os.startfile(entry.target)
        elif backend is not None:
            backend.spawn([entry.target])
        else:
            subprocess.Popen([entry.target], close_fds=True)


class ApplicationController:
    def __init__(self, backend: DesktopBackend, catalog: AppCatalog, windows: WindowManager,
                 context: DesktopContext, browsers: BrowserRegistry | None = None, *,
                 launch_timeout_s: float = 12.0, sleep=time.sleep) -> None:
        self._backend = backend
        self.catalog = catalog
        self._windows = windows
        self._context = context
        self._browsers = browsers
        self._launch_timeout = launch_timeout_s
        self._sleep = sleep

    # ------------------------------------------------------------------ resolution

    def resolve(self, spoken: str) -> tuple[AppEntry | None, str | None]:
        """(entry, note). The note explains a substitution ("Chrome isn't installed, so …")."""
        entry = self.catalog.resolve(spoken)
        if entry is not None:
            return entry, None
        if self._browsers is None:
            return None, None
        query = _norm(spoken)
        query = re.sub(r"^(?:the|my|a|an|a new)\s+", "", query)
        requested = self._browsers.match(query)
        if requested is None and query not in BROWSER_WORDS and not query.endswith(" browser"):
            return None, None
        installed = self._browsers.get(requested) if requested else None
        chosen = installed or self._browsers.default()
        if chosen is None:
            return None, None
        entry = self.catalog.resolve(chosen.name) or AppEntry(chosen.name, chosen.exe, "exe", [chosen.process])
        note = None
        if requested and installed is None:
            note = f"{self._browsers.display(requested)} isn't installed, so I used {chosen.name}."
        return entry, note

    def resolve_name(self, spoken: str) -> AppEntry | None:
        return self.resolve(spoken)[0]

    def windows_for(self, entry: AppEntry) -> list[WindowInfo]:
        images = {p.lower() for p in entry.processes}
        stems = set(WindowManager.stems_for(entry.name))
        matches = []
        for window in self._windows.list():
            if window.process == "explorer.exe" and window.class_name not in ("CabinetWClass", "ExploreWClass"):
                continue
            if window.process in images or window.app.lower() in stems:
                matches.append(window)
        recency = {w.hwnd: i for i, w in enumerate(self._context.recent_windows())}
        matches.sort(key=lambda w: recency.get(w.hwnd, 99))
        return matches

    # ------------------------------------------------------------------ actions

    def open(self, name: str, *, new_window: bool = False, arguments: list[str] | None = None) -> R:
        entry, note = self.resolve(name)
        if entry is None:
            return R.fail("app.open", name, f"I couldn't find an app called {name}.")
        prefix = f"{note} " if note else ""
        page = entry.kind == "uri" and not entry.target.endswith(":")  # e.g. ms-settings:bluetooth: navigate
        existing = [] if arguments or page else self.windows_for(entry)
        if existing and not new_window:
            return self._reuse(entry, existing[0], prefix)
        if existing and new_window and entry.name.lower() in NEW_DOCUMENT_KEYS:
            return self._reuse(entry, existing[0], prefix)  # "a new notepad" → new tab in the running Notepad
        before = {w.hwnd for w in self._backend.list_windows()}
        try:
            self.catalog.launch(entry, arguments, backend=self._backend)
        except Exception as exc:
            return R.fail("app.open", entry.name, f"{entry.name} wouldn't open.", str(exc))
        window = self._wait_for_new_window(entry, before)
        if window is None and page:
            # The page opened in the Settings window that was already there.
            running = self.windows_for(entry)
            if running:
                self._backend.activate(running[0].hwnd)
                self._context.note_action("app.open", running[0], opened=True)
                return R.ok("app.open", entry.name, f"Opened {entry.name}.", hwnd=running[0].hwnd)
        if window is None:
            return R(True, "app.open", entry.name, f"{prefix}I started {entry.name}, but its window hasn't "
                     "appeared yet.", False, None, {})
        if self._backend.foreground() != window.hwnd:
            self._backend.activate(window.hwnd)
        self._context.note_action("app.open", window, opened=True)
        focused = self._backend.foreground() == window.hwnd
        return R(True, "app.open", entry.name, f"{prefix}{display_name(window)}'s open.", focused, None,
                 {"hwnd": window.hwnd, "launched": True})

    def _reuse(self, entry: AppEntry, window: WindowInfo, prefix: str) -> R:
        if not self._backend.activate(window.hwnd):
            return R.fail("app.open", entry.name, f"{display_name(window)} is open, but Windows wouldn't bring it "
                          "to the front.", "focus refused")
        keys = NEW_DOCUMENT_KEYS.get(window.app.lower())
        if keys:
            before = len(self._backend.tabs(window.hwnd))
            from sugar.computer.keys import parse_combo

            vks = parse_combo(keys)
            for vk in vks:
                self._backend.key(vk, up=False)
                self._sleep(0.004)
            for vk in reversed(vks):
                self._backend.key(vk, up=True)
                self._sleep(0.004)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and before and len(self._backend.tabs(window.hwnd)) <= before:
                self._sleep(0.05)
            fresh = self._backend.get_window(window.hwnd) or window
            self._context.note_action("app.open", fresh, opened=True)
            return R.ok("app.open", entry.name, f"{prefix}Opened a new {display_name(window)} tab.",
                        hwnd=window.hwnd, launched=False)
        self._context.note_action("app.open", window, opened=True)
        return R.ok("app.open", entry.name, f"{prefix}Switched to {display_name(window)}.", hwnd=window.hwnd,
                    launched=False)

    def _wait_for_new_window(self, entry: AppEntry, before: set[int]) -> WindowInfo | None:
        deadline = time.monotonic() + self._launch_timeout
        foreground_candidate: WindowInfo | None = None
        while time.monotonic() < deadline:
            self._sleep(0.1)
            for window in self.windows_for(entry):
                if window.hwnd not in before:
                    return window
            fg = self._backend.get_window(self._backend.foreground())
            if fg is not None and fg.hwnd not in before and not self._context.is_own(fg):
                foreground_candidate = fg
            if foreground_candidate is not None and not entry.processes and time.monotonic() > deadline - \
                    self._launch_timeout + 1.5:
                return foreground_candidate  # unknown process name: trust the new foreground window
        # The app may have reused an existing window (single-instance apps).
        existing = self.windows_for(entry)
        return existing[0] if existing else foreground_candidate

    def close(self, name: str) -> R:
        entry, _note = self.resolve(name)
        if entry is None:
            windows = self._windows.find(name)
            if not windows:
                return R.fail("app.close", name, f"{name} isn't open.")
            return self._windows.close_window(windows[0])
        windows = self.windows_for(entry)
        if not windows:
            return R.ok("app.close", entry.name, f"{entry.name} isn't open.", verified=True, closed=0)
        closed = 0
        for window in windows:
            result = self._windows.close_window(window)
            if result.data.get("waiting_for"):
                return result
            if not result.success:
                return result
            closed += 1
        label = display_name(windows[0])
        details = f"Closed {label}." if closed == 1 else f"Closed {closed} {label} windows."
        return R.ok("app.close", entry.name, details, closed=closed)

    def force_close(self, name: str) -> R:
        entry, _note = self.resolve(name)
        windows = self.windows_for(entry) if entry else self._windows.find(name)
        images = {w.process for w in windows}
        if entry is not None:
            images |= {p.lower() for p in entry.processes}
        images -= NEVER_CLOSE
        if not images:
            return R.fail("app.force_close", name, f"{name} isn't running." if entry else
                          f"I couldn't find {name}.")
        pids: set[int] = set()
        for image in images:
            pids.update(self._backend.processes(image))
        if not pids:
            return R.ok("app.force_close", name, f"{entry.name if entry else name} isn't running.", closed=0)
        failed = [pid for pid in pids if not self._backend.kill_process(pid)]
        remaining = [w for w in windows if self._backend.is_window(w.hwnd)]
        for window in windows:
            self._context.forget_window(window.hwnd)
        label = entry.name if entry else name
        if failed or remaining:
            return R.fail("app.force_close", label, f"I couldn't end every {label} process.",
                          f"{len(failed)} processes refused", killed=len(pids) - len(failed))
        return R.ok("app.force_close", label, f"Force-closed {label}.", killed=len(pids))

    def window_action(self, name: str, state: str) -> R:
        entry, _note = self.resolve(name)
        windows = self.windows_for(entry) if entry else []
        target: str | int = windows[0].hwnd if windows else name
        return self._windows.set_state(target, state)
