"""Response processing: incremental chunking and speech normalisation."""

from __future__ import annotations

import pytest

from sugar.intelligence.response import SpeechChunker, normalize_for_speech, speakable_summary, strip_filler


def stream(text: str, step: int = 3, **kwargs) -> list[str]:
    chunker = SpeechChunker(**kwargs)
    out: list[str] = []
    for i in range(0, len(text), step):
        out += list(chunker.feed(text[i:i + step]))
    out += list(chunker.flush())
    return out


@pytest.mark.parametrize("step", [1, 3, 7, 1000])
def test_first_sentence_is_emitted_alone_regardless_of_token_size(step):
    chunks = stream("I found the problem. The issue is inside your authentication middleware, "
                    "where the token is never refreshed. That breaks every request after an hour.", step)
    assert chunks[0] == "I found the problem."
    assert "authentication middleware" in chunks[1]


def test_first_chunk_is_available_before_the_stream_ends():
    chunker = SpeechChunker()
    early = []
    for token in ["Done", ".", " ", "The build"]:
        early += list(chunker.feed(token))
    assert early == ["Done."]


def test_no_split_inside_decimals_abbreviations_or_initials():
    chunks = stream("It costs 3.14 dollars, e.g. per call, and Dr. Smith from the U.S. office agrees. "
                    "J. R. R. Tolkien wrote it. Next sentence follows here for sure.", step=2)
    assert chunks[0].startswith("It costs 3.14 dollars, for example per call, and Dr. Smith")
    assert "office agrees." in chunks[0]


def test_later_chunks_merge_short_sentences():
    chunks = stream("Okay. Yes. No. Maybe. Sure. Fine. Right. Good. Then we continue with this.")
    assert chunks[0] == "Okay."
    assert all(len(c) >= 30 for c in chunks[1:-1])


def test_run_on_text_is_split_at_a_clause():
    chunks = stream("word " * 120, step=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 260 for c in chunks)


def test_code_blocks_are_never_spoken():
    chunks = stream("Here is the fix:\n```python\nimport os\nos.remove('x')\n```\nRun it again.\n")
    joined = " ".join(chunks)
    assert "import" not in joined and "remove" not in joined
    assert "I've put the code on screen." in joined
    assert joined.endswith("Run it again.")


def test_tables_traces_and_json_are_summarised():
    joined = " ".join(stream(
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
        'Traceback (most recent call last):\n  File "x.py", line 3, in <module>\n'
        "TypeError: x is undefined\n"
        '{"status": "error", "code": 500, "detail": "something long enough"}\n'
        "That is all.\n"
    ))
    assert "There's a table on screen." in joined
    assert "There's an error trace on screen." in joined
    assert "Type error: x is undefined." in joined
    assert "The details are on screen." in joined
    assert '"status"' not in joined


def test_markdown_lists_and_headings_become_sentences():
    joined = " ".join(stream("## Results\n- WebSocket reconnect fixed\n- State persisted\n\nAll tests pass.\n"))
    assert joined.startswith("Results.")
    assert "web socket reconnect fixed." in joined.lower()
    assert "**" not in joined and "#" not in joined


def test_filler_opener_is_removed():
    assert stream("Certainly! I'd be happy to help with that. The answer is 4.")[0] == "The answer is 4."
    assert strip_filler("Absolutely! As an AI, I think so.") == "I think so."
    assert strip_filler("Sure enough, it works.") == "Sure enough, it works."


def test_spoken_length_is_capped():
    chunks = stream("This is a reasonably long sentence about something. " * 40, max_spoken_chars=300)
    assert chunks[-1] == "The rest is on screen."
    assert sum(len(c) for c in chunks[:-1]) <= 300


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The expected path is D:\\Projects\\App.", "The expected path is D colon, Projects, App."),
        ("Open main.py now.", "Open main dot py now."),
        ("Edit src/auth/middleware.ts please.", "Edit src slash auth slash middleware dot ts please."),
        ("Check the .env file.", "Check the dot env file."),
        ("See https://github.com/acme/app for details.", "See a link to github dot com for details."),
        ("Upgrade to v2.1.3 today.", "Upgrade to version 2 point 1 point 3 today."),
        ("Version v2.1.3 works.", "Version 2 point 1 point 3 works."),
        ("Pi is 3.14 roughly.", "Pi is 3.14 roughly."),
        ("Call get_user_token() and MAX_RETRIES.", "Call get user token and MAX RETRIES."),
        ("The API is 50% faster on GPU.", "The A P I is 50 percent faster on G P U."),
        ("Use client/server and/or local.", "Use client or server and or local."),
        ("This is **bold** and _italic_ text.", "This is bold and italic text."),
        ("Done ✅ 🎉", "Done"),
        ("a -> b", "a to b"),
        ("at 10:30 today", "at 10:30 today"),
    ],
)
def test_normalize_for_speech(text, expected):
    assert normalize_for_speech(text) == expected


def test_long_windows_paths_are_shortened():
    spoken = normalize_for_speech("D:\\College\\SOMESHIT DOWNLOADS\\SUGAR-AI\\sugar\\audio\\stt.py")
    assert spoken == "D colon, College, sugar, audio, stt dot py"


def test_speakable_summary_skips_code_and_limits_length():
    summary = speakable_summary("## Summary\nI fixed three bugs.\n```js\nx()\n```\n- One\n- Two\n- Three\n\nAll tests pass.")
    assert summary.startswith("Summary. I fixed three bugs.")
    assert "x()" not in summary
