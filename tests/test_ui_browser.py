"""The real web UI in a real Chromium (headless Microsoft Edge), driven over the DevTools protocol.

The UI server and event bus are SID's own; the page is the shipped page. Events are published
on the bus exactly as the core publishes them, and the test reads the page's state through its
read-only ``window.__sid`` diagnostics and the DOM. Screenshots land in ``data/ui_screens``.

Skipped when Edge isn't installed. Set ``SID_UI_BROWSER_TESTS=0`` to skip explicitly.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import run
from websockets.asyncio.client import connect

from sugar.core.events import EventBus
from sugar.ui.server import UIServer

ROOT = Path(__file__).resolve().parents[1]
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists()), None)
pytestmark = pytest.mark.skipif(EDGE is None or os.environ.get("SID_UI_BROWSER_TESTS") == "0",
                                reason="needs Microsoft Edge")
SCREENS = ROOT / "data" / "ui_screens"

STALE = {"project_name": "Q-Shield", "project_path": "D:\\old\\Q-Shield", "status": "completed", "task": "old task",
         "last_activity": 1.0, "activity": [], "checks": {}, "history": [], "claude_session_id": "stale-session"}


def snapshot() -> dict:
    return {
        "state": "idle", "engaged": False, "mic_paused": False, "project": None, "previous_project": None,
        "components": {"ui": {"status": "ready", "detail": ""}, "claude_code": {"status": "ready",
                                                                                "detail": "Claude Code 2.1.326"},
                       "tts": {"status": "ready", "detail": "melo on cuda"}},
        "settings": {"tts": {"speed": 0.97, "voice_profile": "sid"}, "vad": {"threshold": 0.5,
                     "endpoint_default_ms": 750}, "conversation": {"engagement": "wake_word"}},
        "history": [], "projects": [], "sessions": [STALE], "providers": {}, "metrics": {}, "memories": [],
        "pending_permission": None,
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Page:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.ids = itertools.count(1)

    async def call(self, method: str, **params):
        message_id = next(self.ids)
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            reply = json.loads(await self.ws.recv())
            if reply.get("id") == message_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply.get("result", {})

    async def eval(self, expression: str):
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def until(self, expression: str, timeout_s: float = 10.0):
        deadline = time.monotonic() + timeout_s
        value = None
        while time.monotonic() < deadline:
            value = await self.eval(expression)
            if value:
                return value
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out waiting for {expression!r} (last: {value!r})")

    async def screenshot(self, name: str) -> None:
        SCREENS.mkdir(parents=True, exist_ok=True)
        data = await self.call("Page.captureScreenshot", format="png")
        (SCREENS / f"{name}.png").write_bytes(base64.b64decode(data["data"]))


def _launch_edge(profile: Path, width: int, height: int) -> tuple[subprocess.Popen, int]:
    port = free_port()
    process = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        f"--window-size={width},{height}", "--ignore-gpu-blocklist", "--enable-gpu", "--use-angle=d3d11",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, port


def _page_targets(port: int) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
            return [t for t in json.load(response) if t.get("type") == "page"]
    except OSError:
        return []


async def open_page(url: str, profile: Path, width: int = 1360, height: int = 860):
    process, port = await asyncio.to_thread(_launch_edge, profile, width, height)
    targets = []
    for _ in range(100):
        targets = await asyncio.to_thread(_page_targets, port)
        if targets:
            break
        await asyncio.sleep(0.1)
    assert targets, "Edge did not expose a page target"
    ws = await connect(targets[0]["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
    page = Page(ws)
    await page.call("Page.enable")
    await page.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
    await page.call("Page.navigate", url=url)
    return page, process, ws


def test_ui_end_to_end_in_a_real_browser(tmp_path):
    async def scenario():
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        commands = []

        async def on_command(message):
            commands.append(message)
            return {"ok": True}

        server = UIServer(bus, "127.0.0.1", 0, on_command, snapshot)
        await server.start()
        page, process, ws = await open_page(server.url, tmp_path / "profile")
        results = {}
        try:
            # WebGL entity initialises and renders frames
            info = await page.until("window.__sid && window.__sid.entity() && window.__sid.entity().frames > 20 "
                                    "&& window.__sid.entity()", 20)
            results["entity"] = info
            await page.until("window.__sid.state().state === 'idle'")
            # no stale project identity: the old Q-Shield session is not presented as current
            results["project"] = await page.eval("document.getElementById('projectName').textContent")
            results["bench_idle"] = await page.eval("window.__sid.bench()")
            await asyncio.sleep(1.2)
            await page.screenshot("01-idle")

            # entity states follow real events
            states = {}
            for backend, expected in [("listening", "listening"), ("transcribing", "listening"),
                                      ("tool_execution", "executing"), ("speaking", "speaking"), ("paused", "muted")]:
                bus.publish("state.changed", state=backend, previous="idle")
                states[backend] = await page.until(
                    f"window.__sid.state().state === '{expected}' && window.__sid.entity().state === '{expected}' "
                    f"&& window.__sid.status()")
            bus.publish("state.changed", state="idle", previous="paused")
            await page.until("window.__sid.state().state === 'idle'")
            bus.publish("turn.started", turn_id="t1", text="explain closures", source="text")
            bus.publish("state.changed", state="thinking", previous="idle")
            states["before_model"] = await page.until("window.__sid.state().state === 'processing' && window.__sid.status()")
            bus.publish("llm.start", provider="freellm", model="auto")
            states["thinking"] = await page.until("window.__sid.state().state === 'thinking' && window.__sid.status()")
            await page.screenshot("02-thinking")

            # conversation streaming with a code block
            bus.publish("user.message", text="Explain closures in Python", source="text")
            for piece in ["A closure keeps ", "the enclosing scope alive.\n\n```python\n", "def outer():\n    x = 1\n",
                          "    def inner():\n        return x\n    return inner\n```\n"]:
                bus.publish("assistant.delta", turn_id="t1", text=piece)
                await asyncio.sleep(0.05)
            bus.publish("assistant.message", turn_id="t1", text="A closure keeps the enclosing scope alive.\n\n"
                        "```python\ndef outer():\n    x = 1\n    def inner():\n        return x\n    return inner\n```",
                        route="coding_qa")
            bus.publish("state.changed", state="idle", previous="thinking")
            results["turn"] = await page.until(
                "(() => { const t = [...document.querySelectorAll('#captions .turn.assistant')].pop();"
                " return t && t.querySelector('.tok-k') && t.querySelector('.copy') && t.textContent; })()")

            # a refusal is shown as one
            bus.publish("assistant.message", turn_id="t2", text="I'm designed for software engineering and "
                        "development. I can't assist with that request.", route="rejected")
            results["refusal"] = await page.until("document.querySelector('#captions .turn.refusal') !== null")

            # Claude Code task: workbench expands, coding state, steps, checks, cost only when reported
            bus.publish("project.active", name="shop", path="D:\\work\\shop", stack=["node"])
            await page.until("document.getElementById('projectName').textContent === 'shop'")
            running = {"project_name": "shop", "project_path": "D:\\work\\shop", "status": "running",
                       "task": "add a health endpoint", "mode": "work", "last_activity": time.time(),
                       "task_started_at": time.time() - 12, "activity": ["Started: add a health endpoint"],
                       "checks": {}, "history": [], "claude_session_id": None, "last_cost_usd": None,
                       "last_usage": None, "last_duration_ms": None}
            bus.publish("coding.session", **running)
            results["coding_state"] = await page.until(
                "window.__sid.state().state === 'coding' && window.__sid.status()")
            results["bench_running"] = await page.until(
                "window.__sid.bench().mode === 'open' && window.__sid.bench()")
            bus.publish("coding.progress", project="shop", project_path="D:\\work\\shop", kind="tool",
                        text="Editing src/server.ts", tool="Edit")
            results["step"] = await page.until(
                "[...document.querySelectorAll('.steps li')].some(li => li.textContent === 'Editing src/server.ts')")
            await asyncio.sleep(1.0)
            await page.screenshot("03-coding")
            done = {**running, "status": "completed", "claude_session_id": "abc-123", "last_cost_usd": 0.0123,
                    "last_usage": {"input_tokens": 1500, "output_tokens": 320}, "last_duration_ms": 42000,
                    "checks": {"tests": {"status": "passed", "command": "npm test"}},
                    "last_summary": "The endpoint is in place. Tests pass.",
                    "result_text": "Added GET /health.\n\n```ts\napp.get('/health', ...)\n```"}
            bus.publish("coding.session", **done)
            results["after"] = await page.until(
                "window.__sid.state().state === 'idle' && window.__sid.bench().mode === 'rail' && window.__sid.bench()")
            results["output"] = await page.until("document.querySelector('details.output') !== null")

            # error state with a specific message, then back to idle
            bus.publish("error", source="stt", message="Speech recognition failed.")
            results["error"] = await page.until("window.__sid.state().state === 'error' && window.__sid.status()")
            await page.until("window.__sid.state().state === 'idle'", 6)

            # telemetry panel shows measured, not invented, values
            await page.eval("document.getElementById('devToggle').click()")
            await page.eval("document.querySelector('.tab[data-tab=engineering]').click()")
            results["telemetry"] = await page.until("document.getElementById('engineeringFacts').textContent")
            await page.screenshot("04-telemetry")
            await page.eval("document.getElementById('devClose').click()")

            # resize: the renderer follows the window
            before = (await page.eval("window.__sid.entity()"))["size"]
            await page.call("Emulation.setDeviceMetricsOverride", width=820, height=700, deviceScaleFactor=1,
                            mobile=False)
            results["resized"] = await page.until(
                f"(() => {{ const s = window.__sid.entity().size; return (s[0] !== {before[0]}) && s; }})()")
            await asyncio.sleep(0.6)
            await page.screenshot("05-narrow")

            # minimise: when the page reports hidden, rendering stops; restoring resumes it
            await page.eval("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
                            "document.dispatchEvent(new Event('visibilitychange'))")
            frames = (await page.eval("window.__sid.entity()"))["frames"]
            await asyncio.sleep(0.5)
            hidden = await page.eval("window.__sid.entity()")
            results["hidden"] = {"running": hidden["running"], "frames_before": frames, "frames_after": hidden["frames"]}
            await page.eval("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});"
                            "document.dispatchEvent(new Event('visibilitychange'))")
            results["restored"] = await page.until("window.__sid.entity().running")

            # typed input is sent to the core
            await page.eval("const i = document.getElementById('input'); i.value = 'What is a closure?';"
                            "document.getElementById('composer').requestSubmit()")
            await asyncio.sleep(0.3)

            # the core going away shows offline
            await server.stop()
            results["offline"] = await page.until("window.__sid.state().state === 'offline' && window.__sid.status()")
            results["states"] = states
        finally:
            await ws.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        return results, commands

    results, commands = run(scenario())
    assert results["entity"]["backend"] == "webgl" and results["entity"]["points"] > 15000
    assert results["project"] == "NO ACTIVE PROJECT"
    assert results["bench_idle"]["session"] is None and results["bench_idle"]["mode"] == "rail"
    assert results["states"]["listening"] == "Listening"
    assert results["states"]["transcribing"] == "Listening"
    assert results["states"]["tool_execution"] == "Executing task"
    assert results["states"]["speaking"] == "Speaking"
    assert results["states"]["before_model"] == "Analysing request"
    assert results["states"]["thinking"] == "Engineering solution"
    assert "A closure keeps the enclosing scope alive." in results["turn"]
    assert results["coding_state"] == "Engineering in shop"
    assert results["bench_running"]["session"]["statusText"] == "Running"
    assert results["bench_running"]["session"]["cost"] is None  # not reported yet, not invented
    assert results["after"]["session"]["cost"] == "$0.0123"
    assert results["after"]["session"]["checks"][0]["status"] == "passed"
    assert results["error"] == "Speech recognition failed"
    assert "Event bus" in results["telemetry"] and "webgl" in results["telemetry"]
    assert results["hidden"]["running"] is False
    assert results["hidden"]["frames_after"] - results["hidden"]["frames_before"] <= 1
    assert results["offline"] == "Offline. Reconnecting"
    assert {"cmd": "text", "text": "What is a closure?"}.items() <= next(c for c in commands if c["cmd"] == "text").items()
