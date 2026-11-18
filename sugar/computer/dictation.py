"""Dictation and writing into text editors: content as it comes, editing words as commands.

:func:`parse` turns one utterance into operations:

* content — typed as it is (spoken punctuation words become punctuation; the first letter after a sentence end
  or a new line is capitalised; a space joins it to what came before),
* inline layout — "next line" / "go to the next line" (Enter), "new paragraph" (a blank line), never typed as
  words,
* whole-utterance commands — "delete the last sentence", "undo that", "select the previous paragraph",
  "go to the end", "replace X with Y", "make this a heading", "write this exactly: …", "stop dictation"…

:class:`DictationTarget` executes operations on the desktop thread, and only while the target window is still
the one in front: if the user switches apps, typing stops instead of landing somewhere unexpected.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from sugar.computer.backend import DesktopBackend, WindowInfo
from sugar.computer.keys import parse_combo

TEXT_EDITORS = {"notepad", "wordpad", "winword", "word", "code", "notepad++", "sublime_text", "obsidian",
                "onenote", "write", "soffice", "swriter"}
RICH_EDITORS = {"winword", "word", "wordpad", "onenote", "soffice", "swriter"}  # have real headings / lists


@dataclass
class Op:
    kind: str  # text | keys | command
    value: str = ""
    args: dict = field(default_factory=dict)


_INLINE = re.compile(
    r",?\s*\b(?P<cmd>(?:and )?(?:go to the |go to a |on a |onto a |move to the )?(?:next|new) line|line break|"
    r"(?:start a |begin a |and )?new paragraph|next paragraph|insert a blank line|blank line)\b[,.;:!?]?\s*",
    re.I)
_SPOKEN_PUNCTUATION = [
    (re.compile(r"[,.]?\s*\bcomma\b[,.]?", re.I), ","),
    (re.compile(r"[,.]?\s*\b(?:full stop|period)\b[,.]?", re.I), "."),
    (re.compile(r"[,.]?\s*\bquestion mark\b[,.?]?", re.I), "?"),
    (re.compile(r"[,.]?\s*\bexclamation (?:mark|point)\b[,.!]?", re.I), "!"),
    (re.compile(r"[,.]?\s*\bsemicolon\b[,.]?", re.I), ";"),
    (re.compile(r"[,.]?\s*\bcolon\b[,.]?", re.I), ":"),
]

_COMMANDS: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rf"^(?:{pattern})$", re.I)) for name, pattern in [
    ("stop", r"(?:stop|end|finish|exit|quit|turn off|cancel) (?:the )?(?:dictation|dictating|writing)(?: mode)?|"
             r"stop (?:dictating|typing|writing)|i'?m done(?: dictating)?|that'?s all(?: for now)?"),
    ("undo", r"undo(?: that| it| the last (?:thing|change|edit))?"),
    ("redo", r"redo(?: that| it)?"),
    ("scratch", r"scratch that|delete what i just said|remove what i just said|take that back"),
    ("delete_sentence", r"(?:delete|remove|erase) (?:the )?(?:last|previous) sentence|(?:delete|remove) that sentence"),
    ("delete_word", r"(?:delete|remove|erase) (?:the )?(?:last|previous) word|(?:delete|remove) that word"),
    ("delete_line", r"(?:delete|remove|erase) (?:the )?(?:last|previous|this) line"),
    ("select_paragraph", r"select (?:the |this )?(?:current )?paragraph"),
    ("select_previous_paragraph", r"select (?:the )?(?:previous|last) paragraph"),
    ("select_all", r"select (?:everything|all|all the text|the whole (?:document|thing))"),
    ("clear", r"clear (?:this |the )?(?:document|page|text|everything)|clear it all|delete everything"),
    ("go_start", r"(?:move|go|jump|take)(?: the cursor)?(?: me)? (?:back )?to the (?:beginning|start|top)"
                 r"(?: of the (?:document|text|file|page))?"),
    ("go_end", r"(?:move|go|jump|take)(?: the cursor)?(?: me)? (?:back )?to the (?:end|bottom)"
               r"(?: of the (?:document|text|file|page))?"),
    ("line_start", r"(?:move|go|jump)(?: the cursor)? to the (?:beginning|start) of (?:the |this )?line"),
    ("line_end", r"(?:move|go|jump)(?: the cursor)? to the end of (?:the |this )?line"),
    ("heading", r"make (?:this|that|it) (?:a |the )?(?:heading|title)"),
    ("add_heading", r"(?:add|insert|write|start) a (?:new )?heading(?:[,:]? (?:called |saying |that says )?(?P<text>.+))?"),
    ("bullet", r"(?:write|add|insert|start) (?:a )?(?:new )?bullet(?: point)?(?:[,:]? (?P<text>.+))?|new bullet|"
               r"next bullet"),
    ("numbered", r"(?:add|start|insert|make) a numbered list|numbered list"),
    ("indent", r"indent (?:this|that|the) (?:paragraph|line)|indent(?: it)?"),
    ("replace", r"(?:replace|change) (?:the word )?(?P<old>.+?) (?:with|to|into) (?P<new>.+)"),
    ("spelling", r"(?:correct|fix|check) (?:the )?spelling(?: of (?:that|this|the last (?:sentence|paragraph)))?|"
                 r"fix (?:the )?typos"),
    ("exact", r"(?:write|type|put|enter) (?:this |that |the following )?(?:exactly|verbatim|word for word)[,:]?"
              r" (?P<text>.+)"),
    ("continue", r"continue(?: writing)?|keep (?:writing|going)|write (?:some )?more|go on|carry on"),
    ("generate", r"(?:write|compose|draft|create|generate)(?: me)? (?:a|an|the|some|two|three|four|five|one) .+|"
                 r"(?:write|draft) (?:about|on) .+"),
]]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse(text: str, *, dictating: bool = True) -> list[Op]:
    """Operations for one utterance. With ``dictating`` False (writing mode) only commands are returned."""
    raw = _clean(text)
    plain = re.sub(r"[.!?,]+$", "", raw).strip()
    plain = re.sub(r"^(?:sid[,]?\s+|please\s+|can you\s+|now\s+|ok(?:ay)?[,]?\s+)", "", plain, flags=re.I).strip()
    plain = re.sub(r"\s+please$", "", plain, flags=re.I)
    for name, pattern in _COMMANDS:
        match = pattern.match(plain)
        if match:
            if name == "generate" and dictating and not re.match(r"^(?:write|compose|draft)", plain, re.I):
                continue
            args = {k: v.strip() for k, v in match.groupdict().items() if v}
            if name in ("exact", "add_heading", "bullet") and "text" in args:
                original = re.search(re.escape(args["text"]), raw, re.I)
                if original:  # keep the speaker's casing and punctuation
                    args["text"] = raw[original.start():].strip()
            if name == "generate":
                args["prompt"] = raw
            return [Op("command", name, args)]
    if not dictating:
        layout = _INLINE.fullmatch(raw)
        if layout:
            return _layout_op(layout.group("cmd"))
        return []
    return content_ops(raw)


def _layout_op(cmd: str) -> list[Op]:
    cmd = cmd.lower()
    if "paragraph" in cmd or "blank" in cmd:
        return [Op("keys", "enter"), Op("keys", "enter")]
    return [Op("keys", "enter")]


def content_ops(text: str) -> list[Op]:
    for pattern, mark in _SPOKEN_PUNCTUATION:
        text = pattern.sub(mark, text)
    ops: list[Op] = []
    position = 0
    for match in _INLINE.finditer(text):
        before = text[position:match.start()].strip()
        if before:
            ops.append(Op("text", before))
        ops += _layout_op(match.group("cmd"))
        position = match.end()
    rest = text[position:].strip()
    if rest:
        ops.append(Op("text", rest))
    return ops


# ---------------------------------------------------------------------------------------------- executing


class FocusLost(RuntimeError):
    pass


@dataclass
class DictationTarget:
    """The document being written into, and what SID typed there this session (for "scratch that")."""

    window: WindowInfo
    typed: str = ""  # everything typed this session, in order (newlines included)
    chunks: list[str] = field(default_factory=list)  # one per typed piece, for "scratch that"

    @property
    def app(self) -> str:
        return self.window.app.lower()

    @property
    def rich(self) -> bool:
        return self.app in RICH_EDITORS

    def joined(self, text: str) -> str:
        """``text`` as it should continue what's already there: a joining space, sentence capitalisation."""
        if not self.typed:
            return text[:1].upper() + text[1:]
        last = self.typed[-1]
        if last in "\n":
            return text[:1].upper() + text[1:]
        if re.search(r"[.!?]\s*$", self.typed):
            text = text[:1].upper() + text[1:]
        if text[:1] in ",.;:!?)":
            return text
        return text if last.isspace() else " " + text


class DictationTyper:
    """Desktop-thread executor: types and presses keys only while the target window is in front."""

    def __init__(self, backend: DesktopBackend, keyboard, sleep=time.sleep, interval_s: float = 0.004) -> None:
        self._backend = backend
        self._keyboard = keyboard
        self._sleep = sleep
        self.interval_s = interval_s

    def _guard(self, target: DictationTarget) -> None:
        if self._backend.foreground() != target.window.hwnd or not self._backend.is_window(target.window.hwnd):
            raise FocusLost(target.window.title)

    def type(self, target: DictationTarget, text: str, *, join: bool = True) -> int:
        piece = target.joined(text) if join else text
        self._guard(target)
        typed, problem = self._keyboard._type_realtime(piece, target.window, self.interval_s)
        target.typed += piece[:typed]
        target.chunks.append(piece[:typed])
        if problem == "focus":
            raise FocusLost(target.window.title)
        if problem == "cancelled":
            raise InterruptedError("cancelled")
        return typed

    def keys(self, target: DictationTarget, combo: str, times: int = 1) -> None:
        self._guard(target)
        vks = parse_combo(combo)
        for _ in range(times):
            self._keyboard.chord(vks[:-1], vks[-1])
            self._sleep(0.02)
        if combo == "enter":
            target.typed += "\n" * times
            target.chunks.append("\n" * times)

    def backspace(self, target: DictationTarget, count: int) -> None:
        self._guard(target)
        vk = parse_combo("backspace")[-1]
        for _ in range(count):
            self._keyboard.tap(vk)
        target.typed = target.typed[:-count] if count else target.typed

    def delete_last_sentence(self, target: DictationTarget) -> str | None:
        text = target.typed.rstrip(" ")
        if not text.strip():
            return None
        body = text.rstrip(".!?\n ")
        cut = max(body.rfind(". "), body.rfind("! "), body.rfind("? "), body.rfind("\n"))
        start = cut + 1 if cut >= 0 else 0  # keep the full stop (or line break), drop the joining space
        removed = target.typed[start:]
        self.backspace(target, len(removed))
        target.chunks = [c for c in target.chunks if c] or []
        return removed.strip()

    def scratch(self, target: DictationTarget) -> str | None:
        while target.chunks and not target.chunks[-1]:
            target.chunks.pop()
        if not target.chunks:
            return None
        last = target.chunks.pop()
        if target.typed.endswith(last):
            self.backspace(target, len(last))
            return last.strip() or "the line break"
        return None
