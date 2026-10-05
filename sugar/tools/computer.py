"""Computer-control tools: keyboard, windows, apps, clipboard, dialogs and screen.

Thin, permission-annotated wrappers over :mod:`sugar.computer`. Every handler
runs its OS work on the computer-control worker thread and returns the
controller's structured result (with ``verified`` in the data) to the model.
"""

from __future__ import annotations

import re
from typing import Any

from sugar.computer.keys import VK_DELETE, VK_LWIN, VK_SHIFT, KeyParseError, parse_combo
from sugar.computer.results import ComputerActionResult
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

L = PermissionLevel
COMPUTER = frozenset({"agent", "computer"})
EVERYWHERE = frozenset({"agent", "chat", "computer"})
TARGET = {"type": "string", "description": "App name, window title, or 'this' / 'that' / 'previous'. "
                                           "Omit for the window the user is working in."}
_RISKY_NAMES = re.compile(r"\b(?:delete|remove|uninstall|format|erase|discard|don'?t save|empty|reset|sign out|"
                          r"log ?out|buy|purchase|pay|order|send|submit|post|publish|confirm|transfer|unsubscribe)\b",
                          re.I)


def last_code_block(markdown: str) -> str | None:
    blocks = re.findall(r"```[^\n`]*\n(.*?)```", markdown or "", re.S)
    return blocks[-1].rstrip("\n") if blocks else None


def plain_text(markdown: str) -> str:
    text = re.sub(r"```[^\n`]*\n(.*?)```", r"\1", markdown or "", flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    return text.strip()


def register(registry: ToolRegistry, services: ToolServices) -> None:
    computer = services.computer
    working = services.working

    async def run(fn, *args: Any, **kwargs: Any) -> ComputerActionResult:
        try:
            return await computer.run(fn, *args, **kwargs)
        except KeyParseError as exc:
            return ComputerActionResult.fail("keyboard.press", None, f"I don't know the key {exc}.", str(exc))
        except Exception as exc:  # desktop unavailable, COM errors…
            return ComputerActionResult.fail(getattr(fn, "__name__", "computer"), None,
                                             "I couldn't do that on this computer.", repr(exc))

    def record(result: ComputerActionResult, domain: str) -> None:
        working.record_action(domain, result.details, tool=result.action, args={"target": result.target},
                              ok=result.success, error=result.error)

    # ---------------------------------------------------------------- keyboard

    async def type_text(args: dict[str, Any]) -> ToolResult:
        source = args.get("source", "text")
        text = args.get("text") or ""
        if source == "last_code":
            text = last_code_block(services.state.get("last_reply", "")) or ""
            if not text:
                return ToolResult.failure("There's no code in my last answer to type.")
        elif source == "last_reply":
            text = plain_text(services.state.get("last_reply", ""))
        elif source == "clipboard":
            result = await run(computer.keyboard.press, "ctrl+v", target=args.get("target"))
            record(result, "typing")
            return result.to_tool_result()
        if not text.strip():
            return ToolResult.failure("There's nothing to type.")
        result = await run(computer.keyboard.type_text, text, target=args.get("target"), mode=args.get("mode"))
        record(result, "typing")
        quiet = result.success and result.verified and len(text) < 400
        return result.to_tool_result(speak=not quiet)

    def press_level(args: dict[str, Any]) -> PermissionLevel:
        try:
            vks = parse_combo(args.get("keys", ""))
        except KeyParseError:
            return L.SENSITIVE
        if VK_SHIFT in vks and VK_DELETE in vks:
            return L.DESTRUCTIVE  # permanent delete in File Explorer
        if VK_LWIN in vks or (0x11 in vks and 0x12 in vks):
            return L.SENSITIVE  # Win+R, Win+L, Ctrl+Alt+… reach the whole system
        active = computer.context.active() if computer.available else None
        if VK_DELETE in vks and active is not None and active.app == "explorer":
            return L.SENSITIVE  # deletes files
        return L.NON_DESTRUCTIVE

    async def press(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.keyboard.press, args["keys"], target=args.get("target"),
                           times=int(args.get("times", 1)))
        record(result, "keyboard")
        return result.to_tool_result()

    async def edit(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.keyboard.edit, args["operation"], target=args.get("target"))
        record(result, "typing")
        return result.to_tool_result()

    async def save(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.keyboard.save, target=args.get("target"))
        record(result, "keyboard")
        return result.to_tool_result()

    registry.register(Tool(
        "keyboard.type",
        "Type text into an app as real keystrokes (Sugar focuses the app first and checks the text arrived). "
        "Use for any 'type/write X in Notepad' request. source='last_code' types the code block from your "
        "previous answer.",
        params(text={"type": "string"}, target=TARGET,
               mode={"type": "string", "enum": ["auto", "realtime", "paste"], "default": "auto",
                     "description": "realtime = visible keystrokes; paste = clipboard (fast, long text)"},
               source={"type": "string", "enum": ["text", "last_code", "last_reply", "clipboard"], "default": "text"}),
        type_text, L.NON_DESTRUCTIVE, 300, EVERYWHERE,
        describe=lambda a: f"type “{str(a.get('text') or a.get('source'))[:60]}” into {a.get('target') or 'the active app'}",
    ))
    registry.register(Tool(
        "keyboard.press",
        "Press a key or shortcut in an app, e.g. 'enter', 'ctrl+s', 'ctrl+shift+t', 'alt+tab', 'f5'.",
        params(["keys"], keys={"type": "string"}, times={"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
               target=TARGET),
        press, press_level, 20, COMPUTER, describe=lambda a: f"press {a.get('keys')}"
        + (f" in {a['target']}" if a.get("target") else ""),
    ))
    registry.register(Tool(
        "keyboard.edit",
        "Editing commands in the active text app: delete the last line or word, clear everything, select all, "
        "jump to the start or end.",
        params(["operation"], operation={"type": "string", "enum": ["delete_last_line", "delete_last_word",
                                                                     "clear_all", "select_all", "go_to_end",
                                                                     "go_to_start"]}, target=TARGET),
        edit, lambda a: L.SENSITIVE if a.get("operation") == "clear_all" else L.NON_DESTRUCTIVE, 30, COMPUTER,
        describe=lambda a: str(a.get("operation", "")).replace("_", " ") + (f" in {a['target']}" if a.get("target") else ""),
    ))
    registry.register(Tool("keyboard.save", "Save the document in the active app (Ctrl+S) and report whether it saved.",
                           params(target=TARGET), save, L.NON_DESTRUCTIVE, 15, COMPUTER))

    # ---------------------------------------------------------------- windows and apps

    async def open_app(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.apps.open, args["name"], new_window=bool(args.get("new_window")))
        record(result, "app")
        return result.to_tool_result()

    async def close(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.close, args.get("name") or args.get("target"), args.get("scope", "auto"))
        record(result, "app")
        return result.to_tool_result()

    async def force_close(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.apps.force_close, args["name"])
        record(result, "app")
        return result.to_tool_result()

    async def list_apps(args: dict[str, Any]) -> ToolResult:
        query = (args.get("filter") or "").lower()
        names = [n for n in computer.apps.catalog.names() if query in n.lower()][:60]
        return ToolResult(True, f"{len(names)} apps found.", data={"apps": names})

    async def focus(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.focus, args.get("target") or args.get("name"))
        record(result, "app")
        return result.to_tool_result()

    def state_tool(state: str):
        async def handler(args: dict[str, Any]) -> ToolResult:
            result = await run(computer.set_state, args.get("target"), state)
            record(result, "app")
            return result.to_tool_result()

        return handler

    async def snap(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.windows.snap, args.get("target"), args["where"])
        record(result, "app")
        return result.to_tool_result()

    async def move(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.windows.move_resize, args.get("target"), args.get("x"), args.get("y"),
                           args.get("width"), args.get("height"))
        return result.to_tool_result()

    async def list_windows(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.windows.summary)
        listing = "\n".join(f"- **{w['app']}** — {w['title'][:80]}" + (" _(minimized)_" if w["minimized"] else "")
                            for w in (result.data or {}).get("windows", []))
        return result.to_tool_result(display=listing or None)

    async def desktop_context(args: dict[str, Any]) -> ToolResult:
        await run(computer.context.refresh)
        snapshot = computer.context.snapshot()
        active = snapshot.get("active")
        summary = f"You're in {active['app']}: {active['title'][:80]}." if active else "I can't tell what's in front."
        return ToolResult(True, summary, data=snapshot)

    async def answer_dialog(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.windows.answer_dialog, args["choice"])
        record(result, "app")
        return result.to_tool_result()

    registry.register(Tool(
        "app.open", "Open (or switch to, if it's already running) a desktop app by name: Notepad, VS Code, "
        "Spotify, Discord, a browser… new_window=true opens another window/document.",
        params(["name"], name={"type": "string"}, new_window={"type": "boolean", "default": False}),
        open_app, L.NON_DESTRUCTIVE, 25, EVERYWHERE, describe=lambda a: f"open {a.get('name')}",
    ))
    registry.register(Tool(
        "app.close", "Close an app, window or browser tab by name ('Notepad', 'Brave', 'the YouTube tab') or "
        "reference ('this', 'that'). Polite close: apps can still ask to save. scope=tab/window forces the kind.",
        params(name={"type": "string"}, scope={"type": "string", "enum": ["auto", "app", "window", "tab"],
                                               "default": "auto"}),
        close, L.NON_DESTRUCTIVE, 20, EVERYWHERE, describe=lambda a: f"close {a.get('name') or 'this'}",
    ))
    registry.register(Tool(
        "app.force_close", "Force-quit an app (ends its processes; unsaved work is lost). Only when the user asks "
        "to force it or kill it.",
        params(["name"], name={"type": "string"}), force_close, L.SENSITIVE, 15, COMPUTER,
        describe=lambda a: f"force-close {a.get('name')} (unsaved work would be lost)",
    ))
    registry.register(Tool("app.list", "List installed applications, optionally filtered by a name fragment.",
                           params(filter={"type": "string"}), list_apps, L.READ, 10, COMPUTER))
    registry.register(Tool("window.focus", "Switch to an app window or browser tab ('Notepad', 'the YouTube tab', "
                           "'previous').", params(target=TARGET), focus, L.NON_DESTRUCTIVE, 10, EVERYWHERE,
                           describe=lambda a: f"switch to {a.get('target')}"))
    for state, verb in (("minimize", "Minimize"), ("maximize", "Maximize"), ("restore", "Restore (un-minimize)")):
        registry.register(Tool(f"window.{state}", f"{verb} a window (by app name or 'this').",
                               params(target=TARGET), state_tool(state), L.NON_DESTRUCTIVE, 10, COMPUTER,
                               describe=lambda a, s=state: f"{s} {a.get('target') or 'this window'}"))
    registry.register(Tool("window.snap", "Put a window on the left/right/top/bottom half of the screen, center it, "
                           "or maximize it.",
                           params(["where"], target=TARGET, where={"type": "string", "enum": [
                               "left", "right", "top", "bottom", "center", "maximize"]}),
                           snap, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("window.move", "Move and/or resize a window to exact pixel coordinates.",
                           params(target=TARGET, x={"type": "integer"}, y={"type": "integer"},
                                  width={"type": "integer", "minimum": 120}, height={"type": "integer", "minimum": 80}),
                           move, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("window.list", "List the open app windows.", params(), list_windows, L.READ, 10, COMPUTER))
    registry.register(Tool("desktop.context", "What app and window the user is in right now.", params(),
                           desktop_context, L.READ, 5, COMPUTER))
    registry.register(Tool(
        "dialog.answer", "Answer the dialog an app is showing (e.g. 'save changes?'): save, discard (don't save), "
        "cancel, or confirm.",
        params(["choice"], choice={"type": "string", "enum": ["save", "discard", "cancel", "confirm"]}),
        answer_dialog, lambda a: L.SENSITIVE if a.get("choice") == "discard" else L.NON_DESTRUCTIVE, 15, COMPUTER,
        describe=lambda a: {"discard": "close without saving", "save": "save the changes",
                            "cancel": "cancel the dialog", "confirm": "confirm the dialog"}.get(a.get("choice"), "answer"),
    ))

    # ---------------------------------------------------------------- clipboard

    async def clipboard_read(args: dict[str, Any]) -> ToolResult:
        try:
            text = await computer.run(computer.backend.clipboard_text)
        except Exception as exc:
            return ToolResult.failure("I couldn't read the clipboard.", repr(exc))
        if not text:
            return ToolResult(True, "Your clipboard is empty.", data={"text": ""})
        preview = text if len(text) <= 300 else text[:300] + "…"
        return ToolResult(True, "Here's what's on your clipboard.", data={"text": text[:5000]},
                          display=f"```\n{preview}\n```")

    async def clipboard_write(args: dict[str, Any]) -> ToolResult:
        text = str(args["text"])
        try:
            ok = await computer.run(computer.backend.set_clipboard_text, text)
        except Exception as exc:
            return ToolResult.failure("I couldn't use the clipboard.", repr(exc))
        return ToolResult(bool(ok), "Copied." if ok else "The clipboard was busy.", data={"chars": len(text)})

    registry.register(Tool("clipboard.read", "Read the text currently on the clipboard.", params(), clipboard_read,
                           L.READ, 5, COMPUTER))
    registry.register(Tool("clipboard.write", "Put text on the clipboard.", params(["text"], text={"type": "string"}),
                           clipboard_write, L.NON_DESTRUCTIVE, 5, COMPUTER))

    # ---------------------------------------------------------------- screen

    async def capture(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.screen.capture, args.get("target"), window_only=bool(args.get("window_only")))
        if result.success:
            services.state["current_screenshot"] = result.data.get("path")
            services.state["previous_screenshot"] = result.data.get("previous")
            working.record_action("screen", "took a screenshot", tool="screen.capture",
                                  args={"path": result.data.get("path")})
        return result.to_tool_result(display=f"Saved to `{result.data.get('path')}`" if result.success else None)

    async def inspect(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.screen.inspect, args.get("target"))
        return result.to_tool_result()

    async def look(args: dict[str, Any]) -> ToolResult:
        vision = computer.screen.vision
        if not vision.available:
            return ToolResult.failure("I can't see the screen: no vision model is set up. I can read window and "
                                      "button names instead.", "vision provider unavailable")
        shot = await run(computer.screen.capture, None)
        if not shot.success:
            return shot.to_tool_result()
        from pathlib import Path

        answer = await vision.describe(Path(shot.data["path"]), args.get("question"))
        return ToolResult(True, answer, data={"path": shot.data["path"]})

    def click_level(args: dict[str, Any]) -> PermissionLevel:
        name = str(args.get("element") or args.get("name") or "")
        return L.SENSITIVE if not name or _RISKY_NAMES.search(name) else L.NON_DESTRUCTIVE

    async def click(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.screen.click, args.get("x"), args.get("y"), element=args.get("element"),
                           target=args.get("target"), button=args.get("button", "left"),
                           double=bool(args.get("double")))
        return result.to_tool_result()

    async def ui_click(args: dict[str, Any]) -> ToolResult:
        def invoke() -> ComputerActionResult:
            window = computer.windows.resolve(args.get("target"))
            if window is None:
                return ComputerActionResult.fail("ui.click", None, "There's no window to click in.")
            clicked = computer.backend.invoke(window.hwnd, args["name"])
            if clicked is None:
                return ComputerActionResult.fail("ui.click", window.app, f"I can't find {args['name']} there.")
            computer.context.note_action("ui.click", window)
            return ComputerActionResult(True, "ui.click", window.app, f"Clicked {clicked[:60]}.", False, None, {})

        result = await run(invoke)
        return result.to_tool_result()

    async def scroll(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.screen.scroll, int(args.get("clicks", -3)), x=args.get("x"), y=args.get("y"))
        return result.to_tool_result()

    async def drag(args: dict[str, Any]) -> ToolResult:
        result = await run(computer.screen.drag, args["x1"], args["y1"], args["x2"], args["y2"])
        return result.to_tool_result()

    registry.register(Tool("screen.capture", "Take a screenshot (whole screen, or one window with window_only).",
                           params(target=TARGET, window_only={"type": "boolean", "default": False}), capture,
                           L.READ, 15, EVERYWHERE))
    registry.register(Tool("screen.inspect", "List the named buttons, links, fields and tabs in a window (what can "
                           "be clicked by name), from Windows accessibility data.", params(target=TARGET), inspect,
                           L.READ, 20, COMPUTER))
    registry.register(Tool("screen.look", "Describe what's visible on screen (needs a vision model).",
                           params(question={"type": "string"}), look, L.READ, 60, COMPUTER))
    registry.register(Tool("ui.click", "Click a button, link, tab or menu item by its visible name in a window.",
                           params(["name"], name={"type": "string"}, target=TARGET), ui_click, click_level, 15,
                           COMPUTER, describe=lambda a: f"click “{a.get('name')}”"))
    registry.register(Tool("screen.click", "Click at screen coordinates or on a named element (left/right/double).",
                           params(x={"type": "integer"}, y={"type": "integer"}, element={"type": "string"},
                                  target=TARGET, button={"type": "string", "enum": ["left", "right", "middle"],
                                                         "default": "left"},
                                  double={"type": "boolean", "default": False}),
                           click, L.SENSITIVE, 10, COMPUTER,
                           describe=lambda a: f"click {a.get('element') or (a.get('x'), a.get('y'))}"))
    registry.register(Tool("screen.scroll", "Scroll the mouse wheel (negative clicks scroll down).",
                           params(clicks={"type": "integer", "default": -3}, x={"type": "integer"}, y={"type": "integer"}),
                           scroll, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("screen.drag", "Drag with the left mouse button from one point to another.",
                           params(["x1", "y1", "x2", "y2"], x1={"type": "integer"}, y1={"type": "integer"},
                                  x2={"type": "integer"}, y2={"type": "integer"}),
                           drag, L.SENSITIVE, 10, COMPUTER, describe=lambda a: "drag on screen"))
