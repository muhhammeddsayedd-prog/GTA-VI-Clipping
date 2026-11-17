"""The fakes: deterministic, no network, no environment, and usable wherever the real client is expected."""

from pathlib import Path

import pytest
import respx
from pydantic import BaseModel

from sixstars.clients import fakes
from sixstars.clients.anthropic_client import AnthropicClient, AnthropicRefusal
from sixstars.clients.apify import ApifyClient
from sixstars.clients.transcribe import Transcriber
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.upload_post import NotSupported, PlatformResult, RateLimited, UploadPostClient
from sixstars.clients.youtube import YouTubeClient


class Caption(BaseModel):
    title: str
    hashtags: list[str] = []


@pytest.fixture(autouse=True)
def no_network() -> object:
    """Any HTTP request made by a fake fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_fakes_are_instances_of_the_real_clients() -> None:
    assert isinstance(fakes.FakeTwitchClient(), TwitchClient)
    assert isinstance(fakes.FakeYouTubeClient(), YouTubeClient)
    assert isinstance(fakes.FakeApifyClient(), ApifyClient)
    assert isinstance(fakes.FakeAnthropicClient(), AnthropicClient)
    assert isinstance(fakes.FakeTranscriber(), Transcriber)
    assert isinstance(fakes.FakeUploadPostClient(), UploadPostClient)


def test_fake_twitch() -> None:
    client = fakes.FakeTwitchClient()
    game_id = client.get_game_id("Grand Theft Auto V")
    assert game_id == fakes.FAKE_GAME_ID and client.get_game_id("Unknown") is None
    streams = client.get_streams(game_id, max_items=2)
    assert [stream.id for stream in streams] == ["stream0", "stream1"]
    assert streams == fakes.FakeTwitchClient().get_streams(game_id, max_items=2)
    assert len(client.get_videos_by_game(game_id)) == 3
    assert [video.id for video in client.get_videos_by_user("user1")] == ["video1"]
    assert client.get_users(["a", "a", "b"])[1].login == "b"
    assert client.get_followers_total("x") == 100_000
    assert client.calls_to("get_streams") == [{"game_id": game_id, "max_items": 2}]
    assert fakes.FakeTwitchClient(configured=False).is_configured() is False


def test_fake_youtube(tmp_path: Path) -> None:
    client = fakes.FakeYouTubeClient()
    assert client.videos_list(["a", "b"])[1]["id"] == "b"
    assert client.channels_list(["UC1"])[0]["statistics"]["subscriberCount"] == "250000"
    assert len(client.channel_feed("UCabcd")) == 2
    video_id = client.upload_private(tmp_path / "c.mp4", "t", "d")
    status = client.get_status(video_id)
    assert video_id == "fakevid1" and status is not None and status.is_restricted is False
    assert client.delete_video(video_id) is True and client.delete_video(video_id) is False
    assert client.get_status("unknown") is None
    assert client.whoami().id.startswith("UC")
    assert fakes.FakeYouTubeClient(oauth=False).has_oauth() is False


def test_fake_apify() -> None:
    client = fakes.FakeApifyClient()
    live = client.youtube_search(["GTA 6"], live=True, max_results=2)
    assert len(live) == 2 and all(item.is_live and item.viewers for item in live)
    vods = client.youtube_search(["GTA 6"], uploaded_within_hours=48)
    assert len(vods) == 3 and all(not item.is_live and item.views for item in vods)
    client.search_results = [fakes.fake_youtube_item(9, live=True)]
    assert client.youtube_search(["x"]) == [] and len(client.youtube_search(["x"], live=True)) == 1
    run_id = client.start_run("a/b", {"k": 1})
    run = client.wait_for_run(run_id)
    assert run.succeeded and client.get_items(str(run.dataset_id)) == []
    rows = [{"id": "abc", "title": "t", "channelId": "UC1", "viewCount": 3}, {"error": "NO_VIDEOS"}]
    assert [item.video_id for item in fakes.youtube_items_from_rows(rows)] == ["abc"]


def test_fake_anthropic() -> None:
    client = fakes.FakeAnthropicClient(
        {
            "clip_writer": {"title": "No way", "hashtags": ["#gta6"]},
            "sequence": [{"title": "one"}, {"title": "two"}],
            "refused": AnthropicRefusal("general_harms"),
        }
    )

    def ask(prompt_id: str) -> Caption:
        return client.complete_json(prompt_id, "sys", "user", Caption, "claude-sonnet-5-5", 1000)

    assert ask("clip_writer") == Caption(title="No way", hashtags=["#gta6"])
    assert [ask("sequence").title for _ in range(3)] == ["one", "two", "two"]
    with pytest.raises(AnthropicRefusal):
        ask("refused")
    with pytest.raises(KeyError):
        ask("unknown")
    assert client.calls_to("complete_json")[0]["model"] == "claude-sonnet-5-5"
    assert (
        fakes.FakeAnthropicClient(default={"title": "d"})
        .complete_json("any", "s", "u", Caption, "m", 1)
        .title
        == "d"
    )


def test_fake_transcriber(tmp_path: Path) -> None:
    client = fakes.FakeTranscriber()
    transcript = client.transcribe(tmp_path / "a.flac", mode="hosted")
    assert transcript.text == "no way he actually landed that" and transcript.language == "en"
    assert all(word.s < word.e for word in transcript.words)
    assert client.calls_to("transcribe")[0]["mode"] == "hosted"
    client.error = RuntimeError("down")
    with pytest.raises(RuntimeError):
        client.transcribe(tmp_path / "a.flac")


def test_fake_upload_post(tmp_path: Path) -> None:
    client = fakes.FakeUploadPostClient()
    clip = tmp_path / "c.mp4"
    accepted = client.upload_video("default", "youtube", clip, {"title": "t"}, "post:1:youtube_main", "req-1")
    assert accepted.status == "pending" and accepted.request_id == "req-1"
    # Same idempotency key: the first upload is returned and nothing new is created.
    again = client.upload_video("default", "youtube", clip, {"title": "t"}, "post:1:youtube_main", "req-2")
    assert again.request_id == "req-1" and list(client.uploads) == ["req-1"]

    done = client.wait_for_upload("req-1")
    result = done.result_for("youtube")
    assert done.status == "completed" and result is not None and result.post_id == "youtube-req-1"
    assert client.get_upload_status("missing").status == "not_found"
    assert client.get_post_analytics("req-1")["platforms"]["youtube"]["post_metrics"]["views"] == 1000
    assert set(client.get_profile_analytics("default", ["tiktok", "youtube"])) == {"tiktok", "youtube"}
    assert client.list_profiles()[0]["username"] == "default"

    assert client.delete_post("default", "youtube", "youtube-req-1") is True
    with pytest.raises(NotSupported):
        client.delete_post("default", "tiktok", "1")

    client.upload_error = RateLimited("default/tiktok", 3600.0, "daily limit")
    with pytest.raises(RateLimited):
        client.upload_video("default", "tiktok", clip, {}, "post:2:tiktok_main", "req-3")
    client.platform_results["tiktok"] = PlatformResult(
        platform="tiktok", success=False, error="expired token"
    )
    client.upload_video("default", "tiktok", clip, {}, "post:2:tiktok_main", "req-3")
    failed = client.get_upload_status("req-3")
    failed_result = failed.result_for("tiktok")
    assert failed.status == "failed" and failed_result is not None and failed_result.error_kind == "auth"
