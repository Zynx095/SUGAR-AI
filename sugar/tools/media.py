"""Music: Spotify Web API with media-key fallback.

Preserves (and extends) the old Spotify features — current song, pause,
resume, next, previous, play a song by name — and keeps using the existing
OAuth token cache (``.cache`` in the project root), so no re-login is needed.
When the API is unavailable (no credentials, no active device, offline),
play/pause/next/previous fall back to the keyboard media keys, which control
whatever player is active.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sugar.config.settings import ROOT_DIR, Settings
from sugar.tools.registry import PermissionLevel, Tool, ToolRegistry, ToolResult, params
from sugar.tools.services import ToolServices

log = logging.getLogger(__name__)


class SpotifyController:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None
        self._error: str | None = None

    def _get(self):
        if self._client is not None:
            return self._client
        cfg = self._settings.spotify
        client_id = self._settings.secret(cfg.client_id_env)
        client_secret = self._settings.secret(cfg.client_secret_env)
        if not cfg.enabled or not client_id or not client_secret:
            self._error = "Spotify isn't configured"
            return None
        import spotipy
        from spotipy.cache_handler import CacheFileHandler
        from spotipy.oauth2 import SpotifyOAuth

        self._client = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=cfg.redirect_uri,
                scope=cfg.scope,
                cache_handler=CacheFileHandler(cache_path=str(ROOT_DIR / ".cache")),
                open_browser=False,
            ),
            requests_timeout=6,
            retries=1,
        )
        return self._client

    @property
    def available(self) -> bool:
        return self._get() is not None

    # Blocking calls — run via asyncio.to_thread.
    def current(self) -> dict[str, Any] | None:
        client = self._get()
        return client.current_playback() if client else None

    def _device_id(self) -> str | None:
        client = self._get()
        devices = (client.devices() or {}).get("devices", []) if client else []
        active = [d for d in devices if d.get("is_active")]
        chosen = (active or devices or [None])[0]
        return chosen.get("id") if chosen else None

    def play_query(self, query: str) -> str:
        client = self._get()
        if client is None:
            raise RuntimeError(self._error or "Spotify unavailable")
        kind = "track"
        lowered = query.lower()
        for word in ("playlist", "album", "artist"):
            if re.search(rf"\b(?:the )?{word}\b", lowered):
                kind = word
                lowered = re.sub(rf"\b(?:the )?{word}\b", "", lowered).strip()
        search = lowered
        match = re.match(r"(?P<title>.+?)\s+by\s+(?P<artist>.+)$", lowered)
        if kind == "track" and match:
            search = f"track:{match.group('title')} artist:{match.group('artist')}"
        results = client.search(q=search, limit=1, type=kind)
        items = (results.get(f"{kind}s") or {}).get("items") or []
        if not items and search != lowered:
            items = (client.search(q=lowered, limit=1, type=kind).get(f"{kind}s") or {}).get("items") or []
        if not items:
            raise LookupError(f"couldn't find {query} on Spotify")
        item = items[0]
        device = self._device_id()
        if device is None:
            raise RuntimeError("no Spotify device is active — open Spotify first")
        if kind == "track":
            client.start_playback(device_id=device, uris=[item["uri"]])
            artist = item["artists"][0]["name"] if item.get("artists") else ""
            return f"{item['name']} by {artist}".strip()
        client.start_playback(device_id=device, context_uri=item["uri"])
        return item["name"]

    def command(self, action: str) -> None:
        client = self._get()
        if client is None:
            raise RuntimeError(self._error or "Spotify unavailable")
        device = self._device_id()
        if action == "pause":
            client.pause_playback(device_id=device)
        elif action == "resume":
            client.start_playback(device_id=device)
        elif action == "next":
            client.next_track(device_id=device)
        elif action == "previous":
            client.previous_track(device_id=device)


_MEDIA_KEYS = {"pause": "playpause", "resume": "playpause", "next": "nexttrack", "previous": "prevtrack"}


async def _media_key(action: str) -> None:
    import pyautogui

    await asyncio.to_thread(pyautogui.press, _MEDIA_KEYS[action])


def register(registry: ToolRegistry, services: ToolServices) -> None:
    spotify = services.spotify

    async def now_playing(args: dict[str, Any]) -> ToolResult:
        try:
            playback = await asyncio.to_thread(spotify.current)
        except Exception as exc:
            return ToolResult.failure("I can't reach Spotify right now.", str(exc))
        if not playback or not playback.get("item"):
            return ToolResult(True, "Nothing's playing on Spotify.", data={"playing": False})
        item = playback["item"]
        artist = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        verb = "This is" if playback.get("is_playing") else "Paused on"
        return ToolResult(True, f"{verb} {item['name']} by {artist}.",
                          data={"track": item["name"], "artist": artist, "playing": playback.get("is_playing")})

    async def control(action: str) -> ToolResult:
        spoken = {"pause": "Paused.", "resume": "Playing.", "next": "Skipped.", "previous": "Going back."}[action]
        try:
            await asyncio.to_thread(spotify.command, action)
            return ToolResult(True, spoken, data={"via": "spotify"})
        except Exception as exc:
            log.info("Spotify %s failed (%s); using media keys", action, exc)
        try:
            await _media_key(action)
            return ToolResult(True, spoken, data={"via": "media keys"})
        except Exception as exc:
            return ToolResult.failure(f"I couldn't {action} the music.", str(exc))

    async def play(args: dict[str, Any]) -> ToolResult:
        query = args["query"]
        try:
            title = await asyncio.to_thread(spotify.play_query, query)
        except LookupError:
            return ToolResult.failure(f"I couldn't find {query} on Spotify.")
        except Exception as exc:
            message = str(exc)
            if "device" in message.lower():
                return ToolResult.failure("Spotify isn't open on any device. Open Spotify and ask again.", message)
            return ToolResult.failure("I couldn't play that on Spotify.", message)
        services.working.record_action("media", f"playing {title}", tool="media.play", args={"query": query})
        return ToolResult(True, f"Playing {title}.", data={"playing": title})

    registry.register(Tool("media.now_playing", "Say what song is currently playing on Spotify.", params(),
                           now_playing, PermissionLevel.READ, 10, frozenset({"agent", "chat"})))
    for action, description in (("pause", "Pause music playback."), ("resume", "Resume music playback."),
                                ("next", "Skip to the next track."), ("previous", "Go back to the previous track.")):
        async def handler(args: dict[str, Any], _action: str = action) -> ToolResult:
            result = await control(_action)
            services.working.record_action("media", f"{_action} music", tool=f"media.{_action}", ok=result.ok)
            return result

        registry.register(Tool(f"media.{action}", description, params(), handler,
                               PermissionLevel.NON_DESTRUCTIVE, 10, frozenset({"agent", "chat"})))
    registry.register(Tool(
        "media.play", "Play a song, artist, album or playlist on Spotify by name.",
        params(["query"], query={"type": "string", "description": "e.g. 'Master of Puppets by Metallica'"}),
        play, PermissionLevel.NON_DESTRUCTIVE, 15, frozenset({"agent", "chat"}),
        describe=lambda a: f"play {a.get('query')} on Spotify",
    ))
