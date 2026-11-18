"""Dictation and writing: layout words become keys (never text), edits work on what SID typed, typing follows the
target window only, generated text streams in as it's written, and "stop dictation" ends it."""

from __future__ import annotations

import asyncio

import pytest
from conftest import run
from fake_desktop import FakeDesktop, FakeWindow, browser_window
from test_universal import Recording

from sugar.computer.dictation import DictationTarget, DictationTyper, FocusLost, parse
from sugar.computer.keyboard import KeyboardController, TypingSettings
from sugar.providers.base import StreamChunk

# ---------------------------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(("utterance", "expected"), [
    ("Go to the next line.", [("keys", "enter")]),
    ("next line", [("keys", "enter")]),
    ("Start a new paragraph.", [("keys", "enter"), ("keys", "enter")]),
    ("Insert a blank line.", [("keys", "enter"), ("keys", "enter")]),
    ("Dear John. Go to the next line. How are you?",
     [("text", "Dear John."), ("keys", "enter"), ("text", "How are you?")]),
    ("I like apples comma pears and plums period", [("text", "I like apples, pears and plums.")]),
])
def test_layout_words_become_keys_not_text(utterance, expected):
    ops = parse(utterance)
    assert [(o.kind, o.value) for o in ops] == expected
    assert not any("next line" in o.value.lower() for o in ops if o.kind == "text")


@pytest.mark.parametrize(("utterance", "command"), [
    ("Delete the last sentence.", "delete_sentence"), ("Undo that.", "undo"), ("Redo.", "redo"),
    ("Select the previous paragraph.", "select_previous_paragraph"), ("Move the cursor to the beginning.", "go_start"),
    ("Go to the end.", "go_end"), ("Replace cat with dog.", "replace"), ("Correct the spelling.", "spelling"),
    ("Select everything.", "select_all"), ("Clear this document.", "clear"), ("Make this a heading.", "heading"),
    ("Add a numbered list.", "numbered"), ("Start a new bullet.", "bullet"), ("Indent this paragraph.", "indent"),
    ("Continue.", "continue"), ("Keep writing.", "continue"), ("Stop dictation.", "stop"), ("Scratch that", "scratch"),
])
def test_editing_phrases_are_commands(utterance, command):
    ops = parse(utterance)
    assert len(ops) == 1 and ops[0].kind == "command" and ops[0].value == command


def test_write_this_exactly_is_literal():
    ops = parse("Write this exactly: next line is not a command.")
    assert ops[0].value == "exact" and ops[0].args["text"] == "next line is not a command."


def test_writing_mode_only_takes_commands():
    assert parse("what time is it", dictating=False) == []
    assert [o.value for o in parse("new paragraph", dictating=False)] == ["enter", "enter"]


# ---------------------------------------------------------------------------------------------- typing


def editor_setup():
    desktop = FakeDesktop()
    notepad = desktop.add(FakeWindow("Untitled - Notepad", "notepad.exe", 300, class_name="Notepad"))
    from sugar.computer.context import DesktopContext
    from sugar.computer.windows import WindowManager

    context = DesktopContext(desktop)
    context.refresh()
    keyboard = KeyboardController(desktop, WindowManager(desktop, context), context,
                                  TypingSettings(char_interval_s=0, key_gap_s=0, verify=False), __import__(
                                      "threading").Event(), sleep=lambda s: None)
    typer = DictationTyper(desktop, keyboard, sleep=lambda s: None, interval_s=0)
    target = DictationTarget(notepad.info())
    return desktop, notepad, typer, target


def test_next_line_presses_enter_in_the_document():
    desktop, notepad, typer, target = editor_setup()
    for op in parse("Dear John. Go to the next line. How are you?"):
        typer.type(target, op.value) if op.kind == "text" else typer.keys(target, op.value)
    assert notepad.text == "Dear John.\nHow are you?"


def test_consecutive_utterances_join_with_spacing_and_capitals():
    desktop, notepad, typer, target = editor_setup()
    typer.type(target, "this is the first sentence.")
    typer.type(target, "and this is the second.")
    typer.keys(target, "enter")
    typer.keys(target, "enter")
    typer.type(target, "new paragraph here")
    assert notepad.text == "This is the first sentence. And this is the second.\n\nNew paragraph here"


def test_delete_last_sentence_and_scratch_only_touch_what_sid_typed():
    desktop, notepad, typer, target = editor_setup()
    typer.type(target, "One sentence.")
    typer.type(target, "Two sentences.")
    assert typer.delete_last_sentence(target) == "Two sentences."
    assert notepad.text == "One sentence."
    typer.type(target, "Second try.")
    typer.scratch(target)
    assert notepad.text == "One sentence."


def test_typing_stops_when_focus_moves_to_another_app():
    desktop, notepad, typer, target = editor_setup()
    other = desktop.add(browser_window())  # the user switched to the browser
    with pytest.raises(FocusLost):
        typer.type(target, "secret words")
    assert notepad.text == "" and "secret" not in str(other)


# ---------------------------------------------------------------------------------------------- in the app


class SlowWriter(Recording):
    """Streams a paragraph in pieces, recording how much had been typed when each piece was produced."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.seen: list[int] = []

    async def stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        yield StreamChunk("meta", data={"model": "writer"})
        for piece in ["Artificial intelligence ", "is the study of ", "machines that learn. ", "It is ",
                      "changing how we work.\n\n", "Its future ", "is open."]:
            self.seen.append(len(self.window.text))
            await asyncio.sleep(0.01)
            yield StreamChunk("text", text=piece)


def app_with_editor(settings, monkeypatch, provider_factory=None):
    import sugar.computer.engine as engine
    from sugar.app.application import SIDApp

    desktop = FakeDesktop()
    notepad = desktop.add(FakeWindow("Untitled - Notepad", "notepad.exe", 300, class_name="Notepad"))
    monkeypatch.setattr(engine, "create_backend", lambda kind="auto": desktop)
    settings.ui.enabled = False
    settings.computer.backend = "auto"
    settings.computer.typing_interval_ms = 0
    settings.computer.key_gap_ms = 0
    settings.computer.verify_typing = False
    app = SIDApp(settings, voice=False)
    app.audio_output = False
    app.apps.loaded_at = 9e12
    provider = provider_factory(notepad) if provider_factory else Recording()
    app.pool.providers.clear()
    app.pool.providers["freellm"] = provider
    return app, desktop, notepad, provider


async def say(app, text, wait=3.0):
    done = asyncio.Event()
    unsubscribe = app.bus.subscribe("assistant.message", lambda e: done.set())
    await app.conversation.handle_text(text)
    try:
        await asyncio.wait_for(done.wait(), wait)
    except TimeoutError:
        pass
    finally:
        unsubscribe()
    await asyncio.sleep(0.05)


def test_dictation_mode_types_speech_and_takes_edits(settings, monkeypatch):
    async def scenario():
        app, desktop, notepad, provider = app_with_editor(settings, monkeypatch)
        await app.start()
        states = []
        app.bus.subscribe("dictation.state", lambda e: states.append(e.data))
        await say(app, "start dictation")
        await say(app, "Dear John. Go to the next line.")
        await say(app, "Thanks for the report. It was great.")
        await say(app, "Delete the last sentence.")
        await say(app, "new paragraph")
        await say(app, "What is the capital of France?")  # content while dictating, not a question to SID
        await say(app, "stop dictation")
        await say(app, "What is the capital of France?")  # a normal question again
        await app.shutdown()
        return notepad, provider, states

    notepad, provider, states = run(scenario())
    assert notepad.text == "Dear John.\nThanks for the report.\n\nWhat is the capital of France?"
    assert states[0]["active"] and states[0]["app"] and states[-1]["active"] is False
    assert len(provider.calls) == 1  # only the question after "stop dictation" reached a model


def test_write_a_paragraph_streams_into_the_editor(settings, monkeypatch):
    async def scenario():
        app, desktop, notepad, provider = app_with_editor(settings, monkeypatch, SlowWriter)
        await app.start()
        await say(app, "write a paragraph about artificial intelligence", wait=5)
        await say(app, "go to the next line")
        await app.shutdown()
        return notepad, provider

    notepad, provider = run(scenario())
    assert notepad.text.startswith("Artificial intelligence is the study of machines that learn. It is changing")
    assert "\n\nIts future is open.\n" == notepad.text[notepad.text.index("\n\n"):]
    assert provider.seen[3] > 0  # text was already in the document while the model was still writing
