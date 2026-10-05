"""Media tools: play (Spotify / YouTube / exact links), transport controls, what's playing, YouTube search."""

from __future__ import annotations

from typing import Any

from sugar.computer.youtube import parse_media_request
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

L = PermissionLevel
EVERYWHERE = frozenset({"agent", "chat", "computer"})


def register(registry: ToolRegistry, services: ToolServices) -> None:
    media = services.computer.media
    working = services.working

    def record(result, description: str | None = None) -> None:
        working.record_action("media", description or result.details, tool=result.action, ok=result.success,
                              error=result.error)

    async def play(args: dict[str, Any]) -> ToolResult:
        platform = args.get("platform") or None
        result = await media.play(args["query"], None if platform == "auto" else platform)
        record(result)
        display = None
        video = (result.data or {}).get("video")
        if video:
            display = f"[{video['title']}]({video['url']}) — {video['channel']}"
        return result.to_tool_result(display=display)

    async def play_from(args: dict[str, Any]) -> ToolResult:
        result = await media.play_from(args.get("source", "clipboard"))
        record(result)
        return result.to_tool_result()

    def control(action: str):
        async def handler(args: dict[str, Any]) -> ToolResult:
            result = await media.control(action)
            record(result, f"{action} media")
            return result.to_tool_result()

        return handler

    async def now_playing(args: dict[str, Any]) -> ToolResult:
        result = await media.current()
        return result.to_tool_result()

    async def youtube_search(args: dict[str, Any]) -> ToolResult:
        request = parse_media_request(args["query"])
        request.platform = "youtube"
        try:
            ranked = await media.search_youtube(request)
        except Exception as exc:
            return ToolResult.failure("I couldn't reach YouTube.", repr(exc))
        top = [v.to_dict() for v in ranked[:8]]
        lines = [f"{i + 1}. [{v['title']}]({v['url']}) — {v['channel']}" for i, v in enumerate(top)]
        return ToolResult(True, f"{len(ranked)} videos found.", data={"results": top},
                          display="\n".join(lines) or None, speak=False)

    registry.register(Tool(
        "media.play",
        "Play music or a video by description: a song ('Blinding Lights by The Weeknd'), an artist, "
        "'the official video for X', 'the latest MrBeast video', or 'a video about X'. Sugar searches, picks the "
        "best match, opens it and checks it's the right one. platform: youtube, spotify, or auto (Spotify for "
        "music when set up, YouTube for videos). Never pass a URL you made up.",
        params(["query"], query={"type": "string"},
               platform={"type": "string", "enum": ["auto", "youtube", "spotify"], "default": "auto"}),
        play, L.NON_DESTRUCTIVE, 60, EVERYWHERE,
        describe=lambda a: f"play {a.get('query')}" + (f" on {a['platform']}" if a.get("platform") not in (None, "auto") else ""),
    ))
    registry.register(Tool("media.play_from", "Play the YouTube link on the clipboard, or resume the video in the "
                           "active tab ('this').",
                           params(source={"type": "string", "enum": ["clipboard", "this"], "default": "clipboard"}),
                           play_from, L.NON_DESTRUCTIVE, 40, EVERYWHERE))
    for action, description in (("pause", "Pause whatever is playing (Spotify, YouTube, any player)."),
                                ("resume", "Resume playback."), ("next", "Skip to the next track or video."),
                                ("previous", "Go back to the previous track."), ("stop", "Stop playback.")):
        registry.register(Tool(f"media.{action}", description, params(), control(action), L.NON_DESTRUCTIVE, 15,
                               EVERYWHERE))
    registry.register(Tool("media.now_playing", "Say what's playing right now (any app).", params(), now_playing,
                           L.READ, 10, EVERYWHERE))
    registry.register(Tool("youtube.search", "Search YouTube and return ranked videos (title, channel, link) "
                           "without opening anything.", params(["query"], query={"type": "string"}),
                           youtube_search, L.READ, 20, frozenset({"agent", "computer"})))
