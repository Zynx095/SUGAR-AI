"""System tools: volume, time, date and folders.

Keyboard, windows, apps, clipboard and screen tools live in ``tools/computer.py``
on top of the computer-control subsystem.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

log = logging.getLogger(__name__)


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


def register(registry: ToolRegistry, services: ToolServices) -> None:
    working = services.working

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
