"""Key names → Windows virtual-key codes, and spoken shortcut parsing.

``parse_combo("ctrl+shift+t")``, ``parse_combo("control shift t")`` and
``parse_combo("the windows key")`` all work; the result is the list of
virtual keys to hold, modifiers first. Pure Python, no OS calls.
"""

from __future__ import annotations

import re

VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12
VK_CAPITAL, VK_ESCAPE, VK_SPACE = 0x14, 0x1B, 0x20
VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28
VK_INSERT, VK_DELETE, VK_LWIN = 0x2D, 0x2E, 0x5B
VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3

MODIFIERS = {VK_SHIFT: "Shift", VK_CONTROL: "Ctrl", VK_MENU: "Alt", VK_LWIN: "Win", 0x5C: "Win"}

# Keys whose scan code carries the E0 prefix: they need KEYEVENTF_EXTENDEDKEY.
EXTENDED = {VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_INSERT, VK_DELETE,
            VK_LWIN, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5, 0x2C, *range(0xA6, 0xB8)}

# Keys that act globally (no window needs focus for them).
GLOBAL_KEYS = {0xAD, 0xAE, 0xAF, VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE}

_NAMES: dict[str, int] = {
    "backspace": VK_BACK, "back space": VK_BACK, "tab": VK_TAB, "enter": VK_RETURN, "return": VK_RETURN,
    "shift": VK_SHIFT, "ctrl": VK_CONTROL, "control": VK_CONTROL, "ctl": VK_CONTROL, "alt": VK_MENU,
    "option": VK_MENU, "pause break": 0x13, "caps lock": VK_CAPITAL, "capslock": VK_CAPITAL,
    "esc": VK_ESCAPE, "escape": VK_ESCAPE, "space": VK_SPACE, "spacebar": VK_SPACE, "space bar": VK_SPACE,
    "page up": VK_PRIOR, "pageup": VK_PRIOR, "pgup": VK_PRIOR, "page down": VK_NEXT, "pagedown": VK_NEXT,
    "pgdn": VK_NEXT, "end": VK_END, "home": VK_HOME, "left": VK_LEFT, "left arrow": VK_LEFT, "arrow left": VK_LEFT,
    "up": VK_UP, "up arrow": VK_UP, "arrow up": VK_UP, "right": VK_RIGHT, "right arrow": VK_RIGHT,
    "arrow right": VK_RIGHT, "down": VK_DOWN, "down arrow": VK_DOWN, "arrow down": VK_DOWN,
    "print screen": 0x2C, "printscreen": 0x2C, "prtsc": 0x2C, "insert": VK_INSERT, "ins": VK_INSERT,
    "delete": VK_DELETE, "del": VK_DELETE, "win": VK_LWIN, "windows": VK_LWIN, "window": VK_LWIN,
    "super": VK_LWIN, "start": VK_LWIN, "menu": 0x5D, "context menu": 0x5D, "apps": 0x5D,
    "num lock": 0x90, "numlock": 0x90, "scroll lock": 0x91,
    "volume mute": 0xAD, "mute": 0xAD, "volume down": 0xAE, "volume up": 0xAF,
    "next track": VK_MEDIA_NEXT, "nexttrack": VK_MEDIA_NEXT, "previous track": VK_MEDIA_PREV,
    "prev track": VK_MEDIA_PREV, "prevtrack": VK_MEDIA_PREV, "media stop": VK_MEDIA_STOP,
    "play pause": VK_MEDIA_PLAY_PAUSE, "playpause": VK_MEDIA_PLAY_PAUSE, "play": VK_MEDIA_PLAY_PAUSE,
    "browser back": 0xA6, "browser forward": 0xA7, "browser refresh": 0xA8,
    # punctuation keys by name (US positions; combos only — typing is layout-aware)
    "plus": 0xBB, "equals": 0xBB, "equal": 0xBB, "minus": 0xBD, "dash": 0xBD, "hyphen": 0xBD,
    "comma": 0xBC, "period": 0xBE, "dot": 0xBE, "full stop": 0xBE, "slash": 0xBF, "forward slash": 0xBF,
    "backslash": 0xDC, "back slash": 0xDC, "semicolon": 0xBA, "quote": 0xDE, "apostrophe": 0xDE,
    "backtick": 0xC0, "back tick": 0xC0, "grave": 0xC0, "tilde": 0xC0, "left bracket": 0xDB,
    "open bracket": 0xDB, "right bracket": 0xDD, "close bracket": 0xDD,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
}
_NAMES.update({chr(c): 0x41 + i for i, c in enumerate(range(ord("a"), ord("z") + 1))})
_NAMES.update({str(d): 0x30 + d for d in range(10)})
_NAMES.update({f"f{n}": 0x6F + n for n in range(1, 25)})
_NAMES.update({f"numpad {d}": 0x60 + d for d in range(10)})
_NAMES.update({f"numpad{d}": 0x60 + d for d in range(10)})

_SYMBOLS = {"+": 0xBB, "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC, ";": 0xBA,
            "'": 0xDE, "`": 0xC0, "[": 0xDB, "]": 0xDD}

# Spoken words for letters and digits ("control see", "f five", "alt tab").
_SPOKEN = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "see": "c", "sea": "c", "vee": "v", "ex": "x", "zed": "z", "zee": "z",
    "why": "y", "are": "r", "you": "u", "queue": "q", "tee": "t", "pee": "p", "bee": "b", "dee": "d",
    "eff": "f", "gee": "g", "jay": "j", "kay": "k", "el": "l", "em": "m", "en": "n", "oh": "o",
    "ess": "s", "double you": "w",
}

_DISPLAY = {VK_RETURN: "Enter", VK_TAB: "Tab", VK_BACK: "Backspace", VK_ESCAPE: "Esc", VK_SPACE: "Space",
            VK_DELETE: "Delete", VK_HOME: "Home", VK_END: "End", VK_PRIOR: "Page Up", VK_NEXT: "Page Down",
            VK_LEFT: "Left", VK_RIGHT: "Right", VK_UP: "Up", VK_DOWN: "Down", 0x5D: "Menu", VK_CAPITAL: "Caps Lock",
            0xAD: "Mute", 0xAE: "Volume Down", 0xAF: "Volume Up", VK_MEDIA_NEXT: "Next Track",
            VK_MEDIA_PREV: "Previous Track", VK_MEDIA_STOP: "Stop", VK_MEDIA_PLAY_PAUSE: "Play/Pause",
            0x2C: "Print Screen", VK_INSERT: "Insert"}


class KeyParseError(ValueError):
    pass


def _lookup(phrase: str) -> int | None:
    phrase = phrase.strip()
    if not phrase:
        return None
    if phrase in _NAMES:
        return _NAMES[phrase]
    if phrase in _SYMBOLS:
        return _SYMBOLS[phrase]
    if phrase in _SPOKEN:
        return _NAMES.get(_SPOKEN[phrase])
    match = re.fullmatch(r"f ?(\d{1,2})", phrase)
    if match and 1 <= int(match.group(1)) <= 24:
        return 0x6F + int(match.group(1))
    match = re.fullmatch(r"f (one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)", phrase)
    if match:
        words = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
        return 0x70 + words.index(match.group(1))
    return None


def parse_combo(spec: str | list[str]) -> list[int]:
    """Parse "ctrl+shift+z", "control shift t", ["ctrl", "s"] or "the enter key" into virtual keys.

    Modifiers come first, in the order spoken; at most one non-modifier key.
    """
    if isinstance(spec, list):
        parts = [str(p) for p in spec]
    else:
        text = spec.lower().strip()
        text = re.sub(r"\b(?:the|key|keys|button|buttons|keyboard|shortcut)\b", " ", text)
        text = re.sub(r"\s+(?:plus|and|then)\s+", "+", text)
        if text.strip() in _SYMBOLS:
            parts = [text.strip()]
        else:
            parts = [p for p in re.split(r"\s*\+\s*|\s*,\s*", text) if p.strip()]
    words: list[str] = []
    for part in parts:
        part = part.lower().strip()
        if part in _SYMBOLS or _lookup(part) is not None:
            words.append(part)
            continue
        tokens = part.split()
        i = 0
        while i < len(tokens):
            for size in (3, 2, 1):  # greedy multi-word names: "page down", "print screen", "double you"
                chunk = " ".join(tokens[i:i + size])
                if size <= len(tokens) - i and _lookup(chunk) is not None:
                    words.append(chunk)
                    i += size
                    break
            else:
                raise KeyParseError(f"unknown key {tokens[i]!r}")
    if not words:
        raise KeyParseError("no keys")
    vks = [_lookup(w) for w in words]
    resolved = [vk for vk in vks if vk is not None]
    modifiers = [vk for vk in resolved if vk in MODIFIERS]
    others = [vk for vk in resolved if vk not in MODIFIERS]
    if len(others) > 1:
        raise KeyParseError("more than one non-modifier key in a shortcut")
    ordered: list[int] = []
    for vk in modifiers:
        if vk not in ordered:
            ordered.append(vk)
    return ordered + others


def describe_combo(vks: list[int]) -> str:
    names = []
    for vk in vks:
        if vk in MODIFIERS:
            names.append(MODIFIERS[vk])
        elif vk in _DISPLAY:
            names.append(_DISPLAY[vk])
        elif 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
            names.append(chr(vk))
        elif 0x70 <= vk <= 0x87:
            names.append(f"F{vk - 0x6F}")
        else:
            symbol = next((s for s, code in _SYMBOLS.items() if code == vk), None)
            names.append(symbol or f"key {vk:#04x}")
    return "+".join(names)


TIMES = {"once": 1, "twice": 2, "thrice": 3, "two times": 2, "three times": 3, "four times": 4, "five times": 5}


def parse_times(text: str | None) -> int:
    if not text:
        return 1
    text = text.strip().lower()
    if text in TIMES:
        return TIMES[text]
    match = re.fullmatch(r"(\d{1,2}) times?", text)
    return max(1, min(50, int(match.group(1)))) if match else 1
