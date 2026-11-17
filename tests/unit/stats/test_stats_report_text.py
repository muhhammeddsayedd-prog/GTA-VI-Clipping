"""The daily report as Telegram text: all the counts are there, hostile text is escaped, and the
message always fits."""

import re
from copy import deepcopy
from typing import Any

import pytest

from sixstars.stats import report

HOSTILE = '<script>alert("x")</script> & <b>bold</b> </b><a href="tg://user?id=1">click</a> \n\n line'


def sample() -> dict[str, Any]:
    return {
        "day": "2026-11-19",
        "timezone": "UTC",
        "generated_at": "2026-11-20T00:00:05+00:00",
        "env": "staging",
        "accounts": [
            {
                "account_id": "instagram_main",
                "platform": "instagram",
                "handle": "@6starsclippy",
                "enabled": True,
                "paused_until": None,
                "cap_24h": 45,
                "followers": 820,
                "followers_change": 390,
                "views_total": 150000,
                "views_change": 60000,
                "published": 24,
                "failed": 1,
                "dry_run": 0,
            },
            {
                "account_id": "tiktok_live",
                "platform": "tiktok",
                "handle": "@6starsclipping",
                "enabled": False,
                "paused_until": None,
                "cap_24h": 14,
                "followers": None,
                "followers_change": None,
                "views_total": None,
                "views_change": None,
                "published": 0,
                "failed": 0,
                "dry_run": 0,
            },
            {
                "account_id": "tiktok_main",
                "platform": "tiktok",
                "handle": "@6starsclipping",
                "enabled": True,
                "paused_until": "2026-11-20T03:00:00+00:00",
                "cap_24h": 14,
                "followers": 1240,
                "followers_change": -3,
                "views_total": 162000,
                "views_change": 70000,
                "published": 14,
                "failed": 0,
                "dry_run": 0,
            },
            {
                "account_id": "youtube_main",
                "platform": "youtube",
                "handle": "@6starsclips-m3x",
                "enabled": True,
                "paused_until": None,
                "cap_24h": 28,
                "followers": 1050,
                "followers_change": None,
                "views_total": 100000,
                "views_change": 50000,
                "published": 23,
                "failed": 0,
                "dry_run": 0,
            },
        ],
        "views": {
            "total": 412000,
            "change": 180000,
            "median_by_platform": {"instagram": 3400, "tiktok": 6100, "youtube": 5200},
        },
        "top_clips": [
            {
                "title": "He did NOT expect that 😭",
                "views": 88000,
                "platform": "youtube",
                "post_url": "https://y.test/1",
            },
            {"title": "Second best", "views": 51000, "platform": "tiktok", "post_url": None},
        ],
        "ypp": {
            "subscribers": 1050,
            "subscribers_target": 1000,
            "shorts_views_90d": 2_100_000,
            "shorts_views_target": 10_000_000,
        },
        "posts": {
            "hourly_published": 50,
            "spike_published": 11,
            "failed": 1,
            "dry_run": 0,
            "slots_used": 24,
            "slots_skipped_events": 2,
        },
        "spikes": {"detected": 140, "S": 12, "A": 35, "B": 41, "rejected": 52, "median_spike_to_post_s": 190},
        "clips": {
            "rendered": 96,
            "approved": 61,
            "rejected": 35,
            "rejected_by_reason": {"score": 14, "music": 9, "duplicate": 9, "spoiler": 3},
            "expired": 2,
            "queue": {"spike": 1, "hourly": 9, "low_watermark": 12},
        },
        "discovery": {
            "videos_processed": 18,
            "candidates_checked": 240,
            "new_creators": 27,
            "new_by_tier": {"auto": 22, "preferred": 3, "restricted": 2},
        },
        "guardian": {
            "claims": 1,
            "takedowns": 0,
            "emails_ignored": 7,
            "emails_open": 1,
            "strikes": 1,
            "strike_limit": 2,
            "emergency_stop": False,
            "stop_reason": None,
            "manual_removals": [],
        },
        "spend": [
            {"provider": "anthropic", "spent_usd": 14.2, "budget_usd": 25.0},
            {"provider": "transcription", "spent_usd": 1.8, "budget_usd": 5.0},
            {"provider": "apify", "spent_usd": 0.31, "budget_usd": 0.4},
        ],
        "issues": ["14:02 youtube_main rate limited: cap lowered 28 -> 26"],
    }


def visible(text: str) -> str:
    """The text as Telegram shows it: our own tags removed."""
    return re.sub(r"</?b>", "", text)


def test_the_report_carries_every_mandatory_count() -> None:
    text = report.render_telegram(sample())
    expected = [
        "daily report (2026-11-19 UTC, staging)",
        "Followers  instagram_main 820 (+390) · tiktok_main 1.2k (-3) · youtube_main 1.1k",
        "Views  total 412k (+180k) · median per video: IG 3.4k · TT 6.1k · YT 5.2k",
        "Top clip  “He did NOT expect that 😭” — 88k views (YT)",
        "YPP  subs 1.1k / 1.0k ✅ · Shorts views 90d 2.1M / 10M (21%)",
        "Posts  hourly 50 (24/24 slots, 2 skips) · spike 11 · failed 1",
        "per account: instagram_main 24/45 (failed 1) · tiktok_main 14/14 ⏸ · youtube_main 23/28",
        "Spikes  detected 140 · S 12 · A 35 · B 41 · rejected 52 · median spike→post 3m10s",
        "Clips  rendered 96 · approved 61 · rejected 35 (score 14, music 9, duplicate 9, spoiler 3) "
        "· expired 2",
        "Queue  hourly 9 ⚠️ low · spike 1",
        "Discovery  videos processed 18 · candidates 240 · new creators 27 "
        "(auto 22, preferred 3, restricted 2)",
        "Guardian  claims 1 · takedowns 0 · strikes 1/2 · stop: off · emails ignored 7 · emails to read 1",
        "Spend  anthropic $14.20/$25 · transcription $1.80/$5 · apify $0.31/$0.4",
        "Issues (1)",
        "14:02 youtube_main rate limited: cap lowered 28 -&gt; 26",
    ]
    shown = visible(text)
    for line in expected:
        assert line in shown, line
    assert "tiktok_live" not in shown  # a disabled account is not listed
    assert len(text) <= report.TELEGRAM_MAX_CHARS


def test_hostile_titles_reasons_and_handles_cannot_inject_markup() -> None:
    data = sample()
    data["top_clips"][0]["title"] = HOSTILE
    data["top_clips"][1]["title"] = "&lt;already&gt; & <i>x</i>"
    data["accounts"][0]["account_id"] = "<i>acct</i>&"
    data["clips"]["rejected_by_reason"] = {"<u>music</u>": 3}
    data["discovery"]["new_by_tier"] = {"<s>tier</s>": 1}
    data["guardian"].update(emergency_stop=True, stop_reason="</b><a href='x'>y</a> & more")
    data["guardian"]["manual_removals"] = [{"platform": "<tiktok>", "post_url": "https://t.test/?a=1&b=<2>"}]
    data["spend"][0]["provider"] = "<anthropic>"
    data["issues"] = ["<b>12:00</b> <pre>boom</pre> & co"]
    data["env"] = "<env>"
    text = report.render_telegram(data)
    # The only tags in the text are our own bold tags, correctly paired; everything else is escaped.
    tags = re.findall(r"<[^>]*>", text)
    assert set(tags) <= {"<b>", "</b>"} and tags.count("<b>") == tags.count("</b>")
    assert not re.search(r"&(?!(amp|lt|gt);)", text)  # no bare ampersand
    assert '&lt;script&gt;alert("x")&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;' in text
    assert "&amp;lt;already&amp;gt; &amp; &lt;i&gt;x&lt;/i&gt;" in text  # escaped once, not unescaped first
    assert "stop: ON" in text and "&lt;/b&gt;&lt;a href='x'&gt;y&lt;/a&gt; &amp; more" in text
    assert "https://t.test/?a=1&amp;b=&lt;2&gt;" in text
    assert "\n\n line" not in text  # a title cannot add lines


def test_a_long_title_is_cut_before_escaping_so_no_entity_is_split() -> None:
    data = sample()
    data["top_clips"] = [{"title": "&" * 500, "views": 1, "platform": "youtube", "post_url": None}]
    text = report.render_telegram(data)
    line = next(line for line in text.splitlines() if "Top clip" in line)
    assert line.count("&amp;") == report.MAX_TITLE_CHARS - 1 and "…" in line
    assert not re.search(r"&(?!amp;)", line)


@pytest.mark.parametrize("size", [50, 400, 5000])
def test_the_text_never_exceeds_the_limit_however_much_there_is_to_say(size: int) -> None:
    data = sample()
    data["issues"] = [f"{index:02d}:00 " + "something went wrong & <again> " * 4 for index in range(size)]
    data["guardian"]["manual_removals"] = [
        {
            "platform": "tiktok",
            "post_url": f"https://www.tiktok.com/@6starsclipping/video/{index}" + "9" * 150,
        }
        for index in range(size)
    ]
    data["accounts"] = [
        dict(deepcopy(data["accounts"][0]), account_id=f"account_number_{index}") for index in range(60)
    ]
    data["top_clips"] = [{"title": "T" * 300, "views": 5, "platform": "x" * 300, "post_url": None}] * 10
    data["clips"]["rejected_by_reason"] = {f"reason {index} " + "r" * 100: index for index in range(200)}
    text = report.render_telegram(data)
    assert 0 < len(text) <= report.TELEGRAM_MAX_CHARS < report.TELEGRAM_LIMIT
    assert text.startswith("⭐ <b>6 Stars Clips</b>")
    # Cut on whole lines: tags stay paired and no entity is cut in half.
    assert text.count("<b>") == text.count("</b>")
    assert not re.search(r"&(?!(amp|lt|gt);)", text)


def test_the_most_important_lines_survive_when_the_report_is_too_long() -> None:
    data = sample()
    data["issues"] = ["x" * 150] * 100
    data["guardian"]["manual_removals"] = [{"platform": "tiktok", "post_url": "https://t.test/1"}]
    text = report.render_telegram(data, max_chars=900)
    assert len(text) <= 900
    assert "Followers" in text and "Guardian" in text and "still need manual removal" in text
    assert "x" * 150 not in text


def test_an_empty_day_still_renders() -> None:
    data = sample()
    data.update(accounts=[], top_clips=[], spend=[], issues=[])
    data["views"] = {"total": None, "change": None, "median_by_platform": {}}
    data["ypp"] = {
        "subscribers": None,
        "subscribers_target": 1000,
        "shorts_views_90d": 0,
        "shorts_views_target": 10_000_000,
    }
    data["spikes"]["median_spike_to_post_s"] = None
    data["clips"]["rejected_by_reason"] = {}
    data["discovery"]["new_by_tier"] = {}
    text = visible(report.render_telegram(data))
    assert "Followers  no accounts" in text and "Views  total –" in text
    assert "YPP  subs – / 1.0k · Shorts views 90d 0 / 10M (0%)" in text
    assert "median spike→post –" in text and "Spend  nothing" in text


@pytest.mark.parametrize(
    ("number", "text"),
    [(None, "–"), (0, "0"), (999, "999"), (1000, "1.0k"), (6149, "6.1k"), (88000, "88k"), (412_000, "412k"),
     (999_999, "1000k"), (2_100_000, "2.1M"), (10_000_000, "10M"), (3_000_000_000, "3.0B"), (-1500, "-1.5k")],
)  # fmt: skip
def test_compact_numbers(number: int | None, text: str) -> None:
    assert report.compact(number) == text


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("music: ratio 0.71 over 0.4", "music"),
        ("Score", "score"),
        (None, "unknown"),
        ("", "unknown"),
        ("a very long reason without any colon in it at all", "a very long reason witho"),
    ],
)
def test_reject_reasons_are_grouped_by_their_first_word(reason: str | None, label: str) -> None:
    assert report.reason_label(reason) == label
