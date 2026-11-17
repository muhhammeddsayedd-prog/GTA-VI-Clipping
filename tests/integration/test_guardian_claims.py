"""The claims and removals check against Postgres with fake platforms: a problem blocks the creator,
takes their posts down (or flags them for the owner), records a strike, and two strikes stop posting."""

from datetime import timedelta

from guardian_support import (
    IG,
    TT,
    YT,
    UploadPost,
    YouTube,
    creator,
    event_types,
    now,
    outbox,
    post_states,
    published,
    row,
    setting,
    takedown_rows,
    titles,
    world,
)
from posting_support import add_clip

from sixstars import db, settings
from sixstars.clients._http import ApiError
from sixstars.guardian import claims

__all__ = ["world"]  # fixture

STOP_TITLE = "EMERGENCY STOP: all posting halted"


def check(upload_post: UploadPost, youtube: YouTube) -> claims.CheckResult:
    return claims.run_check(upload_post, youtube, now())


def test_a_removed_youtube_video_blocks_the_creator_and_takes_their_other_posts_down(world: None) -> None:
    bad = creator("BadLuck")
    other = creator("Innocent")
    published(bad, YT, "ytGONE00001")
    published(bad, YT, "ytOTHER0001")
    published(
        bad, TT, "7300000000000000001", url="https://www.tiktok.com/@6starsclipping/video/7300000000000000001"
    )
    published(bad, IG, "ig-dry", dry_run=True, status="skipped")
    waiting_clip = add_clip(bad, None, status="approved")
    pending = published(bad, IG, "ig-pending", status="pending")
    published(other, YT, "ytFINE00001")
    upload_post, youtube = UploadPost(), YouTube(oauth=False)
    youtube.missing = {"ytGONE00001"}

    result = check(upload_post, youtube)

    assert result.status == "ok" and result.checked == 4 and result.emergency_stop is False
    assert [(p["platform"], p["kind"], p["creator"]) for p in result.problems] == [
        ("youtube", "removed_by_platform", "BadLuck")
    ]
    states = post_states()
    assert states["ytGONE00001"] == ("removed", "removed_by_platform")  # gone already: nothing to delete
    assert states["ytOTHER0001"][0] == "removed"  # deleted through Upload-Post (no OAuth token)
    assert states["7300000000000000001"][0] == "remove_failed"  # TikTok cannot be deleted by API
    assert states["ig-dry"][0] == "skipped"  # a dry-run row was never public
    assert states["ytFINE00001"] == ("published", "ok")
    assert upload_post.deleted == [("default", "youtube", "ytOTHER0001")]
    assert youtube.calls_to("delete_video") == []
    assert row("posts", pending)["status"] == "skipped"
    assert row("clips", waiting_clip)["status"] == "removed"

    blocked = row("creators", bad)
    assert (blocked["tier"], blocked["claims_count"]) == ("blocked", 1) and blocked["blocked_at"] is not None
    assert row("creators", other)["tier"] == "auto"

    (incident,) = takedown_rows()
    assert (incident["source"], incident["status"], incident["is_takedown"]) == ("platform", "partial", True)
    assert str(incident["creator_id"]) == bad and len(incident["matched_post_ids"]) == 1
    assert incident["actions"]["strike"] is True
    assert result.strikes == 1 and result.manual_removals_pending == 1

    types = event_types()
    for expected in ("post.claimed", "creator.blocked", "post.removed", "post.remove_failed", "clip.removed"):
        assert expected in types, expected
    critical = titles("critical")
    assert critical == ["Claim or removal: BadLuck", "ACTION NEEDED: delete 1 post(s) by hand"]
    manual_body = outbox()[-1][2]
    assert "https://www.tiktok.com/@6starsclipping/video/7300000000000000001" in manual_body
    assert "signed in as @6starsclipping" in manual_body and "Delete" in manual_body

    # The next pass finds nothing new: no second strike, no second alert.
    again = check(upload_post, youtube)
    assert again.problems == [] and again.strikes == 1 and len(takedown_rows()) == 1
    assert titles("critical") == critical
    assert row("creators", bad)["claims_count"] == 1


def test_two_strikes_stop_all_posting_and_only_the_owner_restarts_it(world: None) -> None:
    first, second, third = creator("One"), creator("Two"), creator("Three")
    published(first, YT, "ytAAAAAAAA1")
    published(second, YT, "ytBBBBBBBB1")
    published(third, YT, "ytCCCCCCCC1")
    for index in range(8):  # enough healthy posts that this is not an account-level event
        published(creator(f"Fine{index}"), YT, f"ytFINE{index:05d}")
    upload_post, youtube = UploadPost(), YouTube()

    youtube.missing = {"ytAAAAAAAA1"}
    assert check(upload_post, youtube).emergency_stop is False
    assert setting("control.emergency_stop") is False  # one strike is below the limit of two

    youtube.missing = {"ytAAAAAAAA1", "ytBBBBBBBB1"}
    result = check(upload_post, youtube)
    assert result.emergency_stop is True and result.strikes == 2
    assert setting("control.emergency_stop") is True
    assert "2 strike(s)" in setting("control.stop_reason") and "the limit is 2" in setting(
        "control.stop_reason"
    )
    assert titles("critical").count(STOP_TITLE) == 1
    assert event_types("guardian.stop") == ["guardian.stop"]

    # More passes while stopped: the stop stays, and it is not announced again.
    check(upload_post, youtube)
    assert titles("critical").count(STOP_TITLE) == 1 and event_types("guardian.stop") == ["guardian.stop"]

    # The guardian never clears it. The owner does; the old strikes then do not stop posting again.
    settings.set("control.emergency_stop", False)
    settings.set("control.stop_reason", None)
    result = check(upload_post, youtube)
    assert result.emergency_stop is False and result.strikes == 0
    assert setting("control.emergency_stop") is False

    # One new strike after the restart is below the limit again.
    youtube.missing.add("ytCCCCCCCC1")
    result = check(upload_post, youtube)
    assert result.strikes == 1 and result.emergency_stop is False


def test_a_rejected_video_that_still_exists_is_deleted(world: None) -> None:
    bad = creator("Claimed")
    published(bad, YT, "ytREJECTED1")
    published(creator("Fine"), YT, "ytFINE00001")
    upload_post, youtube = UploadPost(), YouTube()
    youtube.items["ytREJECTED1"] = {"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}

    result = check(upload_post, youtube)

    assert [(p["kind"], p["detail"]) for p in result.problems] == [
        ("claimed", "rejected by YouTube: copyright")
    ]
    assert post_states()["ytREJECTED1"] == ("removed", "claimed")
    assert upload_post.deleted == [("default", "youtube", "ytREJECTED1")]
    (incident,) = takedown_rows()
    assert (incident["source"], incident["status"]) == ("claim", "actioned")
    assert row("creators", bad)["tier"] == "blocked"


def test_with_the_oauth_token_a_private_video_is_checked_and_deleted_with_it(world: None) -> None:
    db.execute("update platform_accounts set privacy = 'private' where id = %s", (YT,))
    bad = creator("Staging")
    published(bad, YT, "ytPRIVATE01")
    published(bad, YT, "ytPRIVATE02")
    upload_post = UploadPost()

    # Without the token a private video cannot be judged: it is counted, never guessed.
    without = check(upload_post, YouTube(oauth=False))
    assert without.problems == [] and without.unverifiable == 2 and without.checked == 0

    youtube = YouTube(oauth=True)
    youtube.missing = {"ytPRIVATE01"}
    result = check(upload_post, youtube)
    assert [p["kind"] for p in result.problems] == ["removed_by_platform"]
    assert youtube.deleted == ["ytPRIVATE02"] and upload_post.deleted == []
    assert post_states()["ytPRIVATE02"][0] == "removed"


def test_when_most_posts_of_a_platform_vanish_posting_stops_and_no_creator_is_blamed(world: None) -> None:
    creators = [creator(f"Creator{index}") for index in range(6)]
    for index, creator_id in enumerate(creators):
        published(creator_id, YT, f"ytMASS{index:05d}")
        published(creator_id, TT, f"73{index:017d}")
    upload_post, youtube = UploadPost(), YouTube()
    youtube.missing = {f"ytMASS{index:05d}" for index in range(6)}

    result = check(upload_post, youtube)

    assert result.mass_event == ["youtube"] and result.emergency_stop is True
    assert "6 posts vanished from youtube" in setting("control.stop_reason")
    rows = db.fetch_all("select tier, claims_count from creators")
    assert {(r["tier"], r["claims_count"]) for r in rows} == {("auto", 0)}
    assert all(state == ("published", "ok") for key, state in post_states().items() if key.startswith("73"))
    assert titles("critical") == [STOP_TITLE]
    (incident,) = takedown_rows()
    assert incident["creator_id"] is None and incident["actions"]["mass"] == ["youtube"]


def test_an_api_error_or_a_rate_limit_never_becomes_a_finding(world: None) -> None:
    published(creator("A"), YT, "ytAAAAAAAA1")
    published(creator("B"), TT, "7300000000000000002")
    upload_post, youtube = UploadPost(), YouTube()
    youtube.fail_list = ApiError("youtube", "GET /videos", 403, "quotaExceeded")
    upload_post.fail = RuntimeError("connection reset")

    result = check(upload_post, youtube)

    assert result.problems == [] and result.checked == 0
    assert result.errors == ["youtube api: ApiError"]
    assert takedown_rows() == [] and outbox() == []
    assert {state for state in post_states().values()} == {("published", None)}


def test_only_posts_inside_the_window_are_read(world: None) -> None:
    someone = creator("Someone")
    published(someone, YT, "ytTOOFRESH1", age=timedelta(minutes=5))
    published(someone, YT, "ytTOOOLD001", age=timedelta(hours=80))
    published(someone, YT, "ytINWINDOW1", age=timedelta(hours=10))
    youtube = YouTube()
    youtube.missing = {"ytTOOFRESH1", "ytTOOOLD001"}

    result = check(UploadPost(), youtube)

    assert result.checked == 1 and result.problems == []
    assert youtube.calls_to("videos_list")[0]["ids"] == ["ytINWINDOW1"]


def test_an_instagram_post_needs_two_passes_and_a_working_token(world: None) -> None:
    bad = creator("InstaGone")
    published(bad, IG, "17900000000000001")
    published(creator("Fine"), IG, "17900000000000002")
    upload_post, youtube = UploadPost(), YouTube()
    upload_post.gone = {"17900000000000001"}

    first = check(upload_post, youtube)
    assert first.problems == [] and first.suspects == 1
    assert post_states()["17900000000000001"] == ("published", "suspect")
    assert row("creators", bad)["tier"] == "auto"

    second = check(upload_post, youtube)
    assert [p["kind"] for p in second.problems] == ["removed_by_platform"]
    assert post_states()["17900000000000001"] == ("removed", "removed_by_platform")
    assert row("creators", bad)["tier"] == "blocked"


def test_a_post_the_owner_deleted_by_hand_is_marked_removed_without_a_new_strike(world: None) -> None:
    bad = creator("Handled")
    waiting = published(bad, TT, "7300000000000000009", status="remove_failed")
    db.execute("update creators set tier = 'blocked' where id = %s", (bad,))
    published(creator("Fine"), TT, "7300000000000000010")
    upload_post, youtube = UploadPost(), YouTube()

    still_there = check(upload_post, youtube)
    assert still_there.removed_by_owner == 0 and still_there.manual_removals_pending == 1
    assert outbox() == []  # the owner was told when it happened; no reminder every 15 minutes

    upload_post.gone = {"7300000000000000009"}
    result = check(upload_post, youtube)
    assert result.removed_by_owner == 1 and result.manual_removals_pending == 0
    assert row("posts", waiting)["status"] == "removed"
    assert result.problems == [] and takedown_rows() == [] and result.strikes == 0


def test_a_post_of_a_blocked_creator_that_went_public_later_is_swept(world: None) -> None:
    blocked = creator("Blocked")
    db.execute("update creators set tier = 'blocked' where id = %s", (blocked,))
    published(blocked, YT, "ytLATE00001", age=timedelta(minutes=1))  # too fresh for the check itself
    published(blocked, TT, "7300000000000000020", age=timedelta(minutes=1))
    upload_post = UploadPost()

    check(upload_post, YouTube())

    assert upload_post.deleted == [("default", "youtube", "ytLATE00001")]
    states = post_states()
    assert states["ytLATE00001"][0] == "removed" and states["7300000000000000020"][0] == "remove_failed"
    assert titles("critical") == ["ACTION NEEDED: delete 1 post(s) by hand"]
    # The next sweep leaves the flagged TikTok post alone and does not alert again.
    check(upload_post, YouTube())
    assert titles("critical") == ["ACTION NEEDED: delete 1 post(s) by hand"]


def test_a_second_pass_at_the_same_time_does_nothing(world: None) -> None:
    published(creator("A"), YT, "ytAAAAAAAA1")
    youtube = YouTube()
    with claims.single_pass(claims.LOCK_NAME) as locked:
        assert locked is True
        result = check(UploadPost(), youtube)
    assert result.status == "already_running" and youtube.calls == []
    assert check(UploadPost(), youtube).status == "ok"  # the lock is free again
