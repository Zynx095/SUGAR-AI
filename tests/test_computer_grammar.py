"""Deterministic computer-control grammar, routing of computer requests, and compound commands end to end."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from conftest import run
from fake_desktop import FakeDesktop, FakeWindow, browser_window

from sugar.intelligence.fastpath import DesktopView, FastPath
from sugar.intelligence.router import COMPUTER_GROUPS, Route, Router
from sugar.intelligence.working import WorkingMemory


@dataclass
class App:
    name: str


APPS = {"notepad": "Notepad", "chrome": "Brave", "brave": "Brave", "brave browser": "Brave", "spotify": "Spotify",
        "vs code": "VS Code", "discord": "Discord", "browser": "Brave"}


def grammar(view: DesktopView | None = None) -> FastPath:
    view = view or DesktopView(active_app="notepad", titles=("untitled - notepad", "lofi girl - youtube - brave"))
    return FastPath(resolve_app=lambda n: App(APPS[n.strip()]) if n.strip() in APPS else None,
                    desktop=lambda: view)


BROWSER_VIEW = DesktopView(active_app="brave", active_is_browser=True, has_search=True,
                           titles=("blinding lights - youtube - brave", "untitled - notepad"))


def call(text: str, view: DesktopView | None = None):
    intent = grammar(view).match(text)
    if intent is None:
        return None
    if intent.name == "sequence":
        return [step.call or (step.name, step.slots.get("name")) for step in intent.slots["steps"]]
    return intent.call or (intent.name, intent.slots.get("name"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Type: Hello, this is Sugar AI.", ("keyboard.type", {"text": "Hello, this is Sugar AI."})),
        ("Type hello.", ("keyboard.type", {"text": "hello"})),
        ("type hello world into notepad", ("keyboard.type", {"text": "hello world", "target": "notepad"})),
        ("In Notepad, type buy milk", ("keyboard.type", {"text": "buy milk", "target": "notepad"})),
        ("type I live in India", ("keyboard.type", {"text": "I live in India"})),
        ("Type this Python code.", ("keyboard.type", {"source": "last_code"})),
        ("type it", ("keyboard.type", {"source": "last_reply"})),
        ("type slowly hello there", ("keyboard.type", {"text": "hello there", "mode": "realtime"})),
        ("press enter", ("keyboard.press", {"keys": "enter", "times": 1})),
        ("hit tab twice", ("keyboard.press", {"keys": "tab", "times": 2})),
        ("press control shift t", ("keyboard.press", {"keys": "control shift t", "times": 1})),
        ("save the file", ("keyboard.save", {})),
        ("delete the last line", ("keyboard.edit", {"operation": "delete_last_line"})),
        ("clear everything", ("keyboard.edit", {"operation": "clear_all"})),
        ("select all", ("keyboard.press", {"keys": "ctrl+a", "times": 1})),
        ("undo that", ("keyboard.press", {"keys": "ctrl+z"})),
        ("Close Notepad.", ("app.close", {"name": "notepad", "scope": "auto"})),
        ("Close this.", ("app.close", {"name": "this", "scope": "auto"})),
        ("close this window", ("app.close", {"name": "this", "scope": "window"})),
        ("close the app I just opened", ("app.close", {"name": "last_opened", "scope": "window"})),
        ("force close discord", ("app.force_close", {"name": "discord"})),
        ("Minimize Chrome.", ("window.minimize", {"target": "chrome"})),
        ("maximize this", ("window.maximize", {"target": "this"})),
        ("bring chrome back", ("window.focus", {"target": "chrome"})),
        ("Switch to Notepad.", ("window.focus", {"target": "notepad"})),
        ("switch to youtube", ("window.focus", {"target": "youtube"})),  # a window title
        ("switch back", ("window.focus", {"target": "previous"})),
        ("snap notepad to the left", ("window.snap", {"target": "notepad", "where": "left"})),
        ("minimize everything", ("keyboard.press", {"keys": "win+d"})),
        ("what's open", ("window.list", {})),
        ("Open a new tab.", ("browser.new_tab", {})),
        ("Close this tab.", ("browser.close_tab", {})),
        ("Close the YouTube tab.", ("browser.close_tab", {"match": "youtube"})),
        ("close all the tabs", ("browser.close_window", {})),
        ("reopen the last tab", ("browser.reopen_tab", {})),
        ("switch to the previous tab", ("browser.switch_tab", {"direction": "previous"})),
        ("go to the third tab", ("browser.switch_tab", {"index": 3})),
        ("go to the github tab", ("browser.switch_tab", {"match": "github"})),
        ("Go back.", ("browser.back", {})),
        ("Go forward.", ("browser.forward", {})),
        ("Reload the page.", ("browser.reload", {})),
        ("scroll down", ("browser.scroll", {"direction": "down", "amount": 1})),
        ("Search YouTube for Blinding Lights by The Weeknd.",
         ("browser.search", {"query": "blinding lights by the weeknd", "engine": "youtube"})),
        ("Search Google for PyTorch CUDA 12.8.", ("browser.search", {"query": "pytorch cuda 12.8", "engine": "google"})),
        ("Search GitHub for FastAPI WebSockets.", ("browser.search", {"query": "fastapi websockets", "engine": "github"})),
        ("In YouTube search for ocean eyes", ("browser.search", {"query": "ocean eyes", "engine": "youtube"})),
        ("Open YouTube on Brave.", ("browser.open_url", {"url": "https://www.youtube.com", "new_tab": True,
                                                         "browser": "brave"})),
        ("Play the official music video for Starboy by The Weeknd on YouTube.",
         ("media.play", {"query": "Play the official music video for Starboy by The Weeknd on YouTube.",
                         "platform": "youtube"})),
        ("Play the latest MrBeast video.", ("media.play", {"query": "Play the latest MrBeast video.",
                                                           "platform": "youtube"})),
        ("Play Starboy.", ("media.play", {"query": "Play Starboy.", "platform": "auto"})),
        ("play the video in my clipboard", ("media.play_from", {"source": "clipboard"})),
        ("skip the ad", ("ui.click", {"name": "Skip"})),
        ("click subscribe", ("ui.click", {"name": "subscribe"})),
    ],
)
def test_computer_commands(text, expected):
    assert call(text) == expected


def test_browser_context_commands():
    assert call("Open the most relevant result.", BROWSER_VIEW) == ("browser.open_result", {"rank": "best"})
    assert call("play the second video", BROWSER_VIEW) == ("browser.open_result", {"rank": "second"})
    assert call("go to the bottom of the page", BROWSER_VIEW) == ("browser.scroll", {"direction": "bottom", "amount": 1})
    assert call("full screen", BROWSER_VIEW) == ("keyboard.press", {"keys": "f11", "times": 1})
    assert call("Open the most relevant result.") is None  # no search to pick from


def test_compound_commands():
    assert call("Open Notepad and type hello this is Sugar") == [
        ("app.open", "notepad"), ("keyboard.type", {"text": "hello this is Sugar"})]
    assert call("Press enter and type: This is a hands-free computer.") == [
        ("keyboard.press", {"keys": "enter", "times": 1}), ("keyboard.type", {"text": "This is a hands-free computer."})]
    assert call("type hello and press enter") == [
        ("keyboard.type", {"text": "hello"}), ("keyboard.press", {"keys": "enter", "times": 1})]
    assert call("Now close all the tabs and close Brave") == [
        ("browser.close_window", {}), ("app.close", {"name": "brave", "scope": "auto"})]
    assert call("Stop playing music on Spotify and close Spotify.") == [
        ("media.pause", {}), ("app.close", {"name": "spotify", "scope": "auto"})]
    assert call("open chrome, open a new tab and search youtube for lofi") == [
        ("app.open", "chrome"), ("browser.new_tab", {}), ("browser.search", {"query": "lofi", "engine": "youtube"})]


@pytest.mark.parametrize("text", [
    "write directly into the notebook.",  # an instruction, not dictation (from the V2 log)
    "Open a new notepad and start writing a Fibonacci code using recursive function.",  # needs a model to write code
    "Write me a function that reverses a list.",
    "type a poem about the sea",
    "close the deal with John",
    "play rock and roll",  # not split into "play rock" + "roll"… and a valid song query
])
def test_left_to_the_model_or_kept_whole(text):
    result = call(text)
    if text == "play rock and roll":
        assert result == ("media.play", {"query": "play rock and roll", "platform": "auto"})
    else:
        assert result is None


def test_dialog_answers_only_when_a_dialog_is_open():
    dialog = DesktopView(active_app="notepad", has_dialog=True, dialog_kind="save")
    assert call("don't save", dialog) == ("dialog.answer", {"choice": "discard"})
    assert call("save it", dialog) == ("dialog.answer", {"choice": "save"})
    assert call("yes", dialog) == ("dialog.answer", {"choice": "save"})
    assert call("no", dialog) == ("dialog.answer", {"choice": "cancel"})  # never discards on a bare "no"
    assert call("don't save") is None


def test_undo_goes_to_coding_after_coding_work():
    coding = DesktopView(active_app="code", last_domain="coding")
    assert call("undo that", coding) is None


# ---------------------------------------------------------------------------------------- routing

def test_computer_requests_route_to_the_agent_with_computer_tools(settings):
    router = Router(settings, grammar(), WorkingMemory())
    for text in ("Open a new notepad and start writing a Fibonacci code using recursive function.",
                 "write directly into the notepad", "close the tab with the error", "summarize this page",
                 "play a song by a Hindi artist"):
        decision = router.route(text)
        assert decision.route == Route.AGENT, text
        assert decision.tool_groups == COMPUTER_GROUPS
    assert router.route("What's your favourite colour?").route == Route.CHAT


# ---------------------------------------------------------------------------------------- end to end

def test_open_notepad_and_type_through_the_whole_app(settings, monkeypatch):
    """Voice text → fast path sequence → executor → computer control → the fake desktop."""
    import sugar.computer.engine as engine
    from sugar.app.application import SugarApp

    desktop = FakeDesktop()
    desktop.add(browser_window())
    desktop.launchers["shell:AppsFolder\\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"] = (
        lambda: FakeWindow("Untitled - Notepad", "notepad.exe", 300, class_name="Notepad"))
    monkeypatch.setattr(engine, "create_backend", lambda kind="auto": desktop)
    settings.ui.enabled = False
    settings.computer.typing_interval_ms = 0
    settings.computer.key_gap_ms = 0

    async def scenario():
        app = SugarApp(settings, voice=False)
        app.audio_output = False
        app.apps.loaded_at = 9e12
        from sugar.computer.apps import AppEntry

        app.apps.add(AppEntry("Notepad", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "aumid", ["notepad.exe"]))
        await app.start()
        replies = []
        done = asyncio.Event()

        def on_message(event):
            replies.append(event.data)
            done.set()

        app.bus.subscribe("assistant.message", on_message)
        texts = {}
        for utterance in ("Open Notepad and type: Hello, this is Sugar AI.", "close this", "don't save"):
            done.clear()
            await app.conversation.handle_text(utterance)
            await asyncio.wait_for(done.wait(), 15)
            notepads = [w for w in desktop.windows.values() if w.process == "notepad.exe"]
            texts[utterance] = notepads[0].text if notepads else None
        await app.shutdown()
        return replies, texts

    replies, texts = run(scenario())
    assert replies[0]["route"] == "command" and "Notepad's open." in replies[0]["text"]
    assert texts["Open Notepad and type: Hello, this is Sugar AI."] == "Hello, this is Sugar AI."
    assert "unsaved changes" in replies[1]["text"]  # "close this" = Notepad, which asks first
    assert texts["close this"] == "Hello, this is Sugar AI."
    assert texts["don't save"] is None and "Closed Notepad" in replies[2]["text"]
