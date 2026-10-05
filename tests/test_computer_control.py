"""Computer control against an in-memory desktop: typing, windows, apps, browser tabs, dialogs, media."""

from __future__ import annotations

import json

import pytest
from conftest import run
from fake_desktop import FakeDesktop, FakeWindow, browser_window

from sugar.computer.apps import AppCatalog
from sugar.computer.backend import MediaSession
from sugar.computer.browser import BrowserInfo, BrowserRegistry, clean_tab_name, page_title, search_url
from sugar.computer.engine import ComputerControl
from sugar.computer.keys import KeyParseError, describe_combo, parse_combo
from sugar.computer.spotify import SpotifyController
from sugar.config.settings import load_settings

NOTEPAD_AUMID = "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"


def notepad(text: str = "", title: str = "Untitled - Notepad") -> FakeWindow:
    window = FakeWindow(title, "notepad.exe", 300, class_name="Notepad", kind="editor", text=text)
    window.tabs = []
    return window


def make(tmp_path, desktop: FakeDesktop | None = None) -> tuple[ComputerControl, FakeDesktop]:
    settings = load_settings(config_file=tmp_path / "none.yaml", use_env=False, data_dir=tmp_path / "data")
    settings.computer.typing_interval_ms = 0
    settings.computer.key_gap_ms = 0
    settings.computer.close_timeout_s = 0.6
    settings.computer.launch_timeout_s = 1.0
    cache = tmp_path / "apps.json"
    cache.write_text(json.dumps({"ts": 1, "apps": [
        {"name": "Notepad", "target": NOTEPAD_AUMID, "kind": "aumid", "processes": ["notepad.exe"]},
        {"name": "Brave", "target": "Brave", "kind": "aumid", "processes": ["brave.exe"]},
        {"name": "Discord", "target": "com.squirrel.Discord.Discord", "kind": "aumid", "processes": ["Discord.exe"]},
    ]}), encoding="utf-8")
    desktop = desktop or FakeDesktop()
    desktop.launchers["shell:AppsFolder\\" + NOTEPAD_AUMID] = notepad
    desktop.launchers[r"C:\brave.exe"] = browser_window
    desktop.launchers["shell:AppsFolder\\Brave"] = browser_window
    browsers = BrowserRegistry([BrowserInfo("brave", "Brave", r"C:\brave.exe", "brave.exe"),
                                BrowserInfo("edge", "Microsoft Edge", r"C:\msedge.exe", "msedge.exe")], "brave")
    computer = ComputerControl(settings, AppCatalog(cache), SpotifyController(settings), backend=desktop,
                               browsers=browsers)
    computer.context.start()
    return computer, desktop


# ---------------------------------------------------------------------------------------- keys

def test_parse_combo_handles_spoken_and_written_shortcuts():
    assert parse_combo("ctrl+shift+z") == [0x11, 0x10, 0x5A]
    assert parse_combo("control shift t") == [0x11, 0x10, 0x54]
    assert parse_combo("alt tab") == [0x12, 0x09]
    assert parse_combo("the enter key") == [0x0D]
    assert parse_combo("f five") == [0x74]
    assert parse_combo(["ctrl", "s"]) == [0x11, 0x53]
    assert describe_combo(parse_combo("ctrl+shift+t")) == "Ctrl+Shift+T"
    for bad in ("ctrl+banana", "a b"):
        with pytest.raises(KeyParseError):
            parse_combo(bad)


# ---------------------------------------------------------------------------------------- typing

SAMPLE = 'Hello Sugar\nThis is a test.\n\nPython:\nprint("Hello World")'
SYMBOLS = '@#$%^&*()_+{}[]:"<>?/\\| café ✓ 😀'


@pytest.mark.parametrize("text", [SAMPLE, SYMBOLS, "Hello, this is Sugar AI.", "MiXeD CaSe 123"])
def test_realtime_typing_is_exact(tmp_path, text):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    result = computer.keyboard.type_text(text, mode="realtime")
    assert result.success and result.verified, result.details
    assert editor.text == text
    assert result.data["mode"] == "realtime"
    assert not desktop.held, "a modifier was left pressed"


def test_caps_lock_does_not_flip_case(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    desktop.caps = True
    assert computer.keyboard.type_text("Hello World", mode="realtime").verified
    assert editor.text == "Hello World"


def test_long_text_is_pasted_and_the_clipboard_restored(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    desktop.clipboard = [(13, "user's clipboard".encode("utf-16-le") + b"\x00\x00"), (49999, b"<rich format>")]
    long_text = "word " * 200
    result = computer.keyboard.type_text(long_text)
    assert result.data["mode"] == "paste" and result.verified
    assert editor.text == long_text.rstrip("\n")
    assert desktop.clipboard_text() == "user's clipboard"
    assert (49999, b"<rich format>") in desktop.clipboard


def test_typing_targets_the_app_not_sugars_own_window(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    sugar_ui = desktop.add(FakeWindow("Sugar", "python.exe", desktop.own_pid, kind="other"))
    assert desktop.foreground() == sugar_ui.hwnd
    result = computer.keyboard.type_text("hi", mode="realtime")
    assert result.success and editor.text == "hi"
    assert desktop.foreground() == editor.hwnd


def test_typing_stops_when_the_target_loses_focus(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    chat = desktop.add(FakeWindow("#general - Discord", "discord.exe", 77, kind="editor"), focus=False)
    original_key = desktop.key

    def key_then_steal(vk, *, up):
        done = original_key(vk, up=up)
        if up and len(editor.text) == 3:
            desktop._focus(chat.hwnd)  # the user clicked into another app mid-sentence
        return done

    desktop.key = key_then_steal
    result = computer.keyboard.type_text("hello world", mode="realtime", target=editor.hwnd)
    assert not result.success and "lost focus" in result.details
    assert editor.text == "hel" and chat.text == ""


def test_elevated_target_is_reported_not_silently_ignored(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    desktop.elevated_pids.add(editor.pid)
    result = computer.keyboard.type_text("x")
    assert not result.success and "administrator" in result.details


def test_editing_commands(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("line one\nline two\n"))
    result = computer.keyboard.edit("delete_last_line")
    assert result.verified and editor.text == "line one"
    assert computer.keyboard.edit("clear_all").verified and editor.text == ""


def test_press_reports_the_shortcut_and_releases_modifiers(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.add(browser_window(tabs=[("Docs", "docs.example.com")]))
    result = computer.keyboard.press("ctrl+t")
    assert result.success and result.details == "Pressed Ctrl+T."
    assert len(desktop.windows[desktop.fg].tabs) == 2
    assert not desktop.held


def test_save_detects_save_as_dialog_and_saved_files(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("draft"))
    editor.unsaved = True
    result = computer.keyboard.save()
    assert result.data.get("waiting_for") == "save_dialog"
    saved = desktop.add(notepad("notes", title="notes.txt - Notepad"))
    saved.file_backed, saved.unsaved = True, True
    assert computer.keyboard.save().verified


# ---------------------------------------------------------------------------------------- windows

def test_window_lookup_and_references(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    browser = desktop.add(browser_window(tabs=[("Blinding Lights - YouTube", "youtube.com/watch?v=x")]))
    assert computer.windows.find("notepad")[0].hwnd == editor.hwnd
    assert computer.windows.find("youtube")[0].hwnd == browser.hwnd  # by title
    assert computer.windows.resolve("this").hwnd == browser.hwnd
    assert computer.windows.resolve("previous").hwnd == editor.hwnd
    computer.windows.focus(editor.hwnd)
    assert computer.context.resolve("that").hwnd == editor.hwnd


def test_focus_failure_is_reported(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    desktop.add(browser_window())
    desktop.refuse_focus.add(editor.hwnd)
    result = computer.windows.focus("notepad")
    assert not result.success and "wouldn't let me" in result.details


def test_minimize_maximize_restore_are_verified(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    assert computer.set_state("notepad", "minimize").verified and editor.minimized
    assert computer.set_state("notepad", "maximize").verified and editor.maximized
    assert computer.set_state("notepad", "restore").verified and not editor.maximized


def test_snap_left_half(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad())
    result = computer.windows.snap("notepad", "left")
    assert result.verified and editor.rect == (0, 0, 960, 1040)


def test_close_this_closes_the_active_window(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.add(browser_window())
    editor = desktop.add(notepad("saved text"))
    result = computer.close("this")
    assert result.success and result.verified and editor.hwnd not in desktop.windows


def test_close_with_unsaved_changes_asks_and_answers(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("draft"))
    editor.unsaved = True
    result = computer.close("notepad")
    assert result.success and not result.verified and result.data["waiting_for"] == "save_prompt"
    assert "unsaved changes" in result.details and editor.hwnd in desktop.windows
    assert computer.context.pending_dialog is not None
    answered = computer.windows.answer_dialog("discard")
    assert answered.success and answered.verified and editor.hwnd not in desktop.windows
    assert computer.context.pending_dialog is None


def test_close_this_in_tabbed_notepad_closes_only_the_document(tmp_path):
    from fake_desktop import Tab

    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("the user's notes"))
    editor.tabs = [Tab("notes.txt", ""), Tab("Untitled", "")]
    result = computer.close("this")
    assert result.success and result.verified and editor.hwnd in desktop.windows
    assert [t.title for t in editor.tabs] == ["notes.txt"]


def test_unsaved_tab_prompt_is_answered_without_closing_other_tabs(tmp_path):
    from fake_desktop import Tab

    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("dictated text"))
    editor.tabs = [Tab("notes.txt", ""), Tab("Untitled", "")]
    editor.unsaved = True
    asked = computer.close("this")
    assert asked.data.get("waiting_for") == "save_prompt" and "unsaved changes" in asked.details
    answered = computer.windows.answer_dialog("discard")
    assert answered.success and answered.verified
    assert editor.hwnd in desktop.windows and [t.title for t in editor.tabs] == ["notes.txt"]


def test_window_list_summary(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.add(notepad())
    desktop.add(browser_window())
    result = computer.windows.summary()
    assert "Notepad" in result.details and "Brave" in result.details


# ---------------------------------------------------------------------------------------- applications

def test_open_launches_waits_for_the_window_and_focuses_it(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.add(browser_window())
    result = computer.apps.open("notepad")
    assert result.success and result.verified and result.details == "Notepad's open."
    front = desktop.windows[desktop.foreground()]
    assert front.process == "notepad.exe"
    assert computer.context.last_opened.hwnd == front.hwnd


def test_open_running_notepad_starts_a_fresh_tab_instead_of_a_copy(tmp_path):
    computer, desktop = make(tmp_path)
    editor = desktop.add(notepad("the user's own notes"))
    desktop.add(browser_window())
    result = computer.apps.open("notepad")
    assert result.success and "new Notepad tab" in result.details
    assert editor.text == "" and desktop.launched == []


def test_open_missing_browser_uses_the_default_and_says_so(tmp_path):
    computer, desktop = make(tmp_path)
    result = computer.apps.open("chrome")
    assert result.success and result.details.startswith("Chrome isn't installed, so I used Brave.")


def test_force_close_kills_only_that_app(tmp_path):
    computer, desktop = make(tmp_path)
    chat = desktop.add(FakeWindow("Discord", "discord.exe", 77, kind="other"))
    editor = desktop.add(notepad())
    result = computer.apps.force_close("discord")
    assert result.success and chat.hwnd not in desktop.windows and editor.hwnd in desktop.windows
    assert desktop.killed == [77]


# ---------------------------------------------------------------------------------------- browser

def test_tab_name_and_title_cleanup():
    assert clean_tab_name("Create Exam Question Bank - High memory usage - 80.5 MB") == "Create Exam Question Bank"
    assert clean_tab_name("Lofi - YouTube - Audio playing") == "Lofi - YouTube"
    assert clean_tab_name("notes.txt. Modified.") == "notes.txt"
    assert page_title("Blinding Lights - YouTube - Brave") == "Blinding Lights - YouTube"
    assert page_title("Inbox - Personal - Microsoft\u200b Edge") == "Inbox"
    assert search_url("youtube", "blinding lights") == "https://www.youtube.com/results?search_query=blinding+lights"


def test_new_tab_navigate_and_close_tab(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("Docs", "docs.example.com")]))
    assert computer.browser.new_tab().verified and len(browser.tabs) == 2
    result = computer.browser.navigate("github.com", new_tab=False, timeout_s=1)
    assert result.success and browser.tabs[1].url == "github.com"
    closed = computer.browser.close_tab()
    assert closed.verified and [t.url for t in browser.tabs] == ["docs.example.com"]


def test_close_named_tab_without_switching(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("Blinding Lights - YouTube", "youtube.com/watch?v=4NRXx6U8ABQ"),
                                               ("Docs", "docs.example.com")]))
    browser.selected = 1
    result = computer.close("youtube")  # no app is called YouTube → the tab
    assert result.verified and [t.title for t in browser.tabs] == ["Docs"]


def test_close_this_in_a_browser_closes_the_tab_not_the_browser(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("A", "a.com"), ("B", "b.com")]))
    computer.close("this")
    assert browser.hwnd in desktop.windows and len(browser.tabs) == 1
    computer.close("this window")
    assert browser.hwnd not in desktop.windows


def test_closing_the_last_tab_closes_the_window(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("A", "a.com")]))
    assert computer.browser.close_tab().verified and browser.hwnd not in desktop.windows


def test_back_forward_reopen_and_switch(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("A", "a.com"), ("B", "b.com")]))
    browser.selected = 0
    computer.browser.navigate("c.com", timeout_s=1)
    assert computer.browser.back().verified and browser.tabs[0].url == "a.com"
    assert computer.browser.forward().verified and browser.tabs[0].url == "c.com"
    assert computer.browser.switch_tab(direction="next").success and browser.selected == 1
    assert computer.browser.switch_tab(match="c").success and browser.selected == 0
    computer.browser.close_tab()
    assert computer.browser.reopen_tab().verified and len(browser.tabs) == 2


def test_search_reuses_a_blank_tab_and_remembers_the_search(tmp_path):
    computer, desktop = make(tmp_path)
    browser = desktop.add(browser_window(tabs=[("Docs", "docs.example.com"), ("New Tab", "brave://newtab")]))
    browser.selected = 1
    result = computer.browser.search("blinding lights", "youtube")
    assert result.success and len(browser.tabs) == 2
    assert browser.tabs[1].url.startswith("www.youtube.com/results?search_query=blinding+lights")
    assert computer.context.last_search.engine == "youtube"


def test_open_browser_when_none_is_running(tmp_path):
    computer, desktop = make(tmp_path)
    result = computer.browser.open(url="https://example.com")
    assert result.success and any("brave" in launched for launched in desktop.launched)


# ---------------------------------------------------------------------------------------- media controls

def test_pause_targets_what_is_actually_playing(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.sessions = [
        MediaSession("SpotifyAB.SpotifyMusic!Spotify", "Song", "Artist", "paused", is_current=True),
        MediaSession("Brave", "Lofi - YouTube", "Channel", "playing"),
    ]
    result = run(computer.media.control("pause"))
    assert result.success and result.verified
    assert desktop.media_log == [("Brave", "pause")]
    resume = run(computer.media.control("resume"))
    assert resume.success and desktop.media_log[-1] == ("Brave", "play")


def test_nothing_playing_is_said_plainly(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.sessions = [MediaSession("Brave", "Video", "", "paused")]
    result = run(computer.media.control("pause"))
    assert result.details == "Nothing's playing." and desktop.media_log == []


def test_now_playing_reads_the_media_session(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.sessions = [MediaSession("SpotifyAB.SpotifyMusic!Spotify", "bellyache", "Billie Eilish", "playing")]
    result = run(computer.media.current())
    assert "bellyache" in result.details and "Spotify" in result.details


def test_media_keys_are_the_last_resort(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.sessions = []
    result = run(computer.media.control("next"))
    assert result.success and not result.verified and result.data["via"] == "media keys"
    assert (0xB0, False) in desktop.key_log


# ---------------------------------------------------------------------------------------- screen

def test_screen_capture_and_click(tmp_path):
    computer, desktop = make(tmp_path)
    desktop.add(notepad())
    shot = computer.screen.capture()
    assert shot.success and shot.data["path"].endswith(".png")
    click = computer.screen.click(element="Save")
    assert click.success and ("move", 400, 300) in desktop.mouse
    assert not computer.screen.vision.available
