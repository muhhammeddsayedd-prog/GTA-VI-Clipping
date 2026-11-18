"""Builds the caption and the per-platform Upload-Post fields for one post as plain data. Nothing is sent.

Caption (the same on every platform, D18):

    <caption line>
    🎥 @<creator> on <Twitch|YouTube>
    <base hashtags> <clip hashtags, at most 5>

YouTube also gets a title and its extra hashtags (`#Shorts`) in the description.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sixstars.posting.gate import privacy_problem, staging_problem
from sixstars.posting.models import Account, Clip, Platform
from sixstars.posting.params import PostingParams

# Platform and API constants (docs/11 §8, docs/14 §1), not thresholds from docs/16.
MAX_CLIP_HASHTAGS = 5
YOUTUBE_TITLE_MAX = 100
CAPTION_MAX = 2200  # TikTok and Instagram caption limit
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_CATEGORY_GAMING = "20"
INSTAGRAM_MEDIA_TYPE = "REELS"
SOURCE_LABELS = {"twitch": "Twitch", "youtube": "YouTube"}
ELLIPSIS = "…"
HANDLE_PLACEHOLDER = "{handle}"  # in `posting.first_comment`: becomes the posting account's handle

FieldValue = str | bool


class UnsafePostError(ValueError):
    """The upload fields were asked for a real post that the gate would refuse."""


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Everything `UploadPostClient.upload_video` needs besides the video file."""

    clip_id: str
    account_id: str
    user: str
    platform: str
    idempotency_key: str
    request_id: str
    fields: dict[str, FieldValue]


def idempotency_key(clip_id: str, account_id: str) -> str:
    """`post:<clip_id>:<account_id>`: one post per clip per account, also used as `request_id`."""
    return f"post:{clip_id}:{account_id}"


def normalise_hashtags(tags: Iterable[str]) -> tuple[str, ...]:
    """Trims, adds the leading `#`, removes spaces inside a tag and drops repeats (case-insensitive)."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in tags:
        body = "".join(raw.split()).lstrip("#")
        if not body or body.lower() in seen:
            continue
        seen.add(body.lower())
        result.append("#" + body)
    return tuple(result)


def hashtags_for(clip: Clip, params: PostingParams, platform: Platform) -> tuple[str, ...]:
    """Base hashtags, then at most `MAX_CLIP_HASHTAGS` clip hashtags, then the YouTube extras."""
    base = normalise_hashtags(params.base_hashtags)
    known = {tag.lower() for tag in base}
    own = tuple(tag for tag in normalise_hashtags(clip.hashtags) if tag.lower() not in known)
    extra = params.youtube_extra_hashtags if platform is Platform.YOUTUBE else ()
    return normalise_hashtags((*base, *own[:MAX_CLIP_HASHTAGS], *extra))


def truncate(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, ending in an ellipsis when something was cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + ELLIPSIS if limit > 0 else ""


def caption_line(clip: Clip) -> str:
    """The clip's own caption text: `caption`, else `hook`, else `title`."""
    for candidate in (clip.caption, clip.hook, clip.title):
        if candidate and candidate.strip():
            return candidate.strip()
    raise ValueError(f"clip {clip.id} has no caption, hook or title")


def credit_line(clip: Clip) -> str:
    """`🎥 @<creator> on <Twitch|YouTube>`. A missing handle is an error: every post credits its creator."""
    handle = clip.creator_handle.strip().lstrip("@")
    if not handle:
        raise ValueError(f"clip {clip.id} has no creator handle for the credit line")
    label = SOURCE_LABELS.get(clip.source_platform)
    if label is None:
        raise ValueError(f"clip {clip.id} has unknown source platform {clip.source_platform!r}")
    return f"🎥 @{handle} on {label}"


def build_caption(clip: Clip, params: PostingParams, platform: Platform, limit: int = CAPTION_MAX) -> str:
    """The full caption. Only the caption line is shortened to fit `limit`; credit and hashtags stay whole."""
    tail = credit_line(clip)
    tags = " ".join(hashtags_for(clip, params, platform))
    if tags:
        tail = f"{tail}\n{tags}"
    room = limit - len(tail) - 1
    if room < 1:
        raise ValueError(f"clip {clip.id}: credit and hashtags alone exceed {limit} characters")
    return f"{truncate(caption_line(clip), room)}\n{tail}"


def youtube_title(clip: Clip) -> str:
    """The YouTube title: `clip.title` (else the caption line) on one line, without `<`/`>`, at most 100."""
    raw = clip.title if clip.title and clip.title.strip() else caption_line(clip)
    cleaned = " ".join(raw.replace("<", "").replace(">", "").split())
    if not cleaned:
        raise ValueError(f"clip {clip.id} has no usable YouTube title")
    return truncate(cleaned, YOUTUBE_TITLE_MAX)


def first_comment_for(account: Account, params: PostingParams) -> str:
    """The first comment for this account: `{handle}` in the setting becomes the account's own handle, so
    each platform names its own account. Empty when the setting is empty."""
    handle = account.handle.strip()
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    return params.first_comment.replace(HANDLE_PLACEHOLDER, handle).strip()


def build_fields(clip: Clip, account: Account, params: PostingParams) -> dict[str, FieldValue]:
    """The multipart fields of `POST /api/upload` for this clip on this account.

    Not included: `user`, `platform[]`, `request_id`, `async_upload` and the video, which the client adds.
    """
    problem = privacy_problem(account)
    if problem is not None:
        raise ValueError(problem)
    fields: dict[str, FieldValue]
    if account.platform is Platform.YOUTUBE:
        title = youtube_title(clip)
        description = build_caption(clip, params, Platform.YOUTUBE, YOUTUBE_DESCRIPTION_MAX)
        fields = {
            "title": title,
            "youtube_title": title,
            "youtube_description": description.replace("<", "").replace(">", ""),
            "categoryId": YOUTUBE_CATEGORY_GAMING,
            "privacyStatus": account.privacy or "",
            "selfDeclaredMadeForKids": False,
        }
    elif account.platform is Platform.TIKTOK:
        fields = {
            "title": build_caption(clip, params, Platform.TIKTOK),
            "privacy_level": account.privacy or "",
        }
    else:
        fields = {
            "title": build_caption(clip, params, Platform.INSTAGRAM),
            "media_type": INSTAGRAM_MEDIA_TYPE,
        }
    comment = first_comment_for(account, params)
    if comment:
        fields["first_comment"] = comment
    return fields


def build_upload_request(
    clip: Clip, account: Account, params: PostingParams, *, preview: bool = False
) -> UploadRequest:
    """The complete request data for one post.

    With `preview=False` (a request that will be sent) the staging safety rule is checked again and
    `UnsafePostError` is raised for a post the gate would refuse. Use `preview=True` to log a dry run.
    """
    if not preview:
        if not account.enabled:
            raise UnsafePostError(f"{account.id} is disabled")
        unsafe = staging_problem(account, params)
        if unsafe is not None:
            raise UnsafePostError(unsafe[1])
    key = idempotency_key(clip.id, account.id)
    return UploadRequest(
        clip_id=clip.id,
        account_id=account.id,
        user=account.upload_post_user,
        platform=account.platform.value,
        idempotency_key=key,
        request_id=key,
        fields=build_fields(clip, account, params),
    )
