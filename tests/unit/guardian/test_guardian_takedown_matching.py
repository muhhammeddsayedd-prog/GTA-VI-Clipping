"""Takedown emails as untrusted text: cleaning, link extraction, and matching against our own rows."""

from typing import Any

import pytest

from sixstars.guardian import takedowns
from sixstars.guardian.takedowns import Keys

# ── cleaning the request ──────────────────────────────────────────────────────────────────────────────────


def test_an_oversized_body_is_cut_not_rejected_and_links_beyond_the_cut_are_still_found() -> None:
    body = "Please remove my video. " + "x" * 50_000 + " https://youtu.be/abcDEF12345 thanks"
    email = takedowns.build_email("a@b.test", "Takedown", body, "m1", "2026-11-19T10:00:00Z", 20_000)
    assert len(email.body) == 20_000 and email.truncated is True
    assert email.links == ("https://youtu.be/abcDEF12345",)
    assert email.message_id == "m1"


def test_long_headers_are_cut_and_control_characters_removed() -> None:
    email = takedowns.build_email(
        "s" * 5000, "line one\r\nBcc: x\x00\x1b[31m" + "t" * 5000, "hi\x00there", "m", None, 100
    )
    assert len(email.sender) == 320
    assert len(email.subject) <= 300 and "\n" not in email.subject and "\x00" not in email.subject
    assert email.subject.startswith("line one Bcc: x")
    assert email.body == "hi there" and email.truncated is False


@pytest.mark.parametrize("bad", [None, 5, ["x"], {"a": 1}, b"bytes"])
def test_fields_that_are_not_text_become_empty(bad: Any) -> None:
    email = takedowns.build_email(bad, bad, bad, bad, bad, 100)
    assert (email.sender, email.subject, email.body, email.claimed_received_at) == ("", "", "", None)
    assert email.message_id.startswith("sha256:")


def test_an_email_without_a_message_id_gets_a_stable_one() -> None:
    first = takedowns.build_email("a@b.test", "S", "body", "", None, 100)
    again = takedowns.build_email("a@b.test", "S", "body", None, None, 100)
    other = takedowns.build_email("a@b.test", "S", "another body", "", None, 100)
    assert first.message_id == again.message_id != other.message_id
    assert first.message_id.startswith("sha256:") and len(first.message_id) == 71


def test_a_message_id_that_is_too_long_is_replaced_by_the_hash() -> None:
    email = takedowns.build_email("a", "s", "b", "i" * 600, None, 100)
    assert email.message_id.startswith("sha256:")


def test_links_are_extracted_without_punctuation_and_capped() -> None:
    text = "See <https://youtu.be/abcDEF12345>, (https://www.tiktok.com/@me/video/123456789). And http://x.test/a!"
    assert takedowns.extract_urls(text) == [
        "https://youtu.be/abcDEF12345",
        "https://www.tiktok.com/@me/video/123456789",
        "http://x.test/a",
    ]
    many = " ".join(f"https://x.test/{n}" for n in range(500))
    assert len(takedowns.extract_urls(many)) == takedowns.MAX_URLS
    assert takedowns.extract_urls("javascript:alert(1) ftp://x.test/a file:///etc/passwd") == []
    assert takedowns.extract_urls("https://x.test/" + "a" * 5000) == []  # absurdly long: dropped


# ── what a link points at ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "keys"),
    [
        ("https://www.youtube.com/watch?v=abcDEF12345", [("video:youtube", "abcDEF12345")]),
        ("https://youtube.com/shorts/abcDEF12345?feature=share", [("video:youtube", "abcDEF12345")]),
        ("https://youtu.be/abcDEF12345", [("video:youtube", "abcDEF12345")]),
        ("https://m.youtube.com/live/abcDEF12345", [("video:youtube", "abcDEF12345")]),
        (
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            [("channel", "UCabcdefghijklmnopqrstuv")],
        ),
        ("https://www.youtube.com/@Some_Streamer", [("handle", "somestreamer")]),
        ("https://www.youtube.com/@Some_Streamer/videos", [("handle", "somestreamer")]),
        ("https://www.youtube.com/c/SomeStreamer", [("handle", "somestreamer")]),
        ("https://www.youtube.com/user/SomeStreamer", [("handle", "somestreamer")]),
        ("https://www.youtube.com/feed/subscriptions", []),
        (
            "https://www.tiktok.com/@6starsclipping/video/7300000000000000000",
            [("video:tiktok", "7300000000000000000"), ("handle", "6starsclipping")],
        ),
        ("https://www.tiktok.com/@some.creator", [("handle", "somecreator")]),
        ("https://vm.tiktok.com/ZMabcdef/", []),  # a short link is never resolved
        ("https://www.instagram.com/reel/CxYz_12-AbC/", [("video:instagram", "CxYz_12-AbC")]),
        ("https://www.instagram.com/p/CxYz_12-AbC/?igsh=1", [("video:instagram", "CxYz_12-AbC")]),
        ("https://www.instagram.com/6starsclippy/reel/CxYz_12-AbC/", [("video:instagram", "CxYz_12-AbC")]),
        ("https://www.instagram.com/some.creator/", [("handle", "somecreator")]),
        ("https://www.twitch.tv/videos/2300000000", [("video:twitch", "2300000000")]),
        ("https://www.twitch.tv/SomeStreamer", [("handle", "somestreamer")]),
        ("https://www.twitch.tv/directory/game/x", []),
        # Look-alike hosts, other schemes and broken links point at nothing.
        ("https://youtube.com.evil.test/watch?v=abcDEF12345", []),
        ("https://evil.test/?u=https://youtu.be/abcDEF12345", []),
        ("https://youtu.be.evil.test/abcDEF12345", []),
        ("ftp://youtube.com/watch?v=abcDEF12345", []),
        ("https://www.youtube.com/watch?v=../../etc/passwd", []),
        ("https://www.youtube.com/watch?v=abc';drop table posts;--", []),
        ("https://[bad", []),
        ("not a url", []),
        ("", []),
    ],
)
def test_what_a_link_points_at(url: str, keys: list[tuple[str, str]]) -> None:
    assert takedowns.url_keys(url) == keys


@pytest.mark.parametrize(
    ("name", "normal"),
    [
        ("@Some_Streamer", "somestreamer"),
        ("some streamer", "somestreamer"),
        ("SOME.STREAMER!", "somestreamer"),
        ("", ""),
    ],
)
def test_names_are_compared_without_case_and_punctuation(name: str, normal: str) -> None:
    assert takedowns.normalise_handle(name) == normal


# ── matching against our rows ─────────────────────────────────────────────────────────────────────────────

POSTS = [
    {
        "id": "post-yt",
        "platform": "youtube",
        "platform_post_id": "ourYT000001",
        "post_url": None,
        "creator_id": "c1",
    },
    {
        "id": "post-tt",
        "platform": "tiktok",
        "platform_post_id": None,
        "post_url": "https://www.tiktok.com/@6starsclipping/video/7300000000000000001",
        "creator_id": "c2",
    },
]
SOURCES = [{"platform": "youtube", "source_id": "srcVID00001", "creator_id": "c3"}]
CREATORS = [
    {
        "id": "c1",
        "platform": "youtube",
        "channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
        "handle": "@Big_Streamer",
        "display_name": "Big Streamer TV",
    },
    {"id": "c2", "platform": "twitch", "channel_id": "123", "handle": "tinyfish", "display_name": None},
    {
        "id": "c3",
        "platform": "youtube",
        "channel_id": "UCcccccccccccccccccccccc",
        "handle": "@third",
        "display_name": "Third One",
    },
    {
        "id": "c4",
        "platform": "youtube",
        "channel_id": "UCdddddddddddddddddddddd",
        "handle": "@ab",
        "display_name": "AB",
    },
]
OWN = ["@6starsclipping", "@6starsclippy", "@6starsclips-m3x"]


def match(urls: list[str], names: list[str] | None = None, min_chars: int = 4) -> takedowns.Match:
    return takedowns.match_rows(
        takedowns.collect_keys(urls), names or [], POSTS, SOURCES, CREATORS, OWN, min_chars
    )


def test_a_link_to_our_post_matches_that_post_and_its_creator() -> None:
    result = match(["https://youtu.be/ourYT000001"])
    assert result.post_ids == ["post-yt"] and result.creator_ids == ["c1"]
    result = match(["https://www.tiktok.com/@6starsclipping/video/7300000000000000001?lang=en"])
    assert result.post_ids == ["post-tt"] and result.creator_ids == ["c2"]


def test_a_link_to_the_video_we_clipped_matches_the_creator() -> None:
    result = match(["https://www.youtube.com/watch?v=srcVID00001"])
    assert result.post_ids == [] and result.creator_ids == ["c3"]


def test_a_link_to_the_creators_channel_matches_exactly() -> None:
    assert match(["https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"]).creator_ids == ["c1"]
    assert match(["https://www.youtube.com/@big_streamer"]).creator_ids == ["c1"]
    assert match(["https://www.youtube.com/channel/UCzzzzzzzzzzzzzzzzzzzzzz"]).creator_ids == []


@pytest.mark.parametrize(
    ("name", "creator_ids"),
    [
        ("Big_Streamer", ["c1"]),
        ("@big streamer", ["c1"]),
        ("BIGSTREAMER", ["c1"]),
        ("Big Streamer TV", ["c1"]),  # the display name
        ("TinyFish", ["c2"]),
        ("Big", []),  # a part of a name is not the name
        ("Big Streamer Fan Page", []),
        ("streamer", []),
        ("ab", []),  # too short to be trusted
        ("", []),
        ("6starsclipping", []),  # our own account is never a creator
    ],
)
def test_creator_names_from_the_email(name: str, creator_ids: list[str]) -> None:
    assert match([], [name]).creator_ids == creator_ids


def test_a_short_name_matches_only_when_the_setting_allows_it() -> None:
    assert match([], ["ab"], min_chars=2).creator_ids == ["c4"]


def test_a_takedown_naming_things_that_are_not_ours_matches_nothing() -> None:
    result = match(
        [
            "https://www.youtube.com/watch?v=someoneELSE1",
            "https://www.tiktok.com/@stranger/video/7399999999999999999",
            "https://www.instagram.com/reel/NotOurs123/",
            "https://evil.test/ourYT000001",
        ],
        ["A Stranger", "another one"],
    )
    assert result.creator_ids == [] and result.post_ids == []


def test_an_id_only_matches_on_its_own_platform() -> None:
    # The TikTok id of our post, used in a YouTube link, is not our post.
    keys = Keys(video_ids=frozenset({("youtube", "7300000000000000001")}))
    assert takedowns.match_rows(keys, [], POSTS, SOURCES, CREATORS, OWN, 4).creator_ids == []


def test_several_creators_can_match_and_each_is_listed_once() -> None:
    result = match(
        ["https://youtu.be/ourYT000001", "https://www.youtube.com/watch?v=srcVID00001"],
        ["big_streamer", "tinyfish"],
    )
    assert result.creator_ids == ["c1", "c3", "c2"] and result.post_ids == ["post-yt"]
    assert len(result.how) == 3


def test_at_most_twenty_names_are_looked_at() -> None:
    names = [f"filler{n}" for n in range(takedowns.MAX_HANDLES)] + ["tinyfish"]
    assert match([], names).creator_ids == []
