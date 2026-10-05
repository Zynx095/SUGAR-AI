"""YouTube: request parsing, results parsing, ranking, page verification and the Rickroll regression."""

from __future__ import annotations

import json

import pytest
from conftest import run
from fake_desktop import FakeDesktop, browser_window
from test_computer_control import make

from sugar.agent.executor import ToolExecutor
from sugar.agent.permissions import PermissionManager
from sugar.computer.backend import MediaSession
from sugar.computer.youtube import (
    MediaRequest,
    VideoResult,
    extract_video_id,
    parse_age,
    parse_duration,
    parse_media_request,
    parse_results_page,
    rank,
    verify_page,
    youtube_page_title,
)
from sugar.config.settings import PermissionSettings
from sugar.core.events import EventBus

# ---------------------------------------------------------------------------------------- requests


@pytest.mark.parametrize(
    ("text", "query", "title", "artist", "kind", "platform", "latest"),
    [
        ("Play Blinding Lights by The Weeknd on YouTube.", "blinding lights the weeknd", "blinding lights",
         "the weeknd", "music", "youtube", False),
        ("Play the official music video for Starboy by The Weeknd on YouTube.", "starboy the weeknd official video",
         "starboy", "the weeknd", "video", "youtube", False),
        ("Play the latest MrBeast video.", "mrbeast", None, "mrbeast", "video", "youtube", True),
        ("Open the video about RTX 5050 benchmarks.", "rtx 5050 benchmarks", "rtx 5050 benchmarks", None, "video",
         "youtube", False),
        ("Open the YouTube video for RTX 5050 benchmarks.", "rtx 5050 benchmarks", "rtx 5050 benchmarks", None,
         "video", "youtube", False),
        ("Play Starboy.", "starboy", "starboy", None, "any", None, False),
        ("Play some Linkin Park.", "linkin park", "linkin park", None, "any", None, False),
        ("Play the official Starboy video.", "starboy official video", "starboy", None, "video", "youtube", False),
        ("Play the latest song from The Weeknd.", "the weeknd new song", None, "the weeknd", "music", "youtube", True),
        ("play master of puppets on spotify", "master of puppets", "master of puppets", None, "any", "spotify", False),
    ],
)
def test_parse_media_request(text, query, title, artist, kind, platform, latest):
    request = parse_media_request(text)
    assert (request.query, request.title, request.artist, request.kind, request.platform, request.latest) == (
        query, title, artist, kind, platform, latest)


def test_links_are_used_exactly():
    request = parse_media_request("play https://youtu.be/4NRXx6U8ABQ")
    assert request.url == "https://www.youtube.com/watch?v=4NRXx6U8ABQ"
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "dQw4w9WgXcQ"
    assert extract_video_id("youtube.com/shorts/abcdefghijk") == "abcdefghijk"
    assert extract_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None


def test_duration_and_age_parsing():
    assert parse_duration("4:23") == 263 and parse_duration("1:16:31") == 4591 and parse_duration("PT3M33S") == 213
    assert parse_age("6y ago") == 6 * 31536000 and parse_age("2 days ago") == 172800 and parse_age("8h ago") == 28800
    assert parse_age("Streamed 3 weeks ago") == 3 * 604800


# ---------------------------------------------------------------------------------------- results page

def _renderer(video_id, title, channel, length="3:45", views="1,000,000 views", age="1y ago", badge=None):
    owner = [{"metadataBadgeRenderer": {"style": badge}}] if badge else []
    return {"videoRenderer": {
        "videoId": video_id, "title": {"runs": [{"text": title}]}, "ownerText": {"runs": [{"text": channel}]},
        "lengthText": {"simpleText": length}, "viewCountText": {"simpleText": views},
        "publishedTimeText": {"simpleText": age}, "ownerBadges": owner}}


def results_page(*renderers) -> str:
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": list(renderers)}}]}}}}}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


BLINDING = [
    _renderer("XwxLwG2_Sxk", "The Weeknd - Blinding Lights (Lyrics)", "7clouds", "3:20", "157,649,085 views",
              badge="BADGE_STYLE_TYPE_VERIFIED"),
    _renderer("4NRXx6U8ABQ", "The Weeknd - Blinding Lights (Official Video)", "The Weeknd", "4:23",
              "1,070,092,165 views", badge="BADGE_STYLE_TYPE_VERIFIED_ARTIST"),
    _renderer("fHI8X4OXluQ", "The Weeknd - Blinding Lights (Official Audio)", "The Weeknd", "3:24",
              "881,148,357 views", badge="BADGE_STYLE_TYPE_VERIFIED_ARTIST"),
    _renderer("cover123456", "Blinding Lights - Piano Cover", "Some Pianist", "3:10", "2,000 views"),
]
RICK = _renderer("dQw4w9WgXcQ", "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
                 "Rick Astley", "3:34", "1,823,590,508 views", badge="BADGE_STYLE_TYPE_VERIFIED_ARTIST")


def test_results_page_parsing():
    results = parse_results_page(results_page(*BLINDING))
    assert [r.video_id for r in results] == ["XwxLwG2_Sxk", "4NRXx6U8ABQ", "fHI8X4OXluQ", "cover123456"]
    official = results[1]
    assert official.channel == "The Weeknd" and official.duration_s == 263 and official.verified_artist
    assert official.views == 1070092165 and official.age_s == 31536000
    assert parse_results_page("<html>consent page</html>") == []


# ---------------------------------------------------------------------------------------- ranking

def test_official_video_beats_lyrics_and_covers():
    request = parse_media_request("Play Blinding Lights by The Weeknd on YouTube")
    ranked = rank(parse_results_page(results_page(*BLINDING)), request)
    assert ranked[0].video_id == "4NRXx6U8ABQ"
    assert ranked[-1].video_id == "cover123456"


def test_official_audio_when_asked_for_audio():
    request = parse_media_request("play the official audio of Blinding Lights by The Weeknd")
    ranked = rank(parse_results_page(results_page(*BLINDING)), request)
    assert ranked[0].video_id == "fHI8X4OXluQ"


def test_latest_video_from_a_creator_prefers_new_full_uploads():
    results = parse_results_page(results_page(
        _renderer("old11111111", "I Built A City", "MrBeast", "19:03", "91,905,814 views", "2w ago",
                  "BADGE_STYLE_TYPE_VERIFIED"),
        _renderer("short111111", "What's Inside My Briefcase?", "MrBeast", "0:40", "42,828,463 views", "8d ago",
                  "BADGE_STYLE_TYPE_VERIFIED"),
        _renderer("new11111111", "Eat Everything In A Grocery Store", "MrBeast", "57:59", "111,998,184 views",
                  "2d ago", "BADGE_STYLE_TYPE_VERIFIED"),
        _renderer("fan11111111", "MrBeast reacts compilation", "Fan Channel", "12:00", "5,000 views", "1d ago"),
    ))
    ranked = rank(results, parse_media_request("Play the latest MrBeast video."))
    assert ranked[0].video_id == "new11111111"


def test_rick_astley_only_when_asked():
    results = parse_results_page(results_page(RICK, *BLINDING))
    assert rank(results, parse_media_request("play blinding lights by the weeknd"))[0].video_id == "4NRXx6U8ABQ"
    assert rank(results, parse_media_request("play never gonna give you up by rick astley"))[0].video_id == "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------------------- verification

def test_page_verification():
    request = parse_media_request("Play Blinding Lights by The Weeknd on YouTube")
    chosen = VideoResult("4NRXx6U8ABQ", "The Weeknd - Blinding Lights (Official Video)", "The Weeknd")
    good = verify_page(request, chosen, "(2) The Weeknd - Blinding Lights (Official Video) - YouTube - Brave",
                       "youtube.com/watch?v=4NRXx6U8ABQ")
    assert good.ok
    rickrolled = verify_page(request, chosen, "Rick Astley - Never Gonna Give You Up (Official Video) - YouTube - Brave",
                             "youtube.com/watch?v=dQw4w9WgXcQ")
    assert not rickrolled.ok and "dQw4w9WgXcQ" in rickrolled.reason
    wrong_title = verify_page(request, chosen, "Rick Astley - Never Gonna Give You Up - YouTube - Brave", None)
    assert not wrong_title.ok
    assert youtube_page_title("(12) Lofi Girl - lofi hip hop radio - YouTube - Brave") == "Lofi Girl - lofi hip hop radio"


# ---------------------------------------------------------------------------------------- end to end (fake desktop)

class FakeSearch:
    """Canned YouTube results per query; records what was asked."""

    def __init__(self, pages: dict[str, list[dict]]) -> None:
        self.pages = pages
        self.queries: list[str] = []
        self.last_backend = "fake"
        self.last_ms = 1
        self._api_key = None

    async def search(self, query: str, *, latest: bool = False, limit: int = 20) -> list[VideoResult]:
        self.queries.append(query)
        for key, renderers in self.pages.items():
            if key in query.lower():
                return parse_results_page(results_page(*renderers))
        return []


def youtube_desktop(titles: dict[str, tuple[str, str]], hijack: str | None = None) -> FakeDesktop:
    """A desktop whose browser 'loads' YouTube pages: watch?v=<id> → (title, channel)."""
    desktop = FakeDesktop()

    def load(url: str) -> str:
        video_id = extract_video_id(url)
        if video_id is None:
            return "YouTube"
        title, channel = titles.get(hijack or video_id, ("Unavailable", ""))
        desktop.sessions = [MediaSession("Brave", title, channel, "playing")]
        return f"{title} - YouTube"

    desktop.page_loader = load
    desktop.add(browser_window(tabs=[("New Tab", "brave://newtab")]))
    return desktop


TITLES = {
    "4NRXx6U8ABQ": ("The Weeknd - Blinding Lights (Official Video)", "The Weeknd"),
    "fHI8X4OXluQ": ("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd"),
    "XwxLwG2_Sxk": ("The Weeknd - Blinding Lights (Lyrics)", "7clouds"),
    "dQw4w9WgXcQ": ("Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)", "Rick Astley"),
    "mc111111111": ("Minecraft Hardcore Survival", "Wrld"),
    "rtx11111111": ("Is RTX 5050 any good? 12 Games Tested!", "Geralt Benchmarks"),
}
PAGES = {
    "blinding lights": BLINDING,
    "never gonna give you up": [RICK],
    "minecraft": [_renderer("mc111111111", "Minecraft Hardcore Survival", "Wrld", "1:34:15", "693,674 views")],
    "rtx 5050": [_renderer("rtx11111111", "Is RTX 5050 any good? 12 Games Tested!", "Geralt Benchmarks", "22:01",
                           "122,494 views")],
}


def test_different_requests_open_different_videos(tmp_path):
    """Regression: V2 opened dQw4w9WgXcQ for every request. Each request must resolve to its own video."""
    computer, desktop = make(tmp_path, youtube_desktop(TITLES))
    computer.youtube = computer.media.youtube = FakeSearch(PAGES)
    opened = {}
    for request in ("Play Blinding Lights by The Weeknd on YouTube", "Play Never Gonna Give You Up by Rick Astley on YouTube",
                    "Play Minecraft on YouTube", "Open the video about RTX 5050 benchmarks"):
        result = run(computer.media.play(request))
        assert result.success and result.verified, result.details
        opened[request] = result.data["video"]["id"]
    assert opened["Play Blinding Lights by The Weeknd on YouTube"] == "4NRXx6U8ABQ"
    assert opened["Play Never Gonna Give You Up by Rick Astley on YouTube"] == "dQw4w9WgXcQ"
    assert len(set(opened.values())) == 4
    browser = next(iter(desktop.windows.values()))
    assert len(browser.tabs) == 1, "videos should reuse the YouTube tab, not pile up tabs"


def test_a_page_that_does_not_match_is_rejected(tmp_path):
    computer, desktop = make(tmp_path, youtube_desktop(TITLES, hijack="dQw4w9WgXcQ"))
    computer.youtube = computer.media.youtube = FakeSearch(PAGES)
    result = run(computer.media.play("Play Blinding Lights by The Weeknd on YouTube"))
    assert not result.success
    assert "didn't match" in result.details
    assert result.data["attempts"], "each rejected candidate should be explained"


def test_model_cannot_open_invented_video_links(tmp_path):
    from sugar.tools import ToolServices, build_registry

    computer, desktop = make(tmp_path, youtube_desktop(TITLES))
    computer.youtube = computer.media.youtube = FakeSearch(PAGES)
    bus = EventBus()
    services = ToolServices(computer.settings, bus, computer.apps.catalog, None, None, None,  # type: ignore[arg-type]
                            _Working(), computer.media.spotify, computer, tmp_path)
    registry = build_registry(services)
    executor = ToolExecutor(registry, PermissionManager(PermissionSettings(), bus), bus)
    refused = run(executor.execute("browser.open_url", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                                   origin="model"))
    assert not refused.ok and "media.play" in refused.error
    found = run(executor.execute("youtube.search", {"query": "never gonna give you up rick astley"}, origin="model"))
    assert found.ok
    allowed = run(executor.execute("browser.open_url", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                                   origin="model"))
    assert allowed.ok, allowed.summary  # the id now comes from a real search result
    by_user = run(executor.execute("browser.open_url", {"url": "https://youtu.be/4NRXx6U8ABQ"}, origin="user"))
    assert by_user.ok


def test_open_the_most_relevant_result_after_a_search(tmp_path):
    computer, desktop = make(tmp_path, youtube_desktop(TITLES))
    computer.youtube = computer.media.youtube = FakeSearch(PAGES)
    assert computer.browser.search("blinding lights by the weeknd", "youtube").success
    result = run(computer.media.open_result(None))
    assert result.success and result.data["video"]["id"] == "4NRXx6U8ABQ"
    second = run(computer.media.open_result(1))
    assert second.data["video"]["id"] == "4NRXx6U8ABQ"  # second on the page (after the lyrics video)


class _Working:
    def record_action(self, *args, **kwargs):
        return None


def test_media_request_describe():
    assert MediaRequest("x", title="starboy", artist="the weeknd").describe() == "starboy by the weeknd"
