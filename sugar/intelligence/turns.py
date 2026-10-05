"""Semantic turn-completion heuristics.

When the user pauses, the conversation layer transcribes what was said so
far with the fast model and asks :func:`assess_completeness` whether it
sounds finished. The answer sets how long the endpointer waits before
ending the turn:

    "open chrome"                     → complete   (~0.4 s)
    "I want you to open VS Code"      → unknown    (~0.75 s)
    "I want you to"                   → incomplete (~1.7 s)
    "open VS Code… actually wait"     → incomplete

The heuristics are deliberately conservative; a wrong "complete" cuts the
user off, a wrong "incomplete" only costs a second.
"""

from __future__ import annotations

import re
from collections.abc import Callable

COMPLETE, INCOMPLETE, UNKNOWN = "complete", "incomplete", "unknown"

_TRAILING_INCOMPLETE = {
    "and", "or", "but", "so", "because", "then", "to", "the", "a", "an", "of", "for", "with", "in", "on",
    "at", "from", "into", "my", "your", "our", "their", "his", "her", "is", "are", "was", "were", "be", "if",
    "when", "while", "also", "like", "um", "uh", "er", "erm", "hmm", "actually", "wait", "maybe", "plus",
    "about", "as", "than", "which", "who", "where", "whether", "can", "could", "would", "should", "will",
    "please", "just", "and then", "by", "without", "after", "before", "until", "unless", "although",
    "though", "let", "lets", "let's", "i", "we", "you", "it's", "its", "this", "these", "those", "some",
    "any", "all", "more", "very", "really", "kind", "sort", "um,", "via",
}
_INCOMPLETE_PHRASES = (
    "i want you to", "can you", "could you", "would you", "will you", "i need you to", "let me", "i think",
    "how do i", "what about", "and also", "no wait", "hold on", "one sec", "one second", "give me a sec",
    "hang on", "i mean", "you know", "what i want is", "the thing is", "so basically",
)
_SHORT_COMPLETE = {
    "yes", "yeah", "yep", "no", "nope", "stop", "wait", "cancel", "never mind", "nevermind", "do it",
    "go ahead", "continue", "thanks", "thank you", "okay", "ok", "sure", "please do", "not now",
}
_QUESTION_WORDS = {"what", "how", "why", "when", "where", "who", "which", "can", "could", "should", "is", "are", "do"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'?.!,…-]", " ", text.lower())).strip()


def assess_completeness(text: str, is_command: Callable[[str], bool] | None = None) -> str:
    cleaned = _clean(text)
    if not cleaned:
        return UNKNOWN
    bare = cleaned.rstrip(".?!,… ").strip()
    words = bare.split()
    if not words:
        return UNKNOWN

    trailing = cleaned.rstrip()
    if trailing.endswith((",", "…", "...", "-")):
        return INCOMPLETE
    last = words[-1]
    last_two = " ".join(words[-2:])
    if last in _TRAILING_INCOMPLETE or last_two in _TRAILING_INCOMPLETE:
        # "wait" alone is a complete request; "open VS Code, actually wait" is not.
        if not (len(words) == 1 and bare in _SHORT_COMPLETE):
            return INCOMPLETE
    if any(bare.endswith(phrase) for phrase in _INCOMPLETE_PHRASES):
        return INCOMPLETE
    if bare in _SHORT_COMPLETE:
        return COMPLETE
    if len(words) <= 2 and words[0] in _QUESTION_WORDS and not trailing.endswith("?"):
        return INCOMPLETE
    if is_command is not None and is_command(bare):
        return COMPLETE
    if trailing.endswith("?") and len(words) >= 3:
        return COMPLETE
    return UNKNOWN
