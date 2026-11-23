"""Campaign lanes end to end in the hourly slot (D118, PR 4): a campaign clip below its daily minimum goes
first on the accounts of its platforms, the post carries its campaign and the paid label, a platform the
campaign does not use takes a Normal clip, and a paused campaign gets no post."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from posting_support import IG, T0, TT, YT, add_clip, add_creator, fixed, go_live, posts, production, video

from sixstars import db, settings
from sixstars.campaigns import service
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.posting.hourly import run_hourly_slot
from sixstars.posting.store import DbStore

__all__ = ["production", "video"]  # fixtures


def campaign_clip(video: Path) -> tuple[str, str]:
    db.execute("truncate clip_campaigns restart identity cascade")
    creator = add_creator("CampaignStreamer")
    campaign = service.apply_change(
        "post-test",
        {
            "name": "Post test",
            "rate_per_1k": 1.0,
            "budget_left": 100,
            "ends_at": datetime.now(UTC) + timedelta(days=3),
            "platforms": ["youtube", "instagram"],
            "creator_ids": [creator],
            "quota": {"youtube": "1-2", "instagram": "1-3"},
            "caption_rules": {
                "required_tags": ["#PostTest"],
                "cta": "Watch the full stream",
                "mention": "@x",
            },
        },
        "test",
        create=True,
    )
    service.set_status("post-test", "active", "test")
    clip = add_clip(creator, video, rank=1.0)
    db.execute(
        "update clips set campaign_id = %s, render_profile = 'clean-disclose' where id = %s",
        (campaign.id, clip),
    )
    assert campaign.id is not None
    return clip, campaign.id


def test_the_campaign_clip_goes_first_with_its_tag_caption_and_label(production: None, video: Path) -> None:
    go_live()
    settings.set("posting.cross_platform_min_gap_s", 0)
    clip, campaign_id = campaign_clip(video)
    normal = add_clip(add_creator("Normal"), video, rank=9.0)
    client = FakeUploadPostClient()

    run_hourly_slot(T0, client, clock=fixed())

    by_account = {row["account_id"]: row for row in posts()}
    assert by_account[YT]["clip"] == clip and str(by_account[YT]["campaign_id"]) == campaign_id
    assert by_account[IG]["clip"] == clip and str(by_account[IG]["campaign_id"]) == campaign_id
    assert by_account[TT]["clip"] == normal and by_account[TT]["campaign_id"] is None  # no TikTok quota
    uploads = {call["platform"]: call for call in client.calls_to("upload_video")}
    youtube = uploads["youtube"]["fields"]
    assert youtube["hasPaidProductPlacement"] is True and "first_comment" not in youtube
    assert "Watch the full stream @x" in youtube["youtube_description"]
    assert "#PostTest" in uploads["instagram"]["fields"]["title"]
    assert "first_comment" in uploads["tiktok"]["fields"]  # the Normal post keeps its first comment


def test_a_paused_campaign_gets_no_post_and_is_refused_at_send_time(production: None, video: Path) -> None:
    go_live()
    clip, campaign_id = campaign_clip(video)
    service.set_status("post-test", "paused", "test")
    assert DbStore().campaign_refusal(campaign_id, T0) == "campaign post-test is paused"

    run_hourly_slot(T0, FakeUploadPostClient(), clock=fixed())

    assert all(row["clip"] != clip for row in posts())
