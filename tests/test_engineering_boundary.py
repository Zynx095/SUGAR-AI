"""SID's engineering boundary: classification, routing and Claude Code isolation, end to end.

Claude Code itself is replaced by a recorder here (``sessions.start_task``); the real CLI
is exercised in ``test_claude_session.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import run

from sugar.app.application import SIDApp
from sugar.coding.sessions import CodingSession
from sugar.intelligence.domain import REFUSAL, EngineeringIntent, RequestContext, classify, redact_secrets
from sugar.providers.base import LLMProvider, ProviderHealth, StreamChunk, ToolCall

QA, ANALYSIS, BUILD, NON = (EngineeringIntent.CODING_QA, EngineeringIntent.PROJECT_ANALYSIS,
                            EngineeringIntent.BUILD_EXECUTION, EngineeringIntent.NON_ENGINEERING)


# ----------------------------------------------------------------------------------------- classifier

@pytest.mark.parametrize(("text", "expected"), [
    # the brief's own examples
    ("What is Python?", QA),
    ("What is the weather in Python?", NON),
    ("Create a Python application", BUILD),
    ("Tell me a joke about Java", NON),
    ("Create a React application with authentication.", BUILD),
    ("Why does this Python function return None?", QA),
    # programming questions
    ("What is a closure in JavaScript?", QA),
    ("Explain recursion", QA),
    ("what is the difference between let and const", QA),
    ("What time complexity does quicksort have?", QA),
    ("How do I get weather data in Python?", QA),
    ("Write a function that reverses a list.", QA),
    ("Compare REST and GraphQL in detail", QA),
    ("Explain the architecture of microservices", QA),
    ("Traceback (most recent call last):\n  File \"app.py\", line 3\nKeyError: 'id'", QA),
    # repository analysis
    ("Analyse the architecture of this project", ANALYSIS),
    ("Review my code", ANALYSIS),
    ("check the dependencies of this project", ANALYSIS),
    ("The build failed.", ANALYSIS),
    ("The tests are failing", ANALYSIS),
    # build / execution
    ("Build a weather app in React", BUILD),
    ("Fix the authentication bug", BUILD),
    ("Run the tests", BUILD),
    ("Refactor utils.py to use dataclasses", BUILD),
    ("Upgrade the dependencies", BUILD),
    ("I want to build a todo app with Next.js", BUILD),
    ("Delete the node_modules folder", BUILD),
    # not engineering
    ("What's the capital of France?", NON),
    ("How do you make butter chicken?", NON),
    ("Play Master of Puppets by Metallica", NON),
    ("Recommend a movie", NON),
    ("remind me to call mom at 5", NON),
    ("Write a poem about Python", NON),
    ("open chrome", NON),
    ("What's 25 times 4?", NON),
    ("Find the files in my downloads folder and rename them", NON),
    ("summarize this page", NON),
    ("how are you", NON),
    ("Tell me about the history of Rome", NON),
    ("Are the Lakers playing tonight?", NON),
])
def test_classification(text, expected):
    assert classify(text).intent == expected, classify(text)


def test_follow_ups_are_ambiguous_without_engineering_context():
    assert classify("Fix it").ambiguous
    assert classify("Do it").ambiguous
    in_context = classify("Fix it", RequestContext(has_project=True, recent_coding=True))
    assert in_context.intent == BUILD and not in_context.ambiguous


def test_courtesy_is_marked():
    assert classify("thanks").courtesy and classify("hello").courtesy
    assert not classify("tell me a joke").courtesy


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
])
def test_secrets_are_redacted(secret):
    assert secret not in redact_secrets(f"the key is {secret} ok")


def test_env_assignments_are_redacted():
    text = "ANTHROPIC_API_KEY=abc123def456\nDEBUG=true\nexport GITHUB_TOKEN='tok_9876543210'"
    redacted = redact_secrets(text)
    assert "abc123def456" not in redacted and "tok_9876543210" not in redacted and "DEBUG=true" in redacted


# ----------------------------------------------------------------------------------------- app fixtures

class RecordingProvider(LLMProvider):
    name = "freellm"
    supports_tools = True

    def __init__(self, turns=None):
        self.turns = list(turns or [])
        self.calls: list[dict] = []

    async def stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        turn = self.turns.pop(0) if self.turns else {"text": ["(answer)"]}
        yield StreamChunk("meta", data={"model": "recording"})
        for piece in turn.get("text", []):
            yield StreamChunk("text", text=piece)
        if turn.get("calls"):
            yield StreamChunk("tool_calls", tool_calls=turn["calls"])

    async def health_check(self):
        return ProviderHealth(True)


def make_app(settings, tmp_path, turns=None):
    settings.ui.enabled = False
    settings.permissions.allowed_roots = [tmp_path]
    app = SIDApp(settings, voice=False)
    app.audio_output = False
    app.apps.loaded_at = 9e12
    provider = RecordingProvider(turns)
    app.pool.providers.clear()
    app.pool.providers["freellm"] = provider
    settings.llm.chat_chain = ["freellm"]
    settings.llm.reasoning_chain = ["freellm"]
    started: list[dict] = []

    async def start_task(project, task, *, mode=None, resume_note=None, destructive_confirmed=False):
        started.append({"name": project.name, "path": project.path, "task": task, "mode": mode,
                        "destructive": destructive_confirmed})
        return CodingSession(project.name, project.path, status="running", task=task, mode=mode or "work")

    app.sessions.start_task = start_task
    tools: list[str] = []
    app.bus.subscribe("tool.start", lambda e: tools.append(e.data.get("name")))
    return app, provider, started, tools


async def say(app, text, wait_s=5.0):
    done = asyncio.Event()
    replies = []

    def on_message(event):
        replies.append(event.data)
        done.set()

    unsubscribe = app.bus.subscribe("assistant.message", on_message)
    await app.conversation.handle_text(text)
    await asyncio.wait_for(done.wait(), wait_s)
    unsubscribe()
    return replies[0]


def project_dir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    (path / ".git").mkdir()
    return path


# ----------------------------------------------------------------------------------------- boundary

NON_ENGINEERING = ["What's the weather in Python?", "Tell me a joke about Java", "Play some music",
                   "Search the web for cheap flights", "Remind me to call mom", "open chrome", "volume 50",
                   "What time is it?", "Who won the match yesterday?", "Recommend a movie", "write a poem",
                   "take a screenshot", "How are you?"]


def test_non_engineering_requests_reach_no_model_no_tool_and_no_claude_code(settings, tmp_path):
    async def scenario():
        app, provider, started, tools = make_app(settings, tmp_path)
        app.working.set_project("shop", str(project_dir(tmp_path, "shop")))  # even with a project active
        await app.start()
        replies = [await say(app, text) for text in NON_ENGINEERING]
        await app.shutdown()
        return replies, provider, started, tools

    replies, provider, started, tools = run(scenario())
    assert all(r["text"] == REFUSAL and r["route"] == "rejected" for r in replies)
    assert provider.calls == [] and started == [] and tools == []


def test_programming_question_reaches_the_model_without_tools(settings, tmp_path):
    async def scenario():
        app, provider, started, tools = make_app(settings, tmp_path, [
            {"text": ["Because it has no return statement."], "calls": [ToolCall("x", "claude__task", {"task": "x"})]},
            {"text": [" Add one."]},
        ])
        await app.start()
        reply = await say(app, "Why does this Python function return None?")
        await app.shutdown()
        return reply, provider, started, tools

    reply, provider, started, tools = run(scenario())
    assert reply["route"] == "coding_qa" and reply["text"].startswith("Because it has no return statement.")
    assert provider.calls and all(call.get("tools") is None for call in provider.calls)
    assert started == [] and tools == []  # an invented tool call is ignored, not executed


def test_build_request_runs_claude_code_in_the_active_project(settings, tmp_path):
    async def scenario():
        app, provider, started, _ = make_app(settings, tmp_path)
        shop = project_dir(tmp_path, "shop")
        app.working.set_project("shop", str(shop))
        await app.start()
        reply = await say(app, "Fix the authentication bug")
        await app.shutdown()
        return reply, started, shop

    reply, started, shop = run(scenario())
    assert reply["route"] == "build_execution"
    assert started == [{"name": "shop", "path": str(shop), "task": "Fix the authentication bug", "mode": "work",
                        "destructive": False}]
    assert "Engineering in shop" in reply["text"]


def test_analysis_runs_read_only(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        app.working.set_project("shop", str(project_dir(tmp_path, "shop")))
        await app.start()
        reply = await say(app, "Analyse the architecture of this project")
        await app.shutdown()
        return reply, started

    reply, started = run(scenario())
    assert reply["route"] == "project_analysis" and started[0]["mode"] == "analyze"


def test_no_project_means_a_question_then_the_answer_resumes_the_task(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        shop = project_dir(tmp_path, "shop")
        app.projects.add(shop)
        await app.start()
        first = await say(app, "Fix the authentication bug")
        before = list(started)
        second = await say(app, "shop")
        await app.shutdown()
        return first, before, second, started, shop

    first, before, second, started, shop = run(scenario())
    assert before == [] and "Which project" in first["text"]
    assert started and started[0]["path"] == str(shop) and started[0]["task"] == "Fix the authentication bug"


def test_named_project_wins_over_the_active_one(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        shop, blog = project_dir(tmp_path, "shop"), project_dir(tmp_path, "blog")
        app.projects.add(shop)
        app.projects.add(blog)
        app.working.set_project("shop", str(shop))
        events = []
        app.bus.subscribe("project.active", lambda e: events.append(e.data))
        await app.start()
        await say(app, "Fix the login bug in blog")
        active = dict(app.working.active_project)
        await app.shutdown()
        return started, active, events, blog

    started, active, events, blog = run(scenario())
    assert started[0]["path"] == str(blog)  # never the previously active "shop"
    assert active["path"] == str(blog) and events[-1]["path"] == str(blog)  # UI and working dir agree


def test_ambiguous_follow_up_executes_nothing(settings, tmp_path):
    async def scenario():
        app, provider, started, tools = make_app(settings, tmp_path)
        await app.start()
        reply = await say(app, "Do it.")
        await app.shutdown()
        return reply, provider, started, tools

    reply, provider, started, tools = run(scenario())
    assert reply["route"] == "clarify" and started == [] and provider.calls == []


def test_new_application_creates_the_typed_directory(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        await app.start()
        target = tmp_path / "storefront"
        reply = await say(app, f"Create a React application with authentication in {target}")
        await app.shutdown()
        return reply, started, target

    reply, started, target = run(scenario())
    assert target.is_dir() and started and started[0]["path"] == str(target)


def test_directories_outside_the_allowed_roots_are_refused(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path / "inside")
        await app.start()
        outside = tmp_path / "elsewhere"
        reply = await say(app, f"Create a Flask API in {outside}")
        await app.shutdown()
        return reply, started, outside

    reply, started, outside = run(scenario())
    assert started == [] and not outside.exists() and "outside the directories" in reply["text"]


def test_new_application_without_a_directory_asks_for_one(settings, tmp_path):
    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        await app.start()
        reply = await say(app, "Can you build me a website?")
        await app.shutdown()
        return reply, started

    reply, started = run(scenario())
    assert started == [] and "target directory" in reply["text"]


def test_previous_sessions_project_is_not_active_after_a_restart(settings, tmp_path):
    data = settings.paths.data_dir
    data.mkdir(parents=True, exist_ok=True)
    (data / "working.json").write_text(json.dumps({
        "active_project": {"name": "Q-Shield", "path": "D:/old/Q-Shield"},
        "current_task": "old task", "last_error": "old error"}), encoding="utf-8")

    async def scenario():
        app, _, started, _ = make_app(settings, tmp_path)
        await app.start()
        snapshot = app.snapshot()
        reply = await say(app, "Fix the authentication bug")
        await app.shutdown()
        return snapshot, reply, started

    snapshot, reply, started = run(scenario())
    assert snapshot["project"] is None
    assert snapshot["previous_project"]["name"] == "Q-Shield"
    assert started == [] and "Which project" in reply["text"]


def test_status_question_reports_only_real_work(settings, tmp_path):
    async def scenario():
        app, _, _, _ = make_app(settings, tmp_path)
        await app.start()
        reply = await say(app, "Are you done?")
        await app.shutdown()
        return reply

    assert run(scenario())["text"].startswith("No engineering task is active.")


@pytest.mark.parametrize(("text", "path"), [
    # regression: a live run created "sid-smoke-1. Keep it under 30 lines in total" before this was fixed
    (r"Create a Python app in D:\work\sid-smoke-1. Keep it under 30 lines in total and run the test.",
     r"D:\work\sid-smoke-1"),
    (r"Create a React app in D:\College\shop with auth", r"D:\College\shop"),
    (r"make a site in D:\College\My Site, then add a footer", r"D:\College\My Site"),
    (r"put it in D:\College\v1.2 and test it", r"D:\College\v1.2"),
    (r'scaffold it in "D:\College\a, b" now', r"D:\College\a, b"),
    ("fix the bug", None),
])
def test_typed_target_paths_stop_at_the_clause_boundary(text, path):
    from pathlib import Path

    from sugar.coding.projects import extract_target_path

    found = extract_target_path(text)
    assert (str(found) if found else None) == (str(Path(path)) if path else None)
