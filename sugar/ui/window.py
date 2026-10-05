"""Hosting the UI in a native window.

Preference order (``ui.window``):
  * ``webview`` — pywebview (Edge WebView2): a real app window whose close
    button shuts Sugar down;
  * ``edge`` — Microsoft Edge in app mode (no tabs/address bar);
  * ``browser`` — the default browser;
  * ``none`` — headless (voice only; the UI can still be opened later).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def webview_available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def run_webview(url: str, on_closed: Callable[[], None], title: str = "Sugar") -> None:
    """Blocks the calling (main) thread until the window is closed."""
    import webview

    window = webview.create_window(title, url, width=1280, height=860, min_size=(860, 600),
                                   background_color="#07080d", text_select=True)
    window.events.closed += lambda: on_closed()
    webview.start(private_mode=False, storage_path=str(Path(os.environ.get("LOCALAPPDATA", ".")) / "Sugar" / "webview"))


def _edge_path() -> str | None:
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


def open_external(url: str, mode: str) -> bool:
    if mode in ("edge", "auto"):
        edge = _edge_path()
        if edge:
            subprocess.Popen([edge, f"--app={url}", "--window-size=1280,860", "--no-first-run"], close_fds=True)
            return True
    if mode in ("browser", "edge", "auto"):
        return webbrowser.open(url)
    return False
