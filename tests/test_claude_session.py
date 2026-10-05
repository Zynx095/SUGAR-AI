"""Claude Code CLI integration: argument building and stream parsing (no CLI needed)."""

from __future__ import annotations

import json
from pathlib import Path

from sugar.coding.claude_code import (
    ClaudeCLIInfo,
    ClaudeCodeRunner,
    ClaudeRunRequest,
    describe_tool_use,
    parse_stream_line,
)

INFO = ClaudeCLIInfo(
    executable="claude.exe",
    version="2.1.289",
    flags=frozenset({"--resume", "--model", "--permission-mode", "--effort", "--append-system-prompt",
                     "--tools", "--include-partial-messages", "--no-session-persistence",
                     "--strict-mcp-config", "--setting-sources", "--allowedTools", "--disallowedTools",
                     "--max-budget-usd", "--system-prompt", "--name"}),
    permission_modes=frozenset({"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}),
)


def test_args_never_contain_the_prompt_and_put_variadics_last():
    runner = ClaudeCodeRunner(INFO)
    args = runner.build_args(ClaudeRunRequest(
        prompt="fix the websocket bug; rm -rf / && echo pwned",
        cwd=Path("."),
        session_id="abc",
        permission_mode="auto",
        allowed_tools=["Bash(npm test *)"],
        disallowed_tools=["Bash(git push *)"],
        append_system_prompt="voice summary please",
    ))
    assert "fix the websocket bug" not in " ".join(args)  # prompt goes through stdin
    assert args[:5] == ["claude.exe", "-p", "--output-format", "stream-json", "--verbose"]
    assert args[args.index("--resume") + 1] == "abc"
    assert args[args.index("--permission-mode") + 1] == "auto"
    assert args.index("--allowedTools") > args.index("--append-system-prompt")
    assert args[-2:] == ["--disallowedTools", "Bash(git push *)"]


def test_unsupported_permission_mode_falls_back():
    old = ClaudeCLIInfo("claude", "1.0.0", frozenset({"--permission-mode"}), frozenset({"acceptEdits", "plan"}))
    args = ClaudeCodeRunner(old).build_args(ClaudeRunRequest(prompt="x", cwd=Path("."), permission_mode="auto"))
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"


def test_isolated_plain_llm_mode():
    args = ClaudeCodeRunner(INFO).build_args(
        ClaudeRunRequest(prompt="x", cwd=Path("."), tools="", isolated=True, persist_session=False))
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--setting-sources") + 1] == "project"


def test_parse_init_tool_use_and_result_lines():
    init = parse_stream_line(json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                                         "model": "claude-opus-5-5", "claude_code_version": "2.1.289"}))
    assert init[0].kind == "init" and init[0].data["session_id"] == "s1"

    assistant = parse_stream_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "Looking at auth."},
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "D:/p/src/auth/middleware.ts"}},
    ]}}))
    assert [e.kind for e in assistant] == ["thinking", "text", "tool_use"]
    assert assistant[2].summary == "Editing auth/middleware.ts"

    result = parse_stream_line(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                           "result": "Done.", "session_id": "s1", "total_cost_usd": 0.1}))
    assert result[0].kind == "result" and result[0].data["result"] == "Done."


def test_parse_ignores_garbage_and_handles_partial_deltas():
    assert parse_stream_line("") == []
    assert parse_stream_line("not json")[0].kind == "other"
    delta = parse_stream_line(json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}))
    assert delta[0].kind == "delta" and delta[0].data["text"] == "hi"


def test_tool_descriptions_are_speakable():
    assert describe_tool_use("Bash", {"command": "npm   test", "description": "Run the test suite"}) == \
        "Running Run the test suite"
    assert describe_tool_use("Grep", {"pattern": "WebSocket"}) == "Searching for 'WebSocket'"
    assert describe_tool_use("Mystery", {}) == "Using Mystery"
