"""YouTube: understanding the request, searching, ranking, and checking what opened.

Sugar never makes up a video link. A request becomes a query; the query
goes to YouTube (the Data API when ``YOUTUBE_API_KEY`` is set, otherwise the
same results page a browser gets, parsed from its embedded ``ytInitialData``
JSON); candidates are ranked on title, artist/creator, official sources,
duration and position; the chosen video's page is then checked against the
request before Sugar says it's playing.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import httpx

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_STOP = {"the", "a", "an", "by", "on", "of", "for", "to", "and", "in", "from", "with", "video", "videos", "song",
         "music", "official", "play", "youtube", "please", "me", "some", "latest", "newest", "new", "recent", "most",
         "about", "called", "titled", "ft", "feat", "featuring", "x", "vs", "audio", "clip", "track"}
_NEGATIVE = ("cover", "remix", "karaoke", "reaction", "react", "nightcore", "slowed", "sped up", "8d", "instrumental",
             "1 hour", "10 hours", "loop", "extended", "tutorial", "how to play", "piano", "guitar lesson",
             "lyrics", "lyric", "live", "concert", "fan made", "fanmade", "parody", "mashup", "bass boosted",
             "edit audio", "shorts", "#shorts")


@dataclass
class MediaRequest:
    query: str
    title: str | None = None  # song or video title
    artist: str | None = None  # artist or creator
    kind: str = "any"  # music | video | any
    platform: str | None = None  # youtube | spotify | None (let Sugar choose)
    latest: bool = False
    official: bool = False
    want_audio: bool = False
    url: str | None = None  # an exact link to open instead of searching

    def describe(self) -> str:
        if self.title and self.artist:
            return f"{self.title} by {self.artist}"
        return self.title or self.artist or self.query


@dataclass
class VideoResult:
    video_id: str
    title: str
    channel: str = ""
    duration_s: int | None = None
    views: int | None = None
    age_s: int | None = None
    verified_artist: bool = False
    verified: bool = False
    live: bool = False
    position: int = 0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def is_short(self) -> bool:
        return self.duration_s is not None and self.duration_s <= 61

    def to_dict(self) -> dict:
        return {"id": self.video_id, "title": self.title, "channel": self.channel, "duration_s": self.duration_s,
                "views": self.views, "url": self.url, "score": round(self.score, 1)}


# ---------------------------------------------------------------------------- request parsing

_URL = re.compile(r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)"
                  r"([A-Za-z0-9_-]{11})")


def extract_video_id(text: str | None) -> str | None:
    if not text:
        return None
    match = _URL.search(text)
    return match.group(1) if match else None


def parse_media_request(text: str) -> MediaRequest:
    """"Play the official music video for Starboy by The Weeknd on YouTube" → structured request."""
    raw = text.strip()
    url_id = extract_video_id(raw)
    if url_id:
        return MediaRequest(query=raw, platform="youtube", url=f"https://www.youtube.com/watch?v={url_id}", kind="video")
    t = raw.lower().strip(" .!?")
    t = re.sub(r"^(?:please\s+|can you\s+|could you\s+)*(?:play|put on|queue up|open|find|show me|pull up|watch)"
               r"(?:\s+me)?\s+", "", t)
    platform = None
    platform_match = re.search(r"\s+(?:on|from|in|using|via)\s+(youtube|you tube|spotify)$", t) or \
        re.search(r"^(youtube|spotify)\s+", t)
    if platform_match:
        platform = "youtube" if "tube" in platform_match.group(1) else "spotify"
        t = (t[: platform_match.start()] + t[platform_match.end():]).strip()
    t = re.sub(r"\s+youtube$", "", t).strip()
    official = bool(re.search(r"\bofficial\b", t))
    want_audio = bool(re.search(r"\b(?:official )?audio\b", t))
    is_video = bool(re.search(r"\b(?:video|videos|music video|mv|vlog|clip|trailer|episode|tutorial)\b", t))
    is_song = bool(re.search(r"\b(?:song|track|single|release)\b", t))
    latest = bool(re.search(r"\b(?:latest|newest|most recent|new(?:est)? upload|last)\b", t)) and (is_video or is_song)
    kind = "video" if is_video else "any"
    if is_video and platform is None:
        platform = "youtube"

    title = artist = None
    core = t
    core = re.sub(r"^(?:the|a|an|some)\s+", "", core)
    core = re.sub(r"^(?:youtube|yt)\s+", "", core)
    # "the latest MrBeast video", "MrBeast's latest video", "the newest video from MrBeast", "latest song from X"
    noun = r"(?:video|videos|upload|song|track|single|release)"
    latest_match = (re.match(rf"^(?:latest|newest|most recent|new|last)\s+(?P<who>.+?)(?:'s)?\s+{noun}$", core)
                    or re.match(rf"^(?P<who>.+?)(?:'s)?\s+(?:latest|newest|most recent|new|last)\s+{noun}$", core)
                    or re.match(rf"^(?:latest|newest|most recent|new|last)\s+{noun}\s+(?:from|by|of)\s+(?P<who>.+)$", core))
    if latest and latest_match:
        artist = latest_match.group("who").strip()
        music = is_song and not is_video
        return MediaRequest(query=artist + (" new song" if music else ""), artist=artist,
                            kind="music" if music else "video", platform=platform or "youtube", latest=True)
    # "official music video for X (by Y)", "video about X", "the X official video"
    core = re.sub(r"^(?:official\s+)?(?:music\s+)?(?:video|videos|audio|song|track|clip)\s+(?:for|of|about|on|called|titled)\s+",
                  "", core)
    core = re.sub(r"^(?:video|videos)\s+", "", core)
    core = re.sub(r"\s+(?:official\s+)?(?:music\s+)?(?:video|audio)$", "", core)
    core = re.sub(r"^(?:the\s+)?(?:song|track)\s+", "", core)
    core = re.sub(r"^(?:official|the official)\s+", "", core).strip()
    by = re.match(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", core)
    if by:
        title, artist = by.group("title").strip(), by.group("artist").strip()
    else:
        title = core or None
    if kind == "any" and artist:
        kind = "music"
    parts = [p for p in (title, artist) if p]
    query = " ".join(parts) or core or t
    if official and kind in ("video", "any") and is_video:
        query += " official video"
    elif want_audio:
        query += " official audio"
    return MediaRequest(query=query.strip(), title=title, artist=artist, kind=kind, platform=platform,
                        latest=False, official=official, want_audio=want_audio)


# ---------------------------------------------------------------------------- parsing results

def _text(node: dict | None) -> str:
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(run.get("text", "") for run in node.get("runs", []))


def parse_duration(text: str | None) -> int | None:
    if not text:
        return None
    if text.startswith("PT"):  # ISO 8601 (Data API)
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
        if not match:
            return None
        h, m, s = (int(g) if g else 0 for g in match.groups())
        return h * 3600 + m * 60 + s
    parts = [int(p) for p in re.findall(r"\d+", text)]
    if not parts or len(parts) > 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def parse_views(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text.split(" ")[0])
    if digits:
        return int(digits)
    match = re.match(r"([\d.]+)\s*([KMB])", text, re.I)
    if match:
        return int(float(match.group(1)) * {"K": 1e3, "M": 1e6, "B": 1e9}[match.group(2).upper()])
    return None


_AGE_UNITS = {"s": 1, "second": 1, "m": 60, "minute": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400,
              "day": 86400, "w": 604800, "week": 604800, "mo": 2592000, "month": 2592000, "y": 31536000,
              "year": 31536000}


def parse_age(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d+)\s*(mo|months?|y|years?|w|weeks?|d|days?|h|hours?|m|min|minutes?|s|seconds?)\b",
                      text.lower())
    if not match:
        return None
    unit = match.group(2).rstrip("s") if match.group(2) not in ("s", "mo", "ms") else match.group(2)
    unit = {"month": "mo", "year": "y", "week": "w", "day": "d", "hour": "h", "minute": "m", "second": "s"}.get(unit, unit)
    return int(match.group(1)) * _AGE_UNITS.get(unit, 0)


def parse_results_page(html: str) -> list[VideoResult]:
    match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S) or \
        re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    results: list[VideoResult] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            renderer = node.get("videoRenderer")
            if isinstance(renderer, dict) and renderer.get("videoId") and renderer["videoId"] not in seen:
                seen.add(renderer["videoId"])
                badges = [b.get("metadataBadgeRenderer", {}).get("style", "") for b in renderer.get("ownerBadges") or []]
                live = any("LIVE" in (b.get("metadataBadgeRenderer", {}).get("style", "") or "")
                           for b in renderer.get("badges") or [])
                results.append(VideoResult(
                    video_id=renderer["videoId"],
                    title=_text(renderer.get("title")),
                    channel=_text(renderer.get("ownerText")) or _text(renderer.get("longBylineText")),
                    duration_s=parse_duration(_text(renderer.get("lengthText"))),
                    views=parse_views(_text(renderer.get("viewCountText"))),
                    age_s=parse_age(_text(renderer.get("publishedTimeText"))),
                    verified_artist=any("VERIFIED_ARTIST" in b for b in badges),
                    verified=any("VERIFIED" in b for b in badges),
                    live=live or not renderer.get("lengthText"),
                    position=len(results),
                ))
            for key, value in node.items():
                if key != "videoRenderer":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return results


# ---------------------------------------------------------------------------- searching

class YouTubeSearch:
    def __init__(self, api_key: str | None = None, region: str = "", timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._region = region
        self._timeout = timeout_s
        self.last_backend = ""
        self.last_ms = 0

    async def search(self, query: str, *, latest: bool = False, limit: int = 20) -> list[VideoResult]:
        started = time.perf_counter()
        results: list[VideoResult] = []
        if self._api_key:
            try:
                results = await self._api(query, latest=latest, limit=limit)
                self.last_backend = "data-api"
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("YouTube Data API search failed (%s); using the results page", exc)
        if not results:
            results = await self._web(query, latest=latest)
            self.last_backend = "web"
        self.last_ms = int((time.perf_counter() - started) * 1000)
        return results[:limit]

    async def _web(self, query: str, *, latest: bool) -> list[VideoResult]:
        params = {"search_query": query, "hl": "en"}
        if self._region:
            params["gl"] = self._region
        if latest:
            params["sp"] = "CAI="  # sort by upload date
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers, follow_redirects=True,
                                     cookies={"SOCS": "CAI"}) as client:  # SOCS skips the EU consent page
            response = await client.get("https://www.youtube.com/results", params=params)
            response.raise_for_status()
        return parse_results_page(response.text)

    async def _api(self, query: str, *, latest: bool, limit: int) -> list[VideoResult]:
        params = {"part": "snippet", "type": "video", "maxResults": min(25, limit), "q": query, "key": self._api_key}
        if latest:
            params["order"] = "date"
        if self._region:
            params["regionCode"] = self._region
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get("https://www.googleapis.com/youtube/v3/search", params=params)
            response.raise_for_status()
            items = response.json().get("items", [])
            ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
            details: dict[str, dict] = {}
            if ids:
                extra = await client.get("https://www.googleapis.com/youtube/v3/videos",
                                         params={"part": "contentDetails,statistics", "id": ",".join(ids),
                                                 "key": self._api_key})
                extra.raise_for_status()
                details = {v["id"]: v for v in extra.json().get("items", [])}
        results = []
        for position, item in enumerate(items):
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            info = details.get(video_id, {})
            published = snippet.get("publishedAt")
            age = None
            if published:
                from datetime import UTC, datetime

                try:
                    age = int((datetime.now(UTC) - datetime.fromisoformat(published.replace("Z", "+00:00"))).total_seconds())
                except ValueError:
                    age = None
            results.append(VideoResult(
                video_id=video_id, title=_unescape(snippet.get("title", "")), channel=snippet.get("channelTitle", ""),
                duration_s=parse_duration(info.get("contentDetails", {}).get("duration")),
                views=int(info.get("statistics", {}).get("viewCount", 0) or 0) or None, age_s=age,
                live=snippet.get("liveBroadcastContent") == "live", position=position,
            ))
        return results


def _unescape(text: str) -> str:
    import html

    return html.unescape(text)


# ---------------------------------------------------------------------------- ranking

def _tokens(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    return [t for t in text.split() if t]


def _content_tokens(text: str) -> set[str]:
    return {t.strip("'") for t in _tokens(text) if t not in _STOP and len(t.strip("'")) > 1}


def _phrase_in(phrase: str, text: str) -> bool:
    words = [w for w in _tokens(phrase) if w not in {"the", "a", "an"}]
    if not words:
        return False
    return re.search(r"\b" + r"\W+(?:\w+\W+)?".join(re.escape(w) for w in words) + r"\b", text.lower()) is not None


def relevance(request: MediaRequest, title: str, channel: str = "") -> float:
    """0..1: how much of what was asked for appears in this title/channel."""
    wanted = _content_tokens(request.describe() if (request.title or request.artist) else request.query)
    if not wanted:
        return 0.0
    have = _content_tokens(title) | _content_tokens(channel)
    hits = sum(1 for word in wanted if word in have or any(h.startswith(word) or word.startswith(h)
                                                            for h in have if len(h) > 3 and len(word) > 3))
    return hits / len(wanted)


def rank(results: list[VideoResult], request: MediaRequest) -> list[VideoResult]:
    asked = request.query.lower()
    for video in results:
        reasons: list[str] = []
        title_l = video.title.lower()
        channel_l = video.channel.lower()
        score = relevance(request, video.title, video.channel) * 40
        if request.title and _phrase_in(request.title, video.title):
            score += 15
            reasons.append("title")
        if request.artist:
            if _phrase_in(request.artist, video.channel) or _content_tokens(request.artist) <= _content_tokens(video.channel):
                score += 18
                reasons.append("channel")
            elif _phrase_in(request.artist, video.title):
                score += 10
                reasons.append("artist in title")
            elif request.latest:
                score -= 30
        music = request.kind in ("music", "any") and not request.latest
        if music:
            if video.verified_artist:
                score += 12
                reasons.append("official artist")
            if channel_l.endswith("vevo"):
                score += 10
                reasons.append("vevo")
            if channel_l.endswith(" - topic"):
                score += 5 if not request.want_audio else 10
            if re.search(r"official (?:music )?video", title_l):
                if request.want_audio:
                    score -= 4
                else:
                    score += 10 if request.kind != "music" or request.official else 6
                    reasons.append("official video")
            elif "official audio" in title_l or "official visualizer" in title_l:
                score += 14 if request.want_audio else 5
        elif video.verified:
            score += 3
        for word in _NEGATIVE:
            if word in title_l and word not in asked:
                score -= 12
                reasons.append(f"-{word}")
                break
        if video.is_short and "short" not in asked:
            score -= 15
        if video.duration_s:
            if music and video.duration_s > 20 * 60 and not re.search(r"\b(?:mix|playlist|album|hour|compilation)\b", asked):
                score -= 12
            if request.kind == "video" and video.duration_s < 90 and not request.latest:
                score -= 5
        if video.live and "live" not in asked:
            score -= 8
        if video.views:
            score += min(8.0, math.log10(max(video.views, 1)) * 0.9)
        score += max(0.0, 8 - video.position * 1.5)
        if request.latest and video.age_s is not None:
            score += max(0.0, 25 - math.log2(max(video.age_s, 3600) / 3600) * 3)
        video.score = score
        video.reasons = reasons
    return sorted(results, key=lambda v: v.score, reverse=True)


# ---------------------------------------------------------------------------- verification

def title_similarity(a: str, b: str) -> float:
    a_tokens, b_tokens = _content_tokens(a), _content_tokens(b)
    if not a_tokens or not b_tokens:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()
    overlap = len(a_tokens & b_tokens) / max(1, min(len(a_tokens), len(b_tokens)))
    return max(overlap, SequenceMatcher(None, a.lower(), b.lower()).ratio())


def youtube_page_title(window_title: str) -> str:
    """'(3) The Weeknd - Blinding Lights (Official Video) - YouTube - Brave' → the video title."""
    title = re.sub(r"^\(\d+\)\s*", "", window_title.strip())
    title = re.sub(r"\s+-\s+YouTube(?:\s+Music)?(?:\s+-\s+.*)?$", "", title)
    return title.strip()


@dataclass
class Verification:
    ok: bool
    reason: str
    page_title: str = ""
    video_id: str | None = None


def verify_page(request: MediaRequest, chosen: VideoResult, window_title: str, address: str | None) -> Verification:
    """Does the page on screen correspond to the chosen video *and* to what the user asked for?"""
    page = youtube_page_title(window_title)
    shown_id = extract_video_id(address or "")
    if shown_id and shown_id != chosen.video_id:
        return Verification(False, f"the browser shows video {shown_id}, not {chosen.video_id}", page, shown_id)
    if not page or page.lower() in ("youtube", "new tab"):
        return Verification(False, "the page hasn't loaded a video title yet", page, shown_id)
    if title_similarity(page, chosen.title) < 0.5:
        return Verification(False, f"the page title '{page}' doesn't match '{chosen.title}'", page, shown_id)
    if request.url is None and relevance(request, page, chosen.channel) < 0.34:
        return Verification(False, f"'{page}' isn't what was asked for ({request.describe()})", page, shown_id)
    return Verification(True, "title and link match", page, shown_id or chosen.video_id)


def watch_url(video_id: str) -> str:
    if not _VIDEO_ID.match(video_id):
        raise ValueError("not a YouTube video id")
    return "https://www.youtube.com/watch?" + urllib.parse.urlencode({"v": video_id})
