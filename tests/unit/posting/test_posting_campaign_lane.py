"""Campaign lanes in posting (D118, PR 4): slot order per account, campaign hours, daily min/max, the
60-minute campaign gap, the Normal lane's room before 18 UTC, captions and paid labels. Pure."""

from dataclasses import replace
from datetime import timedelta

from posting_helpers import T0, acct, hourly_clip, make_params, post

from sixstars.posting import campaign_lane
from sixstars.posting.campaign_lane import CampaignQuota, LaneRules
from sixstars.posting.captions import build_caption, build_fields
from sixstars.posting.models import Platform
from sixstars.posting.selection import plan_hourly_slot

RULES = LaneRules(hours=frozenset(range(8, 22)), return_unused_hour=18, gap_s=3600)
NOON = T0  # 12:00 UTC: campaign hours, before the return hour
K1 = CampaignQuota("k1", "dylan", min_day=2, max_day=3)
K2 = CampaignQuota("k2", "tomo", min_day=1, max_day=2)


def camp(clip_id: str, campaign: str, rank: float = 5.0):  # type: ignore[no-untyped-def]
    return hourly_clip(clip_id, rank, creator=f"c-{clip_id}", campaign_id=campaign)


def order(candidates, history=(), quotas=None, now=NOON, account=None):  # type: ignore[no-untyped-def]
    return [
        clip.id
        for clip in campaign_lane.order_candidates(
            account or acct("instagram_main"),
            list(candidates),
            list(history),
            {"k1": K1, "k2": K2} if quotas is None else quotas,
            RULES,
            now,
            False,
        )
    ]


def cpost(campaign: str, at, account: str = "instagram_main"):  # type: ignore[no-untyped-def]
    return replace(post(account, at), campaign_id=campaign)


def test_campaigns_below_their_minimum_go_first_furthest_below_first() -> None:
    clips = [hourly_clip("n1", 9.0), camp("a", "k2", 8.0), camp("b", "k1", 1.0)]
    assert order(clips) == ["b", "a", "n1"]  # k1 needs 2, k2 needs 1


def test_campaigns_at_their_minimum_come_after_normal_and_stop_at_their_maximum() -> None:
    history = [cpost("k1", NOON - timedelta(hours=3)), cpost("k1", NOON - timedelta(hours=2))]
    clips = [camp("b", "k1"), hourly_clip("n1")]
    assert order(clips, history, quotas={"k1": K1}) == ["n1", "b"]
    history.append(cpost("k1", NOON - timedelta(hours=1, minutes=30)))
    assert order(clips, history, quotas={"k1": K1}) == ["n1"]  # 3 = max


def test_a_campaign_clip_waits_outside_campaign_hours_and_for_a_closed_campaign() -> None:
    clips = [camp("b", "k1"), hourly_clip("n1")]
    assert order(clips, now=NOON.replace(hour=23)) == ["n1"]
    assert order(clips, quotas={"k2": K2}) == ["n1"]  # k1 not open on this account


def test_the_same_campaign_waits_sixty_minutes_on_one_account() -> None:
    clips = [camp("b", "k1"), hourly_clip("n1")]
    recent = [cpost("k1", NOON - timedelta(minutes=30))]
    assert order(clips, recent, quotas={"k1": K1}) == ["n1"]
    older = [cpost("k1", NOON - timedelta(minutes=61))]
    assert order(clips, older, quotas={"k1": K1}) == ["b", "n1"]
    other_account = [cpost("k1", NOON - timedelta(minutes=5), account="youtube_main")]
    assert order(clips, other_account, quotas={"k1": K1}) == ["b", "n1"]


def test_normal_leaves_room_for_open_minimums_until_the_return_hour() -> None:
    small = acct("instagram_main", cap_24h=5)
    history = [post("instagram_main", NOON - timedelta(hours=h)) for h in range(1, 3)]  # 2 normal today
    clips = [hourly_clip("n1")]
    # cap 5 - open minimums 3 (k1 2 + k2 1) = 2 normal posts before 18 UTC: no more room.
    assert order(clips, history, account=small) == []
    assert order(clips, history, account=small, now=NOON.replace(hour=18)) == ["n1"]
    assert order(clips, history, account=small, quotas={}) == ["n1"]


def test_the_slot_plan_uses_the_campaign_order() -> None:
    accounts = [acct("instagram_main")]
    queue = [hourly_clip("n1", 9.0), camp("b", "k1", 1.0)]
    plan = plan_hourly_slot(
        accounts,
        [],
        queue,
        NOON,
        NOON + timedelta(minutes=1),
        make_params(),
        campaign_quotas={"instagram_main": {"k1": K1}},
        lane_rules={"instagram": RULES},
    )
    chosen = [d.clip_id for d in plan.decisions if d.clip_id]
    assert chosen == ["b"]
    # Without campaign lanes a campaign clip is never taken as a Normal clip.
    plain = plan_hourly_slot(
        accounts, [], [camp("b", "k1")], NOON, NOON + timedelta(minutes=1), make_params()
    )
    assert [d.clip_id for d in plain.decisions if d.clip_id] == []


# ── captions and paid labels ─────────────────────────────────


def test_campaign_caption_order_and_required_parts_are_never_cut() -> None:
    clip = replace(
        hourly_clip("b", caption="x" * 3000),
        campaign_id="k1",
        campaign_line="Watch the marathon @DylanPage",
        campaign_tags=("#DylanPage",),
    )
    text = build_caption(clip, make_params(), Platform.INSTAGRAM)
    lines = text.split("\n")
    assert lines[1].startswith("🎥 @SomeStreamer")
    assert lines[2] == "Watch the marathon @DylanPage"
    assert lines[3].startswith("#DylanPage #GTA6")
    assert len(text) <= 2200 and lines[0].endswith("…")


def test_campaign_posts_get_paid_labels_and_no_first_comment() -> None:
    clip = replace(hourly_clip("b"), campaign_id="k1")
    params = make_params()
    youtube = build_fields(clip, acct("youtube_main"), params)
    assert youtube["hasPaidProductPlacement"] is True and "first_comment" not in youtube
    tiktok = build_fields(clip, acct("tiktok_main"), params)
    assert tiktok["brand_content_toggle"] is True
    private = build_fields(clip, acct("tiktok_main", privacy="SELF_ONLY"), make_params("staging"))
    assert "brand_content_toggle" not in private  # Upload-Post refuses the label on SELF_ONLY posts
    instagram = build_fields(clip, acct("instagram_main"), params)
    assert "first_comment" not in instagram and "paid_partnership" not in instagram
    normal = build_fields(hourly_clip("n1"), acct("instagram_main"), params)
    assert "first_comment" in normal and "hasPaidProductPlacement" not in normal
