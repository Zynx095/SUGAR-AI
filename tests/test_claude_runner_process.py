"""Claude Code orchestration against a real child process (``tests/fake_claude.py``).

These run the actual subprocess, streaming, cancellation and timeout code paths;
only the CLI on the other end of the pipe is a stand-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import Recorder, run

from sugar.coding.claude_code import ClaudeCLIInfo, ClaudeCodeRunner, ClaudeRunRequest
from sugar.coding.projects import Project
from sugar.coding.sessions import DESTRUCTIVE_TOOLS, CodingSessionManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the fake CLI wrapper is a .cmd file")

FAKE = Path(__file__).with_name("fake_claude.py")
FLAGS = frozenset({"--resume", "--model", "--permission-mode", "--append-system-prompt", "--allowedTools",
                   "--disallowedTools", "--name"})


@pytest.fixture
def fake_cli(tmp_path, monkeypatch) -> tuple[ClaudeCLIInfo, Path]:
    wrapper = tmp_path / "claude.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    info = ClaudeCLIInfo(str(wrapper), "2.1.0", FLAGS, frozenset({"acceptEdits", "auto", "plan", "default"}))
    return info, log


def calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def manager(settings, bus, info, tmp_path) -> CodingSessionManager:
    sessions = CodingSessionManager(settings.coding, bus, tmp_path / "sessions.json")
    sessions._info = info
    return sessions


def project(tmp_path, name) -> Project:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return Project(name, str(path))


async def wait_for_status(session, statuses, limit_s=30.0):
    deadline = time.monotonic() + limit_s
    while session.status not in statuses:
        if time.monotonic() > deadline:
            raise TimeoutError(f"session stuck in {session.status}")
        await asyncio.sleep(0.05)


def pid_alive(pid: int) -> bool:
    import subprocess

    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def test_task_streams_progress_and_records_session_cost_usage_and_checks(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli
    recorder = Recorder(bus)
    announced = []

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)

        async def announce(text, kind):
            announced.append((text, kind))

        sessions.on_announce = announce
        shop = project(tmp_path, "shop")
        session = await sessions.start_task(shop, "add a health endpoint")
        await wait_for_status(session, {"completed", "failed"})
        return session, shop

    session, shop = run(scenario())
    assert session.status == "completed", session.last_error
    assert session.claude_session_id == "fake-session-1"
    assert session.last_cost_usd == pytest.approx(0.0123)
    assert session.last_usage == {"input_tokens": 1500, "output_tokens": 320, "cache_read_input_tokens": 9000}
    assert session.checks["tests"] | {"ts": 0} == {"status": "passed", "command": "Run tests", "ts": 0}
    assert session.last_duration_ms and session.last_duration_ms > 0
    record = session.history[-1]
    assert record["status"] == "completed" and record["project_path"] == shop.path
    progress = [e.data["text"] for e in recorder.of("coding.progress") if e.data["kind"] == "tool"]
    assert progress[0].startswith("Reading") and any(p.startswith("Running") for p in progress)
    assert [e.data["status"] for e in recorder.of("coding.check")] == ["running", "passed"]
    assert announced[-1] == ("The change is in place. The test suite ran.", "coding.finished")
    # the CLI really ran inside the project, with the prompt on stdin and no auto-commit/push/merge
    call = calls(log)[0]
    assert Path(call["cwd"]).resolve() == Path(shop.path).resolve() and call["prompt"] == "add a health endpoint"
    disallowed = call["args"][call["args"].index("--disallowedTools") + 1:]
    assert {"Bash(git commit *)", "Bash(git push *)", "Bash(git merge *)", "Bash(rm -rf *)"} <= set(disallowed)


def test_second_task_resumes_the_same_claude_session(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        shop = project(tmp_path, "shop")
        first = await sessions.start_task(shop, "first")
        await wait_for_status(first, {"completed", "failed"})
        second = await sessions.start_task(shop, "second")
        await wait_for_status(second, {"completed", "failed"})
        return second

    session = run(scenario())
    first_call, second_call = calls(log)
    assert "--resume" not in first_call["args"]
    assert second_call["args"][second_call["args"].index("--resume") + 1] == "fake-session-1"
    assert len(session.history) == 2


def test_each_project_runs_in_its_own_directory(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        a, b = project(tmp_path, "alpha"), project(tmp_path, "beta")
        for target in (a, b):
            session = await sessions.start_task(target, f"work on {target.name}")
            await wait_for_status(session, {"completed", "failed"})
        return a, b, sessions

    a, b, sessions = run(scenario())
    cwds = [Path(c["cwd"]).resolve() for c in calls(log)]
    assert cwds == [Path(a.path).resolve(), Path(b.path).resolve()]
    assert sessions.get(a.path) is not sessions.get(b.path)


def test_cancellation_kills_the_process(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        session = await sessions.start_task(project(tmp_path, "shop"), "SLEEP forever")
        for _ in range(200):
            if session.activity and any("Reading" in a for a in session.activity):
                break
            await asyncio.sleep(0.05)
        stopped = await sessions.stop(session)
        return session, stopped

    session, stopped = run(scenario())
    assert stopped and session.status == "cancelled"
    assert session.history[-1]["status"] == "cancelled"
    time.sleep(0.5)
    assert not pid_alive(calls(log)[0]["pid"])


def test_timeout_stops_a_hung_run(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli
    settings.coding.task_timeout_s = 2.0

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        session = await sessions.start_task(project(tmp_path, "shop"), "SLEEP forever")
        await wait_for_status(session, {"failed", "completed"}, limit_s=30)
        return session

    session = run(scenario())
    assert session.status == "failed" and "timed out" in session.last_error
    time.sleep(0.5)
    assert not pid_alive(calls(log)[0]["pid"])


def test_cli_failure_is_reported_with_its_error(settings, bus, fake_cli, tmp_path):
    info, _ = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        session = await sessions.start_task(project(tmp_path, "shop"), "CRASH now")
        await wait_for_status(session, {"failed", "completed"})
        return session

    session = run(scenario())
    assert session.status == "failed" and "simulated CLI failure" in session.last_error
    assert session.last_cost_usd is None and session.last_usage is None  # nothing invented


def test_failing_tests_are_reported_as_failed(settings, bus, fake_cli, tmp_path):
    info, _ = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        session = await sessions.start_task(project(tmp_path, "shop"), "TESTFAIL NOUSAGE")
        await wait_for_status(session, {"failed", "completed"})
        return session

    session = run(scenario())
    assert session.checks["tests"]["status"] == "failed"
    assert session.last_cost_usd is None and session.last_usage is None  # the CLI didn't report them


def test_startup_failure_is_an_error_not_a_crash(tmp_path):
    info = ClaudeCLIInfo(str(tmp_path / "missing-claude.exe"), "0", FLAGS, frozenset())

    async def scenario():
        return await ClaudeCodeRunner(info).run(ClaudeRunRequest(prompt="x", cwd=tmp_path))

    result = run(scenario())
    assert not result.ok and "could not start Claude Code" in result.error


def test_confirmed_destructive_task_lifts_only_the_deletion_bans(settings, bus, fake_cli, tmp_path):
    info, log = fake_cli

    async def scenario():
        sessions = manager(settings, bus, info, tmp_path)
        session = await sessions.start_task(project(tmp_path, "shop"), "delete the build folder",
                                            destructive_confirmed=True)
        await wait_for_status(session, {"failed", "completed"})

    run(scenario())
    args = calls(log)[0]["args"]
    disallowed = set(args[args.index("--disallowedTools") + 1:])
    assert "Bash(rm -rf *)" not in disallowed and not (set(DESTRUCTIVE_TOOLS) & disallowed)
    assert {"Bash(git push *)", "Bash(git push --force*)", "Bash(format *)"} <= disallowed


def test_env_is_clean_of_parent_claude_session(monkeypatch):
    from sugar.core.processes import clean_child_env

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    env = clean_child_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert os.environ.get("CLAUDECODE") == "1"
