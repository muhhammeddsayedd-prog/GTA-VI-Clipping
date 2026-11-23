"""Find a campaign streamer by platform id or name and register them as a creator (D118). A streamer is
always chosen by their platform account and confirmed by a person, never matched by name alone ("Jerma"
vs "Jerma985").

References: `twitch:<login>`, `youtube:@<handle>` or `youtube:<UC… channel id>`.
"""

from dataclasses import dataclass
from typing import Any

from sixstars.campaigns import store
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient


class StreamerNotFoundError(ValueError):
    """The reference is malformed or names no account."""


@dataclass(frozen=True)
class Streamer:
    """An account found on its platform, ready to be confirmed and saved."""

    platform: str
    channel_id: str
    handle: str
    display_name: str
    followers: int | None
    picture: str | None

    def describe(self) -> str:
        followers = "?" if self.followers is None else f"{self.followers:,}"
        name = f"{self.platform}:{self.handle}"
        return f"{name} ({self.display_name}, id {self.channel_id}, {followers} followers)"


def parse_ref(ref: str) -> tuple[str, str]:
    platform, sep, value = ref.strip().partition(":")
    platform = platform.lower()
    if not sep or platform not in ("twitch", "youtube") or not value.strip():
        raise StreamerNotFoundError(f"{ref!r}: use twitch:<login>, youtube:@<handle> or youtube:<channel id>")
    return platform, value.strip()


def _twitch(login: str, client: TwitchClient) -> Streamer:
    users = client.get_users_by_login([login])
    if not users:
        raise StreamerNotFoundError(f"no Twitch user {login!r}")
    user = users[0]
    followers = client.get_followers_total(user.id)
    return Streamer(
        "twitch", user.id, user.login, user.display_name, followers, user.profile_image_url or None
    )


def _youtube_item(value: str, client: YouTubeClient) -> dict[str, Any]:
    if value.startswith("UC") and len(value) == 24:
        items = client.channels_list([value])
        if items:
            return items[0]
        raise StreamerNotFoundError(f"no YouTube channel {value!r}")
    item = client.channel_for_handle(value)
    if item is None:
        raise StreamerNotFoundError(f"no YouTube channel with handle {value!r}")
    return item


def _youtube(value: str, client: YouTubeClient) -> Streamer:
    item = _youtube_item(value, client)
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    handle = str(snippet.get("customUrl") or value).removeprefix("@")
    count = stats.get("subscriberCount")
    picture = ((snippet.get("thumbnails") or {}).get("default") or {}).get("url")
    return Streamer(
        "youtube",
        str(item["id"]),
        handle,
        str(snippet.get("title") or handle),
        int(count) if count is not None and not stats.get("hiddenSubscriberCount") else None,
        str(picture) if picture else None,
    )


def find(ref: str, twitch: TwitchClient, youtube: YouTubeClient) -> Streamer:
    """Look the account up on its platform (no database write)."""
    platform, value = parse_ref(ref)
    return _twitch(value, twitch) if platform == "twitch" else _youtube(value, youtube)


def resolve(ref: str, twitch: TwitchClient, youtube: YouTubeClient) -> tuple[dict[str, Any], Streamer | None]:
    """The creator row for `ref`: a known creator straight from the database, else looked up on the
    platform and registered (tier `auto`). Returns the row and the platform record when one was fetched."""
    platform, value = parse_ref(ref)
    known = store.find_creator(platform, value)
    if known is not None:
        return known, None
    streamer = find(ref, twitch, youtube)
    row = store.upsert_creator(
        streamer.platform, streamer.channel_id, streamer.handle, streamer.display_name, streamer.followers
    )
    return row, streamer
