"""YouTube resolution: search → rank → (optionally) open → verify → playing.

    python benchmarks/media_resolution.py            # network only: search and ranking
    python benchmarks/media_resolution.py --live     # also opens each video in a new browser window

The live run acts only on a browser window it opens itself, pauses each
video once it's confirmed playing, leaves other players alone, and closes
the window at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from _common import ms_since, row, save, table

from sugar.computer.youtube import YouTubeSearch, parse_media_request, rank

REQUESTS = [
    ("Play Blinding Lights by The Weeknd on YouTube", "the weeknd"),
    ("Play Never Gonna Give You Up by Rick Astley on YouTube", "rick astley"),
    ("Play Minecraft on YouTube", None),
    ("Open the video about RTX 5050 benchmarks", None),
    ("Play the official music video for Starboy by The Weeknd on YouTube", "the weeknd"),
    ("Play the latest MrBeast video", "mrbeast"),
]


async def offline(repeat: int) -> tuple[list[dict], list[dict]]:
    search = YouTubeSearch()
    search_ms: list[float] = []
    rank_ms: list[float] = []
    picks = []
    for text, expected_channel in REQUESTS:
        request = parse_media_request(text)
        for attempt in range(repeat):
            start = time.perf_counter()
            results = await search.search(request.query, latest=request.latest)
            search_ms.append(ms_since(start))
            start = time.perf_counter()
            ranked = rank(results, request)
            rank_ms.append(ms_since(start))
            if attempt == 0:
                top = ranked[0] if ranked else None
                picks.append({"request": text, "query": request.query, "results": len(results),
                              "top": top.to_dict() if top else None,
                              "channel_ok": bool(top and (expected_channel is None or
                                                          top.channel.lower().startswith(expected_channel)))})
    rows = [row("YouTube search (results page, network)", search_ms, f"{len(REQUESTS)} queries × {repeat}"),
            row("Ranking", rank_ms, "parse + score + sort")]
    return rows, picks


async def live() -> list[dict]:
    from live_helpers import ForegroundKeeper, busy_reason, make_computer

    computer = make_computer()
    reason = busy_reason(computer)
    if reason:
        computer.stop()
        raise SystemExit(f"not touching the desktop: {reason}")
    keeper = ForegroundKeeper(computer)
    computer.media.pause_other_media = False
    opened = await computer.run(computer.browser.open, None, None, new_window=True)
    hwnd = opened.data.get("hwnd")
    totals, verify, notes = [], [], []
    try:
        for text, _channel in REQUESTS[:4]:
            start = time.perf_counter()
            result = await computer.media.play(text, "youtube")
            totals.append(ms_since(start))
            if result.success:
                verify.append(result.data["total_ms"] - result.data["search_ms"])
            notes.append(f"{text[:40]}… → {result.data.get('video', {}).get('id')} verified={result.verified} "
                         f"playing={result.data.get('playing')}")
            await computer.media.control("pause")
    finally:
        if hwnd and computer.backend.is_window(hwnd):
            window = computer.backend.get_window(hwnd)
            await computer.run(computer.windows.close_window, window)
        keeper.restore()
        computer.stop()
    return [row("Request → playing (search, open, verify, playback)", totals, "; ".join(notes)),
            row("Open + verify page + confirm playback", verify, "after search")]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    rows, picks = await offline(args.repeat)
    if args.live:
        rows += await live()
    for pick in picks:
        top = pick["top"] or {}
        print(f"- {pick['request']} → {top.get('title')} ({top.get('channel')}) [{top.get('id')}] "
              f"channel_ok={pick['channel_ok']}")
    print()
    print(table(rows))
    distinct = len({(p["top"] or {}).get("id") for p in picks})
    print(f"\nDistinct videos for {len(picks)} requests: {distinct}")
    print("saved", save("media_resolution", rows, {"picks": picks, "distinct": distinct}))


if __name__ == "__main__":
    asyncio.run(main())
