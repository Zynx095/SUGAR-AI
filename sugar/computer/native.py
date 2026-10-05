"""The real Windows backend: Win32 (ctypes), UI Automation (comtypes) and SMTC (WinRT)."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sugar.computer import win32 as w
from sugar.computer.backend import FocusInfo, KeyChord, MediaSession, TabInfo, WindowInfo
from sugar.computer.keys import EXTENDED, VK_CONTROL, VK_MENU, VK_SHIFT
from sugar.computer.uia import Accessibility

log = logging.getLogger(__name__)

# Top-level windows that are part of the shell, not applications.
SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
    "ApplicationManager_ImmersiveShellWindow", "MultitaskingViewFrame", "TaskListThumbnailWnd",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow",
    "ForegroundStaging", "Windows.Internal.Shell.TabProxyWindow", "PseudoConsoleWindow", "IME", "MSCTFIME UI",
}
_STATES = {"minimize": w.SW_MINIMIZE, "maximize": w.SW_MAXIMIZE, "restore": w.SW_RESTORE, "show": w.SW_SHOW}
_PLAYBACK = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


class WindowsDesktop:
    """Implements :class:`sugar.computer.backend.DesktopBackend` on Windows 10/11."""

    def __init__(self) -> None:
        self.own_pid = os.getpid()
        self.uia = Accessibility()
        self._names: dict[int, tuple[str, float]] = {}
        self._clipboard_owner: int | None = None
        self._elevated: bool | None = None

    # ------------------------------------------------------------------ worker-thread setup

    def thread_init(self) -> None:
        """Runs once in the sugar-desktop worker: COM (MTA) and per-monitor DPI awareness."""
        w.ole32.CoInitializeEx(None, w.COINIT_MULTITHREADED)
        if hasattr(w.user32, "SetThreadDpiAwarenessContext"):
            w.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(w.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        if "comtypes" not in sys.modules:
            # comtypes initialises COM for the thread that imports it first; make that match ours.
            sys.coinit_flags = w.COINIT_MULTITHREADED  # type: ignore[attr-defined]
            try:
                import comtypes  # noqa: F401
            finally:
                del sys.coinit_flags  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ windows

    def _process_name(self, pid: int) -> str:
        cached = self._names.get(pid)
        if cached and time.monotonic() - cached[1] < 30:
            return cached[0]
        image = w.process_image(pid)
        name = os.path.basename(image).lower() if image else ""
        self._names[pid] = (name, time.monotonic())
        return name

    def _real_process(self, hwnd: int, pid: int, name: str) -> tuple[int, str]:
        """Store apps run inside ApplicationFrameHost; report the app's own process instead."""
        if name != "applicationframehost.exe":
            return pid, name
        for child in w.enum_child_windows(hwnd):
            child_pid, _ = w.window_pid(child)
            if child_pid and child_pid != pid:
                return child_pid, self._process_name(child_pid)
        return pid, name

    def _info(self, hwnd: int) -> WindowInfo | None:
        if not hwnd or not w.user32.IsWindow(hwnd):
            return None
        pid, _thread = w.window_pid(hwnd)
        name = self._process_name(pid)
        pid, name = self._real_process(hwnd, pid, name)
        return WindowInfo(
            hwnd=int(hwnd), title=w.window_text(hwnd), pid=pid, process=name, class_name=w.class_name(hwnd),
            rect=w.frame_rect(hwnd), minimized=bool(w.user32.IsIconic(hwnd)), maximized=bool(w.user32.IsZoomed(hwnd)),
        )

    @staticmethod
    def _is_app_window(hwnd: int) -> bool:
        if not w.user32.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
            return False
        if not w.window_text(hwnd) or w.class_name(hwnd) in SHELL_CLASSES:
            return False
        exstyle = w.user32.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)
        if exstyle & w.WS_EX_APPWINDOW:
            return True
        if exstyle & (w.WS_EX_TOOLWINDOW | w.WS_EX_NOACTIVATE):
            return False
        return not w.user32.GetWindow(hwnd, w.GW_OWNER)

    def list_windows(self) -> list[WindowInfo]:
        windows = []
        for hwnd in w.enum_windows():  # z-order, topmost first
            if hwnd and self._is_app_window(hwnd):
                info = self._info(hwnd)
                if info is not None:
                    windows.append(info)
        return windows

    def get_window(self, hwnd: int) -> WindowInfo | None:
        return self._info(hwnd)

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(w.user32.IsWindow(hwnd)) and bool(w.user32.IsWindowVisible(hwnd))

    def foreground(self) -> int:
        return int(w.user32.GetForegroundWindow() or 0)

    def activate(self, hwnd: int) -> bool:
        """Bring a window to the foreground, past Windows' focus-stealing rules.

        Plain ``SetForegroundWindow`` fails from a background process. A
        zero-distance mouse event makes this process the source of the last
        input, which lifts the lock without sending a key to any app; thread
        input attachment and (last) an Alt tap are the fallbacks.
        """
        if not self.is_window(hwnd):
            return False
        if w.user32.IsIconic(hwnd):
            w.user32.ShowWindow(hwnd, w.SW_RESTORE)
        if self.foreground() == hwnd:
            return True

        def nudge() -> None:
            w.send_inputs(w.mouse_input(flags=w.MOUSEEVENTF_MOVE))
            w.user32.SetForegroundWindow(hwnd)

        def attach() -> None:
            current = w.user32.GetForegroundWindow()
            _, fg_thread = w.window_pid(current) if current else (0, 0)
            me = w.kernel32.GetCurrentThreadId()
            attached = bool(fg_thread) and fg_thread != me and w.user32.AttachThreadInput(me, fg_thread, True)
            try:
                w.user32.BringWindowToTop(hwnd)
                w.user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    w.user32.AttachThreadInput(me, fg_thread, False)

        def alt_tap() -> None:
            w.send_inputs(w.keyboard_input(VK_MENU))
            w.send_inputs(w.keyboard_input(VK_MENU, flags=w.KEYEVENTF_KEYUP))
            w.user32.SetForegroundWindow(hwnd)

        for attempt in (lambda: w.user32.SetForegroundWindow(hwnd), nudge, attach, alt_tap):
            attempt()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                if self.foreground() == hwnd:
                    return True
                time.sleep(0.01)
        return False

    def set_state(self, hwnd: int, state: str) -> None:
        w.user32.ShowWindow(hwnd, _STATES[state])

    def close_window(self, hwnd: int) -> None:
        self.uia.forget(hwnd)
        w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)

    def set_rect(self, hwnd: int, left: int, top: int, width: int, height: int) -> None:
        if w.user32.IsZoomed(hwnd) or w.user32.IsIconic(hwnd):
            w.user32.ShowWindow(hwnd, w.SW_RESTORE)
        # Compensate for the invisible resize borders so the *visible* frame lands where asked.
        outer, frame = w.window_rect(hwnd), w.frame_rect(hwnd)
        dl, dt = frame[0] - outer[0], frame[1] - outer[1]
        dr, db = outer[2] - frame[2], outer[3] - frame[3]
        w.user32.SetWindowPos(hwnd, None, left - dl, top - dt, width + dl + dr, height + dt + db,
                              w.SWP_NOZORDER | w.SWP_NOACTIVATE)

    def work_area(self, hwnd: int) -> tuple[int, int, int, int]:
        monitor = w.user32.MonitorFromWindow(hwnd, w.MONITOR_DEFAULTTONEAREST)
        info = w.MONITORINFO()
        info.cbSize = ctypes.sizeof(w.MONITORINFO)
        if not w.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return (0, 0, 1920, 1080)
        rect = info.rcWork
        return (rect.left, rect.top, rect.right, rect.bottom)

    def owned_windows(self, hwnd: int) -> list[WindowInfo]:
        owned = []
        for candidate in w.enum_windows():
            if candidate and w.user32.IsWindowVisible(candidate) and w.user32.GetWindow(candidate, w.GW_OWNER) == hwnd:
                info = self._info(candidate)
                if info is not None:
                    owned.append(info)
        return owned

    def processes(self, image: str) -> list[int]:
        import psutil

        image = image.lower()
        found = []
        for process in psutil.process_iter(["name", "pid"]):
            name = (process.info.get("name") or "").lower()
            if name == image:
                found.append(process.info["pid"])
        return found

    def kill_process(self, pid: int) -> bool:
        import psutil

        try:
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=3)
            return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.AccessDenied, psutil.TimeoutExpired):
            return False

    def is_elevated(self, pid: int) -> bool | None:
        handle = w.kernel32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return w.token_elevated(handle)
        finally:
            w.kernel32.CloseHandle(handle)

    def self_elevated(self) -> bool:
        if self._elevated is None:
            self._elevated = bool(w.token_elevated(w.kernel32.GetCurrentProcess()))
        return self._elevated

    def start(self, target: str) -> None:
        os.startfile(target)  # noqa: S606 — shell:AppsFolder ids, URIs and files from the app catalog

    def spawn(self, argv: list[str]) -> int:
        process = subprocess.Popen(argv, close_fds=True)  # noqa: S603 — catalog executables
        return process.pid

    def watch_foreground(self, callback: Callable[[int], None]) -> Callable[[], None]:
        """Calls ``callback(hwnd)`` on every foreground change (WinEvent hook on its own thread)."""
        ready = threading.Event()
        state: dict[str, Any] = {}

        def on_event(_hook, _event, hwnd, _obj, _child, _thread, _time) -> None:
            if hwnd:
                try:
                    callback(int(hwnd))
                except Exception:
                    log.exception("foreground callback failed")

        def pump() -> None:
            proc = w.WINEVENTPROC(on_event)
            hook = w.user32.SetWinEventHook(w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_FOREGROUND, None, proc,
                                            0, 0, w.WINEVENT_OUTOFCONTEXT)
            state["thread"] = w.kernel32.GetCurrentThreadId()
            state["proc"] = proc  # keep the callback alive
            ready.set()
            message = wt.MSG()
            while w.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                w.user32.TranslateMessage(ctypes.byref(message))
                w.user32.DispatchMessageW(ctypes.byref(message))
            if hook:
                w.user32.UnhookWinEvent(hook)

        thread = threading.Thread(target=pump, name="sugar-foreground", daemon=True)
        thread.start()
        ready.wait(2.0)

        def stop() -> None:
            if state.get("thread"):
                w.user32.PostThreadMessageW(state["thread"], w.WM_QUIT, 0, 0)
            thread.join(timeout=2.0)

        return stop

    # ------------------------------------------------------------------ input

    def key(self, vk: int, *, up: bool) -> bool:
        scan = w.user32.MapVirtualKeyExW(vk, w.MAPVK_VK_TO_VSC, None)
        flags = (w.KEYEVENTF_KEYUP if up else 0) | (w.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
        return w.send_inputs(w.keyboard_input(vk, scan, flags)) == 1

    def unicode(self, code_unit: int, *, up: bool) -> bool:
        flags = w.KEYEVENTF_UNICODE | (w.KEYEVENTF_KEYUP if up else 0)
        return w.send_inputs(w.keyboard_input(0, code_unit, flags)) == 1

    def chord_for(self, char: str, hwnd: int) -> KeyChord | None:
        """The key (and Shift/Ctrl/Alt) that types ``char`` on the target window's keyboard layout."""
        if len(char) != 1 or ord(char) > 0xFFFF:
            return None  # outside the BMP (emoji): no key produces it, and it doesn't fit a WCHAR
        _, thread = w.window_pid(hwnd) if hwnd else (0, 0)
        layout = w.user32.GetKeyboardLayout(thread)
        result = w.user32.VkKeyScanExW(char, layout)
        if result == -1:
            return None
        vk, shift_state = result & 0xFF, (result >> 8) & 0xFF
        if shift_state & ~0x07:
            return None  # Hankaku and other states this engine doesn't drive
        if w.user32.MapVirtualKeyExW(vk, w.MAPVK_VK_TO_CHAR, layout) & 0x80000000:
            return None  # a dead key (e.g. ' on US-International) would combine with the next letter
        modifiers = tuple(m for bit, m in ((1, VK_SHIFT), (2, VK_CONTROL), (4, VK_MENU)) if shift_state & bit)
        return KeyChord(vk, modifiers)

    def caps_lock(self) -> bool:
        return bool(w.user32.GetKeyState(0x14) & 1)

    def mouse_move(self, x: int, y: int) -> None:
        w.user32.SetCursorPos(int(x), int(y))

    def mouse_button(self, button: str, *, up: bool) -> None:
        down_flag, up_flag = w.MOUSE_BUTTONS[button]
        w.send_inputs(w.mouse_input(flags=up_flag if up else down_flag))

    def mouse_wheel(self, clicks: int, *, horizontal: bool = False) -> None:
        flag = w.MOUSEEVENTF_HWHEEL if horizontal else w.MOUSEEVENTF_WHEEL
        w.send_inputs(w.mouse_input(data=clicks * w.WHEEL_DELTA, flags=flag))

    def cursor(self) -> tuple[int, int]:
        point = wt.POINT()
        w.user32.GetCursorPos(ctypes.byref(point))
        return (point.x, point.y)

    # ------------------------------------------------------------------ clipboard

    def _owner(self) -> int:
        # EmptyClipboard with a NULL owner makes SetClipboardData fail, so own the clipboard with a
        # hidden window created on this (the worker) thread.
        if self._clipboard_owner is None:
            self._clipboard_owner = int(w.user32.CreateWindowExW(0, "STATIC", "sugar-clipboard", 0, 0, 0, 0, 0,
                                                                 None, None, None, None) or 0)
        return self._clipboard_owner

    def _open_clipboard(self) -> bool:
        owner = self._owner()
        for _ in range(20):  # another app may hold it for a moment
            if w.user32.OpenClipboard(owner):
                return True
            time.sleep(0.02)
        return False

    def clipboard_text(self) -> str | None:
        if not self._open_clipboard():
            return None
        try:
            if not w.user32.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
                return None
            handle = w.user32.GetClipboardData(w.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = w.kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer) if pointer else None
            finally:
                w.kernel32.GlobalUnlock(handle)
        finally:
            w.user32.CloseClipboard()

    @staticmethod
    def _global(data: bytes):
        handle = w.kernel32.GlobalAlloc(w.GMEM_MOVEABLE, max(1, len(data)))
        if not handle:
            return None
        pointer = w.kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        w.kernel32.GlobalUnlock(handle)
        return handle

    def set_clipboard_text(self, text: str) -> bool:
        if not self._open_clipboard():
            return False
        try:
            w.user32.EmptyClipboard()
            handle = self._global(text.encode("utf-16-le") + b"\x00\x00")
            if handle is None or not w.user32.SetClipboardData(w.CF_UNICODETEXT, handle):
                if handle is not None:
                    w.kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            w.user32.CloseClipboard()

    def clipboard_snapshot(self) -> list[tuple[int, bytes]]:
        """Every memory-backed clipboard format (text, HTML, images as DIB, file lists…)."""
        if not self._open_clipboard():
            return []
        saved: list[tuple[int, bytes]] = []
        try:
            fmt = w.user32.EnumClipboardFormats(0)
            while fmt:
                if fmt not in w.GDI_CLIPBOARD_FORMATS and not 0x0300 <= fmt <= 0x03FF:
                    handle = w.user32.GetClipboardData(fmt)
                    size = w.kernel32.GlobalSize(handle) if handle else 0
                    if handle and 0 < size <= 64 * 1024 * 1024:
                        pointer = w.kernel32.GlobalLock(handle)
                        if pointer:
                            try:
                                saved.append((fmt, ctypes.string_at(pointer, size)))
                            finally:
                                w.kernel32.GlobalUnlock(handle)
                fmt = w.user32.EnumClipboardFormats(fmt)
        finally:
            w.user32.CloseClipboard()
        return saved

    def restore_clipboard(self, snapshot: list[tuple[int, bytes]]) -> None:
        if not self._open_clipboard():
            return
        try:
            w.user32.EmptyClipboard()
            for fmt, data in snapshot:
                handle = self._global(data)
                if handle is not None and not w.user32.SetClipboardData(fmt, handle):
                    w.kernel32.GlobalFree(handle)
        finally:
            w.user32.CloseClipboard()

    # ------------------------------------------------------------------ accessibility

    def tabs(self, hwnd: int) -> list[TabInfo]:
        return self.uia.tabs(hwnd)

    def select_tab(self, hwnd: int, index: int) -> bool:
        return self.uia.select_tab(hwnd, index)

    def close_tab(self, hwnd: int, index: int) -> bool:
        return self.uia.close_tab(hwnd, index)

    def address(self, hwnd: int) -> str | None:
        return self.uia.address(hwnd)

    def focused(self) -> FocusInfo | None:
        return self.uia.focused()

    def focused_text(self, limit: int = 20000) -> str | None:
        return self.uia.focused_text(limit)

    def invoke(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> str | None:
        return self.uia.invoke(hwnd, name, kinds)

    def element_center(self, hwnd: int, name: str, kinds: tuple[str, ...] = ()) -> tuple[int, int] | None:
        return self.uia.element_center(hwnd, name, kinds)

    def buttons(self, hwnd: int) -> list[str]:
        names = self.uia.buttons(hwnd)
        for owned in self.owned_windows(hwnd):
            names.extend(n for n in self.uia.buttons(owned.hwnd) if n not in names)
        return names

    def page_text(self, hwnd: int, limit: int = 20000) -> str | None:
        return self.uia.page_text(hwnd, limit)

    def describe_ui(self, hwnd: int, limit: int = 150) -> list[dict[str, Any]]:
        return self.uia.describe_ui(hwnd, limit)

    # ------------------------------------------------------------------ media sessions (SMTC)

    async def _sessions(self) -> list[tuple[Any, MediaSession]]:
        import winrt.windows.foundation.collections  # noqa: F401  (makes the session list iterable)
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

        manager = await Manager.request_async()
        current = manager.get_current_session()
        current_id = current.source_app_user_model_id if current else None
        result = []
        for session in manager.get_sessions():
            try:
                properties = await session.try_get_media_properties_async()
                info = session.get_playback_info()
                status = _PLAYBACK.get(int(info.playback_status), "unknown")
                controls = info.controls
                result.append((session, MediaSession(
                    app_id=session.source_app_user_model_id or "", title=properties.title or "",
                    artist=properties.artist or "", status=status,
                    is_current=session.source_app_user_model_id == current_id,
                    can_next=bool(controls.is_next_enabled) if controls else False,
                )))
            except Exception:
                log.debug("media session unreadable", exc_info=True)
        return result

    def media_sessions(self) -> list[MediaSession]:
        try:
            return [info for _, info in asyncio.run(self._sessions())]
        except ImportError:
            return []

    def media_command(self, app_id: str, action: str) -> bool:
        async def run() -> bool:
            for session, info in await self._sessions():
                if info.app_id != app_id:
                    continue
                method = {"play": session.try_play_async, "pause": session.try_pause_async,
                          "toggle": session.try_toggle_play_pause_async, "next": session.try_skip_next_async,
                          "previous": session.try_skip_previous_async, "stop": session.try_stop_async}[action]
                return bool(await method())
            return False

        try:
            return asyncio.run(run())
        except ImportError:
            return False

    # ------------------------------------------------------------------ screen

    def screenshot(self, path: Path, hwnd: int | None = None) -> Path:
        from PIL import ImageGrab

        path.parent.mkdir(parents=True, exist_ok=True)
        if hwnd:
            image = ImageGrab.grab(bbox=w.frame_rect(hwnd), all_screens=True)
        else:
            image = ImageGrab.grab(all_screens=True)
        image.save(path)
        return path
