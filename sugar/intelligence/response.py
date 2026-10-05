"""Turning model output into natural speech.

Two jobs:

1. :class:`SpeechChunker` — consumes a token stream and emits speakable
   chunks at natural boundaries as soon as they are complete, so TTS can
   start on the first sentence while the model is still writing the rest.
   It is markdown-aware: code blocks, tables and stack traces are never read
   aloud (one short spoken pointer to the screen replaces them).

2. :func:`normalize_for_speech` — rewrites one chunk so a TTS engine says it
   the way a person would: ``D:\\Projects\\App`` → "D colon, Projects, App",
   ``main.py`` → "main dot py", URLs → "a link to github dot com", versions,
   acronyms, symbols, markdown emphasis, emoji.

The display text is left untouched; only the spoken text is rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# --------------------------------------------------------------------------- filler

_FILLER_PATTERNS = [
    r"(?:certainly|absolutely|of course|sure thing|sure|great question|good question|no problem|definitely)[!.,]+",
    r"i'?d be (?:happy|glad|delighted) to help(?: you)?(?: with that)?[!.,]*",
    r"i'?d be (?:happy|glad|delighted) to[!.,]*",
    r"as an ai(?: language model| assistant)?,?",
    r"(?:here(?:'s| is) (?:the|your) answer)[:.!]*",
]
_FILLER_RE = re.compile(r"^\s*(?:(?:" + "|".join(_FILLER_PATTERNS) + r")\s*)+", re.IGNORECASE)


def strip_filler(text: str) -> str:
    """Drop robotic openers ("Certainly!", "As an AI…") from the start of a reply."""
    stripped = _FILLER_RE.sub("", text, count=1)
    if stripped and stripped != text:
        stripped = stripped[0].upper() + stripped[1:]
    return stripped


# --------------------------------------------------------------------------- normalisation

ACRONYMS = {
    "API": "A P I", "APIs": "A P Is", "CLI": "C L I", "UI": "U I", "UX": "U X", "URL": "U R L",
    "URLs": "U R Ls", "HTTP": "H T T P", "HTTPS": "H T T P S", "CPU": "C P U", "GPU": "G P U",
    "RAM": "ram", "SQL": "sequel", "JSON": "Jason", "YAML": "yammel", "HTML": "H T M L",
    "CSS": "C S S", "JS": "JavaScript", "TS": "TypeScript", "npm": "N P M", "pip": "pip",
    "PR": "P R", "PRs": "P Rs", "CI": "C I", "CD": "C D", "VS": "V S", "ID": "I D", "IDs": "I Ds",
    "OS": "O S", "SSH": "S S H", "SDK": "S D K", "LLM": "L L M", "AI": "A I", "TTS": "T T S",
    "STT": "S T T", "README": "read me", "OAuth": "O auth", "JWT": "J W T", "DB": "database",
    "env": "env", "async": "a-sync", "regex": "rej ex", "stdout": "standard out",
    "stderr": "standard error", "WebSocket": "web socket", "WebSockets": "web sockets",
    "localhost": "local host", "GitHub": "GitHub", "PyTorch": "PyTorch", "iOS": "i O S",
}
_ACRONYM_RE = re.compile(
    r"(?<![\w\-])(" + "|".join(sorted(map(re.escape, ACRONYMS), key=len, reverse=True)) + r")(?![\w\-])"
)

_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
_PATH_PART = r"[^\\/\s:*?\"<>|,;]+"
# Inner components may contain single spaces ("SOMESHIT DOWNLOADS"); the last one may not,
# otherwise a path at the end of a sentence would swallow the following words.
_WIN_PATH_RE = re.compile(
    r"\b([A-Za-z]):[\\/]((?:" + _PATH_PART + r"(?: " + _PATH_PART + r")*[\\/])*(?:" + _PATH_PART + r")?)"
)
_SLASH_PATH_RE = re.compile(r"(?<![\w.:/])((?:\.{1,2}/|~/|/)?[\w.\-]+(?:/[\w.\-]+)+/?)")
_EXTENSIONS = (r"py|js|jsx|ts|tsx|json|md|txt|yaml|yml|toml|ini|cfg|html|css|scss|java|kt|go|rs|rb|php|cs|cpp|c|h|"
               r"hpp|sh|ps1|bat|cmd|sql|csv|lock|env|xml|vue|svelte|dart|swift|mjs|cjs|exe|dll|zip|png|jpg|jpeg|"
               r"gif|svg|pdf|log|ipynb")
_FILE_RE = re.compile(r"\b([\w\-]+(?:\.[\w\-]+)*)\.(" + _EXTENSIONS + r")\b")
_DOTFILE_RE = re.compile(r"(?<![\w/\\])\.(env|gitignore|github|vscode|venv|dockerignore|eslintrc|prettierrc)\b")
_VERSION_RE = re.compile(r"\b(v)?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?\b", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORE_EMPHASIS_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F]")
_IDENTIFIER_RE = re.compile(r"\b([A-Za-z][a-z0-9]*(?:_[A-Za-z0-9]+)+)\b")  # snake_case / SCREAMING_CASE
_CALL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\(\)")
_LATIN = {r"\be\.g\.": "for example", r"\bi\.e\.": "that is", r"\betc\.": "et cetera", r"\bvs\.?(?=\s)": "versus"}


def _file_words(part: str) -> str:
    return _FILE_RE.sub(lambda f: f"{f.group(1)} dot {f.group(2)}", part)


def _domain(url: str) -> str:
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", url, flags=re.IGNORECASE).split("/")[0]
    return host.replace(".", " dot ")


def _win_path(m: re.Match) -> str:
    drive, rest = m.group(1).upper(), m.group(2)
    parts = [p for p in re.split(r"[\\/]+", rest.strip("\\/")) if p]
    if len(parts) > 4:
        parts = parts[:1] + parts[-3:]
    spoken = ", ".join(_file_words(p) for p in parts)
    return f"{drive} colon, {spoken}" if spoken else f"{drive} drive"


def _slash_path(m: re.Match) -> str:
    raw = m.group(1)
    parts = [p for p in raw.split("/") if p and p not in {".", ".."}]
    looks_like_path = raw.startswith(("/", "./", "../", "~/")) or raw.count("/") >= 2 or _FILE_RE.search(parts[-1] if parts else "")
    if not looks_like_path:
        return raw  # "and/or", "client/server"
    if len(parts) > 3:
        parts = parts[-2:]
    return " slash ".join(_file_words(p) for p in parts)


def _version(m: re.Match) -> str:
    numbers = [g for g in m.groups()[1:] if g is not None]
    if m.group(1) is None and len(numbers) < 3:
        return m.group(0)  # a plain decimal like 3.14
    spoken = " point ".join(numbers)
    return f"version {spoken}" if m.group(1) else spoken


def normalize_for_speech(text: str) -> str:
    if not text:
        return ""
    s = text
    s = _IMAGE_RE.sub("", s)
    s = _LINK_RE.sub(lambda m: m.group(1), s)
    s = _URL_RE.sub(lambda m: f"a link to {_domain(m.group(0))}", s)
    s = _INLINE_CODE_RE.sub(lambda m: m.group(1), s)
    s = _CALL_RE.sub(lambda m: m.group(1), s)
    for pattern, repl in _LATIN.items():
        s = re.sub(pattern, repl, s, flags=re.IGNORECASE)
    s = _WIN_PATH_RE.sub(_win_path, s)
    s = _SLASH_PATH_RE.sub(_slash_path, s)
    s = _DOTFILE_RE.sub(lambda m: f"dot {m.group(1)}", s)
    s = _FILE_RE.sub(lambda m: f"{m.group(1)} dot {m.group(2)}", s)
    s = _VERSION_RE.sub(_version, s)
    s = re.sub(r"\b(version)\s+version\b", r"\1", s, flags=re.IGNORECASE)
    s = _IDENTIFIER_RE.sub(lambda m: m.group(1).replace("_", " "), s)
    s = _EMPHASIS_RE.sub(lambda m: m.group(2), s)
    s = _UNDERSCORE_EMPHASIS_RE.sub(lambda m: m.group(1), s)
    s = _ACRONYM_RE.sub(lambda m: ACRONYMS[m.group(1)], s)

    replacements = [
        (r"\s*(?:->|→|=>)\s*", " to "),
        (r"\s*&&\s*", " and "),
        (r"\s+&\s+", " and "),
        (r"(\d)\s*%", r"\1 percent"),
        (r"\s+@\s+", " at "),
        (r"(?<=\s)#(\d+)", r"number \1"),
        (r"(?<=\d)\s*x\s*(?=\d)", " by "),
        (r"\s*==\s*", " equals "),
        (r"\s*!=\s*", " is not "),
        (r"\s*<=\s*", " less than or equal to "),
        (r"\s*>=\s*", " greater than or equal to "),
        (r"~(?=\d)", "about "),
        (r"\band/or\b", "and or"),
        (r"(?<=\w)/(?=\w)", " or "),
        (r"[*#>|`_]+", " "),
        (r"\s*[–—]\s*", ", "),
        (r"\.{3,}|…", "… "),
    ]
    for pattern, repl in replacements:
        s = re.sub(pattern, repl, s)
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"\s+([,.!?;:])", r"\1", s)
    s = re.sub(r"([,;:])(?=[^\s\d])", r"\1 ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


# --------------------------------------------------------------------------- chunking

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx", "inc", "ltd",
    "co", "corp", "dept", "fig", "no", "vol", "est", "min", "max", "avg", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}
_STACK_LINE_RE = re.compile(r'^\s*(?:File ".*", line \d+|at \S+ \(.*:\d+:\d+\)|at .*\.java:\d+\)|Traceback \(most recent)')
_ERROR_LINE_RE = re.compile(r"^\s*([A-Z][A-Za-z]*(?:Error|Exception|Warning)):\s*(.+)$")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


class SpeechChunker:
    """Incremental text → speakable chunks.

    ``feed`` accepts arbitrary token fragments and yields chunks as soon as a
    sentence boundary has been *seen* (the boundary must be followed by
    whitespace, so "3." of "3.14" or "e." of "e.g." never splits early).

    * the first chunk may be very short ("Done.") to minimise time to first
      audio; later chunks are merged to at least ``min_chars`` so prosody does
      not reset every few words;
    * run-on text is split at a clause boundary once it exceeds ``max_chars``;
    * complete markdown lines are interpreted (code fences, tables, stack
      traces, headings, list items); the unfinished line is spoken from
      provisionally unless it could be the start of one of those blocks;
    * after ``max_spoken_chars`` the chunker says "The rest is on screen."
      once and stops.
    """

    def __init__(
        self,
        *,
        first_min_chars: int = 2,
        min_chars: int = 40,
        max_chars: int = 240,
        max_spoken_chars: int | None = 900,
    ) -> None:
        self._first_min = first_min_chars
        self._min = min_chars
        self._max = max_chars
        self._limit = max_spoken_chars
        self._buffer = ""  # raw text of the current (unfinished) line
        self._ready = ""  # cleaned text from completed lines, not yet spoken
        self._emitted_chars = 0
        self._emitted_count = 0
        self._in_code = False
        self._code_announced = False
        self._table_announced = False
        self._trace_announced = False
        self._truncated = False
        self._filler_checked = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    # ----------------------------------------------------------------- public

    def feed(self, text: str) -> Iterator[str]:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._consume_line(line)
        yield from self._emit(final=False)

    def flush(self) -> Iterator[str]:
        if self._buffer:
            line, self._buffer = self._buffer, ""
            self._consume_line(line)
        yield from self._emit(final=True)

    # ----------------------------------------------------------------- lines

    def _consume_line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            if not self._in_code and not self._code_announced:
                self._append_ready("I've put the code on screen.")
                self._code_announced = True
            self._in_code = not self._in_code
            return
        if self._in_code:
            return
        if stripped.startswith("|") and stripped.endswith("|"):
            if not self._table_announced:
                self._append_ready("There's a table on screen.")
                self._table_announced = True
            return
        if _STACK_LINE_RE.match(line):
            if not self._trace_announced:
                self._append_ready("There's an error trace on screen.")
                self._trace_announced = True
            return
        error = _ERROR_LINE_RE.match(line)
        if error:
            kind = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", error.group(1)).lower()
            message = error.group(2).strip().rstrip(".") + "."
            self._append_ready(f"{kind[0].upper() + kind[1:]}: {message}")
            return
        if not stripped:
            self._end_paragraph()
            return
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")) and len(stripped) > 40:
            self._append_ready("The details are on screen.")
            return
        text = _HEADING_RE.sub("", line)
        heading = text != line
        is_list = bool(_LIST_MARKER_RE.match(text))
        text = _LIST_MARKER_RE.sub("", text).strip()
        if (is_list or heading) and text and not text.endswith((".", "!", "?", ":", ";")):
            text += "."
        self._append_ready(text)

    def _end_paragraph(self) -> None:
        ready = self._ready.rstrip()
        if ready and not ready.endswith((".", "!", "?", ":", "…")):
            self._ready = ready + ". "

    def _append_ready(self, text: str) -> None:
        text = text.strip()
        if text:
            self._ready = (self._ready.rstrip() + " " + text + " ").lstrip()

    def _partial_view(self) -> tuple[str, int]:
        """Speakable view of the unfinished line and how many raw chars were markup."""
        raw = self._buffer
        stripped = raw.lstrip()
        if self._in_code or not stripped:
            return "", 0
        if stripped.startswith(("`", "~", "|", "{", "[")) or _STACK_LINE_RE.match(raw) or _ERROR_LINE_RE.match(raw):
            return "", 0  # might be a fence, table, JSON or trace — wait for the full line
        prefix = _HEADING_RE.match(raw) or _LIST_MARKER_RE.match(raw)
        skip = prefix.end() if prefix else len(raw) - len(stripped)
        return raw[skip:], skip

    # ----------------------------------------------------------------- chunking

    def _emit(self, final: bool) -> Iterator[str]:
        while not self._truncated:
            view, markup = self._partial_view()
            text = self._ready + view
            cut = self._find_cut(text, final)
            if cut is None:
                break
            chunk = text[:cut]
            if cut <= len(self._ready):
                self._ready = self._ready[cut:]
            else:
                taken = cut - len(self._ready)
                self._ready = ""
                self._buffer = self._buffer[markup + taken:]
            spoken = self._finalize(chunk)
            if spoken:
                yield spoken
        if final:
            self._ready = ""
            self._buffer = ""

    def _finalize(self, chunk: str) -> str:
        chunk = chunk.strip()
        if not self._filler_checked and chunk:
            chunk = strip_filler(chunk)
            # Keep checking while the opener was nothing but filler ("Certainly! I'd be happy to…").
            self._filler_checked = bool(chunk)
        spoken = normalize_for_speech(chunk)
        if not spoken or not re.search(r"\w", spoken):
            return ""
        if self._limit is not None and self._emitted_count and self._emitted_chars + len(spoken) > self._limit:
            self._truncated = True
            return "The rest is on screen."
        self._emitted_chars += len(spoken)
        self._emitted_count += 1
        return spoken

    def _find_cut(self, text: str, final: bool) -> int | None:
        """Index just after the boundary to cut at, or None to wait for more text."""
        if not text.strip():
            return None
        minimum = self._first_min if self._emitted_count == 0 else self._min
        for match in _BOUNDARY_RE.finditer(text):
            if _is_abbreviation(text, match.start()):
                continue
            if match.end() >= minimum:
                return match.end()
        if len(text) > self._max:
            window = text[: self._max]
            for pattern in (r"[;:](?=\s)", r",(?=\s)", r"\s"):
                positions = [m.end() for m in re.finditer(pattern, window) if m.end() >= self._min]
                if positions:
                    return positions[-1]
            return self._max
        return len(text) if final else None


_BOUNDARY_RE = re.compile(r"[.!?…]+[\"')\]]*(?=\s)")


def _is_abbreviation(text: str, period_index: int) -> bool:
    if text[period_index] != ".":
        return False
    start = period_index
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "."):
        start -= 1
    word = text[start:period_index].lower()
    if not word:
        return False
    if word in _ABBREVIATIONS:
        return True
    parts = word.split(".")
    return all(len(p) == 1 and p.isalpha() for p in parts)  # initials: "J. R.", "U.S."


def speakable_summary(text: str, max_sentences: int = 3, max_chars: int = 420) -> str:
    """Short spoken version of a long report (e.g. a Claude Code result)."""
    chunker = SpeechChunker(max_spoken_chars=None)
    sentences: list[str] = []
    for chunk in [*chunker.feed(text), *chunker.flush()]:
        if chunk in {"I've put the code on screen.", "There's a table on screen.", "There's an error trace on screen."}:
            continue
        sentences.append(chunk)
        if len(sentences) >= max_sentences or sum(len(s) for s in sentences) >= max_chars:
            break
    return " ".join(sentences)
