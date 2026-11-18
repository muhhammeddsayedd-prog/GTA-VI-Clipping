"""`tiers.youtube_channel_items`: channels.list answers are reused from the cache. No database: the store
functions are replaced by an in-memory dict."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

import pytest

from sixstars.clients.fakes import FakeYouTubeClient
from sixstars.discovery import store, tiers
from sixstars.discovery.models import Creator


class MemoryCache:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.max_ages: list[int] = []

    def read(self, ids: Collection[str], max_age_s: int) -> dict[str, dict[str, Any]]:
        self.max_ages.append(max_age_s)
        if max_age_s <= 0:
            return {}
        return {cid: self.rows[cid] for cid in ids if cid in self.rows}

    def write(self, items: Iterable[Mapping[str, Any]]) -> None:
        for item in items:
            self.rows[str(item["id"])] = dict(item)


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> MemoryCache:
    memory = MemoryCache()
    monkeypatch.setattr(store, "cached_youtube_channels", memory.read)
    monkeypatch.setattr(store, "save_youtube_channels", memory.write)
    monkeypatch.setattr(tiers, "channel_cache_ttl_s", lambda: 3600)
    return memory


def channel_calls(client: FakeYouTubeClient) -> list[list[str]]:
    return [call["ids"] for call in client.calls_to("channels_list")]


def creator(index: int, platform: str = "youtube") -> Creator:
    return Creator(
        id=f"c{index}",
        platform=platform,  # type: ignore[arg-type]
        channel_id=f"UC{index}",
        handle=f"h{index}",
        display_name=None,
        tier="auto",
    )


def test_miss_then_hit(cache: MemoryCache) -> None:
    client = FakeYouTubeClient()
    first = tiers.youtube_channel_items(client, ["UCa", "UCb"])
    assert set(first) == {"UCa", "UCb"}
    assert channel_calls(client) == [["UCa", "UCb"]]
    again = tiers.youtube_channel_items(client, ["UCb", "UCa"])
    assert again == first
    assert len(channel_calls(client)) == 1  # served from the cache, no request


def test_partial_hit_fetches_only_the_misses(cache: MemoryCache) -> None:
    client = FakeYouTubeClient()
    tiers.youtube_channel_items(client, ["UCa"])
    tiers.youtube_channel_items(client, ["UCa", "UCb", "UCc", "UCb"])
    assert channel_calls(client) == [["UCa"], ["UCb", "UCc"]]


def test_stale_entries_are_fetched_again(cache: MemoryCache) -> None:
    client = FakeYouTubeClient()
    tiers.youtube_channel_items(client, ["UCa"])
    tiers.youtube_channel_items(client, ["UCa"], max_age_s=0)  # TTL 0: the cache is off
    assert channel_calls(client) == [["UCa"], ["UCa"]]


def test_the_default_age_is_the_setting(cache: MemoryCache) -> None:
    tiers.youtube_channel_items(FakeYouTubeClient(), ["UCa"])
    assert cache.max_ages == [3600]


def test_a_channel_the_api_does_not_return_is_cached_as_missing(
    cache: MemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeYouTubeClient()
    real = client.channels_list

    def without_gone(ids: Sequence[str], parts: Sequence[str] = ()) -> list[dict[str, Any]]:
        return [item for item in real(ids, parts) if item["id"] != "UCgone"]

    monkeypatch.setattr(client, "channels_list", without_gone)
    assert set(tiers.youtube_channel_items(client, ["UCa", "UCgone"])) == {"UCa"}
    assert cache.rows["UCgone"] == {"id": "UCgone", "missing": True}
    assert tiers.youtube_channel_items(client, ["UCgone"]) == {}
    assert len(channel_calls(client)) == 1  # not asked for again


def test_a_broken_cache_falls_back_to_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any) -> Any:
        raise RuntimeError("relation youtube_channel_cache does not exist")

    monkeypatch.setattr(store, "cached_youtube_channels", broken)
    monkeypatch.setattr(store, "save_youtube_channels", broken)
    monkeypatch.setattr(tiers, "channel_cache_ttl_s", lambda: 3600)
    assert set(tiers.youtube_channel_items(FakeYouTubeClient(), ["UCa"])) == {"UCa"}


def test_channel_info_parses_the_cached_item(cache: MemoryCache) -> None:
    client = FakeYouTubeClient()
    client.channels["UCa"] = {
        "id": "UCa",
        "snippet": {"title": "Alpha", "description": "clip me", "customUrl": "@alpha"},
        "statistics": {"subscriberCount": "1234"},
    }
    client.channels["UCh"] = {"id": "UCh", "snippet": {}, "statistics": {"hiddenSubscriberCount": True}}
    info = tiers.youtube_channel_info(client, ["UCa", "UCh"])
    expected = tiers.ChannelInfo(name="Alpha", description="clip me", handle="@alpha", followers=1234)
    assert info["UCa"] == expected
    assert info["UCh"].followers == 0
    assert tiers.youtube_channel_info(client, ["UCa"]) == {"UCa": info["UCa"]}
    assert len(channel_calls(client)) == 1


def test_prefetch_is_one_batched_call_for_the_youtube_creators(cache: MemoryCache) -> None:
    client = FakeYouTubeClient()
    creators = [creator(index) for index in range(120)] + [creator(999, "twitch")]
    assert tiers.prefetch_youtube_channels(client, creators) == 120
    assert [len(ids) for ids in channel_calls(client)] == [120]  # the client sends it as 3 requests
    # The permission jobs then read every channel from the cache.
    for item in creators[:120]:
        assert tiers.youtube_channel_info(client, [item.channel_id])
    assert len(channel_calls(client)) == 1


def test_prefetch_without_a_client_does_nothing(cache: MemoryCache) -> None:
    assert tiers.prefetch_youtube_channels(None, [creator(1)]) == 0
