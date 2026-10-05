"""Media: one controller for YouTube, Spotify and whatever else is playing.

* **Play** — a request is parsed (title, artist, platform, "latest",
  "official"), then: Spotify through its Web API for music when it's set up,
  YouTube for videos (search → rank → open in the user's browser → verify the
  page → confirm playback), or an exact link when one was given.
* **Pause / resume / skip / stop / what's playing** — through Windows' media
  sessions (SMTC), which see Spotify, browser tabs and other players with
  their titles and state, so "pause" stops what is actually playing rather
  than whichever app last grabbed the media keys. The Spotify Web API and
  media keys are fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sugar.computer.backend import MediaSession
from sugar.computer.context import SearchContext
from sugar.computer.keys import VK_MEDIA_NEXT, VK_MEDIA_PLAY_PAUSE, VK_MEDIA_PREV
from sugar.computer.results import ComputerActionResult
from sugar.computer.spotify import NoActiveDevice, SpotifyController, SpotifyUnavailable
from sugar.computer.youtube import (
    MediaRequest,
    VideoResult,
    YouTubeSearch,
    extract_video_id,
    parse_media_request,
    rank,
    relevance,
    title_similarity,
    verify_page,
    youtube_page_title,
)

if TYPE_CHECKING:
    from sugar.computer.engine import ComputerControl

log = logging.getLogger(__name__)
R = ComputerActionResult

BROWSER_SESSION_HINTS = ("brave", "chrome", "msedge", "edge", "firefox", "opera", "vivaldi")


def platform_of(session: MediaSession) -> str:
    app_id = session.app_id.lower()
    if "spotify" in app_id:
        return "spotify"
    if any(hint in app_id for hint in BROWSER_SESSION_HINTS):
        return "browser"
    return "other"


def _app_label(session: MediaSession) -> str:
    app_id = session.app_id.lower()
    for hint, label in (("spotify", "Spotify"), ("brave", "Brave"), ("chrome", "Chrome"), ("msedge", "Edge"),
                        ("firefox", "Firefox"), ("vlc", "VLC"), ("music", "Media Player")):
        if hint in app_id:
            return label
    return session.app_id.split("!")[-1].split("_")[0] or "a player"


class MediaController:
    def __init__(self, computer: ComputerControl, spotify: SpotifyController, youtube: YouTubeSearch, *,
                 music_platform: str = "auto", pause_other_media: bool = True) -> None:
        self._c = computer
        self.spotify = spotify
        self.youtube = youtube
        self.music_platform = music_platform
        self.pause_other_media = pause_other_media
        self.active_session: str | None = None  # SMTC app id of what Sugar last started
        self.active_platform: str | None = None
        self.last_video: VideoResult | None = None

    # ------------------------------------------------------------------ sessions

    async def sessions(self) -> list[MediaSession]:
        try:
            return await self._c.run(self._c.backend.media_sessions)
        except Exception:
            log.debug("media sessions unavailable", exc_info=True)
            return []

    async def _command(self, session: MediaSession, action: str) -> bool:
        try:
            return bool(await self._c.run(self._c.backend.media_command, session.app_id, action))
        except Exception:
            return False

    async def _pause_others(self, keep: str) -> list[str]:
        """Pause players other than ``keep`` ("spotify" or "browser") so two things don't play at once."""
        if not self.pause_other_media:
            return []
        paused = []
        for session in await self.sessions():
            if session.status == "playing" and platform_of(session) != keep:
                if await self._command(session, "pause"):
                    paused.append(_app_label(session))
        return paused

    # ------------------------------------------------------------------ play

    def choose_platform(self, request: MediaRequest) -> str:
        if request.platform:
            return request.platform
        if request.kind == "video" or request.latest:
            return "youtube"
        if self.music_platform in ("spotify", "youtube"):
            return self.music_platform
        return "spotify" if self.spotify.configured else "youtube"

    async def play(self, query: str, platform: str | None = None) -> R:
        request = parse_media_request(query)
        if platform in ("youtube", "spotify"):
            request.platform = platform
        if request.url:
            return await self.play_url(request.url)
        chosen = self.choose_platform(request)
        if chosen == "spotify":
            result = await self.play_spotify(request)
            if result.success or not result.data.get("fallback_ok"):
                return result
            youtube = await self.play_youtube(request)
            youtube.details = f"{result.details} {youtube.details}".strip()
            return youtube
        return await self.play_youtube(request)

    async def play_spotify(self, request: MediaRequest) -> R:
        query = request.describe() if request.title else request.query
        try:
            title = await asyncio.to_thread(self.spotify.play_query, query)
        except SpotifyUnavailable:
            return R.fail("media.play", "spotify", "Spotify isn't set up, so I'll use YouTube.", fallback_ok=True)
        except LookupError:
            return R.fail("media.play", "spotify", f"I couldn't find {query} on Spotify.", fallback_ok=True)
        except NoActiveDevice:
            title = await self._start_spotify_then_play(query)
            if title is None:
                return R.fail("media.play", "spotify", "Spotify didn't come online, so I'll use YouTube.",
                              fallback_ok=True)
        except Exception as exc:  # network, auth
            log.info("Spotify play failed: %s", exc)
            return R.fail("media.play", "spotify", "Spotify didn't respond, so I'll use YouTube.", str(exc),
                          fallback_ok=True)
        await self._pause_others("spotify")
        self.active_platform = "spotify"
        verified = False
        for _ in range(8):
            await asyncio.sleep(0.35)
            for session in await self.sessions():
                if platform_of(session) == "spotify" and session.status == "playing":
                    self.active_session = session.app_id
                    verified = title_similarity(session.title, title.split(" by ")[0]) >= 0.5 or verified
            if verified:
                break
        return R(True, "media.play", "spotify", f"Playing {title} on Spotify.", verified, None,
                 {"platform": "spotify", "title": title})

    async def _start_spotify_then_play(self, query: str) -> str | None:
        opened = await self._c.run(self._c.apps.open, "spotify")
        if not opened.success:
            return None
        for _ in range(16):  # the desktop app needs a few seconds to register as a Connect device
            await asyncio.sleep(0.5)
            try:
                return await asyncio.to_thread(self.spotify.play_query, query)
            except NoActiveDevice:
                continue
            except Exception:
                return None
        return None

    async def search_youtube(self, request: MediaRequest) -> list[VideoResult]:
        results = await self.youtube.search(request.query, latest=request.latest)
        ranked = rank(results, request)
        self._c.context.known_video_ids.update(v.video_id for v in ranked)
        return ranked

    async def play_youtube(self, request: MediaRequest, ranked: list[VideoResult] | None = None) -> R:
        started = time.perf_counter()
        if ranked is None:
            try:
                ranked = await self.search_youtube(request)
            except Exception as exc:
                return R.fail("media.play", "youtube", "I couldn't reach YouTube to search.", str(exc))
        search_ms = int((time.perf_counter() - started) * 1000)
        candidates = [v for v in ranked if relevance(request, v.title, v.channel) >= 0.34][:3] or ranked[:1]
        if not candidates:
            return R.fail("media.play", "youtube", f"I couldn't find {request.describe()} on YouTube.")
        top = candidates[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        confident = relevance(request, top.title, top.channel) >= 0.75 and (
            runner_up is None or top.score - runner_up.score >= 4)
        paused = await self._pause_others("browser")
        attempts: list[str] = []
        for video in candidates:
            opened = await self._c.run(self._c.browser.open_video, video.url)
            if not opened.success:
                return opened
            hwnd = opened.data.get("hwnd")
            check = await self._verify(request, video, hwnd)
            if check.ok:
                break
            attempts.append(check.reason)
            log.warning("YouTube result rejected: %s", check.reason)
        else:
            return R.fail("media.play", "youtube", "YouTube opened something that didn't match what you asked for, "
                          "so I stopped.", "; ".join(attempts), attempts=attempts)
        self.last_video = video
        self.active_platform = "youtube"
        playing = await self._ensure_playing(video)
        total_ms = int((time.perf_counter() - started) * 1000)
        note = "" if confident else "I found several matches; this is the closest. "
        status = "Playing" if playing else "Opened"
        details = f"{note}{status} {check.page_title or video.title} on YouTube."
        if paused:
            details += f" Paused {' and '.join(paused)}."
        return R(True, "media.play", "youtube", details, True, None, {
            "platform": "youtube", "video": video.to_dict(), "playing": playing, "confident": confident,
            "rejected": attempts, "search_ms": search_ms, "total_ms": total_ms, "backend": self.youtube.last_backend,
            "candidates": [v.to_dict() for v in ranked[:5]],
        })

    async def _verify(self, request: MediaRequest, video: VideoResult, hwnd: int | None, timeout_s: float = 12.0):
        """Poll the tab until it shows the chosen video. A wrong video id fails at once; a wrong title that
        stays put for 2.5 s fails too (titles lag a moment behind navigation, so one mismatch isn't enough)."""
        deadline = time.monotonic() + timeout_s
        check = None
        mismatch_since: float | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            state = await self._c.run(self._c.browser.tab_state, hwnd)
            if state is None:
                continue
            title, address = state
            check = verify_page(request, video, title, address)
            if check.ok or (check.video_id and check.video_id != video.video_id):
                return check
            if check.page_title and check.page_title.lower() not in ("youtube", "new tab"):
                mismatch_since = mismatch_since or time.monotonic()
                if time.monotonic() - mismatch_since > 2.5:
                    return check
            else:
                mismatch_since = None
        return check or verify_page(request, video, "", None)

    async def _ensure_playing(self, video: VideoResult, timeout_s: float = 6.0) -> bool:
        deadline = time.monotonic() + timeout_s
        nudged = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            for session in await self.sessions():
                if platform_of(session) != "browser" or title_similarity(session.title, video.title) < 0.5:
                    continue
                self.active_session = session.app_id
                if session.status == "playing":
                    return True
                if session.status == "paused" and not nudged:
                    nudged = await self._command(session, "play")
        return False

    async def play_url(self, url: str) -> R:
        video_id = extract_video_id(url)
        if video_id is None:
            opened = await self._c.run(self._c.browser.navigate, url, new_tab=True)
            return opened
        self._c.context.known_video_ids.add(video_id)
        video = VideoResult(video_id=video_id, title="")
        opened = await self._c.run(self._c.browser.open_video, video.url)
        if not opened.success:
            return opened
        hwnd = opened.data.get("hwnd")
        title = ""
        for _ in range(30):
            await asyncio.sleep(0.3)
            state = await self._c.run(self._c.browser.tab_state, hwnd)
            if state is None:
                continue
            window_title, address = state
            shown = extract_video_id(address or "")
            title = youtube_page_title(window_title)
            if shown == video_id and title and title.lower() not in ("youtube", "new tab"):
                break
        else:
            return R(True, "media.play", "youtube", "I opened the link, but the video hasn't loaded yet.", False,
                     None, {"video_id": video_id})
        video.title = title
        self.last_video = video
        self.active_platform = "youtube"
        await self._pause_others("browser")
        playing = await self._ensure_playing(video)
        return R.ok("media.play", "youtube", f"{'Playing' if playing else 'Opened'} {title}.", video_id=video_id,
                    playing=playing, title=title)

    async def play_from(self, source: str) -> R:
        """"Play the video in my clipboard" / "play this video" (the active tab)."""
        if source == "clipboard":
            text = await self._c.run(self._c.backend.clipboard_text)
            video_id = extract_video_id(text or "")
            if video_id is None:
                return R.fail("media.play", "clipboard", "There's no YouTube link on your clipboard.")
            return await self.play_url(f"https://www.youtube.com/watch?v={video_id}")
        current = await self._c.run(self._c.browser.current)
        address = (current.data or {}).get("url") if current.success else None
        if address and extract_video_id(address):
            return await self.control("resume")
        return R.fail("media.play", "this", "The page in front isn't a YouTube video.")

    # ------------------------------------------------------------------ transport controls

    def _pick(self, sessions: list[MediaSession], action: str) -> MediaSession | None:
        def first(predicate) -> MediaSession | None:
            return next((s for s in sessions if predicate(s)), None)

        mine = first(lambda s: s.app_id == self.active_session)
        if action in ("pause", "stop"):
            if mine is not None and mine.status == "playing":
                return mine
            return first(lambda s: s.is_current and s.status == "playing") or first(lambda s: s.status == "playing")
        if action == "resume":
            if mine is not None and mine.status in ("paused", "stopped"):
                return mine
            return first(lambda s: s.is_current and s.status in ("paused", "stopped")) or \
                first(lambda s: s.status == "paused")
        if mine is not None and mine.status == "playing":
            return mine
        return first(lambda s: s.status == "playing") or first(lambda s: s.is_current)

    async def control(self, action: str) -> R:
        spoken = {"pause": "Paused", "stop": "Stopped", "resume": "Playing", "next": "Skipped",
                  "previous": "Going back"}[action]
        sessions = await self.sessions()
        target = self._pick(sessions, action)
        if target is None and action in ("pause", "stop") and sessions:
            return R.ok(f"media.{action}", None, "Nothing's playing.", verified=True)
        if target is not None:
            command = {"pause": "pause", "stop": "pause", "resume": "play", "next": "next", "previous": "previous"}[action]
            if action == "next" and platform_of(target) == "browser" and not target.can_next:
                return await self._browser_next(target)
            if await self._command(target, command):
                verified = await self._confirm(target, action)
                self.active_session = target.app_id
                label = _app_label(target)
                what = f" {target.title}" if action == "resume" and target.title else ""
                return R(True, f"media.{action}", label, f"{spoken}{what}." if what else f"{spoken} {label}.",
                         verified, None, {"via": "media session", "app": target.app_id})
        if self.spotify.configured and (self.active_platform in (None, "spotify")):
            try:
                await asyncio.to_thread(self.spotify.command, "resume" if action == "resume" else
                                        "pause" if action in ("pause", "stop") else action)
                return R.ok(f"media.{action}", "spotify", f"{spoken}.", verified=False, via="spotify api")
            except Exception as exc:
                log.info("Spotify %s failed (%s); using media keys", action, exc)
        key = {"pause": VK_MEDIA_PLAY_PAUSE, "stop": VK_MEDIA_PLAY_PAUSE, "resume": VK_MEDIA_PLAY_PAUSE,
               "next": VK_MEDIA_NEXT, "previous": VK_MEDIA_PREV}[action]
        try:
            await self._c.run(self._c.keyboard.chord, [], key)
        except Exception as exc:
            return R.fail(f"media.{action}", None, "I couldn't reach any media player.", str(exc))
        return R(True, f"media.{action}", None, f"{spoken}.", False, None, {"via": "media keys"})

    async def _confirm(self, target: MediaSession, action: str) -> bool:
        expected = {"pause": "paused", "stop": "paused", "resume": "playing"}.get(action)
        for _ in range(6):
            await asyncio.sleep(0.25)
            fresh = next((s for s in await self.sessions() if s.app_id == target.app_id), None)
            if fresh is None:
                return action in ("pause", "stop")
            if expected and fresh.status == expected:
                return True
            if not expected and fresh.title != target.title:
                return True
        return False

    async def _browser_next(self, target: MediaSession) -> R:
        result = await self._c.run(self._c.browser.youtube_next)
        if result.success:
            self.active_session = target.app_id
        return result

    async def current(self) -> R:
        sessions = await self.sessions()
        playing = next((s for s in sessions if s.status == "playing" and s.app_id == self.active_session), None) or \
            next((s for s in sessions if s.status == "playing"), None)
        if playing is None:
            current = next((s for s in sessions if s.is_current), None)
            if current is not None and current.title:
                return R.ok("media.current", _app_label(current), f"Paused on {current.title}"
                            f"{' by ' + current.artist if current.artist else ''} in {_app_label(current)}.",
                            title=current.title, artist=current.artist, playing=False)
            if self.spotify.configured:
                try:
                    playback = await asyncio.to_thread(self.spotify.current)
                    if playback and playback.get("item"):
                        item = playback["item"]
                        artist = ", ".join(a["name"] for a in item.get("artists", [])[:2])
                        return R.ok("media.current", "spotify", f"Paused on {item['name']} by {artist}.",
                                    title=item["name"], artist=artist, playing=bool(playback.get("is_playing")))
                except Exception:
                    pass
            return R.ok("media.current", None, "Nothing's playing.", playing=False)
        by = f" by {playing.artist}" if playing.artist and platform_of(playing) != "browser" else ""
        where = _app_label(playing)
        return R.ok("media.current", where, f"This is {playing.title}{by}, playing in {where}.",
                    title=playing.title, artist=playing.artist, playing=True, app=playing.app_id)

    # ------------------------------------------------------------------ search-result follow-ups

    async def open_result(self, rank_index: int | None = None) -> R:
        """"Open the most relevant result" / "the second video" after a search."""
        search = self._c.context.last_search
        if search is None:
            return R.fail("browser.open_result", None, "There's no search to pick a result from.")
        if search.engine == "youtube":
            request = parse_media_request(search.query)
            request.platform = "youtube"
            ranked = search.results
            if ranked is None:
                try:
                    ranked = await self.search_youtube(request)
                except Exception as exc:
                    return R.fail("browser.open_result", "youtube", "I couldn't reach YouTube.", str(exc))
                search.results = ranked
            if not ranked:
                return R.fail("browser.open_result", "youtube", "That search had no videos.")
            if rank_index is not None:
                if not 0 <= rank_index < len(ranked) and rank_index != -1:
                    return R.fail("browser.open_result", "youtube", "There aren't that many results.")
                by_position = sorted(ranked, key=lambda v: v.position)
                pick = by_position[rank_index]
                return await self.play_youtube(request, [pick])
            return await self.play_youtube(request, ranked)
        return await self._open_web_result(search, rank_index)

    async def _open_web_result(self, search: SearchContext, rank_index: int | None) -> R:
        from sugar.tools.web import duckduckgo, github_search

        try:
            if search.engine == "github":
                links = await github_search(search.query)
            else:
                site = {"stack overflow": "stackoverflow.com", "reddit": "reddit.com", "wikipedia": "wikipedia.org",
                        "amazon": "amazon.in", "npm": "npmjs.com", "pypi": "pypi.org"}.get(search.engine)
                links = await duckduckgo(f"{search.query} site:{site}" if site else search.query, 8)
        except Exception as exc:
            return R.fail("browser.open_result", search.engine, "I couldn't look up the results.", str(exc))
        if not links:
            return R.fail("browser.open_result", search.engine, "I couldn't find any results to open.")
        index = rank_index if rank_index is not None else 0
        if not -len(links) <= index < len(links):
            return R.fail("browser.open_result", search.engine, "There aren't that many results.")
        link = links[index]
        opened = await self._c.run(self._c.browser.navigate, link["url"], new_tab=False)
        if opened.success:
            opened.action = "browser.open_result"
            opened.details = f"Opened {link['title'] or link['url']}."
        return opened
