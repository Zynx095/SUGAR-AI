"""Desktop control: apps, volume, keyboard, clipboard, screen, time."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

log = logging.getLogger(__name__)

SAFE_KEYS = {
    "enter", "tab", "esc", "escape", "space", "backspace", "up", "down", "left", "right", "home", "end",
    "pageup", "pagedown", "f5", "f11", "playpause", "nexttrack", "prevtrack", "volumeup", "volumedown",
    "volumemute", "ctrl", "shift", "alt", "win", "c", "v", "x", "z", "y", "s", "t", "w", "n", "r", "l", "f",
    "a", "p", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
}


def _speakers():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def _set_volume(level: int) -> int:
    volume = _speakers()
    volume.SetMute(0, None)
    volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level / 100.0)), None)
    return round(volume.GetMasterVolumeLevelScalar() * 100)


def _step_volume(delta: int) -> int:
    volume = _speakers()
    current = volume.GetMasterVolumeLevelScalar() * 100
    return _set_volume(int(round(current + delta)))


def _mute(muted: bool) -> None:
    _speakers().SetMute(1 if muted else 0, None)


def _paste_text(text: str) -> None:
    """Type via the clipboard (handles any Unicode), then restore the clipboard."""
    import pyautogui
    import pyperclip

    previous = None
    try:
        previous = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def register(registry: ToolRegistry, services: ToolServices) -> None:
    apps = services.apps
    working = services.working
    data_dir = services.data_dir

    # ---------------------------------------------------------------- apps

    async def open_app(args: dict[str, Any]) -> ToolResult:
        entry = apps.resolve(args["name"])
        if entry is None:
            return ToolResult.failure(f"I couldn't find an app called {args['name']}.")
        try:
            await asyncio.to_thread(apps.launch, entry)
        except Exception as exc:
            return ToolResult.failure(f"{entry.name} wouldn't open.", str(exc))
        working.record_action("app", f"opened {entry.name}", tool="app.open", args={"name": entry.name})
        return ToolResult(True, f"Opening {entry.name}.", data={"app": entry.name})

    async def close_app(args: dict[str, Any]) -> ToolResult:
        entry = apps.resolve(args["name"])
        if entry is None:
            return ToolResult.failure(f"I couldn't find an app called {args['name']}.")
        closed = await asyncio.to_thread(apps.close, entry)
        if not closed:
            return ToolResult(True, f"{entry.name} isn't running.", data={"closed": []})
        working.record_action("app", f"closed {entry.name}", tool="app.close", args={"name": entry.name})
        return ToolResult(True, f"Closed {entry.name}.", data={"closed": closed})

    async def list_apps(args: dict[str, Any]) -> ToolResult:
        query = (args.get("filter") or "").lower()
        names = [n for n in apps.names() if query in n.lower()][:60]
        return ToolResult(True, f"{len(names)} apps found.", data={"apps": names})

    registry.register(Tool("app.open", "Open a desktop application by name (e.g. Chrome, VS Code, Spotify, Terminal).",
                           params(["name"], name={"type": "string"}), open_app, PermissionLevel.NON_DESTRUCTIVE, 15,
                           frozenset({"agent", "chat"}), describe=lambda a: f"open {a.get('name')}"))
    registry.register(Tool("app.close", "Close a running application gracefully (it may ask to save).",
                           params(["name"], name={"type": "string"}), close_app, PermissionLevel.NON_DESTRUCTIVE, 15,
                           frozenset({"agent"}), describe=lambda a: f"close {a.get('name')}"))
    registry.register(Tool("app.list", "List installed applications, optionally filtered by a name fragment.",
                           params(filter={"type": "string"}), list_apps, PermissionLevel.READ, 10, frozenset({"agent"})))

    # ---------------------------------------------------------------- volume

    async def volume_set(args: dict[str, Any]) -> ToolResult:
        level = await asyncio.to_thread(_set_volume, int(args["level"]))
        return ToolResult(True, f"Volume {level}.", data={"level": level})

    async def volume_change(args: dict[str, Any]) -> ToolResult:
        level = await asyncio.to_thread(_step_volume, int(args.get("delta", 10)))
        return ToolResult(True, f"Volume {level}.", data={"level": level})

    async def volume_mute(args: dict[str, Any]) -> ToolResult:
        muted = bool(args.get("muted", True))
        await asyncio.to_thread(_mute, muted)
        return ToolResult(True, "Muted." if muted else "Unmuted.", data={"muted": muted})

    registry.register(Tool("volume.set", "Set the system volume to a percentage.",
                           params(["level"], level={"type": "integer", "minimum": 0, "maximum": 100}),
                           volume_set, PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"agent", "chat"})))
    registry.register(Tool("volume.change", "Raise or lower the system volume by a number of points.",
                           params(["delta"], delta={"type": "integer", "minimum": -100, "maximum": 100}),
                           volume_change, PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"agent"})))
    registry.register(Tool("volume.mute", "Mute or unmute the system audio.",
                           params(muted={"type": "boolean", "default": True}), volume_mute,
                           PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"agent"})))

    # ---------------------------------------------------------------- keyboard

    async def type_text(args: dict[str, Any]) -> ToolResult:
        text = str(args["text"]).rstrip("\r\n")
        delay = float(args.get("delay", services.settings.permissions.typing_delay_s))
        if not text:
            return ToolResult.failure("There's nothing to type.")
        await asyncio.sleep(max(0.0, delay))
        await asyncio.to_thread(_paste_text, text)
        working.record_action("typing", f"typed {len(text)} characters", tool="keyboard.type")
        return ToolResult(True, "Typed it.", data={"chars": len(text)}, speak=False)

    async def press_keys(args: dict[str, Any]) -> ToolResult:
        import pyautogui

        keys = [k.strip().lower() for k in args["keys"] if k.strip()]
        unknown = [k for k in keys if k not in SAFE_KEYS]
        if not keys or unknown:
            return ToolResult.failure(f"I won't press {', '.join(unknown) or 'nothing'}.")
        await asyncio.to_thread(pyautogui.hotkey, *keys)
        working.record_action("keyboard", "pressed " + "+".join(keys), tool="keyboard.press")
        return ToolResult(True, "Done.", data={"keys": keys})

    registry.register(Tool("keyboard.type", "Type text into the focused window (after a short delay so the user can focus it).",
                           params(["text"], text={"type": "string"}, delay={"type": "number", "minimum": 0, "maximum": 10}),
                           type_text, PermissionLevel.NON_DESTRUCTIVE, 60, frozenset({"agent"}),
                           describe=lambda a: f"type “{str(a.get('text'))[:60]}” into the focused window"))
    registry.register(Tool("keyboard.press", "Press a key or key combination in the focused window, e.g. ['ctrl','s'].",
                           params(["keys"], keys={"type": "array", "items": {"type": "string"}}), press_keys,
                           PermissionLevel.SENSITIVE, 10, frozenset({"agent"}),
                           describe=lambda a: "press " + "+".join(a.get("keys", []))))

    # ---------------------------------------------------------------- clipboard

    async def clipboard_read(args: dict[str, Any]) -> ToolResult:
        import pyperclip

        text = await asyncio.to_thread(pyperclip.paste)
        if not text:
            return ToolResult(True, "Your clipboard is empty.", data={"text": ""})
        preview = text if len(text) <= 300 else text[:300] + "…"
        return ToolResult(True, "Here's what's on your clipboard.", data={"text": text[:5000]},
                          display=f"```\n{preview}\n```")

    async def clipboard_write(args: dict[str, Any]) -> ToolResult:
        import pyperclip

        await asyncio.to_thread(pyperclip.copy, str(args["text"]))
        return ToolResult(True, "Copied.", data={"chars": len(str(args["text"]))})

    registry.register(Tool("clipboard.read", "Read the text currently on the clipboard.", params(), clipboard_read,
                           PermissionLevel.READ, 5, frozenset({"agent"})))
    registry.register(Tool("clipboard.write", "Put text on the clipboard.", params(["text"], text={"type": "string"}),
                           clipboard_write, PermissionLevel.NON_DESTRUCTIVE, 5, frozenset({"agent"})))

    # ---------------------------------------------------------------- screen

    async def screenshot(args: dict[str, Any]) -> ToolResult:
        import pyautogui

        folder = data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        image = await asyncio.to_thread(pyautogui.screenshot)
        await asyncio.to_thread(image.save, path)
        services.state["last_screenshot"] = str(path)
        previous = services.state.get("previous_screenshot")
        services.state["previous_screenshot"] = services.state.get("current_screenshot")
        services.state["current_screenshot"] = str(path)
        working.record_action("screen", "took a screenshot", tool="screen.capture", args={"path": str(path)})
        return ToolResult(True, "Got the screenshot.", data={"path": str(path), "previous": previous},
                          display=f"Saved to `{path}`")

    registry.register(Tool("screen.capture", "Take a screenshot of the whole screen and save it.", params(),
                           screenshot, PermissionLevel.READ, 15, frozenset({"agent", "chat"})))

    # ---------------------------------------------------------------- time & folders

    async def now(args: dict[str, Any]) -> ToolResult:
        stamp = datetime.now()
        return ToolResult(True, f"It's {stamp.strftime('%I:%M %p').lstrip('0')}.",
                          data={"iso": stamp.isoformat(timespec="minutes"), "weekday": stamp.strftime("%A")})

    async def today(args: dict[str, Any]) -> ToolResult:
        stamp = datetime.now()
        return ToolResult(True, f"It's {stamp.strftime('%A, %B')} {stamp.day}.", data={"date": stamp.date().isoformat()})

    async def open_folder(args: dict[str, Any]) -> ToolResult:
        path = Path(os.path.expandvars(os.path.expanduser(args["path"])))
        if not path.exists():
            return ToolResult.failure(f"{path} doesn't exist.")
        await asyncio.to_thread(os.startfile, str(path))
        working.record_action("file", f"opened folder {path.name}", tool="folder.open", args={"path": str(path)})
        return ToolResult(True, f"Opened {path.name}.", data={"path": str(path)})

    registry.register(Tool("system.time", "Get the current local time.", params(), now, PermissionLevel.READ, 2,
                           frozenset({"agent"})))
    registry.register(Tool("system.date", "Get today's date.", params(), today, PermissionLevel.READ, 2,
                           frozenset({"agent"})))
    registry.register(Tool("folder.open", "Open a folder in File Explorer.", params(["path"], path={"type": "string"}),
                           open_folder, PermissionLevel.NON_DESTRUCTIVE, 10, frozenset({"agent"}),
                           describe=lambda a: f"open the folder {a.get('path')}"))
