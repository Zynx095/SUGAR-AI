"""Browser-control latency on the real default browser, in a window this script opens.

    python benchmarks/browser_control.py

New window, new tab, navigation (typed address → page title), tab and
address-bar reads through UI Automation, tab switching, back, and tab close.
Closes its window at the end and never touches other windows or tabs.
"""

from __future__ import annotations

import asyncio
import time

from _common import ms_since, row, save, table
from live_helpers import ForegroundKeeper, busy_reason, make_computer

PAGES = ["https://example.com", "https://www.wikipedia.org", "https://github.com"]


async def main() -> None:
    computer = make_computer()
    reason = busy_reason(computer)
    if reason:
        computer.stop()
        raise SystemExit(f"not touching the desktop: {reason}")
    keeper = ForegroundKeeper(computer)
    browser = computer.browser
    rows: list[dict] = []
    start = time.perf_counter()
    opened = await computer.run(browser.open, None, None, new_window=True)
    rows.append(row("Open a new browser window", [ms_since(start)], opened.details))
    hwnd = opened.data.get("hwnd")
    try:
        new_tab, navigate, tabs_read, address_read, switch, back, close = [], [], [], [], [], [], []
        verified = 0
        for url in PAGES:
            start = time.perf_counter()
            result = await computer.run(browser.new_tab)
            new_tab.append(ms_since(start))
            start = time.perf_counter()
            result = await computer.run(browser.navigate, url)
            navigate.append(ms_since(start))
            verified += result.verified
        rows.append(row("New tab (Ctrl+T, verified by tab count)", new_tab))
        rows.append(row("Navigate (type address → title + host verified)", navigate,
                        f"{verified}/{len(PAGES)} verified; includes page load"))
        for _ in range(10):
            start = time.perf_counter()
            await computer.run(computer.backend.tabs, hwnd)
            tabs_read.append(ms_since(start))
            start = time.perf_counter()
            await computer.run(computer.backend.address, hwnd)
            address_read.append(ms_since(start))
        rows.append(row("Read tab list (UI Automation, cached strip)", tabs_read))
        rows.append(row("Read address bar (UI Automation)", address_read))
        for direction in ("previous", "previous", "next"):
            start = time.perf_counter()
            await computer.run(browser.switch_tab, direction=direction)
            switch.append(ms_since(start))
        rows.append(row("Switch tab (Ctrl+Tab / Ctrl+Shift+Tab)", switch))
        await computer.run(browser.navigate, "https://example.org", new_tab=False)
        start = time.perf_counter()
        result = await computer.run(browser.back)
        back.append(ms_since(start))
        rows.append(row("Back (Alt+Left, title change verified)", back, f"verified={result.verified}"))
        while computer.backend.is_window(hwnd) and len(computer.backend.tabs(hwnd)) > 1:
            start = time.perf_counter()
            result = await computer.run(browser.close_tab)
            close.append(ms_since(start))
        rows.append(row("Close tab (UIA close button, verified)", close))
    finally:
        if hwnd and computer.backend.is_window(hwnd):
            await computer.run(computer.windows.close_window, computer.backend.get_window(hwnd))
        keeper.restore()
        computer.stop()
    print(table(rows))
    print("saved", save("browser_control", rows))


if __name__ == "__main__":
    asyncio.run(main())
