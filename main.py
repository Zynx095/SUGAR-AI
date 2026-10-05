"""Sugar — entry point.

    python main.py                       voice + UI window (default)
    python main.py --window edge         UI in an Edge app window instead of pywebview
    python main.py --no-voice            UI only, type to talk
    python main.py --text "what time is it"   one-shot, prints the reply (no audio, no UI)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from sugar.app.application import SugarApp  # noqa: E402
from sugar.config.settings import load_settings  # noqa: E402
from sugar.core.logging import setup_logging  # noqa: E402

log = logging.getLogger("sugar.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sugar voice assistant")
    parser.add_argument("--no-voice", action="store_true", help="disable microphone and speech recognition")
    parser.add_argument("--no-ui", action="store_true", help="run without the UI (voice only)")
    parser.add_argument("--window", choices=["auto", "webview", "edge", "browser", "none"], help="how to show the UI")
    parser.add_argument("--text", help="process one typed request, print the reply and exit")
    parser.add_argument("--debug", action="store_true", help="verbose console logging + developer panel")
    return parser.parse_args(argv)


async def run_text_once(app: SugarApp, text: str) -> int:
    app.audio_output = False
    await app.start()
    replies: list[str] = []
    done = asyncio.Event()

    def on_message(event) -> None:
        if event.data.get("turn_id") is not None or event.data.get("quick"):
            replies.append(event.data.get("text") or "")
            done.set()

    app.bus.subscribe("assistant.message", on_message)
    await app.conversation.handle_text(text)
    try:
        await asyncio.wait_for(done.wait(), timeout=180)
    except TimeoutError:
        print("(no reply within 180 s)")
        return 1
    print(replies[-1] if replies else "")
    await app.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; model output is Unicode
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    settings = load_settings()
    if args.debug:
        settings.logging.level = "DEBUG"
        settings.ui.developer_mode = True
    if args.no_ui or args.text:
        settings.ui.enabled = False
    setup_logging(settings.logging.level, settings.paths.data_dir / "logs" if settings.logging.file else None,
                  settings.logging.console)

    if args.text:
        app = SugarApp(settings, voice=False)
        return asyncio.run(run_text_once(app, args.text))

    window_mode = args.window or settings.ui.window
    if not settings.ui.enabled:
        window_mode = "none"
    voice = not args.no_voice

    async def core(holder: dict) -> None:
        app = SugarApp(settings, voice=voice)
        holder["app"] = app
        holder["ready"].set()
        try:
            await app.start()
            await app.run_forever()
        finally:
            await app.shutdown()

    holder: dict = {"ready": threading.Event()}
    thread, holder = _start_core(core, holder)
    holder["ready"].wait(timeout=30)
    app: SugarApp | None = holder.get("app")
    if app is None:
        print("Sugar failed to start:", holder.get("error"))
        return 1

    if window_mode != "none" and settings.ui.enabled:
        app.ui_ready.wait(timeout=30)
        url = app.ui.url if app.ui else None
        if url:
            from sugar.ui.window import open_external, run_webview, webview_available

            if window_mode in ("auto", "webview") and webview_available():
                try:
                    run_webview(url, on_closed=app.request_stop)  # blocks until the window closes
                except Exception:
                    log.exception("pywebview failed; falling back to Edge")
                    open_external(url, "edge")
            else:
                open_external(url, window_mode)
            print(f"Sugar UI: {url}")

    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        app.request_stop()
        thread.join(timeout=10)
    return 0 if "error" not in holder else 1


def _start_core(core, holder: dict) -> tuple[threading.Thread, dict]:
    def target() -> None:
        try:
            asyncio.run(core(holder))
        except Exception as exc:
            holder["error"] = exc
            log.exception("Sugar core crashed")
        finally:
            holder["ready"].set()

    thread = threading.Thread(target=target, name="sugar-core", daemon=False)
    thread.start()
    time.sleep(0)  # let the core thread begin
    return thread, holder


if __name__ == "__main__":
    sys.exit(main())
