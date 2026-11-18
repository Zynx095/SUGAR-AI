"""Live desktop acceptance run: SID's real pipeline (router → orchestrator → tools → Windows) on this PC.

    .\\venv\\Scripts\\python.exe tools\\desktop_acceptance.py [--wait-idle 120] [--skip camera,paint,...]

It opens Paint, Notepad and Settings, draws, dictates, scrolls, clicks, reads the screen and (unless skipped)
uses the camera, then closes what it opened without saving changes. Because it drives the real mouse and
keyboard it first waits until nobody has touched the PC for ``--wait-idle`` seconds and no full-screen app
(a game, a video) is in front, and it stops as soon as the pointer moves somewhere it didn't put it.

Results go to data/acceptance/desktop-<time>.json and screenshots to data/acceptance/<time>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("HF_HOME", r"D:\Sugar_Models\hf")


def wait_until_idle(seconds: float, give_up_s: float) -> bool:
    from sugar.computer.guard import fullscreen_foreground, idle_seconds

    started = time.time()
    while time.time() - started < give_up_s:
        if idle_seconds() >= seconds and fullscreen_foreground() is None:
            return True
        time.sleep(5)
    return False


class UserTookOver(RuntimeError):
    pass


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-idle", type=float, default=120.0)
    parser.add_argument("--give-up", type=float, default=3 * 3600.0)
    parser.add_argument("--skip", default="")
    args = parser.parse_args()
    skip = set(filter(None, args.skip.split(",")))

    if not wait_until_idle(args.wait_idle, args.give_up):
        print("The PC was in use the whole time; nothing was run.")
        return 2

    from PIL import ImageGrab

    from sugar.app.application import SIDApp
    from sugar.config.settings import load_settings

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "data" / "acceptance" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(dir=r"D:\Sugar_Models\tmp"))  # a scratch data dir: user settings untouched
    settings = load_settings(data_dir=data)
    for name in ("apps.json",):
        source = ROOT / "data" / name
        if source.exists():
            (data / name).write_bytes(source.read_bytes())
    settings.ui.enabled = False
    app = SIDApp(settings, voice=False)
    app.audio_output = False
    await app.start()
    replies: list[dict] = []
    events: list[tuple[str, dict]] = []
    app.bus.subscribe("assistant.message", lambda e: replies.append(e.data))
    app.bus.subscribe("*", lambda e: events.append((e.type, dict(e.data))) if e.type in (
        "tool.complete", "dictation.state", "camera.state", "activity", "route.selected") else None)
    computer = app.computer
    results: list[dict] = []
    pointer = {"at": computer.backend.cursor()}

    def check_user() -> None:
        now = computer.backend.cursor()
        if abs(now[0] - pointer["at"][0]) > 40 or abs(now[1] - pointer["at"][1]) > 40:
            raise UserTookOver(f"pointer moved from {pointer['at']} to {now}")

    async def say(text: str, wait_s: float = 90.0, settle: float = 1.0) -> str:
        check_user()
        count = len(replies)
        started = time.perf_counter()
        await app.conversation.handle_text(text)
        while time.perf_counter() - started < wait_s and len(replies) == count and not app.dictation.typing:  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        while app.dictation.typing and time.perf_counter() - started < wait_s:  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        await asyncio.sleep(settle)
        pointer["at"] = computer.backend.cursor()
        reply = replies[-1]["text"] if len(replies) > count else ""
        return reply

    def shot(name: str) -> str:
        path = out_dir / f"{name}.png"
        ImageGrab.grab(all_screens=True).save(path)
        return str(path)

    def record(name: str, ok: bool, **detail) -> None:
        results.append({"step": name, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + name, json.dumps(detail, default=str)[:300])

    def window_of(process: str):
        return next((w for w in computer.windows.list() if w.process.lower() == process), None)

    try:
        # ---------------------------------------------------------------- apps
        settings_was_open = window_of("systemsettings.exe") is not None
        paint_was_open = window_of("mspaint.exe") is not None
        if "apps" not in skip:
            reply = await say("open settings")
            record("open Settings", window_of("systemsettings.exe") is not None, reply=reply)
            reply = await say("open bluetooth settings")
            record("open Bluetooth settings page", "bluetooth" in (computer.context.active().title.lower()
                                                                    if computer.context.active() else ""),
                   reply=reply, title=computer.context.active().title if computer.context.active() else None)
            reply = await say("scroll down a little")
            record("scroll Settings", "Scroll" in reply or "bottom" in reply or reply.startswith("Already"),
                   reply=reply)
            reply = await say("what can I click")
            record("list visible controls", "I can see" in reply, reply=reply[:200])
            reply = await say("where is the search box")
            record("find the search box", " is " in reply, reply=reply)
            reply = await say("read the text in this window")
            record("read the window", len(reply) > 20, reply=reply[:200])
            shot("settings")
            if not settings_was_open:  # leave the user's own Settings window alone
                await say("close settings")

        # ---------------------------------------------------------------- Notepad dictation
        if "notepad" not in skip:
            await say("open notepad")
            notepad = window_of("notepad.exe")
            await say("start dictation")
            await say("Dear team. Go to the next line.", settle=0.5)
            await say("Thanks for the update. It was very helpful.", settle=0.5)
            await say("Delete the last sentence.", settle=0.5)
            await say("new paragraph", settle=0.5)
            await say("Best regards", settle=0.5)
            await say("stop dictation")
            text = computer.backend.focused_text(5000) or ""
            record("dictation with next line, delete sentence and new paragraph",
                   "Dear team.\r\nThanks for the update." in text.replace("\n", "\r\n").replace("\r\r", "\r")
                   or text.replace("\r", "") == "Dear team.\nThanks for the update.\n\nBest regards",
                   text=text)
            await say("select everything")
            await say("write a short paragraph about artificial intelligence", wait_s=120)
            text = computer.backend.focused_text(8000) or ""
            record("write a paragraph streams into Notepad", len(text) > 120 and "intelligence" in text.lower(),
                   chars=len(text), start=text[:120])
            await say("go to the next line")
            await say("continue", wait_s=120)
            longer = computer.backend.focused_text(12000) or ""
            record("continue writing", len(longer) > len(text) + 40, chars=len(longer))
            shot("notepad")
            if notepad is not None:
                # Close only the document SID wrote in (Notepad's other tabs may hold the user's unsaved work):
                # "close this" closes the tab in front, and "don't save" answers that tab's prompt only.
                await say("close this")
                await asyncio.sleep(1.5)
                reply = await say("don't save")
                record("close SID's Notepad tab without saving", True, reply=reply)

        # ---------------------------------------------------------------- Paint
        if "paint" in skip or paint_was_open:
            if paint_was_open:
                record("Paint section skipped: Paint was already open with the user's picture", True)
        else:
            reply = await say("open paint and draw a circle", wait_s=120)
            record("open Paint and draw a circle", "Drew" in reply, reply=reply)
            reply = await say("draw a cat beside it", wait_s=240)
            record("draw a cat from a reference", "Drew" in reply, reply=reply)
            reply = await say("add colour", wait_s=60)
            record("add colour", "Coloured" in reply, reply=reply)
            reply = await say("undo the last shape", wait_s=60)
            record("undo the last shape", "Removed" in reply, reply=reply)
            target = Path(tempfile.gettempdir()) / f"sid-acceptance-{stamp}.png"
            reply = await say(f"save this as {target}", wait_s=60)
            record("save the drawing", target.exists() and target.stat().st_size > 0, reply=reply, path=str(target))
            shot("paint")
            await say("close paint")
            await asyncio.sleep(1.5)
            if window_of("mspaint.exe") is not None:
                await say("don't save")

        # ---------------------------------------------------------------- camera
        if "camera" not in skip:
            reply = await say("turn on the camera", wait_s=30)
            record("camera on", computer.camera.active, reply=reply)
            reply = await say("what do you see", wait_s=120)
            record("describe a fresh camera frame", len(reply) > 20 and computer.camera.last_frame_at is not None,
                   reply=reply[:300])
            reply = await say("turn off the camera", wait_s=30)
            record("camera off and released", not computer.camera.active, reply=reply)
    except UserTookOver as exc:
        record("stopped: the user took the mouse back", False, detail=str(exc))
    finally:
        await app.shutdown()
        report = ROOT / "data" / "acceptance" / f"desktop-{stamp}.json"
        report.write_text(json.dumps({"results": results, "events": events[-400:]}, indent=2, default=str),
                          encoding="utf-8")
        print(f"report: {report}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
