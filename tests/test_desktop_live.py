"""Real-machine computer-control checks (opt-in: SUGAR_DESKTOP_TESTS=1).

They act only on windows they create — a Tk text window, a temporary file in
Notepad (its own tab), a separate browser window — never on the user's own
documents or tabs, skip when a game or full-screen app is in front, and give
the foreground back afterwards. Running them steals focus for a few seconds.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from live_helpers import ForegroundKeeper, TkTarget, busy_reason, make_computer, temp_text_file

pytestmark = pytest.mark.skipif(os.environ.get("SUGAR_DESKTOP_TESTS") != "1",
                                reason="real-desktop tests are opt-in: set SUGAR_DESKTOP_TESTS=1")

SAMPLE = 'Hello Sugar\nThis is a test.\n\nPython:\nprint("Hello World")\nSymbols: @#$%^&*()_+{}[]:"<>?/\\| ~`=\'-; café ✓'


def do(computer, fn, *args, **kwargs):
    return asyncio.run(computer.run(fn, *args, **kwargs))


@pytest.fixture(scope="module")
def computer():
    computer = make_computer()
    reason = busy_reason(computer)
    if reason:
        computer.stop()
        pytest.skip(f"not touching the desktop: {reason}")
    keeper = ForegroundKeeper(computer)
    computer.media.pause_other_media = False  # leave the user's music alone
    yield computer
    keeper.restore()
    computer.stop()


def test_realtime_and_paste_typing_are_exact(computer):
    target = TkTarget()
    try:
        hwnd = target.wait(computer)
        result = do(computer, computer.keyboard.type_text, SAMPLE, target=hwnd, mode="realtime")
        assert result.success, result.details
        assert target.text() == SAMPLE
        do(computer, computer.keyboard.press, "ctrl+a", target=hwnd)
        do(computer, computer.keyboard.press, "delete", target=hwnd)
        before = do(computer, computer.backend.clipboard_text)
        long_text = "\n".join(f"{i:03d} the quick brown fox jumps over the lazy dog" for i in range(40))
        pasted = do(computer, computer.keyboard.type_text, long_text, target=hwnd)
        assert pasted.success and pasted.data["mode"] == "paste"
        assert target.text() == long_text
        assert do(computer, computer.backend.clipboard_text) == before, "clipboard not restored"
    finally:
        target.close()


def test_switching_minimizing_and_closing_windows(computer):
    first, second = TkTarget("Sugar Live A"), TkTarget("Sugar Live B")
    try:
        a, b = first.wait(computer), second.wait(computer)
        assert do(computer, computer.windows.focus, a).success
        assert computer.backend.foreground() == a
        assert do(computer, computer.windows.set_state, a, "minimize").verified
        assert do(computer, computer.windows.set_state, a, "restore").verified
        assert do(computer, computer.focus, "sugar live b").success and computer.backend.foreground() == b
        closed = do(computer, computer.close, "this")  # "this" = Sugar Live B
        assert closed.success and closed.verified and not computer.backend.is_window(b)
        assert do(computer, computer.close, "sugar live a").verified
    finally:
        first.close()
        second.close()


def _wait_for_title(computer, fragment: str, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in computer.backend.list_windows():
            if fragment.lower() in window.title.lower():
                return window.hwnd
        time.sleep(0.1)
    raise TimeoutError(f"no window titled like {fragment}")


def test_notepad_type_enter_type_save_and_close_our_tab(computer):
    path = temp_text_file()
    try:
        do(computer, computer.backend.spawn, ["notepad.exe", str(path)])
        hwnd = _wait_for_title(computer, path.stem)
        assert do(computer, computer.keyboard.type_text, "Hello, this is Sugar AI.", target=hwnd).success
        assert path.stem in computer.backend.get_window(hwnd).title  # still our tab
        assert do(computer, computer.keyboard.press, "enter", target=hwnd).success
        typed = do(computer, computer.keyboard.type_text, "This is a hands-free computer.", target=hwnd)
        assert typed.success and typed.verified, typed.details
        assert do(computer, computer.keyboard.save, target=hwnd).verified
        saved = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        assert saved == "Hello, this is Sugar AI.\nThis is a hands-free computer."
        if path.stem in computer.backend.get_window(hwnd).title:
            do(computer, computer.keyboard.press, "ctrl+w", target=hwnd)  # our tab only, it's saved
        time.sleep(0.5)
        window = computer.backend.get_window(hwnd)
        assert window is None or path.stem not in window.title
    finally:
        path.unlink(missing_ok=True)


def test_browser_new_tab_youtube_result_and_close(computer):
    opened = do(computer, computer.browser.open, None, None, new_window=True)
    assert opened.success, opened.details
    hwnd = opened.data["hwnd"]
    try:
        assert do(computer, computer.browser.new_tab).verified
        search = do(computer, computer.browser.search, "Blinding Lights The Weeknd", "youtube")
        assert search.success and "YouTube" in computer.backend.get_window(hwnd).title
        played = asyncio.run(computer.media.open_result(None))
        assert played.success and played.verified, played.details
        assert played.data["video"]["channel"].lower().startswith("the weeknd")
        asyncio.run(computer.media.control("pause"))
        closed = do(computer, computer.browser.close_tab)
        assert closed.verified
    finally:
        if computer.backend.is_window(hwnd):
            window = computer.backend.get_window(hwnd)
            for _ in range(len(computer.backend.tabs(hwnd)) + 1):
                if not computer.backend.is_window(hwnd):
                    break
                do(computer, computer.backend.close_tab, hwnd, 0)
                time.sleep(0.3)
            if computer.backend.is_window(hwnd):
                do(computer, computer.windows.close_window, window)
