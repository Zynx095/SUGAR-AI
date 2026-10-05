"""Spotify Web API client (search and start playback; transport controls).

Keeps using the existing OAuth token cache (``.cache`` in the project root),
so no re-login is needed. Pausing, skipping and "what's playing" normally go
through Windows' media sessions instead (see ``media.py``); the Web API is
what lets Sugar *find* a song and start it on a device.
"""

from __future__ import annotations

import re
from typing import Any

from sugar.config.settings import ROOT_DIR, Settings


class SpotifyUnavailable(RuntimeError):
    pass


class NoActiveDevice(RuntimeError):
    pass


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
    def configured(self) -> bool:
        cfg = self._settings.spotify
        return bool(cfg.enabled and self._settings.secret(cfg.client_id_env) and self._settings.secret(cfg.client_secret_env))

    @property
    def available(self) -> bool:
        return self._get() is not None

    # Blocking calls — run via asyncio.to_thread.
    def current(self) -> dict[str, Any] | None:
        client = self._get()
        return client.current_playback() if client else None

    def devices(self) -> list[dict[str, Any]]:
        client = self._get()
        return (client.devices() or {}).get("devices", []) if client else []

    def _device_id(self) -> str | None:
        devices = self.devices()
        active = [d for d in devices if d.get("is_active")]
        computer = [d for d in devices if (d.get("type") or "").lower() == "computer"]
        chosen = (active or computer or devices or [None])[0]
        return chosen.get("id") if chosen else None

    def play_query(self, query: str) -> str:
        client = self._get()
        if client is None:
            raise SpotifyUnavailable(self._error or "Spotify unavailable")
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
            raise NoActiveDevice("no Spotify device is active — open Spotify first")
        if kind == "track":
            client.start_playback(device_id=device, uris=[item["uri"]])
            artist = item["artists"][0]["name"] if item.get("artists") else ""
            return f"{item['name']} by {artist}".strip()
        client.start_playback(device_id=device, context_uri=item["uri"])
        return item["name"]

    def command(self, action: str) -> None:
        client = self._get()
        if client is None:
            raise SpotifyUnavailable(self._error or "Spotify unavailable")
        device = self._device_id()
        if action == "pause":
            client.pause_playback(device_id=device)
        elif action == "resume":
            client.start_playback(device_id=device)
        elif action == "next":
            client.next_track(device_id=device)
        elif action == "previous":
            client.previous_track(device_id=device)
