"""A stand-in for ``claude -p --output-format stream-json`` used by the runner tests.

It speaks the same line protocol as Claude Code 2.1.x and reports facts the tests check
(the working directory it really ran in, the arguments it got). Behaviour is chosen by
words in the prompt: SLEEP (hang until killed), CRASH (exit 1 without a result),
TESTFAIL (the test command exits non-zero), NOUSAGE (no cost/usage in the result).
"""

from __future__ import annotations

import json
import os
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"args": args, "cwd": os.getcwd(), "pid": os.getpid(), "prompt": prompt}) + "\n")
    resumed = args[args.index("--resume") + 1] if "--resume" in args else None
    session_id = resumed or "fake-session-1"
    emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake-opus",
          "claude_code_version": "2.1.0", "cwd": os.getcwd()})
    if "CRASH" in prompt:
        sys.stderr.write("fatal: simulated CLI failure\n")
        return 1
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": os.path.join(os.getcwd(), "app.py")}},
    ]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}})
    if "SLEEP" in prompt:
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}})
        time.sleep(120)
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q", "description": "Run tests"}},
    ]}})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": "TESTFAIL" in prompt},
    ]}})
    text = f"Done in {os.getcwd()}.\nSPOKEN SUMMARY: The change is in place. The test suite ran."
    result = {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session_id,
              "num_turns": 3, "duration_ms": 1234}
    if "NOUSAGE" not in prompt:
        result["total_cost_usd"] = 0.0123
        result["usage"] = {"input_tokens": 1500, "output_tokens": 320, "cache_read_input_tokens": 9000}
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
