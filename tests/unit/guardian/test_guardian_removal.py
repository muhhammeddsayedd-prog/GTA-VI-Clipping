"""Taking a post down, per platform, without the network: which call is made, what happens when a
platform cannot delete, and the message the owner gets for the posts that need hands."""

from typing import Any

import pytest
from guardian_helpers import YouTube, post

from sixstars.clients._http import ApiError
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import NotSupported, UploadPostError
from sixstars.guardian import removal
from sixstars.guardian.removal import Removal


class UploadPost(FakeUploadPostClient):
    def __init__(self, error: Exception | None = None, gone: bool = False) -> None:
        super().__init__()
        self.error = error
        self.gone = gone

    def delete_post(self, user: str, platform: str, post_id: str) -> bool:
        if self.error is not None:
            self._record("delete_post", user=user, platform=platform, post_id=post_id)
            raise self.error
        if self.gone:
            self._record("delete_post", user=user, platform=platform, post_id=post_id)
            return False
        return super().delete_post(user, platform, post_id)


def outcome(result: Removal) -> tuple[str, str]:
    return result.outcome, result.method


def test_youtube_without_the_oauth_token_is_deleted_through_upload_post() -> None:
    upload_post, youtube = UploadPost(), YouTube(oauth=False)
    result = removal.delete_on_platform(post(), upload_post, youtube)
    assert outcome(result) == ("removed", "upload_post")
    assert upload_post.deleted == [("default", "youtube", "vid00000001")]
    assert youtube.calls_to("delete_video") == []  # the OAuth call is not even tried


def test_youtube_with_the_oauth_token_is_deleted_directly() -> None:
    upload_post, youtube = UploadPost(), YouTube(oauth=True)
    result = removal.delete_on_platform(post(), upload_post, youtube)
    assert outcome(result) == ("removed", "youtube_oauth")
    assert youtube.deleted == ["vid00000001"] and upload_post.deleted == []


def test_youtube_already_deleted_counts_as_done() -> None:
    youtube = YouTube(oauth=True)
    youtube.missing = {"vid00000001"}
    assert outcome(removal.delete_on_platform(post(), UploadPost(), youtube)) == (
        "already_gone",
        "youtube_oauth",
    )
    assert outcome(removal.delete_on_platform(post(), UploadPost(gone=True), YouTube())) == (
        "already_gone",
        "upload_post",
    )


def test_a_failing_oauth_delete_degrades_to_upload_post() -> None:
    upload_post, youtube = UploadPost(), YouTube(oauth=True)
    youtube.fail_delete = ApiError("youtube", "DELETE /videos", 401, "invalid_grant")
    result = removal.delete_on_platform(post(), upload_post, youtube)
    assert outcome(result) == ("removed", "upload_post")
    assert upload_post.deleted == [("default", "youtube", "vid00000001")]


def test_when_both_youtube_paths_fail_the_post_needs_hands_and_both_errors_are_kept() -> None:
    youtube = YouTube(oauth=True)
    youtube.fail_delete = ApiError("youtube", "DELETE /videos", 401, "invalid_grant")
    result = removal.delete_on_platform(post(), UploadPost(UploadPostError("HTTP 500: oops")), youtube)
    assert outcome(result) == ("failed", "upload_post") and not result.done
    assert "UploadPostError" in result.detail and "YouTube OAuth" in result.detail


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_tiktok_and_instagram_cannot_be_deleted_and_need_hands(platform: str) -> None:
    upload_post = UploadPost()
    ref = post("p1", platform, platform_post_id="777", url=f"https://example.test/{platform}/777")
    result = removal.delete_on_platform(ref, upload_post, YouTube(oauth=True))
    assert outcome(result) == ("manual", "upload_post") and not result.done
    assert platform in result.detail
    assert upload_post.deleted == []
    assert [call["platform"] for call in upload_post.calls_to("delete_post")] == [platform]


def test_not_supported_from_upload_post_is_manual_for_any_platform() -> None:
    result = removal.delete_on_platform(post(), UploadPost(NotSupported("no")), YouTube(oauth=False))
    assert outcome(result) == ("manual", "upload_post")


def test_a_post_without_a_platform_id_cannot_be_deleted_by_an_api() -> None:
    upload_post = UploadPost()
    ref = post("p1", "youtube", platform_post_id=None, url=None)
    result = removal.delete_on_platform(ref, upload_post, YouTube(oauth=True))
    assert outcome(result) == ("manual", "none")
    assert upload_post.calls_to("delete_post") == []


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
        ("https://youtube.com/watch?v=abcDEF12345&t=3", "abcDEF12345"),
        ("https://youtu.be/abcDEF12345?si=x", "abcDEF12345"),
        ("https://m.youtube.com/watch?v=abcDEF12345", "abcDEF12345"),
        ("https://www.youtube.com/@channel", None),
        ("https://example.test/shorts/abcDEF12345", None),
        ("", None),
        (None, None),
    ],
)
def test_youtube_id_from_a_link(url: str | None, video_id: str | None) -> None:
    assert removal.youtube_id_from_url(url) == video_id


# ── the message for the owner ─────────────────────────────────────────────────────────────────────────────


def manual(platform: str, url: str | None, post_id: str = "p") -> Removal:
    ref = post(post_id, platform, platform_post_id="777", url=url)
    return Removal(ref, "manual", "upload_post", f"Upload-Post cannot delete {platform} posts")


def test_the_manual_removal_message_has_the_link_the_account_and_the_steps() -> None:
    removals = [
        manual("tiktok", "https://www.tiktok.com/@6starsclipping/video/777"),
        manual("instagram", "https://www.instagram.com/reel/Cabc/"),
        Removal(post("done"), "removed", "upload_post"),  # removed automatically: not listed
    ]
    messages = removal.manual_removal_messages(removals, "takedown request from someone@example.test")
    assert len(messages) == 1
    title, body = messages[0]
    assert title == "ACTION NEEDED: delete 2 post(s) by hand"
    assert "Why: takedown request from someone@example.test" in body
    assert "1. tiktok @6starsclipping: https://www.tiktok.com/@6starsclipping/video/777" in body
    assert "2. instagram @6starsclippy: https://www.instagram.com/reel/Cabc/" in body
    assert "TikTok app, signed in as @6starsclipping" in body and "Delete" in body
    assert "Instagram app, signed in as @6starsclippy" in body
    assert "the creator is already blocked" in body
    assert len(body) < 3500  # the n8n relay cuts longer bodies


def test_a_post_without_a_stored_link_is_still_listed() -> None:
    _, body = removal.manual_removal_messages([manual("tiktok", None)], "claim")[0]
    assert "(no link stored; platform id 777)" in body


def test_no_message_when_nothing_needs_hands() -> None:
    assert removal.manual_removal_messages([Removal(post(), "removed", "upload_post")], "claim") == []
    assert removal.manual_removal_messages([], "claim") == []


def test_a_long_list_is_split_so_no_message_is_cut_by_the_relay() -> None:
    removals = [
        manual("tiktok", f"https://www.tiktok.com/@6starsclipping/video/{n}", f"p{n}") for n in range(30)
    ]
    messages = removal.manual_removal_messages(removals, "claim")
    assert len(messages) == 3
    assert [title for title, _ in messages] == [
        "ACTION NEEDED: delete 12 post(s) by hand (1)",
        "ACTION NEEDED: delete 12 post(s) by hand (2)",
        "ACTION NEEDED: delete 6 post(s) by hand (3)",
    ]
    assert all(len(body) < 3500 for _, body in messages)
    listed = sum(body.count("https://www.tiktok.com/") for _, body in messages)
    assert listed == 30


def test_a_failed_youtube_removal_gets_the_studio_steps() -> None:
    failed: Any = Removal(post(), "failed", "upload_post", "HTTP 500")
    _, body = removal.manual_removal_messages([failed], "claim")[0]
    assert "studio.youtube.com" in body and "@6starsclips-m3x" in body
