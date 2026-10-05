"""Tool registry, argument validation, permissions and the executor."""

from __future__ import annotations

import asyncio

import pytest
from conftest import run

from sugar.agent.executor import ToolExecutor
from sugar.agent.permissions import PermissionManager
from sugar.config.settings import PermissionSettings
from sugar.tools.registry import (
    PermissionLevel,
    Tool,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
    params,
    validate_arguments,
)


def make_tool(name="files.delete", level=PermissionLevel.DESTRUCTIVE, handler=None, timeout=5.0):
    async def default_handler(args):
        return ToolResult(True, f"did {args}")

    return Tool(
        name=name,
        description="test tool",
        parameters=params(["path"], path={"type": "string"}, force={"type": "boolean", "default": False}),
        handler=handler or default_handler,
        level=level,
        timeout_s=timeout,
        describe=lambda a: f"delete {a['path']}",
    )


def test_schema_uses_function_safe_names():
    registry = ToolRegistry()
    registry.register(make_tool())
    schema = registry.schemas()[0]
    assert schema["function"]["name"] == "files__delete"
    assert registry.get("files__delete") is registry.get("files.delete")


def test_duplicate_registration_is_an_error():
    registry = ToolRegistry()
    registry.register(make_tool())
    with pytest.raises(ValueError):
        registry.register(make_tool())


def test_validation_coerces_fills_defaults_and_drops_unknown_keys():
    schema = params(["n"], n={"type": "integer", "minimum": 0, "maximum": 100}, flag={"type": "boolean", "default": True})
    assert validate_arguments(schema, {"n": "42", "evil": "x"}) == {"n": 42, "flag": True}
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {"n": 500})
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {})
    with pytest.raises(ToolValidationError):
        validate_arguments(params(["mode"], mode={"type": "string", "enum": ["a", "b"]}), {"mode": "c"})


def test_permission_policy_matrix(bus):
    manager = PermissionManager(PermissionSettings(auto_approve_level=1), bus)
    read = make_tool(level=PermissionLevel.READ)
    sensitive = make_tool(level=PermissionLevel.SENSITIVE)
    destructive = make_tool(level=PermissionLevel.DESTRUCTIVE)
    assert not manager.requires_confirmation(read, PermissionLevel.READ, "model")
    assert manager.requires_confirmation(sensitive, PermissionLevel.SENSITIVE, "model")
    assert not manager.requires_confirmation(sensitive, PermissionLevel.SENSITIVE, "user")
    assert manager.requires_confirmation(destructive, PermissionLevel.DESTRUCTIVE, "user")
    trusted = PermissionManager(PermissionSettings(trusted_tools=["files.delete"]), bus)
    assert not trusted.requires_confirmation(destructive, PermissionLevel.DESTRUCTIVE, "model")


def build_executor(bus, tool, **perm):
    registry = ToolRegistry()
    registry.register(tool)
    manager = PermissionManager(PermissionSettings(confirmation_timeout_s=perm.pop("timeout", 2.0), **perm), bus)
    return ToolExecutor(registry, manager, bus), manager


def test_destructive_tool_waits_for_confirmation_and_runs_when_approved(bus, recorder):
    executor, manager = build_executor(bus, make_tool())
    asked = []

    async def ask(request):
        asked.append(request.action)
        asyncio.get_running_loop().call_later(0.01, manager.resolve, True)

    manager.on_request = ask
    result = run(executor.execute("files.delete", {"path": "build"}, origin="user"))
    assert result.ok
    assert asked == ["delete build"]
    assert {"permission.request", "permission.resolved", "tool.start", "tool.complete"} <= set(recorder.types())


def test_denied_tool_never_runs(bus):
    ran = []

    async def handler(args):
        ran.append(args)
        return ToolResult(True, "ran")

    executor, manager = build_executor(bus, make_tool(handler=handler))

    async def ask(request):
        asyncio.get_running_loop().call_later(0.01, manager.resolve, False)

    manager.on_request = ask
    result = run(executor.execute("files.delete", {"path": "build"}, origin="model"))
    assert not result.ok and result.error == "permission denied by user"
    assert ran == []


def test_unanswered_confirmation_times_out_as_denied(bus):
    executor, _ = build_executor(bus, make_tool(), timeout=0.05)
    result = run(executor.execute("files.delete", {"path": "x"}))
    assert not result.ok


def test_executor_turns_exceptions_and_timeouts_into_results(bus):
    async def boom(args):
        raise RuntimeError("disk on fire")

    async def slow(args):
        await asyncio.sleep(5)
        return ToolResult(True, "late")

    executor, _ = build_executor(bus, make_tool("t.boom", PermissionLevel.READ, boom))
    result = run(executor.execute("t.boom", {"path": "x"}))
    assert not result.ok and "disk on fire" in result.summary

    executor, _ = build_executor(bus, make_tool("t.slow", PermissionLevel.READ, slow, timeout=0.05))
    result = run(executor.execute("t.slow", {"path": "x"}))
    assert not result.ok and result.error == "timeout"


def test_executor_rejects_bad_arguments_before_permission(bus):
    executor, manager = build_executor(bus, make_tool())
    result = run(executor.execute("files.delete", {"force": True}))
    assert not result.ok and "missing required" in result.summary
    assert manager.pending is None


def test_unknown_tool(bus):
    executor, _ = build_executor(bus, make_tool())
    assert not run(executor.execute("nope", {})).ok


def test_result_for_model_is_bounded():
    text = ToolResult(True, "ok", data="x" * 20000).for_model(limit=500)
    assert len(text) <= 500
