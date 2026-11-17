"""One real, read-only smoke call per client. See `conftest.py` in this folder for how to run them."""

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.clients.apify import ApifyClient
from sixstars.clients.transcribe import Transcriber
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.upload_post import UploadPostClient
from sixstars.clients.youtube import YouTubeClient

FAST_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
GOOGLE_DEVELOPERS_CHANNEL = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
UPLOAD_POST_PROFILE = "default"
APIFY_MAX_RESULTS = 5
APIFY_MAX_CHARGE_USD = 0.05


class Sum(BaseModel):
    answer: int
    explanation: str


def _speech_clip(path: Path) -> None:
    """Five seconds of 16 kHz mono audio: synthetic speech when ffmpeg has libflite, else a tone."""
    text = "No way. He actually landed that jump. Clip it."
    speech = ["-f", "lavfi", "-i", f"flite=text='{text}':voice=slt"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440"]
    for source in (speech, tone):
        command = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            *source,
            "-af",
            "apad",
            "-t",
            "5",
            "-ac",
            "1",
            "-ar",
            "16000",
        ]
        if subprocess.run([*command, str(path)], capture_output=True).returncode == 0:
            return
    pytest.skip("ffmpeg could not generate a test clip")


def test_twitch_game_id() -> None:
    client = TwitchClient()
    if not client.is_configured():
        pytest.skip("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set (decision D100)")
    assert client.get_game_id("Grand Theft Auto V")


def test_youtube_channels_list() -> None:
    client = YouTubeClient()
    items = client.channels_list([GOOGLE_DEVELOPERS_CHANNEL])
    assert len(items) == 1
    assert items[0]["id"] == GOOGLE_DEVELOPERS_CHANNEL
    assert int(items[0]["statistics"]["subscriberCount"]) > 0


def test_youtube_channel_feed() -> None:
    entries = YouTubeClient().channel_feed(GOOGLE_DEVELOPERS_CHANNEL)
    assert entries
    assert all(entry.video_id and entry.channel_id == GOOGLE_DEVELOPERS_CHANNEL for entry in entries)


def test_youtube_oauth_whoami() -> None:
    client = YouTubeClient()
    if not client.has_oauth():
        pytest.skip("YOUTUBE_OAUTH_REFRESH_TOKEN is not set (decision D103)")
    assert client.whoami().id


def test_apify_small_search() -> None:
    client = ApifyClient()
    items = client.youtube_search(
        ["GTA 5 funny moments"],
        uploaded_within_hours=24 * 7,
        max_results=APIFY_MAX_RESULTS,
        max_charge_usd=APIFY_MAX_CHARGE_USD,
    )
    # YouTube search through the actor is flaky: an empty page is possible, a crash is not.
    assert len(items) <= APIFY_MAX_RESULTS
    for item in items:
        assert item.video_id and item.channel_id and item.title
        assert item.is_live is False and item.views is not None


@pytest.mark.parametrize("model", [FAST_MODEL, WRITER_MODEL])
def test_anthropic_tiny_json(model: str) -> None:
    result = AnthropicClient().complete_json(
        prompt_id="live_smoke",
        system="You are a calculator.",
        user_content="What is 19 + 23?",
        schema=Sum,
        model=model,
        max_tokens=1024,
    )
    assert result.answer == 42


def test_openai_transcription_word_timestamps(tmp_path: Path) -> None:
    clip = tmp_path / "clip.flac"
    _speech_clip(clip)
    transcriber = Transcriber(express_provider="openai")
    if "openai" not in transcriber.configured_providers():
        pytest.skip("OPENAI_API_KEY is not set")
    transcript = transcriber.transcribe(clip, mode="hosted", language="en")
    assert transcript.provider == "openai"
    assert 3.0 <= transcript.duration_s <= 7.0
    assert all(0.0 <= word.s <= word.e <= 7.0 for word in transcript.words)
    print(f"words={len(transcript.words)} text={transcript.text!r}")


def test_groq_transcription_word_timestamps(tmp_path: Path) -> None:
    transcriber = Transcriber(express_provider="groq")
    if "groq" not in transcriber.configured_providers():
        pytest.skip("GROQ_API_KEY is not set (decision D104)")
    clip = tmp_path / "clip.flac"
    _speech_clip(clip)
    transcript = transcriber.transcribe(clip, mode="hosted", language="en")
    assert transcript.provider == "groq"
    assert all(word.s <= word.e for word in transcript.words)


def test_upload_post_profile_and_analytics() -> None:
    client = UploadPostClient(posting_guard=None)
    profiles = client.list_profiles()
    assert any(profile.get("username") == UPLOAD_POST_PROFILE for profile in profiles)
    analytics = client.get_profile_analytics(UPLOAD_POST_PROFILE, ["tiktok", "instagram", "youtube"])
    assert set(analytics) >= {"tiktok", "instagram", "youtube"}
    print(
        {
            platform: sorted(data)[:12] if isinstance(data, dict) else data
            for platform, data in analytics.items()
        }
    )
