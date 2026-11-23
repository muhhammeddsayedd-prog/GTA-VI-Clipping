"""The factory's hook for campaign clips (D118, PR 3): before a clip is drawn, run `brief_match` and pick
its look. A clip that fails its brief is not dropped: its campaign tag is cleared and it is made as a
Normal clip in the `brand` look (`campaign.brief_failed` event); one that passes gets its campaign's
render profile. The result is kept in `clips.brief_result`.
"""

from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db, events
from sixstars.campaigns import brief, profiles
from sixstars.factory.style import StyleRules
from sixstars.logging import get_logger

_log = get_logger("campaigns")


def _campaign_of(clip_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        select k.id::text as id, k.slug, k.brief_rules, c.creator_id::text as creator_id,
               exists (select 1 from clip_campaign_creators l where l.campaign_id = k.id
                       and l.creator_id = c.creator_id) as creator_in_campaign
        from clips c join clip_campaigns k on k.id = c.campaign_id
        where c.id = %s
        """,
        (clip_id,),
    )


def clear_tag(clip_id: str, result: dict[str, Any], reason: str) -> None:
    """Move a clip to the Normal lane: no campaign, the `brand` look."""
    db.execute(
        "update clips set campaign_id = null, render_profile = 'brand', brief_result = %s where id = %s",
        (Jsonb(result), clip_id),
    )
    events.emit(
        "campaign.brief_failed",
        level="info",
        entity_type="clip",
        entity_id=clip_id,
        message=f"campaign {result.get('campaign')}: {reason}; made as a Normal clip",
        data=result,
        service="campaigns",
    )


def rules_for(clip_id: str, rules: StyleRules, *, has_facecam: bool, duration_s: float | None) -> StyleRules:
    """The rules to draw this clip with. Normal clips get `rules` unchanged."""
    campaign = _campaign_of(clip_id)
    if campaign is None:
        return rules
    outcome = brief.check(
        campaign["brief_rules"] or {},
        has_facecam=has_facecam,
        duration_s=duration_s,
        creator_in_campaign=bool(campaign["creator_in_campaign"]),
    )
    result = outcome.as_json(campaign["slug"])
    if not outcome.passed:
        clear_tag(clip_id, result, brief.summary(outcome.failures))
        _log.info(
            "campaign_brief_failed", clip_id=clip_id, campaign=campaign["slug"], failures=outcome.failures
        )
        return rules
    db.execute("update clips set brief_result = %s where id = %s", (Jsonb(result), clip_id))
    return profiles.styled(clip_id, rules)
