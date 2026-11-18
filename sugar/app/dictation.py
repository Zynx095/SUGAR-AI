"""Dictation mode and writing mode — SID typing into the document you're working in.

* **Dictation mode** ("start dictation"): everything you say is content for the document in front, typed as
  each sentence is recognised; editing phrases ("new paragraph", "delete the last sentence", "undo that") are
  commands; SID doesn't chat back. "Hey SID, …" still reaches SID normally; "stop dictation" ends it.
* **Writing mode** (after "write a paragraph about …" in an editor): SID streams the text into the document as
  the model writes it; afterwards only writing and editing commands ("continue", "next line", "make it a
  bullet", "delete the last sentence") are taken as being about that document, everything else is a normal
  request. It ends after three idle minutes or when you leave the document.

Typing only happens while the target window is in front. If you switch apps, typing stops and SID says so.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sugar.computer.dictation import DictationTarget, DictationTyper, FocusLost, Op, parse
from sugar.computer.windows import display_name
from sugar.core.logging import log_event
from sugar.tools.registry import PermissionLevel, Tool, ToolResult, params

log = logging.getLogger(__name__)

WRITER = """You write text that will be typed straight into the user's document ({app}).
Output only the text itself: no preamble ("Here is…"), no quotes around it, no markdown symbols (#, **, -)
unless the user asked for a list. Plain sentences and paragraphs; separate paragraphs with one blank line.
Match the requested length."""


class DictationManager:
    WRITING_IDLE_S = 180.0

    def __init__(self, app) -> None:
        self._app = app
        self.mode: str | None = None  # dictation | writing
        self.target: DictationTarget | None = None
        self.last_activity = 0.0
        self.typing = False
        self._typer = DictationTyper(app.computer.backend, app.computer.keyboard,
                                     interval_s=app.computer.keyboard.settings.char_interval_s)

    def interrupt(self) -> None:
        """"Stop" while SID is typing: stop at the next character (the mode itself stays on)."""
        self._app.computer.cancel()

    # ------------------------------------------------------------------ state

    @property
    def active(self) -> bool:
        if self.mode is None or self.target is None:
            return False
        if self.mode == "writing" and time.monotonic() - self.last_activity > self.WRITING_IDLE_S:
            self._end("idle")
            return False
        return True

    @property
    def dictating(self) -> bool:
        return self.active and self.mode == "dictation"

    def _publish(self, reason: str = "") -> None:
        window = self.target.window if self.target else None
        self._app.bus.publish("dictation.state", active=self.mode is not None, mode=self.mode,
                              app=display_name(window) if window else None, title=window.title if window else None,
                              reason=reason)

    def _end(self, reason: str) -> None:
        if self.mode is None:
            return
        log_event("DICTATION_END", mode=self.mode, reason=reason, chars=len(self.target.typed) if self.target else 0)
        self.mode = None
        self._publish(reason)

    async def start(self, mode: str = "dictation", target: Any = None) -> ToolResult:
        computer = self._app.computer
        window = await computer.run(computer.interact._window, target)
        if window is None or computer.context.is_own(window):
            return ToolResult.failure("Open the document you want me to write in first, then say start dictation.")
        await computer.run(lambda: computer.backend.foreground() == window.hwnd or computer.backend.activate(window.hwnd))
        same = self.target is not None and self.target.window.hwnd == window.hwnd
        self.target = self.target if same else DictationTarget(window)
        self.mode = mode
        self.last_activity = time.monotonic()
        self._publish("start")
        log_event("DICTATION_START", mode=mode, app=window.app)
        if mode == "dictation":
            return ToolResult(True, f"Dictating into {display_name(window)}. Say stop dictation when you're done.")
        return ToolResult(True, f"Writing in {display_name(window)}.", speak=False)

    async def stop(self, reason: str = "user") -> ToolResult:
        if self.mode is None:
            return ToolResult(True, "I wasn't dictating.")
        mode = self.mode
        self._end(reason)
        return ToolResult(True, "Dictation stopped." if mode == "dictation" else "Done writing.")

    # ------------------------------------------------------------------ input

    async def handle(self, text: str) -> bool:
        """Consume an utterance if it belongs to the document. Returns False to let SID handle it normally."""
        if not self.active:
            return False
        ops = parse(text, dictating=self.mode == "dictation")
        if not ops:
            return False
        if self.mode == "writing" and ops[0].kind == "command" and ops[0].value == "generate":
            return False  # a new writing request: the router starts it (it may name another app)
        self.last_activity = time.monotonic()
        reply = await self.run(ops)
        if reply:
            await self._app.conversation.quick_reply(reply)
        else:
            self._app.conversation._settle_state("dictation")
        return True

    async def run(self, ops: list[Op]) -> str | None:
        computer = self._app.computer
        target = self.target
        if target is None:
            return "There's no document to write in."
        computer.cancel_event.clear()
        self.typing = True
        try:
            for op in ops:
                if op.kind == "text":
                    await computer.run(self._typer.type, target, op.value)
                elif op.kind == "keys":
                    await computer.run(self._typer.keys, target, op.value)
                else:
                    reply = await self._command(op, target)
                    if reply is not None:
                        return reply
        except FocusLost:
            self._end("focus")
            return (f"You switched away from {display_name(target.window)}, so I stopped typing. "
                    "Say start dictation to carry on there.")
        except InterruptedError:
            return "Stopped."
        finally:
            self.typing = False
        return None

    async def _keys(self, target: DictationTarget, *combos: str) -> None:
        for combo in combos:
            await self._app.computer.run(self._typer.keys, target, combo)

    async def _command(self, op: Op, target: DictationTarget) -> str | None:
        computer = self._app.computer
        name, args = op.value, op.args
        if name == "stop":
            return (await self.stop()).summary
        if name in ("undo", "redo"):
            await self._keys(target, "ctrl+z" if name == "undo" else "ctrl+y")
            return None
        if name == "scratch":
            removed = await computer.run(self._typer.scratch, target)
            return None if removed else "There's nothing of mine to take back."
        if name == "delete_sentence":
            removed = await computer.run(self._typer.delete_last_sentence, target)
            return None if removed is not None else "I haven't typed a sentence here yet."
        if name == "delete_word":
            await self._keys(target, "ctrl+backspace")
            return None
        if name == "delete_line":
            await self._keys(target, "shift+home", "backspace")
            return None
        if name == "select_paragraph":
            await self._keys(target, "ctrl+up", "ctrl+shift+down")
            return None
        if name == "select_previous_paragraph":
            await self._keys(target, "ctrl+up", "ctrl+shift+up")
            return None
        if name == "select_all":
            await self._keys(target, "ctrl+a")
            return None
        if name == "clear":
            await self._keys(target, "ctrl+a", "delete")
            target.typed, target.chunks = "", []
            return None
        if name in ("go_start", "go_end", "line_start", "line_end"):
            combo = {"go_start": "ctrl+home", "go_end": "ctrl+end", "line_start": "home", "line_end": "end"}[name]
            await self._keys(target, combo)
            return None
        if name == "heading":
            if target.rich:
                await self._keys(target, "ctrl+alt+1")
                return None
            return (f"{display_name(target.window)} is plain text, so it has no real headings. "
                    "I can put the line in capitals, or start it with a hash sign for Markdown.")
        if name == "add_heading":
            text = args.get("text", "")
            if target.typed and not target.typed.endswith("\n"):
                await self._keys(target, "enter")
            if target.rich:
                await self._keys(target, "ctrl+alt+1")
            if text:
                await computer.run(self._typer.type, target, text)
                await self._keys(target, "enter")
                return None
            return "What should the heading say?" if target.rich else \
                "Go ahead with the heading. It's plain text here, so it'll be a line on its own."
        if name == "bullet":
            if target.typed and not target.typed.endswith("\n"):
                await self._keys(target, "enter")
            await computer.run(self._typer.type, target, "- " + args.get("text", ""), join=False)
            return None
        if name == "numbered":
            if target.typed and not target.typed.endswith("\n"):
                await self._keys(target, "enter")
            await computer.run(self._typer.type, target, "1. ", join=False)
            return None
        if name == "indent":
            await self._keys(target, "home", "tab")
            return None
        if name == "replace":
            return await self._replace(target, args.get("old", ""), args.get("new", ""))
        if name == "exact":
            await computer.run(self._typer.type, target, args.get("text", ""))
            return None
        if name == "spelling":
            return await self._spelling(target)
        if name == "continue":
            return await self.generate("Continue the text naturally from where it stops.", continuation=True)
        if name == "generate":
            return await self.generate(args.get("prompt", ""))
        return None

    async def _replace(self, target: DictationTarget, old: str, new: str) -> str | None:
        computer = self._app.computer
        if not old or not new:
            return "Say what to replace and what with."
        found = await computer.run(computer.backend.find_text, target.window.hwnd, old)
        if found:
            await computer.run(self._typer.type, target, new, join=False)
            index = target.typed.lower().rfind(old.lower())
            if index >= 0:
                target.typed = target.typed[:index] + new + target.typed[index + len(old):]
            return None
        index = target.typed.lower().rfind(old.lower())
        if index < 0:
            return f"I can't find {old} in the text."
        tail = target.typed[index + len(old):]
        await computer.run(self._typer.backspace, target, len(target.typed) - index)
        await computer.run(self._typer.type, target, new + tail, join=False)
        return None

    async def _spelling(self, target: DictationTarget) -> str | None:
        paragraph = target.typed.rsplit("\n\n", 1)[-1]
        if not paragraph.strip():
            return "There's nothing of mine to check here."
        corrected = await self._complete("Correct only the spelling and obvious typos in this text. Return the "
                                         "corrected text and nothing else:\n\n" + paragraph, max_chars=len(paragraph) * 2)
        corrected = corrected.strip()
        if not corrected or corrected == paragraph.strip():
            return "The spelling looks right."
        computer = self._app.computer
        await computer.run(self._typer.backspace, target, len(paragraph))
        await computer.run(self._typer.type, target, corrected, join=False)
        return "Fixed the spelling."

    # ------------------------------------------------------------------ writing with the model

    def _messages(self, prompt: str, continuation: bool) -> list[dict[str, Any]]:
        target = self.target
        system = WRITER.format(app=display_name(target.window) if target else "an editor")
        if continuation and target and target.typed.strip():
            prompt += "\n\nThe document so far ends with:\n" + target.typed[-1500:]
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    async def _complete(self, prompt: str, max_chars: int = 4000) -> str:
        parts = []
        async for chunk in self._app.pool.stream(self._app.settings.llm.chat_chain,
                                                 [{"role": "user", "content": prompt}], purpose="chat"):
            if chunk.kind == "text":
                parts.append(chunk.text)
                if sum(len(p) for p in parts) > max_chars:
                    break
        return "".join(parts)

    async def generate(self, prompt: str, *, continuation: bool = False) -> str | None:
        """Stream the model's text into the document, a few words at a time, as it is written."""
        computer = self._app.computer
        target = self.target
        if target is None:
            return "There's no document to write in."
        self._app.bus.publish("activity", kind="writing", active=True, app=display_name(target.window))
        pending = ""
        first = not continuation or not target.typed
        typed_any = False
        started = time.perf_counter()
        first_char_ms = None
        computer.cancel_event.clear()
        self.typing = True
        try:
            async for chunk in self._app.pool.stream(self._app.settings.llm.chat_chain,
                                                     self._messages(prompt, continuation), purpose="chat"):
                if chunk.kind != "text":
                    continue
                pending += chunk.text
                cut = max(pending.rfind(" "), pending.rfind("\n"))
                if cut <= 0:
                    continue
                piece, pending = pending[:cut], pending[cut:]
                piece = piece.replace("**", "").replace("\r", "")
                if not typed_any:
                    piece = piece.lstrip()
                    if not piece:
                        continue
                await computer.run(self._type_piece, target, piece, first and not typed_any)
                if first_char_ms is None:
                    first_char_ms = int((time.perf_counter() - started) * 1000)
                typed_any = True
            if pending.strip():
                await computer.run(self._type_piece, target, pending.rstrip(), first and not typed_any)
        except FocusLost:
            self._end("focus")
            return f"You switched away from {display_name(target.window)}, so I stopped writing."
        except InterruptedError:
            return "Stopped writing."
        finally:
            self.typing = False
            self._app.bus.publish("activity", kind="writing", active=False)
        self.last_activity = time.monotonic()
        log_event("DICTATION_GENERATED", chars=len(target.typed), first_char_ms=first_char_ms)
        return None

    def _type_piece(self, target: DictationTarget, piece: str, join: bool) -> None:
        """Type model text, turning its newlines into real Enter presses (the typer handles that)."""
        self._typer.type(target, piece, join=join)


def register_dictation_tools(app) -> None:
    manager: DictationManager = app.dictation
    registry = app.registry
    group = frozenset({"agent", "computer"})

    async def start(args: dict[str, Any]) -> ToolResult:
        return await manager.start("dictation", args.get("target"))

    async def stop(args: dict[str, Any]) -> ToolResult:
        return await manager.stop()

    async def write(args: dict[str, Any]) -> ToolResult:
        """Write generated text into the editor in front (or the named app), streaming it as it's written."""
        target = args.get("target")
        if target:
            computer = app.computer
            window = await computer.run(computer.windows.resolve, target)
            if window is None:  # "write a poem in Notepad" with Notepad closed: open it first
                opened = await computer.run(computer.apps.open, str(target))
                if not opened.success:
                    return opened.to_tool_result()
                target = None  # the app just opened is in front
        started = await manager.start("writing", target)
        if not started.ok:
            return started
        reply = await manager.generate(str(args["prompt"]))
        if reply:
            return ToolResult(False, reply)
        return ToolResult(True, "Done. Say continue for more, or tell me what to change.",
                          data={"chars": len(manager.target.typed) if manager.target else 0})

    async def edit(args: dict[str, Any]) -> ToolResult:
        """Apply an editing or layout command to the document SID is writing in."""
        if not manager.active:
            started = await manager.start("writing", args.get("target"))
            if not started.ok:
                return started
        ops = parse(str(args["command"]), dictating=False) or parse(str(args["command"]), dictating=True)
        reply = await manager.run(ops)
        return ToolResult(reply is None or not reply.startswith(("You switched", "I can't", "There's")),
                          reply or "Done.", speak=bool(reply))

    registry.register(Tool("dictation.start", "Start dictation mode: everything the user says is typed into the "
                           "document in front until they say stop dictation.", params(target={"type": "string"}),
                           start, PermissionLevel.NON_DESTRUCTIVE, 15, group))
    registry.register(Tool("dictation.stop", "Stop dictation or writing mode.", params(), stop, PermissionLevel.READ,
                           5, group))
    registry.register(Tool("dictation.write", "Compose text with the model and type it into the editor in front as it "
                           "is written (e.g. 'a paragraph about artificial intelligence').",
                           params(["prompt"], prompt={"type": "string"}, target={"type": "string"}), write,
                           PermissionLevel.NON_DESTRUCTIVE, 180, group))
    registry.register(Tool("dictation.edit", "Edit the document being written: 'next line', 'new paragraph', 'delete "
                           "the last sentence', 'undo that', 'replace X with Y', 'add a bullet: …'.",
                           params(["command"], command={"type": "string"}, target={"type": "string"}), edit,
                           PermissionLevel.NON_DESTRUCTIVE, 60, group))
