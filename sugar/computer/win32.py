"""ctypes declarations for the Win32 calls computer control needs.

Every function gets explicit ``argtypes``/``restype`` so 64-bit handles and
pointers are never truncated to ``int``. Importing this module on a
non-Windows system is harmless; the DLL handles are simply ``None``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

IS_WINDOWS = sys.platform == "win32"

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HANDLE = wt.HANDLE

# ---------------------------------------------------------------- constants
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x00040000, 0x08000000
GW_OWNER = 4
SW_RESTORE, SW_MINIMIZE, SW_MAXIMIZE, SW_SHOW = 9, 6, 3, 5
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
WM_CLOSE, WM_QUIT = 0x0010, 0x0012
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSE_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL, WHEEL_DELTA = 0x0800, 0x1000, 120
MAPVK_VK_TO_VSC, MAPVK_VK_TO_CHAR = 0, 2
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0x0000
PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE = 0x1000, 0x0001
TOKEN_QUERY, TOKEN_ELEVATION_CLASS = 0x0008, 20
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
MONITOR_DEFAULTTONEAREST = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
COINIT_MULTITHREADED = 0x0
# Clipboard formats whose handles are GDI objects, not HGLOBAL memory (can't be copied byte-wise).
GDI_CLIPBOARD_FORMATS = {2, 3, 9, 14, 0x0080, 0x0082, 0x0083, 0x008E}


# ---------------------------------------------------------------- structures
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

user32 = kernel32 = dwmapi = advapi32 = ole32 = None

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")

    def _proto(dll, name: str, restype, *argtypes) -> None:
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)

    _proto(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
    _proto(user32, "EnumChildWindows", wt.BOOL, wt.HWND, WNDENUMPROC, wt.LPARAM)
    _proto(user32, "IsWindow", wt.BOOL, wt.HWND)
    _proto(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
    _proto(user32, "IsIconic", wt.BOOL, wt.HWND)
    _proto(user32, "IsZoomed", wt.BOOL, wt.HWND)
    _proto(user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
    _proto(user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _proto(user32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _proto(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
    _proto(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, wt.HWND, ctypes.c_int)
    _proto(user32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
    _proto(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    _proto(user32, "GetForegroundWindow", wt.HWND)
    _proto(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
    _proto(user32, "BringWindowToTop", wt.BOOL, wt.HWND)
    _proto(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
    _proto(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, wt.UINT)
    _proto(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _proto(user32, "AttachThreadInput", wt.BOOL, wt.DWORD, wt.DWORD, wt.BOOL)
    _proto(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _proto(user32, "GetKeyState", ctypes.c_short, ctypes.c_int)
    _proto(user32, "VkKeyScanExW", ctypes.c_short, wt.WCHAR, ctypes.c_void_p)
    _proto(user32, "MapVirtualKeyExW", wt.UINT, wt.UINT, wt.UINT, ctypes.c_void_p)
    _proto(user32, "GetKeyboardLayout", ctypes.c_void_p, wt.DWORD)
    _proto(user32, "MonitorFromWindow", HANDLE, wt.HWND, wt.DWORD)
    _proto(user32, "GetMonitorInfoW", wt.BOOL, HANDLE, ctypes.POINTER(MONITORINFO))
    _proto(user32, "SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
    _proto(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
    _proto(user32, "OpenClipboard", wt.BOOL, wt.HWND)
    _proto(user32, "CloseClipboard", wt.BOOL)
    _proto(user32, "EmptyClipboard", wt.BOOL)
    _proto(user32, "GetClipboardData", HANDLE, wt.UINT)
    _proto(user32, "SetClipboardData", HANDLE, wt.UINT, HANDLE)
    _proto(user32, "EnumClipboardFormats", wt.UINT, wt.UINT)
    _proto(user32, "IsClipboardFormatAvailable", wt.BOOL, wt.UINT)
    _proto(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
    _proto(user32, "SetWinEventHook", HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD,
           wt.DWORD, wt.DWORD)
    _proto(user32, "UnhookWinEvent", wt.BOOL, HANDLE)
    _proto(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
    _proto(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
    _proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wt.MSG))
    _proto(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
    try:
        _proto(user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p, ctypes.c_void_p)
    except AttributeError:  # Windows < 10 1607
        pass

    _proto(kernel32, "GetCurrentThreadId", wt.DWORD)
    _proto(kernel32, "OpenProcess", HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    _proto(kernel32, "CloseHandle", wt.BOOL, HANDLE)
    _proto(kernel32, "QueryFullProcessImageNameW", wt.BOOL, HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    _proto(kernel32, "GlobalAlloc", HANDLE, wt.UINT, ctypes.c_size_t)
    _proto(kernel32, "GlobalLock", ctypes.c_void_p, HANDLE)
    _proto(kernel32, "GlobalUnlock", wt.BOOL, HANDLE)
    _proto(kernel32, "GlobalSize", ctypes.c_size_t, HANDLE)
    _proto(kernel32, "GlobalFree", HANDLE, HANDLE)
    _proto(kernel32, "TerminateProcess", wt.BOOL, HANDLE, wt.UINT)
    _proto(kernel32, "GetCurrentProcess", HANDLE)

    _proto(advapi32, "OpenProcessToken", wt.BOOL, HANDLE, wt.DWORD, ctypes.POINTER(HANDLE))
    _proto(advapi32, "GetTokenInformation", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
           ctypes.POINTER(wt.DWORD))

    _proto(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
    _proto(ole32, "CoInitializeEx", ctypes.c_long, ctypes.c_void_p, wt.DWORD)


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_pid(hwnd: int) -> tuple[int, int]:
    """(process id, thread id) of a window."""
    pid = wt.DWORD()
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, thread


def is_cloaked(hwnd: int) -> bool:
    value = wt.DWORD()
    result = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return result == 0 and value.value != 0


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The visible frame (without the invisible resize borders DWM adds on Windows 10/11)."""
    rect = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        return window_rect(hwnd)
    return (rect.left, rect.top, rect.right, rect.bottom)


def enum_windows() -> list[int]:
    handles: list[int] = []

    def collect(hwnd, _lparam):
        handles.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(collect), 0)
    return handles


def enum_child_windows(hwnd: int) -> list[int]:
    handles: list[int] = []

    def collect(child, _lparam):
        handles.append(child)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(collect), 0)
    return handles


def process_image(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def token_elevated(process_handle) -> bool | None:
    token = HANDLE()
    if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevation = TOKEN_ELEVATION()
        size = wt.DWORD()
        if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevation),
                                            ctypes.sizeof(elevation), ctypes.byref(size)):
            return None
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def send_inputs(*inputs: INPUT) -> int:
    array = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def keyboard_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def mouse_input(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> INPUT:
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return event
