"""The cover engine's weekly report section (docs/29a section 9, C8) and the console's Covers grid.

`weekly_section(days)`:
- QA pass rate and the top rule failures;
- the fallback rate and deadline misses;
- the arm results with sample sizes;
- the 5 best and the 5 worst covers by views against the account's median at 72 h.

`grid(account, filter)`: the last 30 covers of an account as the platform grid shows them (3:4 previews for
TikTok and Instagram, the 9:16 cover for YouTube), with status, arm and QA total.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from sixstars import db
from sixstars import storage as clip_storage
from sixstars.covers import learn

GRID_SIZE = 30

_WEEK_SQL = """
select id::text as id, clip_id::text as clip_id, account_id, platform, arm, layout, line_text, status, error,
       (scores -> 'qa' -> 'vision' ->> 'total')::numeric as qa_total, post_id::text as post_id, created_at
from covers
where created_at >= now() - make_interval(days => %s)
"""

_RATIO_SQL = """
with snap as (
  select p.account_id, s.post_id, s.views
  from post_metrics_snapshots s join posts p on p.id = s.post_id
  where s.target_age_h = %(age)s and s.views is not null
), med as (
  select account_id, percentile_cont(0.5) within group (order by views) as median
  from snap group by account_id
)
select c.id::text as id, c.account_id, c.arm, c.line_text, snap.views, med.median,
       case when med.median > 0 then snap.views / med.median end as ratio
from covers c join snap on snap.post_id = c.post_id join med on med.account_id = snap.account_id
where c.status = 'applied' and c.created_at >= now() - make_interval(days => %(days)s)
"""


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pass rate, failure reasons, fallbacks and deadline misses from cover rows. Pure."""
    attempts = [r for r in rows if r["status"] in ("passed", "applied", "failed", "fallback_plain")]
    passed = [r for r in attempts if r["status"] != "failed"]
    reasons: Counter[str] = Counter()
    for r in rows:
        if r["status"] == "failed" and r.get("error"):
            for part in str(r["error"]).split(", "):
                reasons[part.split(":")[0].split("=")[0]] += 1
        elif r["status"] == "failed":
            reasons["vision"] += 1
    finals = [r for r in rows if r["status"] in ("passed", "applied", "fallback_plain", "fallback_default")]
    fallbacks = [r for r in finals if r["status"] in ("fallback_plain", "fallback_default")]
    return {
        "attempts": len(attempts),
        "qa_pass_rate": round(len(passed) / len(attempts), 3) if attempts else None,
        "top_fail_reasons": reasons.most_common(5),
        "covers": len(finals),
        "fallback_rate": round(len(fallbacks) / len(finals), 3) if finals else None,
        "deadline_misses": sum(1 for r in rows if r["status"] == "skipped_deadline"),
    }


def weekly_section(days: int = 7) -> str:
    """The markdown section for `docs/reports/<week>.md`."""
    rows = db.fetch_all(_WEEK_SQL, (days,))
    s = summarise(rows)
    ratios = db.fetch_all(_RATIO_SQL, {"age": learn.REWARD_AGE_H, "days": days})
    lines = [
        "## Covers",
        "",
        f"- QA pass rate: {_pct(s['qa_pass_rate'])} of {s['attempts']} attempts",
        "- Top fail reasons: " + (", ".join(f"{k} ({v})" for k, v in s["top_fail_reasons"]) or "none"),
        f"- Fallback rate: {_pct(s['fallback_rate'])} of {s['covers']} covers;"
        f" deadline misses: {s['deadline_misses']}",
        "",
        "| Platform | Arm | Posts scored | Above median |",
        "|---|---|---|---|",
    ]
    for platform in ("instagram", "tiktok", "youtube"):
        for arm, st in sorted(learn.load_stats(platform).items()):
            share = _pct(st.wins / st.plays if st.plays else None)
            lines.append(f"| {platform} | `{arm}` | {st.plays} | {st.wins} ({share}) |")
    ranked = sorted(
        (r for r in ratios if r["ratio"] is not None), key=lambda r: float(r["ratio"]), reverse=True
    )
    for title, picked in (("Best", ranked[:5]), ("Worst", ranked[-5:][::-1])):
        lines += ["", f"**{title} 5** (views ÷ account median at {learn.REWARD_AGE_H} h)", ""]
        lines += [
            f"- ×{float(r['ratio']):.2f} `{r['arm']}` {r['account_id']}: "
            f"{r['line_text'] or '(no line)'} (cover {r['id']})"
            for r in picked
        ] or ["- none yet"]
    return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


_GRID_SQL = """
select c.id::text as id, c.clip_id::text as clip_id, c.platform, c.layout, c.arm, c.status, c.line_text,
       c.error, c.storage_path, (c.scores -> 'qa' -> 'vision' ->> 'total')::numeric as qa_total, c.created_at
from covers c
where c.account_id = %(account)s
  and (%(filter)s = 'all'
       or (%(filter)s = 'fallbacks'
           and c.status in ('fallback_plain', 'fallback_default', 'skipped_deadline'))
       or (%(filter)s = 'failed' and c.status in ('failed', 'apply_failed')))
order by c.created_at desc
limit %(limit)s
"""


def preview_key(storage_path: str | None, platform: str) -> str | None:
    """The image the platform's grid shows: the 3:4 preview (TikTok, Instagram) or the whole cover
    (YouTube). Pure."""
    if not storage_path:
        return None
    if platform == "youtube":
        return storage_path
    return storage_path[: -len(".jpg")] + "_3x4.jpg" if storage_path.endswith(".jpg") else storage_path


def grid(account: str, filter_: str = "all") -> dict[str, Any]:
    """The last 30 covers of an account for the console, with signed preview URLs."""
    rows = [
        dict(r) for r in db.fetch_all(_GRID_SQL, {"account": account, "filter": filter_, "limit": GRID_SIZE})
    ]
    keys = [k for k in (preview_key(r["storage_path"], str(r["platform"])) for r in rows) if k]
    urls: Mapping[str, str | None] = {}
    storage = clip_storage.ClipStorage()
    if keys and storage.is_configured():
        urls = storage.signed_urls(keys)
    covers = []
    for r in rows:
        key = preview_key(r["storage_path"], str(r["platform"]))
        covers.append(
            {
                "id": r["id"],
                "clip_id": r["clip_id"],
                "platform": r["platform"],
                "layout": r["layout"],
                "arm": r["arm"],
                "status": r["status"],
                "line_text": r["line_text"],
                "problems": r["error"],
                "qa_total": float(r["qa_total"]) if r["qa_total"] is not None else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "preview_url": urls.get(key) if key else None,
                "aspect": "9:16" if r["platform"] == "youtube" else "3:4",
            }
        )
    accounts = [
        {"account_id": str(a["id"]), "platform": str(a["platform"])}
        for a in db.fetch_all("select id, platform from platform_accounts order by id")
    ]
    return {"account": account, "filter": filter_, "accounts": accounts, "covers": covers}
