"""Computer control: windows, keyboard, apps, browser, media and screen.

    ComputerControl (engine.py) ─ one sugar-desktop worker thread
      ├─ WindowManager        windows.py   Win32 window state, focus, close, snap
      ├─ KeyboardController   keyboard.py  layout-aware SendInput typing, shortcuts, edits
      ├─ ApplicationController apps.py     Start-menu catalog + app windows
      ├─ BrowserController    browser.py   tabs/address bar via UI Automation, navigation, search
      ├─ MediaController      media.py     SMTC sessions, Spotify, YouTube (youtube.py)
      ├─ ScreenController     screen.py    capture, inspect, pointer; VisionProvider seam
      └─ DesktopContext       context.py   foreground history, "this"/"that", pending dialogs

Every action returns a :class:`ComputerActionResult` that separates
"it ran" from "Sugar saw it happen".
"""

from sugar.computer.engine import ComputerControl, create_backend
from sugar.computer.results import ComputerActionResult

__all__ = ["ComputerActionResult", "ComputerControl", "create_backend"]
