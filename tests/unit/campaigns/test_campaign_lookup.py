"""Streamer lookup for campaigns (D117): references are parsed strictly and resolved on the platform."""

import pytest

from sixstars.campaigns import lookup
from sixstars.clients.fakes import FakeTwitchClient, FakeYouTubeClient


@pytest.mark.parametrize("ref", ["dylanpage", "kick:bichou", "twitch:", "youtube"])
def test_bad_references(ref: str) -> None:
    with pytest.raises(lookup.StreamerNotFoundError):
        lookup.parse_ref(ref)


def test_twitch_by_login() -> None:
    twitch = FakeTwitchClient()
    twitch.followers["id_luckylatvian"] = 120_000
    streamer = lookup.find("twitch:LuckyLatvian", twitch, FakeYouTubeClient())
    assert (streamer.platform, streamer.channel_id, streamer.handle) == (
        "twitch",
        "id_luckylatvian",
        "luckylatvian",
    )
    assert streamer.followers == 120_000
    assert "120,000 followers" in streamer.describe()
    with pytest.raises(lookup.StreamerNotFoundError):
        lookup.find("twitch:missing-person", twitch, FakeYouTubeClient())


def test_youtube_by_handle_and_by_channel_id() -> None:
    youtube = FakeYouTubeClient()
    by_handle = lookup.find("youtube:@DylanPage", youtube=youtube, twitch=FakeTwitchClient())
    assert (by_handle.channel_id, by_handle.handle) == ("UC_DylanPage", "DylanPage")
    channel_id = "UC" + "x" * 22
    by_id = lookup.find(f"youtube:{channel_id}", FakeTwitchClient(), youtube)
    assert by_id.channel_id == channel_id
    with pytest.raises(lookup.StreamerNotFoundError):
        lookup.find("youtube:@missing", FakeTwitchClient(), youtube)
