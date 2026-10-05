"""Finding and launching Windows applications by spoken name.

The catalog is built from ``Get-StartApps`` (every Start-menu app, including
Store apps, with its AppUserModelID) plus a few built-ins, cached in
``data/apps.json`` and refreshed in the background. Apps are launched through
``shell:AppsFolder\\<AppID>``, which works for desktop and Store apps alike
and needs no hard-coded install paths (the old code's ``code`` / ``start
chrome`` calls failed when those weren't on PATH).
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

from sugar.core.processes import CREATE_NO_WINDOW

log = logging.getLogger(__name__)


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
}

# canonical name fragment -> process image names (for closing)
PROCESSES = {
    "google chrome": ["chrome.exe"], "visual studio code": ["Code.exe"], "microsoft edge": ["msedge.exe"],
    "spotify": ["Spotify.exe"], "discord": ["Discord.exe"], "brave": ["brave.exe"], "notepad": ["notepad.exe"],
    "terminal": ["WindowsTerminal.exe"], "calculator": ["CalculatorApp.exe"], "postman": ["Postman.exe"],
    "claude": ["claude.exe"], "word": ["WINWORD.EXE"], "excel": ["EXCEL.EXE"], "powerpoint": ["POWERPNT.EXE"],
    "outlook": ["OUTLOOK.EXE", "olk.exe"], "teams": ["ms-teams.exe"], "docker desktop": ["Docker Desktop.exe"],
}
NEVER_CLOSE = {"explorer.exe", "dwm.exe", "winlogon.exe", "csrss.exe", "lsass.exe", "services.exe", "svchost.exe"}


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

    def names(self) -> list[str]:
        with self._lock:
            return [e.name for e in self._entries]

    # ------------------------------------------------------------------ resolution

    def resolve(self, spoken: str) -> AppEntry | None:
        query = _norm(spoken)
        query = re.sub(r"^(?:the|my)\s+", "", query)
        query = re.sub(r"\s+(?:app|application|program)$", "", query)
        if not query or len(query) > 40:
            return None
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

    # ------------------------------------------------------------------ actions

    def launch(self, entry: AppEntry, arguments: list[str] | None = None) -> None:
        if arguments:
            executable = self.executable_for(entry)
            if executable is None:
                raise RuntimeError(f"can't pass arguments to {entry.name}: executable not found")
            subprocess.Popen([executable, *arguments], creationflags=CREATE_NO_WINDOW, close_fds=True)
            return
        if entry.kind == "aumid":
            os.startfile(f"shell:AppsFolder\\{entry.target}")
        elif entry.kind == "uri":
            os.startfile(entry.target)
        else:
            subprocess.Popen([entry.target], close_fds=True)

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

    def running_processes(self) -> list[str]:
        try:
            output = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                    timeout=10, creationflags=CREATE_NO_WINDOW).stdout
        except (subprocess.SubprocessError, OSError):
            return []
        names = []
        for line in output.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if parts and parts[0]:
                names.append(parts[0].strip('"'))
        return names

    def close(self, entry: AppEntry) -> list[str]:
        """Ask the app's windows to close (no /F, so the app can prompt to save)."""
        candidates = entry.processes or PROCESSES.get(next((c for c in PROCESSES if c in _norm(entry.name)), ""), [])
        if not candidates:
            stem = _norm(entry.name).split(" ")[-1]
            candidates = [p for p in self.running_processes() if _norm(p).startswith(stem)]
        running = {p.lower() for p in self.running_processes()}
        closed = []
        for image in candidates:
            if image.lower() in NEVER_CLOSE or image.lower() not in running:
                continue
            subprocess.run(["taskkill", "/IM", image], capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            closed.append(image)
        return closed
