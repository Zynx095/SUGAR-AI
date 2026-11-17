"""Computer-control latency on the real desktop.

    python benchmarks/computer_control.py

Measures window focus, real-time and paste typing, minimize/restore and
close on throw-away Tk windows, then the full path a spoken command takes
(grammar → executor → computer control) through SugarApp. Acts only on its
own windows, refuses to run while a game or full-screen app is in front, and
gives the foreground back afterwards.
"""

from __future__ import annotations

import asyncio
import time

from _common import ms_since, row, save, table
from live_helpers import ForegroundKeeper, TkTarget, busy_reason, make_computer

TEXT = "The quick brown fox jumps over the lazy dog, 1234567890!"
LONG = "\n".join(f"{i:03d} lorem ipsum dolor sit amet, consectetur adipiscing elit" for i in range(40))


async def controller_rows(computer) -> list[dict]:
    rows: list[dict] = []
    a, b, c = TkTarget("Sugar Bench A"), TkTarget("Sugar Bench B"), TkTarget("Sugar Bench C")
    try:
        ha, hb, hc = a.wait(computer), b.wait(computer), c.wait(computer)
        focus = []
        for i in range(8):
            target = ha if i % 2 == 0 else hb
            start = time.perf_counter()
            result = await computer.run(computer.windows.focus, target)
            focus.append(ms_since(start))
            assert result.success, result.details
        rows.append(row("Window focus (activate + verify)", focus, "two windows of another process, alternating"))

        realtime, per_char, exact = [], [], 0
        for _ in range(3):
            await computer.run(computer.keyboard.press, "ctrl+a", target=ha)
            await computer.run(computer.keyboard.press, "delete", target=ha)
            start = time.perf_counter()
            result = await computer.run(computer.keyboard.type_text, TEXT, target=ha, mode="realtime")
            elapsed = ms_since(start)
            realtime.append(elapsed)
            per_char.append(elapsed / len(TEXT))
            exact += a.text() == TEXT and result.success
        rows.append(row(f"Type {len(TEXT)} chars, real time", realtime, f"exact {exact}/3, incl. focus check"))
        rows.append(row("Real-time typing per character", per_char, "paced key events"))

        first = []
        for _ in range(5):
            start = time.perf_counter()
            await computer.run(computer.keyboard.type_text, "x", target=hb, mode="realtime")
            first.append(ms_since(start))
        rows.append(row("Typing call → first character", first, "focus switch + one character"))

        paste, exact = [], 0
        for _ in range(3):
            await computer.run(computer.keyboard.press, "ctrl+a", target=ha)
            await computer.run(computer.keyboard.press, "delete", target=ha)
            start = time.perf_counter()
            result = await computer.run(computer.keyboard.type_text, LONG, target=ha, mode="paste")
            paste.append(ms_since(start))
            exact += a.text() == LONG and result.success
        rows.append(row(f"Paste {len(LONG)} chars", paste, f"exact {exact}/3, clipboard restored"))

        minimize, restore = [], []
        for _ in range(4):
            start = time.perf_counter()
            assert (await computer.run(computer.windows.set_state, hc, "minimize")).verified
            minimize.append(ms_since(start))
            start = time.perf_counter()
            assert (await computer.run(computer.windows.set_state, hc, "restore")).verified
            restore.append(ms_since(start))
        rows.append(row("Minimize (verified)", minimize))
        rows.append(row("Restore (verified)", restore))

        start = time.perf_counter()
        closed = await computer.run(computer.windows.close, hc)
        rows.append(row("Close window (WM_CLOSE → gone)", [ms_since(start)], f"verified={closed.verified}"))
    finally:
        for target in (a, b, c):
            target.close()
    return rows


async def pipeline_rows() -> list[dict]:
    """Spoken-command path: turn start → tool start (grammar, routing, permission) → tool done."""
    from sugar.app.application import SugarApp
    from sugar.config.settings import load_settings

    settings = load_settings()
    settings.ui.enabled = False
    app = SugarApp(settings, voice=False)
    app.audio_output = False
    await app.start()
    marks: dict[str, float] = {}
    app.bus.subscribe("turn.started", lambda e: marks.__setitem__("turn", time.perf_counter()))
    app.bus.subscribe("tool.start", lambda e: marks.setdefault("tool_start", time.perf_counter()))
    app.bus.subscribe("tool.complete", lambda e: marks.__setitem__("tool_done", time.perf_counter()))
    done = asyncio.Event()
    app.bus.subscribe("assistant.message", lambda e: done.set())
    target = TkTarget("Sugar Bench Pipeline")
    start_ms, action_ms, notes = [], [], []
    try:
        target.wait(app.computer)
        await asyncio.sleep(0.3)
        for command in ("switch to sugar bench pipeline", "minimize sugar bench pipeline",
                        "bring sugar bench pipeline back", "type hello into sugar bench pipeline",
                        "maximize this", "restore this"):
            marks.clear()
            done.clear()
            await app.conversation.handle_text(command)
            await asyncio.wait_for(done.wait(), 20)
            if "turn" in marks and "tool_start" in marks:
                start_ms.append((marks["tool_start"] - marks["turn"]) * 1000)
                action_ms.append((marks.get("tool_done", marks["tool_start"]) - marks["tool_start"]) * 1000)
                notes.append(command)
    finally:
        target.close()
        await app.shutdown()
    return [row("Command → action start", start_ms, "turn start → tool start (grammar, route, permission)"),
            row("Action start → completion", action_ms, "; ".join(notes))]


async def main() -> None:
    computer = make_computer()
    reason = busy_reason(computer)
    if reason:
        computer.stop()
        raise SystemExit(f"not touching the desktop: {reason}")
    keeper = ForegroundKeeper(computer)
    try:
        rows = await controller_rows(computer)
    finally:
        keeper.restore()
        computer.stop()
    rows += await pipeline_rows()
    keeper.restore()
    print(table(rows))
    print("saved", save("computer_control", rows))


if __name__ == "__main__":
    asyncio.run(main())
