"""Edge cases found by breaking the rules on purpose (each of these changes used to go unnoticed)."""

from guardian_helpers import YouTube, config, post

from sixstars.clients._http import ApiError
from sixstars.guardian import claims, takedowns
from sixstars.guardian.takedowns import Keys
from sixstars.stats import report

CREATORS = [
    {
        "id": "c1",
        "platform": "youtube",
        "channel_id": "UC1",
        "handle": "@6starsclipping",
        "display_name": None,
    },
    {"id": "c2", "platform": "twitch", "channel_id": "2", "handle": "tinyfish", "display_name": None},
]


def test_when_the_youtube_api_fails_private_posts_still_go_to_the_next_source() -> None:
    youtube = YouTube(oauth=False)
    youtube.fail_list = ApiError("youtube", "GET /videos", 500, "backend error")
    private = post("priv", privacy="private")
    observations, leftover = claims.observe_youtube([post("pub"), private], youtube, config(), [])
    assert observations == [] and leftover == [private]


def test_a_creator_row_with_our_own_account_name_is_never_matched() -> None:
    # A creator row that carries our own handle (bad data) must not make "remove @6starsclipping" hit it.
    result = takedowns.match_rows(Keys(), ["6starsclipping"], [], [], CREATORS, ["@6starsclipping"], 4)
    assert result.creator_ids == []
    link = takedowns.collect_keys(["https://www.tiktok.com/@6starsclipping"])
    assert takedowns.match_rows(link, [], [], [], CREATORS, ["@6starsclipping"], 4).creator_ids == []


def test_a_source_video_id_only_matches_on_its_own_platform() -> None:
    sources = [{"platform": "twitch", "source_id": "2300000000", "creator_id": "c2"}]
    wrong = Keys(video_ids=frozenset({("youtube", "2300000000")}))
    right = Keys(video_ids=frozenset({("twitch", "2300000000")}))
    assert takedowns.match_rows(wrong, [], [], sources, CREATORS, [], 4).creator_ids == []
    assert takedowns.match_rows(right, [], [], sources, CREATORS, [], 4).creator_ids == ["c2"]


def test_a_cut_report_is_a_prefix_of_the_lines_and_never_skips_ahead() -> None:
    lines = [(0, "first line"), (0, "x" * 500), (0, "short later line")]
    original = report._lines
    report._lines = lambda data: lines  # type: ignore[assignment]
    try:
        assert report.render_telegram({}, max_chars=100) == "first line"
    finally:
        report._lines = original  # type: ignore[assignment]
