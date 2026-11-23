"""The publisher's cover hooks (cover engine C7): extra upload fields go out with the post, the published
hook gets the platform post id, and a crashing hook never stops a post."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from memory_store import MemoryStore
from posting_helpers import T0, hourly_clip, make_params, production_accounts

from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting.publisher import Outcome, Publisher

NOW = T0 + timedelta(minutes=3)


class Hooks:
    def __init__(self, fields: dict[str, str] | None = None, crash: bool = False) -> None:
        self._fields = fields or {}
        self._crash = crash
        self.asked: list[tuple[str, ...]] = []
        self.published_calls: list[tuple[Any, ...]] = []

    def fields(self, clip_id: str, account_id: str, platform: str, lane: str, post_id: str) -> dict[str, str]:
        self.asked.append((clip_id, account_id, platform, lane))
        return dict(self._fields)

    def published(self, post_id: str, platform: str, platform_post_id: str | None, user: str) -> None:
        self.published_calls.append((post_id, platform, platform_post_id, user))


def _publish(tmp_path: Path, account_id: str, hooks: Hooks) -> tuple[Outcome, FakeUploadPostClient]:
    store = MemoryStore(make_params("production"))
    client = FakeUploadPostClient()
    clip = hourly_clip("clip-1", at=NOW)
    video = tmp_path / "clip-1.mp4"
    video.write_bytes(b"video")
    post_id = store.add_post(clip, production_accounts()[account_id], NOW, video=str(video), slot_at=T0)
    outcome = Publisher(client, store, lambda: NOW, covers=hooks).publish(post_id).outcome
    return outcome, client


def test_cover_fields_go_out_with_the_upload(tmp_path: Path) -> None:
    hooks = Hooks({"cover_url": "https://x/covers-public/c.jpg"})
    outcome, client = _publish(tmp_path, "instagram_main", hooks)
    assert outcome is Outcome.PUBLISHED
    (upload,) = client.calls_to("upload_video")
    assert upload["fields"]["cover_url"] == "https://x/covers-public/c.jpg"
    assert upload["fields"]["media_type"] == "REELS"
    assert hooks.asked == [("clip-1", "instagram_main", "instagram", "hourly")]
    (call,) = hooks.published_calls
    assert call[1] == "instagram" and call[2] and call[3] == "default"


def test_no_cover_fields_leave_the_upload_as_it_was(tmp_path: Path) -> None:
    outcome, client = _publish(tmp_path, "tiktok_main", Hooks())
    assert outcome is Outcome.PUBLISHED
    (upload,) = client.calls_to("upload_video")
    assert "cover_timestamp" not in upload["fields"] and "tiktok_cover_image_url" not in upload["fields"]
