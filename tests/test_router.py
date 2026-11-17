"""Fast-path grammar and routing — including the legacy false positives."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sugar.intelligence.fastpath import FastPath, normalize
from sugar.intelligence.router import Route, Router
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
    assert r.route("show my projects").route == Route.COMMAND
    assert r.route("What is Claude doing?").route == Route.COMMAND
    assert r.route("How do you make butter chicken?").route == Route.ANSWER
    assert r.route("Find the files in my downloads folder and rename them").route == Route.DESKTOP
    assert r.route("Compare REST and GraphQL in detail with pros and cons").route == Route.CODING_QA
    assert r.route("Compare REST and GraphQL in detail with pros and cons").purpose == "reasoning"


@pytest.mark.parametrize("text", ["open chrome", "Play Master of Puppets by Metallica.", "Volume 50", "What time is it?",
                                  "What's the weather like today?", "Take a screenshot.", "Open YouTube.",
                                  "Type this: hello world", "What's 25 times 4?", "Remember that I prefer tea."])
def test_desktop_commands_are_rejected_when_desktop_control_is_off(settings, text):
    settings.conversation.desktop_control = False
    settings.conversation.scope = "engineering_only"
    assert router(settings).route(text).route == Route.REJECTED


@pytest.mark.parametrize("text", ["open chrome", "Play Master of Puppets by Metallica.", "Volume 50", "What time is it?",
                                  "Take a screenshot.", "Open YouTube.", "Type this: hello world"])
def test_desktop_commands_run_directly_when_desktop_control_is_on(settings, text):
    decision = router(settings).route(text)
    assert decision.route == Route.DESKTOP and decision.intent is not None


@pytest.mark.parametrize(("text", "platform", "query"), [
    ("type lofi beats in youtube", "youtube", "lofi beats"),
    ("search spotify for daft punk", "spotify", "daft punk"),
    ("Search YouTube for RTX 5050 benchmarks", "youtube", "rtx 5050 benchmarks"),
    ("spotify search interstellar soundtrack", "spotify", "interstellar soundtrack"),
    ("type blinding lights into spotify", "spotify", "blinding lights"),
])
def test_youtube_and_spotify_searches(settings, text, platform, query):
    decision = router(settings).route(text)
    assert decision.route == Route.DESKTOP
    assert decision.intent.call == ("media.search", {"platform": platform, "query": query})


def test_free_form_computer_operations_go_to_the_agent_with_computer_tools(settings):
    for text in ("open a new notepad and start writing a fibonacci function", "close the tab with the error",
                 "move the files in my downloads folder into a new folder", "turn on bluetooth"):
        decision = router(settings).route(text)
        assert decision.route == Route.DESKTOP and decision.intent is None and "computer" in decision.tool_groups, text


def test_universal_requests_get_a_model_without_claude_code_or_desktop_tools(settings):
    r = router(settings)
    expected = {"Tell me a joke about Java": "creative_writing", "How do you make butter chicken?": "general_knowledge",
                "Recommend a movie": "general_knowledge", "Explain how black holes work.": "general_knowledge",
                "Write a dark fantasy poem.": "creative_writing", "hello": "general_conversation",
                "Explain what haemoglobin measures in a blood test.": "health_information",
                "Let's roleplay a conversation between an astronaut and an alien.": "roleplay",
                "Solve x^2 - 5x + 6 = 0": "math_reasoning", "Teach me how photosynthesis works": "education"}
    for text, category in expected.items():
        decision = r.route(text)
        assert decision.route == Route.ANSWER and decision.category == category, (text, decision.category)
        assert "terminal.run" not in (decision.tool_names or set()), text


def test_research_gets_only_read_only_web_tools(settings):
    decision = router(settings).route("Research the latest developments in solid state batteries")
    assert decision.route == Route.ANSWER and decision.tool_names == {"web.lookup", "web.fetch"}


def test_personal_health_data_stays_local_unless_authorised(settings):
    r = router(settings)
    decision = r.route("My haemoglobin is 9.8 g/dL, is that bad?")
    assert decision.local_only
    r.health_authorized = True
    assert not r.route("My haemoglobin is 9.8 g/dL, is that bad?").local_only
    assert not r.route("Explain what haemoglobin measures in a blood test.").local_only


@pytest.mark.parametrize(("text", "tool", "args"), [
    ("switch to your female voice", "voice.set", {"voice": "female"}),
    ("change your voice back", "voice.set", {"voice": "sid", "reset": True}),
    ("switch back to your original voice", "voice.set", {"voice": "sid"}),
    ("speak more slowly", "voice.adjust", {"setting": "speed", "delta": -0.08}),
    ("make your voice deeper", "voice.adjust", {"setting": "pitch", "delta": -1.5}),
    ("use a softer voice", "voice.adjust", {"setting": "volume", "delta": -0.2}),
    ("be more professional", "personality.set", {"personality": "professional"}),
    ("switch to your cinematic personality", "personality.set", {"personality": "cinematic"}),
    ("end the roleplay", "roleplay.end", {}),
    ("allow online health answers", "health.authorize", {"allow": True, "persist": False}),
])
def test_self_commands(settings, text, tool, args):
    decision = router(settings).route(text)
    assert decision.route == Route.COMMAND and decision.intent.call == (tool, args)


def test_programming_questions_get_no_tools(settings):
    decision = router(settings).route("Why does this Python function return None?")
    assert decision.route == Route.CODING_QA and not decision.tool_groups and not decision.tool_names


def test_engineering_memory_is_allowed(settings):
    assert router(settings).route("Remember that this project uses pnpm.").route == Route.COMMAND


def test_execution_routes(settings):
    r = router(settings)
    assert r.route("Fix the authentication bug").route == Route.BUILD_EXECUTION
    assert r.route("Create a React application with authentication.").route == Route.BUILD_EXECUTION
    assert r.route("Analyze the architecture of this project").route == Route.PROJECT_ANALYSIS
    assert r.route("The build failed.").route == Route.PROJECT_ANALYSIS
    assert r.route("What's the capital of France?").route == Route.ANSWER


def test_named_project_is_attached_to_the_decision(settings):
    decision = router(settings).route("Fix the login bug in Jiva")
    assert decision.route == Route.BUILD_EXECUTION and decision.project.name == "Jiva"


def test_follow_ups_need_engineering_context(settings):
    assert router(settings).route("Do it.").route == Route.CLARIFY
    working = WorkingMemory()
    working.set_project("Jiva", "D:/Jiva")
    working.record_action("coding", "asked Claude Code to analyze")
    r = router(settings, working)
    assert r.route("Fix all three.").route == Route.BUILD_EXECUTION
    assert r.route("Do it.").route == Route.BUILD_EXECUTION


def test_status_questions_go_to_the_workbench(settings):
    r = router(settings)
    for text in ("Are you done?", "status", "what's the status"):
        decision = r.route(text)
        assert decision.route == Route.COMMAND and decision.intent.name == "claude.status", text


@pytest.mark.parametrize("text", ["check how much free space is left on my D drive", "what's my battery level",
                                  "list the processes using the most memory", "kill discord"])
def test_system_questions_go_to_the_agent(settings, text):
    assert router(settings).route(text).route == Route.DESKTOP, text


def test_named_project_without_a_change_asks_what_to_change(settings):
    working = WorkingMemory()
    working.set_project("SUGAR-AI", "D:/sugar")  # an unrelated active repository
    decision = router(settings, working).route("Modify my portfolio.")
    assert decision.route == Route.CLARIFY and decision.project.name == "yukith-portfolio"
    specific = router(settings, working).route("Fix the contact form in my portfolio")
    assert specific.route == Route.BUILD_EXECUTION and specific.project.name == "yukith-portfolio"
