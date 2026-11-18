"""Caption and Upload-Post field building per platform, and the ranking maths (freshness, bonuses, expiry)."""

from datetime import timedelta

import pytest
from posting_helpers import T0, acct, hourly_clip, make_params, production_accounts, staging_accounts

from sixstars.posting.captions import (
    CAPTION_MAX,
    YOUTUBE_TITLE_MAX,
    UnsafePostError,
    build_caption,
    build_fields,
    build_upload_request,
    caption_line,
    credit_line,
    hashtags_for,
    idempotency_key,
    normalise_hashtags,
    truncate,
    youtube_title,
)
from sixstars.posting.models import Platform
from sixstars.posting.ranking import freshness, half_life_h, is_expired, rank_score

FIRST_COMMENT = "Follow {handle} for the best GTA 6 moments every hour 🎮⭐"


def first_comment(handle: str) -> str:
    return FIRST_COMMENT.replace("{handle}", handle)


TAGS = "#GTA6 #GTAVI #GTA6Clips #Gaming #stunt #viceCity"
CAPTION = f"Nobody expected this jump 😱\n🎥 @SomeStreamer on YouTube\n{TAGS}"


# ── caption ───────────────────────────────────────────────────────────────────────────────────────────────
def test_the_caption_has_the_three_documented_lines() -> None:
    assert build_caption(hourly_clip(), make_params(), Platform.TIKTOK) == CAPTION


def test_d18_tiktok_and_instagram_get_the_same_caption_and_youtube_adds_shorts() -> None:
    clip, params = hourly_clip(), make_params()

    assert build_caption(clip, params, Platform.TIKTOK) == build_caption(clip, params, Platform.INSTAGRAM)
    assert build_caption(clip, params, Platform.YOUTUBE) == CAPTION + " #Shorts"


@pytest.mark.parametrize(
    ("handle", "source", "line"),
    [
        ("SomeStreamer", "youtube", "🎥 @SomeStreamer on YouTube"),
        ("@SomeStreamer", "twitch", "🎥 @SomeStreamer on Twitch"),
        ("  xqc  ", "twitch", "🎥 @xqc on Twitch"),
    ],
)
def test_credit_line(handle: str, source: str, line: str) -> None:
    assert credit_line(hourly_clip(creator_handle=handle, source_platform=source)) == line


@pytest.mark.parametrize(
    "changes",
    [{"creator_handle": ""}, {"creator_handle": " @ "}, {"source_platform": "kick"}, {"source_platform": ""}],
)
def test_a_post_without_a_proper_credit_cannot_be_built(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build_caption(hourly_clip(**changes), make_params(), Platform.TIKTOK)


@pytest.mark.parametrize(
    ("changes", "line"),
    [
        ({}, "Nobody expected this jump 😱"),
        ({"caption": "  padded  "}, "padded"),
        ({"caption": None, "hook": "WAIT FOR IT"}, "WAIT FOR IT"),
        ({"caption": "   ", "hook": None}, "He jumped the WHOLE bridge"),
    ],
)
def test_caption_line_falls_back_to_hook_then_title(changes: dict[str, object], line: str) -> None:
    assert caption_line(hourly_clip(**changes)) == line


def test_a_clip_without_any_text_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="no caption"):
        caption_line(hourly_clip(caption=None, hook="", title=None))


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ((), ()),
        (("#GTA6", "gta6", "#Gta6"), ("#GTA6",)),
        (("stunt", " #vice city ", "##double", "#", "", "  "), ("#stunt", "#vicecity", "#double")),
    ],
)
def test_normalise_hashtags(tags: tuple[str, ...], expected: tuple[str, ...]) -> None:
    assert normalise_hashtags(tags) == expected


def test_at_most_five_clip_hashtags_and_no_repeat_of_a_base_hashtag() -> None:
    clip = hourly_clip(hashtags=("#gta6", "#one", "two", "#three", "#four", "#five", "#six"))

    tags = hashtags_for(clip, make_params(), Platform.INSTAGRAM)

    assert tags == ("#GTA6", "#GTAVI", "#GTA6Clips", "#Gaming", "#one", "#two", "#three", "#four", "#five")
    assert hashtags_for(clip, make_params(), Platform.YOUTUBE) == (*tags, "#Shorts")


def test_youtube_extra_hashtag_is_not_repeated_when_the_clip_already_has_it() -> None:
    tags = hashtags_for(hourly_clip(hashtags=("#shorts",)), make_params(), Platform.YOUTUBE)
    assert [t.lower() for t in tags].count("#shorts") == 1


def test_hashtags_come_from_settings() -> None:
    params = make_params(base_hashtags=("#GTA5",), youtube_extra_hashtags=("#Shorts", "#YTGaming"))
    clip = hourly_clip(hashtags=())

    assert hashtags_for(clip, params, Platform.TIKTOK) == ("#GTA5",)
    assert hashtags_for(clip, params, Platform.YOUTUBE) == ("#GTA5", "#Shorts", "#YTGaming")


def test_no_hashtags_at_all_leaves_two_lines() -> None:
    params = make_params(base_hashtags=(), youtube_extra_hashtags=())
    caption = build_caption(hourly_clip(hashtags=()), params, Platform.YOUTUBE)
    assert caption == "Nobody expected this jump 😱\n🎥 @SomeStreamer on YouTube"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("hello", 5, "hello"),
        ("hello!", 5, "hell…"),
        ("hello world", 7, "hello…"),
        ("abc", 1, "…"),
        ("abc", 0, ""),
    ],
)
def test_truncate(text: str, limit: int, expected: str) -> None:
    assert truncate(text, limit) == expected
    assert len(truncate(text, limit)) <= limit


def test_a_long_caption_is_cut_but_credit_and_hashtags_stay_whole() -> None:
    caption = build_caption(hourly_clip(caption="x" * 5000), make_params(), Platform.INSTAGRAM)

    assert len(caption) == CAPTION_MAX
    first, credit, tags = caption.split("\n")
    assert first.endswith("…")
    assert credit == "🎥 @SomeStreamer on YouTube"
    assert tags == TAGS


def test_credit_and_hashtags_longer_than_the_limit_are_refused() -> None:
    with pytest.raises(ValueError, match="exceed"):
        build_caption(hourly_clip(), make_params(), Platform.TIKTOK, limit=40)


# ── YouTube title ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "He jumped the WHOLE bridge"),
        ({"title": "  two\nlines   here "}, "two lines here"),
        ({"title": "<b>angle</b> brackets"}, "bangle/b brackets"),
        ({"title": None}, "Nobody expected this jump 😱"),
        ({"title": "   "}, "Nobody expected this jump 😱"),
        ({"title": "a" * 100}, "a" * 100),
        ({"title": "a" * 101}, "a" * 99 + "…"),
    ],
)
def test_youtube_title(changes: dict[str, object], expected: str) -> None:
    title = youtube_title(hourly_clip(**changes))

    assert title == expected
    assert len(title) <= YOUTUBE_TITLE_MAX


def test_a_title_of_only_angle_brackets_is_refused() -> None:
    with pytest.raises(ValueError, match="title"):
        youtube_title(hourly_clip(title="<<>>"))


# ── fields per platform ───────────────────────────────────────────────────────────────────────────────────
def test_tiktok_fields() -> None:
    fields = build_fields(hourly_clip(), production_accounts()["tiktok_main"], make_params())

    assert fields == {
        "title": CAPTION,
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "first_comment": first_comment("@6starsclipping"),
    }


def test_instagram_fields() -> None:
    fields = build_fields(hourly_clip(), production_accounts()["instagram_main"], make_params())

    expected = first_comment("@6starsclippy")
    assert fields == {"title": CAPTION, "media_type": "REELS", "first_comment": expected}


def test_youtube_fields() -> None:
    fields = build_fields(hourly_clip(), production_accounts()["youtube_main"], make_params())

    assert fields == {
        "title": "He jumped the WHOLE bridge",
        "youtube_title": "He jumped the WHOLE bridge",
        "youtube_description": CAPTION + " #Shorts",
        "categoryId": "20",
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
        "first_comment": first_comment("@6starsClips-m3x"),
    }


@pytest.mark.parametrize(
    ("account_id", "field", "value"),
    [("tiktok_main", "privacy_level", "SELF_ONLY"), ("youtube_main", "privacyStatus", "private")],
)
def test_staging_privacy_comes_from_the_account_row(account_id: str, field: str, value: str) -> None:
    fields = build_fields(hourly_clip(), staging_accounts()[account_id], make_params("staging"))

    assert fields[field] == value
    assert "PUBLIC_TO_EVERYONE" not in fields.values()
    assert "public" not in fields.values()


def test_instagram_gets_no_privacy_field_at_all() -> None:
    fields = build_fields(hourly_clip(), acct("instagram_main", privacy="private"), make_params())
    assert not {"privacy_level", "privacyStatus"} & set(fields)


@pytest.mark.parametrize(
    ("account_id", "privacy"),
    [
        ("tiktok_main", None),
        ("tiktok_main", "private"),
        ("youtube_main", None),
        ("youtube_main", "SELF_ONLY"),
    ],
)
def test_fields_are_not_built_with_a_missing_or_foreign_privacy_value(
    account_id: str, privacy: str | None
) -> None:
    with pytest.raises(ValueError, match="privacy"):
        build_fields(hourly_clip(), acct(account_id, privacy=privacy), make_params())


@pytest.mark.parametrize("first_comment", ["", "   "])
def test_an_empty_first_comment_is_left_out(first_comment: str) -> None:
    fields = build_fields(hourly_clip(), acct("instagram_main"), make_params(first_comment=first_comment))
    assert "first_comment" not in fields


def test_the_first_comment_comes_from_settings() -> None:
    params = make_params(first_comment="Follow for more (staging test)")
    assert build_fields(hourly_clip(), acct("instagram_main"), params)["first_comment"] == (
        "Follow for more (staging test)"
    )


def test_each_platform_names_its_own_account_in_the_first_comment() -> None:
    params = make_params(first_comment="Follow {handle} for more")
    comments = {
        account.id: build_fields(hourly_clip(), account, params)["first_comment"]
        for account in production_accounts().values()
    }
    assert comments["tiktok_main"] == "Follow @6starsclipping for more"
    assert comments["instagram_main"] == "Follow @6starsclippy for more"
    assert comments["youtube_main"] == "Follow @6starsClips-m3x for more"


def test_a_handle_stored_without_the_at_sign_still_gets_one() -> None:
    params = make_params(first_comment="Follow {handle}")
    fields = build_fields(hourly_clip(), acct("instagram_main", handle="6starsclippy"), params)
    assert fields["first_comment"] == "Follow @6starsclippy"


def test_the_client_adds_user_platform_and_request_id_itself() -> None:
    for account in production_accounts().values():
        fields = build_fields(hourly_clip(), account, make_params())
        assert not {"user", "platform[]", "platform", "request_id", "async_upload", "video"} & set(fields)


def test_youtube_description_has_no_angle_brackets() -> None:
    clip = hourly_clip(caption="he said <wow> twice")
    fields = build_fields(clip, acct("youtube_main"), make_params())
    assert "<" not in str(fields["youtube_description"]) and ">" not in str(fields["youtube_description"])


# ── upload request ────────────────────────────────────────────────────────────────────────────────────────
def test_idempotency_key_is_one_per_clip_and_account() -> None:
    assert idempotency_key("clip-uuid", "youtube_main") == "post:clip-uuid:youtube_main"


@pytest.mark.parametrize("account_id", ["tiktok_main", "instagram_main", "youtube_main"])
def test_upload_request_for_production(account_id: str) -> None:
    account = production_accounts()[account_id]

    request = build_upload_request(hourly_clip("clip-1"), account, make_params())

    assert (request.clip_id, request.account_id) == ("clip-1", account_id)
    assert (request.user, request.platform) == ("default", account.platform.value)
    assert request.idempotency_key == request.request_id == f"post:clip-1:{account_id}"
    assert request.fields == build_fields(hourly_clip("clip-1"), account, make_params())


@pytest.mark.parametrize(
    ("account_id", "privacy", "allow_instagram", "ok"),
    [
        ("tiktok_main", "SELF_ONLY", False, True),
        ("tiktok_main", "PUBLIC_TO_EVERYONE", False, False),
        ("youtube_main", "private", False, True),
        ("youtube_main", "public", False, False),
        ("youtube_main", "unlisted", False, False),
        ("instagram_main", None, False, False),
        ("instagram_main", None, True, True),
    ],
)
def test_a_sendable_request_is_never_built_for_a_public_staging_post(
    account_id: str, privacy: str | None, allow_instagram: bool, ok: bool
) -> None:
    params = make_params("staging", staging_allow_instagram=allow_instagram)
    account = acct(account_id, privacy=privacy)

    if ok:
        assert build_upload_request(hourly_clip(), account, params).account_id == account_id
    else:
        with pytest.raises(UnsafePostError):
            build_upload_request(hourly_clip(), account, params)
    assert build_upload_request(hourly_clip(), account, params, preview=True).account_id == account_id


def test_a_sendable_request_is_never_built_for_a_disabled_account() -> None:
    with pytest.raises(UnsafePostError, match="disabled"):
        build_upload_request(hourly_clip(), production_accounts()["tiktok_live"], make_params())


# ── ranking ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("age_h", "half_life", "expected"),
    [(0, 6, 1.0), (6, 6, 0.5), (12, 6, 0.25), (3, 6, 0.5**0.5), (24, 24, 0.5), (72, 24, 0.125), (-2, 6, 1.0)],
)
def test_freshness_halves_every_half_life(age_h: float, half_life: float, expected: float) -> None:
    assert freshness(age_h, half_life) == pytest.approx(expected)


def test_half_life_per_source_comes_from_settings() -> None:
    params = make_params(freshness_half_life_live_h=3.0, freshness_half_life_vod_h=12.0)

    assert (half_life_h("live", params), half_life_h("vod", params)) == (3.0, 12.0)
    with pytest.raises(ValueError, match="source type"):
        half_life_h("clip", params)


@pytest.mark.parametrize(
    ("source", "tier", "preferred", "age_h", "expected"),
    [
        ("vod", None, False, 0, 8.0),
        ("vod", None, False, 24, 4.0),
        ("live", None, False, 6, 4.0),
        ("live", "S", False, 0, 8.0 * 1.3),
        ("live", "A", False, 0, 8.0 * 1.15),
        ("live", "B", False, 0, 8.0),
        ("vod", None, True, 0, 8.0 * 1.1),
        ("live", "S", True, 6, 8.0 * 0.5 * 1.3 * 1.1),
    ],
)
def test_rank_score(source: str, tier: str | None, preferred: bool, age_h: float, expected: float) -> None:
    score = rank_score(8.0, source, tier, preferred, T0 - timedelta(hours=age_h), T0, make_params())
    assert score == pytest.approx(expected)


@pytest.mark.parametrize(
    ("age", "expired"),
    [
        (timedelta(hours=71, minutes=59), False),
        (timedelta(hours=72), False),
        (timedelta(hours=72, seconds=1), True),
        (timedelta(days=30), True),
        (timedelta(0), False),
    ],
)
def test_expiry_after_the_maximum_clip_age(age: timedelta, expired: bool) -> None:
    assert is_expired(T0 - age, T0, make_params()) is expired


def test_expiry_age_comes_from_settings() -> None:
    assert is_expired(T0 - timedelta(hours=10), T0, make_params(max_clip_age_h=9))
    assert not is_expired(T0 - timedelta(hours=10), T0, make_params(max_clip_age_h=10))
