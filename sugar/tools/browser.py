"""Browser tools: open, navigate, tabs, search, results, reading and clicking pages.

URLs a *model* writes are treated with suspicion: a YouTube video link must
carry an id Sugar has actually seen (from a search result, the clipboard or
the user's own words). Models asked for "any video" otherwise invent IDs —
the V2 log shows ``dQw4w9WgXcQ`` twice — so the tool refuses and points the
model to ``media.play``, which searches.
"""

from __future__ import annotations

import re
from typing import Any

from sugar.computer.results import ComputerActionResult
from sugar.computer.youtube import extract_video_id
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, current_origin, params
from sugar.tools.services import ToolServices
from sugar.tools.web import UNTRUSTED, fetch_text

L = PermissionLevel
COMPUTER = frozenset({"agent", "computer"})
EVERYWHERE = frozenset({"agent", "chat", "computer"})
BROWSER = {"type": "string", "description": "brave, chrome, edge, firefox… omit for the browser in use or the default"}


def register(registry: ToolRegistry, services: ToolServices) -> None:
    computer = services.computer
    browser = computer.browser
    working = services.working

    async def run(fn, *args: Any, **kwargs: Any) -> ComputerActionResult:
        try:
            return await computer.run(fn, *args, **kwargs)
        except Exception as exc:
            return ComputerActionResult.fail(getattr(fn, "__name__", "browser"), None,
                                             "I couldn't control the browser.", repr(exc))

    def record(result: ComputerActionResult) -> None:
        working.record_action("browser", result.details, tool=result.action, ok=result.success, error=result.error)

    def guard_url(url: str) -> ToolResult | None:
        video_id = extract_video_id(url)
        if video_id and current_origin.get() == "model" and video_id not in computer.context.known_video_ids:
            return ToolResult.failure(
                "I won't open a YouTube link I haven't seen in a search.",
                "unverified YouTube video id: never write video links yourself. Use media.play with a search "
                "query (or youtube.search) and Sugar will find, open and verify the real video.")
        if re.match(r"^\s*(?:javascript|data|file|vbscript):", url, re.I):
            return ToolResult.failure("I won't open that kind of link.", "blocked URL scheme")
        return None

    async def open_browser(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.open, args.get("browser"), args.get("url"),
                           new_window=bool(args.get("new_window")), private=bool(args.get("private")))
        record(result)
        return result.to_tool_result()

    async def open_url(args: dict[str, Any]) -> ToolResult:
        url = str(args["url"]).strip()
        refused = guard_url(url)
        if refused is not None:
            return refused
        if extract_video_id(url):
            result = await computer.media.play_url(url)
        else:
            result = await run(browser.navigate, url, new_tab=bool(args.get("new_tab", True)), browser=args.get("browser"))
        record(result)
        return result.to_tool_result()

    async def new_tab(args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if url:
            refused = guard_url(url)
            if refused is not None:
                return refused
        result = await run(browser.new_tab, url, browser=args.get("browser"))
        record(result)
        return result.to_tool_result()

    async def close_tab(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.close_tab, args.get("match"), browser=args.get("browser"))
        record(result)
        return result.to_tool_result()

    async def switch_tab(args: dict[str, Any]) -> ToolResult:
        index = args.get("index")
        result = await run(browser.switch_tab, direction=args.get("direction"), index=index,
                           match=args.get("match"), browser=args.get("browser"))
        record(result)
        return result.to_tool_result()

    def simple(method_name: str):
        async def handler(args: dict[str, Any]) -> ToolResult:
            result = await run(getattr(browser, method_name), browser=args.get("browser"))
            record(result)
            return result.to_tool_result()

        return handler

    async def search(args: dict[str, Any]) -> ToolResult:
        engine = args.get("engine") or "google"
        result = await run(browser.search, args["query"], engine, browser=args.get("browser"))
        record(result)
        if result.success and result.data.get("engine") == "youtube":
            # Resolve the real results now, so "open the most relevant result" is instant and exact.
            import asyncio

            from sugar.computer.youtube import parse_media_request

            search_ctx = computer.context.last_search
            if search_ctx is not None:
                request = parse_media_request(args["query"])
                search_ctx.pending = asyncio.ensure_future(_fill(search_ctx, request))
        return result.to_tool_result()

    async def _fill(search_ctx, request) -> None:
        try:
            search_ctx.results = await computer.media.search_youtube(request)
        except Exception:
            search_ctx.results = None

    async def open_result(args: dict[str, Any]) -> ToolResult:
        search_ctx = computer.context.last_search
        if search_ctx is not None and search_ctx.pending is not None and not search_ctx.pending.done():
            try:
                await search_ctx.pending
            except Exception:
                pass
        rank = args.get("rank")
        index = None if rank in (None, "best") else {"first": 0, "second": 1, "third": 2, "fourth": 3,
                                                      "fifth": 4, "last": -1}.get(str(rank))
        if index is None and isinstance(rank, str) and rank.isdigit():
            index = int(rank) - 1
        result = await computer.media.open_result(index)
        record(result)
        return result.to_tool_result()

    async def scroll(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.scroll, args.get("direction", "down"), int(args.get("amount", 1)),
                           browser=args.get("browser"))
        return result.to_tool_result()

    async def zoom(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.zoom, args.get("how", "in"), browser=args.get("browser"))
        return result.to_tool_result()

    async def current(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.current, args.get("browser"))
        return result.to_tool_result()

    async def copy_url(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.current, args.get("browser"))
        url = (result.data or {}).get("url")
        if not result.success or not url:
            return ToolResult.failure("I can't read the page address.")
        ok = await computer.run(computer.backend.set_clipboard_text, url if "://" in url else "https://" + url)
        return ToolResult(bool(ok), "Copied the link." if ok else "The clipboard was busy.", data={"url": url})

    async def read_page(args: dict[str, Any]) -> ToolResult:
        window, address, text = await computer.run(browser.page_text)
        if window is None:
            return ToolResult.failure("No browser window is open.")
        content = None
        if address and not address.startswith(("chrome://", "brave://", "edge://", "about:")):
            url = address if "://" in address else "https://" + address
            fetched = await fetch_text(url)
            if fetched and len(fetched) > 200:
                content = fetched
        content = content or text
        if not content:
            return ToolResult.failure("I couldn't read this page.")
        return ToolResult(True, "Read the page.", data={"note": UNTRUSTED, "url": address, "text": content[:12000]})

    async def click(args: dict[str, Any]) -> ToolResult:
        result = await run(browser.click, args["text"], browser=args.get("browser"))
        record(result)
        return result.to_tool_result()

    registry.register(Tool("browser.open", "Open or switch to a web browser (optionally a specific one, a new "
                           "window, or a private window).",
                           params(browser=BROWSER, url={"type": "string"}, new_window={"type": "boolean", "default": False},
                                  private={"type": "boolean", "default": False}),
                           open_browser, L.NON_DESTRUCTIVE, 25, EVERYWHERE))
    registry.register(Tool("browser.open_url", "Open a web address in the browser (new tab by default). Never "
                           "invent links: for videos use media.play.",
                           params(["url"], url={"type": "string"}, new_tab={"type": "boolean", "default": True},
                                  browser=BROWSER),
                           open_url, L.NON_DESTRUCTIVE, 40, EVERYWHERE, describe=lambda a: f"open {a.get('url')}"))
    registry.register(Tool("browser.new_tab", "Open a new browser tab (optionally at a URL).",
                           params(url={"type": "string"}, browser=BROWSER), new_tab, L.NON_DESTRUCTIVE, 20, COMPUTER))
    registry.register(Tool("browser.close_tab", "Close the current browser tab, or the tab whose title matches "
                           "`match` (e.g. 'YouTube').", params(match={"type": "string"}, browser=BROWSER), close_tab,
                           L.NON_DESTRUCTIVE, 15, EVERYWHERE,
                           describe=lambda a: f"close the {a.get('match') or 'current'} tab"))
    registry.register(Tool("browser.switch_tab", "Switch tabs: direction next/previous, a 1-based index (-1 = last), "
                           "or a tab whose title matches.",
                           params(direction={"type": "string", "enum": ["next", "previous"]},
                                  index={"type": "integer", "minimum": -1, "maximum": 99},
                                  match={"type": "string"}, browser=BROWSER),
                           switch_tab, L.NON_DESTRUCTIVE, 15, COMPUTER))
    for name, method, description in (
        ("browser.reopen_tab", "reopen_tab", "Reopen the last closed tab."),
        ("browser.back", "back", "Go back a page."),
        ("browser.forward", "forward", "Go forward a page."),
        ("browser.reload", "reload", "Reload the page."),
        ("browser.close_window", "close_window", "Close the browser window (all its tabs)."),
    ):
        registry.register(Tool(name, description, params(browser=BROWSER), simple(method), L.NON_DESTRUCTIVE, 15,
                               COMPUTER))
    registry.register(Tool("browser.search", "Search a site in the browser and show the results: google, youtube, "
                           "github, wikipedia, stack overflow, reddit, amazon, maps, images, bing, duckduckgo…",
                           params(["query"], query={"type": "string"}, engine={"type": "string", "default": "google"},
                                  browser=BROWSER),
                           search, L.NON_DESTRUCTIVE, 30, EVERYWHERE,
                           describe=lambda a: f"search {a.get('engine') or 'the web'} for {a.get('query')}"))
    registry.register(Tool("browser.open_result", "Open a result from the last search: 'best' (most relevant), "
                           "'first', 'second', 'third', 'last' or a number.",
                           params(rank={"type": "string", "default": "best"}), open_result, L.NON_DESTRUCTIVE, 45,
                           EVERYWHERE))
    registry.register(Tool("browser.scroll", "Scroll the page down/up a number of screens, or to the top/bottom.",
                           params(direction={"type": "string", "enum": ["down", "up", "top", "bottom"], "default": "down"},
                                  amount={"type": "integer", "minimum": 1, "maximum": 20, "default": 1}, browser=BROWSER),
                           scroll, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("browser.zoom", "Zoom the page in, out, or reset.",
                           params(how={"type": "string", "enum": ["in", "out", "reset"], "default": "in"}, browser=BROWSER),
                           zoom, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("browser.current", "The current tab's title and address.", params(browser=BROWSER),
                           current, L.READ, 10, COMPUTER))
    registry.register(Tool("browser.copy_url", "Copy the current page's address to the clipboard.",
                           params(browser=BROWSER), copy_url, L.NON_DESTRUCTIVE, 10, COMPUTER))
    registry.register(Tool("browser.read_page", "Read the text of the page in the current tab (to summarise or "
                           "answer questions about it).", params(), read_page, L.READ, 30, EVERYWHERE))
    registry.register(Tool("browser.click", "Click a link or button on the current page by its visible text.",
                           params(["text"], text={"type": "string"}, browser=BROWSER), click,
                           lambda a: L.SENSITIVE if re.search(r"\b(?:buy|pay|order|delete|remove|send|submit|"
                                                              r"post|confirm|unsubscribe)\b",
                                                              str(a.get("text", "")), re.I) else L.NON_DESTRUCTIVE,
                           20, COMPUTER, describe=lambda a: f"click “{a.get('text')}” on the page"))
