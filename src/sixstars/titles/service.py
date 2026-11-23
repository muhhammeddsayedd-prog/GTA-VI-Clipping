"""Title writer v2 for one clip, end to end: facts → P11 (five candidates) → code checks → one retry when
nothing passed → choice (with exploration) → `clip_titles` row → the clip's title, caption and tags.

`after_approval` is the gate's hook: it runs only when scoring_config `titles.v2` is enabled and never
raises, so a failed title never costs a clip its approval; the clip then keeps the clip writer's title.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sixstars import events
from sixstars.ai import calls
from sixstars.clients.anthropic_client import AnthropicClient, BudgetKind
from sixstars.factory import lines as subtitle_lines
from sixstars.logging import get_logger
from sixstars.scoring import config as scoring_config
from sixstars.scoring import log as scoring_log
from sixstars.titles import checks, facts, store

FLAG = "titles.v2"
STAGE = "title"
DEFAULT_TAG_TAIL = ("streamerclips",)

_log = get_logger("titles")


@dataclass(frozen=True, slots=True)
class Options:
    """The `titles.v2` flag object, read once per run."""

    model: str = "fast"
    explore_share: float = 0.2
    gta_label: str = "GTA 6"
    tag_tail: tuple[str, ...] = DEFAULT_TAG_TAIL
    s_tier_names: tuple[str, ...] = ()
    tier_followers: Mapping[str, Any] = field(default_factory=dict)
    extra_names: Mapping[str, Sequence[str]] = field(default_factory=dict)

    @classmethod
    def from_flag(cls, values: Mapping[str, Any]) -> "Options":
        extra = values.get("extra_names")
        tiers = values.get("tier_followers")
        return cls(
            model=str(values.get("model") or "fast"),
            explore_share=scoring_config.number(values, "explore_share", 0.2),
            gta_label=str(values.get("gta_label") or "GTA 6"),
            tag_tail=scoring_config.strings(values, "tag_tail") or DEFAULT_TAG_TAIL,
            s_tier_names=scoring_config.strings(values, "s_tier_names"),
            tier_followers=tiers if isinstance(tiers, Mapping) else {},
            extra_names={
                str(name): [str(alias).lower() for alias in aliases]
                for name, aliases in (extra.items() if isinstance(extra, Mapping) else ())
                if isinstance(aliases, list)
            },
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one run did."""

    clip_id: str
    prior_title: str | None
    title: str
    caption: str
    hashtags: tuple[str, ...]
    source: str  # "v2" or "fallback"
    pattern: str | None
    explored: bool
    candidates: tuple[checks.Checked, ...]
    run_id: int | None = None
    applied: bool = False


def _event(row: Mapping[str, Any]) -> str:
    """What happens in the clip: the visual judge's sentence, else the clip writer's headline or caption."""
    parts = row.get("score_parts")
    visual = parts.get("visual") if isinstance(parts, Mapping) else None
    seen = visual.get("seen") if isinstance(visual, Mapping) else None
    if isinstance(seen, str) and seen.strip():
        return seen.strip()
    texts = row.get("style_texts")
    headline = texts.get("headline") if isinstance(texts, Mapping) else None
    for text in (headline, row.get("caption"), row.get("hook")):
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _lines(row: Mapping[str, Any]) -> list[facts.TimedLine]:
    transcript = row.get("transcript")
    if not isinstance(transcript, Mapping):
        return []
    words = subtitle_lines.words_from_transcript(transcript)
    return [
        facts.TimedLine(line.start_s, line.end_s, line.text) for line in subtitle_lines.group_lines(words)
    ]


def _number(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def stream_offset_s(row: Mapping[str, Any]) -> float | None:
    """How far into the stream the moment is: the replay spike's VOD offset, the live spike's time since the
    stream started, or the start of a Twitch VOD clip (a Twitch VOD is a whole stream). Pure."""
    offset = _number(row.get("spike_offset_s"))
    if offset is not None and row.get("spike_mode") == "replay":
        return offset
    live = _number(row.get("live_offset_s"))
    if live is not None:
        return live
    if row.get("source_type") == "vod" and row.get("source_platform") == "twitch":
        return _number(row.get("start_s"))
    return None


def peak_in_clip(row: Mapping[str, Any]) -> float | None:
    offset, start = _number(row.get("spike_offset_s")), _number(row.get("start_s"))
    if offset is None or start is None or row.get("spike_mode") != "replay":
        return None
    return offset - start


def _chat(row: Mapping[str, Any]) -> list[str]:
    sample = row.get("chat_sample")
    return (
        [str(item.get("text") or "") for item in sample if isinstance(item, Mapping)]
        if isinstance(sample, list)
        else []
    )


def build_facts(row: Mapping[str, Any], book: facts.NameBook, options: Options) -> facts.TitleFacts:
    """The facts for the clip row of `store.clip_row`."""
    handle = str(row.get("handle") or "")
    streamer = str(row.get("title_name") or "") or facts.clean_name(row.get("display_name"), handle)
    return facts.build(
        streamer=streamer,
        book=book,
        event=_event(row),
        lines=_lines(row),
        peak_s=peak_in_clip(row),
        offset_s=stream_offset_s(row),
        chat=_chat(row),
        gta_label=options.gta_label,
        spoiler=bool(row.get("spoiler")),
    )


def _book(row: Mapping[str, Any], options: Options) -> facts.NameBook:
    rows: list[Mapping[str, Any]] = list(store.book_rows())
    if not any(str(item.get("handle")) == str(row.get("handle")) for item in rows):
        rows.append(row)  # the clip's own creator is always in the book
    return facts.book_from_rows(rows, options.extra_names, options.s_tier_names, options.tier_followers)


def decide(
    clip_id: str,
    facts_: facts.TitleFacts,
    book: facts.NameBook,
    options: Options,
    ask: "Asker",
    topic: str | None,
) -> tuple[list[checks.Checked], checks.Choice | None, str]:
    """Ask, check, ask once more when nothing passed. Returns every candidate checked (both rounds), the
    choice (an index into that list, None: use the fallback) and the short caption."""
    reply = ask(None)
    checked = checks.check_all([c.model_dump() for c in reply.candidates], facts_, book)
    caption = reply.caption_short
    exploring = checks.explore(clip_id, options.explore_share)
    choice = checks.choose(checked, exploring=exploring)
    if choice is None:
        retry = ask(checks.retry_note(checked))
        again = checks.check_all([c.model_dump() for c in retry.candidates], facts_, book)
        first_round = len(checked)
        checked = [*checked, *again]
        picked = checks.choose(again, exploring=exploring)
        if picked is not None:
            choice = checks.Choice(picked.index + first_round, picked.explored)
            caption = retry.caption_short or caption
    return checked, choice, caption


class Asker:
    """Calls P11 with the facts; `__call__(note)` returns the reply. A class so tests can stand in."""

    def __init__(
        self,
        facts_json: str,
        options: Options,
        lane: BudgetKind,
        client: AnthropicClient | None,
        clip_id: str,
    ) -> None:
        self.facts_json, self.options, self.lane, self.client, self.clip_id = (
            facts_json,
            options,
            lane,
            client,
            clip_id,
        )

    def __call__(self, note: str | None) -> Any:
        return calls.write_titles(
            self.facts_json,
            model_choice=self.options.model,
            lane=self.lane,
            client=self.client,
            clip_id=self.clip_id,
            extra_note=note,
        )


def retitle(
    clip_id: str,
    *,
    apply: bool,
    options: Options | None = None,
    client: AnthropicClient | None = None,
    lane: BudgetKind | None = None,
) -> Outcome:
    """Write v2 titles for the clip and store the run. With `apply` the chosen title, caption and tags
    replace the clip's own while it is not posted; without it nothing on the clip changes (a dry
    comparison). Raises on a missing clip and on API errors (the caller decides what a failure means)."""
    if options is None:
        options = Options.from_flag(scoring_config.flag(FLAG) or scoring_config.section(FLAG))
    row = store.clip_row(clip_id)
    if row is None:
        raise ValueError(f"clip {clip_id} does not exist")
    book = _book(row, options)
    title_facts = build_facts(row, book, options)
    chosen_lane: BudgetKind = lane or ("express" if row.get("gate_profile") == "express" else "vod")
    asker = Asker(title_facts.to_json(), options, chosen_lane, client, clip_id)
    checked, choice, caption_short = decide(clip_id, title_facts, book, options, asker, row.get("topic"))
    if choice is not None:
        title, pattern, source, explored = (
            checked[choice.index].title,
            checked[choice.index].pattern,
            "v2",
            choice.explored,
        )
    else:
        title, pattern, source, explored = (
            checks.fallback_title(title_facts, row.get("topic")),
            None,
            "fallback",
            False,
        )
    caption_problems = checks.check_caption(caption_short, title_facts)
    caption = caption_short.strip() if not caption_problems else checks.strip_emoji(title)
    tags = facts.hashtags(title_facts, options.tag_tail)
    feats = checks.features(title, pattern or "fallback", title_facts, book, explored=explored, source=source)
    extra = {"caption_problems": caption_problems} if caption_problems else {}
    features = {**feats.as_dict(), **extra}
    prior = row.get("title")
    run_id = store.insert_run(
        clip_id,
        config_version=scoring_config.version(),
        model=calls.title_model(options.model),
        facts=title_facts.as_dict(),
        candidates=[item.as_dict() for item in checked],
        chosen_index=choice.index if choice is not None else None,
        chosen_title=title,
        caption_short=caption,
        hashtags=tags,
        explored=explored,
        source=source,
        features=features,
        prior_title=str(prior) if prior else None,
    )
    applied = store.apply(run_id, clip_id, title, caption, tags) if apply else False
    scoring_log.record(
        STAGE,
        decision=pattern or "fallback",
        reason=f"{sum(item.passed for item in checked)} of {len(checked)} candidates passed"
        + ("; explored the runner-up" if explored else ""),
        sub_scores={
            "candidates": [item.as_dict() for item in checked],
            "features": features,
            "applied": applied,
            "prior_title": prior,
        },
        spike_id=row.get("spike_id"),
        clip_id=clip_id,
        creator_id=row.get("creator_id"),
    )
    if applied:
        events.emit(
            "clip.retitled" if source == "v2" else "clip.title_fallback",
            level="info" if source == "v2" else "warn",
            entity_type="clip",
            entity_id=clip_id,
            message=f"{prior!r} -> {title!r}"[:300],
            data={"pattern": pattern, "explored": explored, "run_id": run_id},
            service="titles",
        )
    _log.info(
        "clip_titled",
        clip_id=clip_id,
        source=source,
        pattern=pattern,
        explored=explored,
        applied=applied,
        title=title,
    )
    return Outcome(
        clip_id,
        prior,
        title,
        caption,
        tuple(tags),
        source,
        pattern,
        explored,
        tuple(checked),
        run_id,
        applied,
    )


def after_approval(clip_id: str, *, client: AnthropicClient | None = None) -> Outcome | None:
    """The gate's hook after a clip is approved: v2 titles when the flag is on. Never raises."""
    values = scoring_config.flag(FLAG)
    if values is None:
        return None
    try:
        if store.already_titled(clip_id):
            return None
        return retitle(clip_id, apply=True, options=Options.from_flag(values), client=client)
    except Exception as exc:  # noqa: BLE001 - the clip keeps the clip writer's title
        _log.warning("title_v2_failed", clip_id=clip_id, error=f"{type(exc).__name__}: {exc}"[:300])
        return None
