"""Fast-path grammar and routing — including the legacy false positives."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sugar.intelligence.fastpath import FastPath, normalize
from sugar.intelligence.router import CHAT_TOOLS, Route, Router
from sugar.intelligence.working import WorkingMemory
from sugar.tools.calculator import evaluate_spoken_math


@dataclass
class FakeApp:
    name: str


@dataclass
class FakeProject:
    name: str
    path: str = "D:/p"


APPS = {"chrome": "Google Chrome", "vs code": "Visual Studio Code", "spotify": "Spotify", "calculator": "Calculator",
        "terminal": "Terminal"}
PROJECTS = {"jiva": "Jiva", "q shield": "Q-Shield", "portfolio": "yukith-portfolio"}


def fastpath() -> FastPath:
    return FastPath(
        resolve_app=lambda n: FakeApp(APPS[n.strip()]) if n.strip() in APPS else None,
        resolve_project=lambda n: FakeProject(PROJECTS[n.strip()]) if n.strip() in PROJECTS else None,
        evaluate_math=evaluate_spoken_math,
    )


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Sugar? Stop.", None),  # wake word is stripped by the conversation layer, not here
        ("Stop.", "control.stop"),
        ("Wait, stop.", "control.wait"),
        ("never mind", "control.stop"),
        ("Go to sleep.", "control.sleep"),
        ("What time is it?", "system.time"),
        ("Can you tell me the time please?", "system.time"),
        ("What's the weather like today?", "weather.current"),
        ("Open Chrome.", "app.open"),
        ("Please open VS Code for me.", "app.open"),
        ("close spotify", "app.close"),
        ("Pause.", "media.pause"),
        ("pause the music", "media.pause"),
        ("Next song", "media.next"),
        ("What song is this?", "media.current"),
        ("Play Master of Puppets by Metallica.", "media.play"),
        ("play master of puppets", "media.play"),
        ("Volume 50", "volume.set"),
        ("set the volume to fifty percent", "volume.set"),
        ("turn it up", "volume.up"),
        ("mute", "volume.mute"),
        ("Take a screenshot.", "screen.capture"),
        ("Open my JIVA project.", "project.open"),
        ("Switch to Q shield.", "project.open"),
        ("work on the project in D colon College Projects JIVA", "project.open"),
        ("Show my projects.", "project.list"),
        ("What is Claude doing?", "claude.status"),
        ("Pause the coding session.", "claude.pause"),
        ("Resume Claude.", "claude.resume"),
        ("Continue from where we stopped.", "claude.resume"),
        ("Don't commit yet.", "coding.no_commit"),
        ("Show me what changed.", "git.diff"),
        ("Connect Claude Code to this project.", "claude.connect"),
        ("Search the web for the latest React documentation.", "web.search"),
        ("Open YouTube.", "browser.site"),
        ("Open the browser.", "browser.open"),
        ("Remember that I prefer Python.", "memory.remember"),
        ("Type this: hello world", "type.text"),
        ("What's 25 times 4?", "calc.evaluate"),
        ("what is fifteen percent of eighty", "calc.evaluate"),
    ],
)
def test_fast_path_intents(text, intent):
    match = fastpath().match(text)
    assert (match.name if match else None) == intent


@pytest.mark.parametrize(
    "text",
    [
        "Write me a function that reverses a list.",   # legacy: typed into the focused window
        "What time complexity does quicksort have?",   # legacy: answered with the clock
        "Pause the coding session and explain why.",   # not a bare media pause
        "Why is my application crashing?",
        "Play a game with me.",
        "Search my files for the invoice.",            # local search, not the web
        "Open the pod bay doors.",                     # unknown app → model decides
        "Explain the architecture of this project.",
        "prototype a login page",
    ],
)
def test_no_false_positive_commands(text):
    assert fastpath().match(text) is None


def test_type_text_preserves_original_casing():
    match = fastpath().match("type this: Hello World, it's ME")
    assert match.slots["text"] == "Hello World, it's ME"


def test_song_slot_and_project_slot():
    fp = fastpath()
    assert fp.match("Play Master of Puppets by Metallica").slots["query"] == "master of puppets by metallica"
    assert fp.match("open my portfolio").slots["project"].name == "yukith-portfolio"
    assert fp.match("open the zork project").slots == {"name": "zork", "unresolved": True}


def test_weather_location_slot_and_compound_question():
    fp = fastpath()
    assert fp.match("what's the weather in new york").slots == {"location": "new york"}
    assert fp.match("What's the weather like in Chennai right now, and should I carry an umbrella?") is None


def test_normalize_strips_politeness():
    assert normalize("Hey, could you please open Chrome for me?") == "open chrome"
    assert normalize("Okay so um just pause") == "pause"


@pytest.mark.parametrize(
    ("spoken", "value"),
    [("25 times 4", 100), ("what is fifteen percent of eighty", 12), ("square root of 144", 12),
     ("2 to the power of 10", 1024), ("three hundred and twenty five plus 5", 330), ("10 divided by 4", 2.5),
     ("1,000 minus 1", 999)],
)
def test_spoken_math(spoken, value):
    assert evaluate_spoken_math(spoken) == pytest.approx(value)


@pytest.mark.parametrize("spoken", ["hello there", "what is love", "import os", "__import__('os')", "2 ** 999999",
                                    "open 3 tabs"])
def test_spoken_math_rejects_non_arithmetic(spoken):
    assert evaluate_spoken_math(spoken) is None


def router(settings, working=None, coding=False) -> Router:
    return Router(settings, fastpath(), working or WorkingMemory(), coding_active=lambda: coding)


def test_routes(settings):
    r = router(settings)
    assert r.route("stop").route == Route.CONTROL
    assert r.route("open chrome").route == Route.COMMAND
    assert r.route("How do you make butter chicken?").route == Route.CHAT
    assert r.route("Find the files in my downloads folder and rename them").route == Route.AGENT
    assert r.route("Compare REST and GraphQL in detail with pros and cons").route == Route.REASONING


def test_chat_uses_a_small_toolset(settings):
    decision = router(settings).route("Tell me a joke")
    assert decision.tool_names == CHAT_TOOLS and len(CHAT_TOOLS) <= 15


def test_coding_requires_project_context(settings):
    assert router(settings).route("Fix the authentication bug").route != Route.CODING
    working = WorkingMemory()
    working.set_project("Jiva", "D:/Jiva")
    r = router(settings, working)
    assert r.route("Fix the authentication bug").route == Route.CODING
    assert r.route("Analyze the project and tell me what is broken").route == Route.CODING
    assert r.route("Make the UI cleaner").route == Route.CODING
    assert r.route("What's the capital of France?").route == Route.CHAT


def test_coding_follow_ups(settings):
    working = WorkingMemory()
    working.set_project("Jiva", "D:/Jiva")
    working.record_action("coding", "asked Claude Code to analyze")
    r = router(settings, working)
    assert r.route("Fix all three.").route == Route.CODING
    assert r.route("Do it.").route == Route.CODING
    assert r.route("Why did that test fail?").route == Route.CODING
