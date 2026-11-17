"""YouTube web chat parsing: watch pages (both embedding forms), chat API responses and chat actions, on
trimmed real data."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from chat_yt_helpers import chat_response, load_actions

from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatMessage, ChatSourceError

VIDEO_ID = "AAAAAAAAAAA"


def _page(fixtures_dir: Path, name: str) -> innertube.WatchPage:
    return innertube.parse_watch_page((fixtures_dir / name).read_text(encoding="utf-8"), VIDEO_ID)


def _messages(actions: list[dict[str, Any]]) -> list[ChatMessage]:
    return [message for action in actions for _, message in innertube.action_to_messages(action)]


def _html(initial: dict[str, Any], player: dict[str, Any] | None = None) -> str:
    return (
        '<html><script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.20261116.00.00"});</script>'
        f"<script>var ytInitialPlayerResponse = {json.dumps(player or {})};</script>"
        f"<script>var ytInitialData = {json.dumps(initial)};</script></html>"
    )


# --- video ids --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=10s",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        " https://m.youtube.com/watch?v=dQw4w9WgXcQ ",
    ],
)
def test_extract_video_id(raw: str) -> None:
    assert innertube.extract_video_id(raw) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "raw", ["", "some_channel_name", "https://www.youtube.com/@channel/streams", "2902449425"]
)
def test_extract_video_id_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        innertube.extract_video_id(raw)


def test_is_youtube_reference() -> None:
    assert innertube.is_youtube_reference("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert innertube.is_youtube_reference("https://youtu.be/dQw4w9WgXcQ")
    assert not innertube.is_youtube_reference("somechannel")
    assert not innertube.is_youtube_reference("https://www.twitch.tv/videos/1")


# --- watch pages ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["youtube_watch_live_var.html", "youtube_watch_live_tag.html"])
def test_live_watch_page_in_both_embedding_forms(fixtures_dir: Path, name: str) -> None:
    page = _page(fixtures_dir, name)
    assert page == innertube.WatchPage(
        video_id=VIDEO_ID,
        client_version="2.20261116.00.00",
        api_key="FAKE_API_KEY",
        visitor_data="FAKE_VISITOR_DATA",
        continuation="FAKE_TOP_CHAT_TOKEN",
        is_replay=False,
        is_live=True,
        duration_s=None,
        title="Example live stream",
        chat_notice=None,
        unplayable_reason=None,
    )


def test_replay_watch_page(fixtures_dir: Path) -> None:
    page = _page(fixtures_dir, "youtube_watch_replay.html")
    assert page.continuation == "FAKE_TOP_CHAT_TOKEN"
    assert (page.is_replay, page.is_live, page.duration_s) == (True, False, 19676.0)
    assert page.title == "Example finished stream"


def test_watch_page_of_a_normal_upload_has_no_chat(fixtures_dir: Path) -> None:
    page = _page(fixtures_dir, "youtube_watch_no_chat.html")
    assert page.continuation is None
    assert (page.is_replay, page.is_live, page.duration_s, page.unplayable_reason) == (
        False,
        False,
        213.0,
        None,
    )


def test_watch_page_with_chat_turned_off_reports_the_notice() -> None:
    notice = {"messageRenderer": {"text": {"runs": [{"text": "Chat is disabled for this live stream."}]}}}
    bar = {"conversationBarRenderer": {"availabilityMessage": notice}}
    page = innertube.parse_watch_page(
        _html({"contents": {"twoColumnWatchNextResults": {"conversationBar": bar}}}), VIDEO_ID
    )
    assert page.continuation is None
    assert page.chat_notice == "Chat is disabled for this live stream."
    assert page.api_key is None and page.visitor_data is None


def test_unplayable_video_reports_the_reason() -> None:
    player = {
        "playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you’re not a bot"}
    }
    page = innertube.parse_watch_page(_html({"contents": {}}, player), VIDEO_ID)
    assert page.unplayable_reason == "Sign in to confirm you’re not a bot"
    without_reason = innertube.parse_watch_page(
        _html({}, {"playabilityStatus": {"status": "ERROR"}}), VIDEO_ID
    )
    assert without_reason.unplayable_reason == "ERROR"


@pytest.mark.parametrize(
    "page",
    [
        "<html><body>Before you continue to YouTube</body></html>",
        '<html><script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.1"});</script></html>',
        "<script>var ytInitialData = {};</script>",
        '<script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.1"});var ytInitialData = [1, 2];</script>',
    ],
)
def test_pages_that_are_not_watch_pages_raise(page: str) -> None:
    with pytest.raises(ChatSourceError, match="usable watch page"):
        innertube.parse_watch_page(page, VIDEO_ID)


# --- chat responses ---------------------------------------------------------------------------------


def test_chat_response_with_views_offers_the_all_messages_token() -> None:
    page = innertube.parse_chat_response(chat_response([{"a": 1}], "NEXT", views=("TOP", "ALL")))
    assert page == innertube.ChatPage(
        actions=[{"a": 1}], continuation="NEXT", timeout_ms=10000, all_chat_continuation="ALL"
    )


def test_chat_response_already_on_all_messages() -> None:
    page = innertube.parse_chat_response(
        chat_response([], "NEXT", views=("TOP", "ALL"), all_chat_selected=True)
    )
    assert page is not None and page.all_chat_continuation is None


def test_chat_response_replay_continuation_has_no_timeout() -> None:
    body = chat_response([], "NEXT", timeout_ms=None, kind="liveChatReplayContinuationData")
    page = innertube.parse_chat_response(body)
    assert page is not None
    assert (page.continuation, page.timeout_ms, page.all_chat_continuation) == ("NEXT", None, None)


def test_chat_response_at_the_end_has_no_continuation() -> None:
    page = innertube.parse_chat_response(chat_response([{"a": 1}]))
    assert page is not None and page.continuation is None
    only_seek = {
        "continuationContents": {
            "liveChatContinuation": {"continuations": [{"playerSeekContinuationData": {"continuation": "S"}}]}
        }
    }
    seek_page = innertube.parse_chat_response(only_seek)
    assert seek_page is not None and seek_page.continuation is None and seek_page.actions == []


@pytest.mark.parametrize("body", [{}, {"responseContext": {}}, {"continuationContents": {"other": {}}}])
def test_chat_response_without_chat_is_none(body: dict[str, Any]) -> None:
    assert innertube.parse_chat_response(body) is None


# --- actions ----------------------------------------------------------------------------------------


def test_replay_sample_kinds_and_times(fixtures_dir: Path) -> None:
    actions = load_actions(fixtures_dir / "youtube_replay_sample.jsonl")
    assert len(actions) == 194
    messages = _messages(actions)
    # The 9 ticker actions are not messages.
    assert Counter(message.kind for message in messages) == {
        "chat": 171,
        "sub": 9,
        "donation": 2,
        "gift": 2,
        "system": 1,
    }
    assert all(message.platform == "youtube" for message in messages)
    times = [message.t for message in messages]
    assert times == sorted(times)
    assert (times[0], times[-1]) == (0.0, 19406.187)
    assert sum(message.is_mod for message in messages) == 15
    assert len({message.user_id for message in messages}) == 89


def test_replay_sample_special_messages(fixtures_dir: Path) -> None:
    messages = _messages(load_actions(fixtures_dir / "youtube_replay_sample.jsonl"))
    system = messages[0]
    assert (system.kind, system.user_id, system.username) == ("system", "youtube", "youtube")
    assert system.text.startswith("Live chat replay is on.")
    donations = [message for message in messages if message.kind == "donation"]
    assert [message.text for message in donations] == ["$1.99", "$1.00"]
    gifts = [message for message in messages if message.kind == "gift"]
    assert gifts[0].text.startswith("Sent 1 ") and gifts[0].username.startswith("@user_")
    assert gifts[1].text.startswith("received a gift membership by @user_")
    subs = [message.text for message in messages if message.kind == "sub"]
    assert subs[0] == "Welcome to Supporter - Tier 1!"
    assert all(message.user_id.startswith("UC") for message in messages if message.kind != "system")


def test_live_sample_uses_message_timestamps(fixtures_dir: Path) -> None:
    actions = load_actions(fixtures_dir / "youtube_live_sample.jsonl")
    assert len(actions) == 79
    messages = _messages(actions)
    assert Counter(message.kind for message in messages) == {"chat": 77, "system": 1}
    chat = [message for message in messages if message.kind == "chat"]
    assert chat[0].t == 1794875710.816359
    assert all(1794875000 < message.t < 1794877000 for message in messages)
    assert chat[1] == ChatMessage(
        platform="youtube",
        t=1794875718.288634,
        user_id="UCa1d5e5672fbd3e8d7e94d5",
        username="@user_895eb8a7",
        text="WHEW GIRL",
        kind="chat",
        is_mod=True,
    )


def test_live_sample_message_ids_are_unique(fixtures_dir: Path) -> None:
    actions = load_actions(fixtures_dir / "youtube_live_sample.jsonl")
    ids = [message_id for action in actions for message_id, _ in innertube.action_to_messages(action)]
    assert len(ids) == len(set(ids)) == 78


def test_text_of_handles_emoji_and_plain_forms() -> None:
    node = {
        "runs": [
            {"text": "no way "},
            {"emoji": {"emojiId": "💀", "shortcuts": [":skull:"]}},
            {"emoji": {"emojiId": "UCabc/xyz", "shortcuts": [":_wrp:"], "isCustomEmoji": True}},
            {"emoji": {"isCustomEmoji": True}},
            "not a run",
        ]
    }
    assert innertube.text_of(node) == "no way 💀:_wrp:"
    assert innertube.text_of({"simpleText": "$5.00"}) == "$5.00"
    assert innertube.text_of(None) == ""
    assert innertube.text_of({}) == ""


def _item(name: str, **fields: Any) -> dict[str, Any]:
    renderer = {"id": "m1", "timestampUsec": "1700000000000000", "authorExternalChannelId": "UCx"} | fields
    return {"addChatItemAction": {"item": {name: renderer}}}


def test_owner_badge_marks_the_broadcaster() -> None:
    badges = [{"liveChatAuthorBadgeRenderer": {"icon": {"iconType": "OWNER"}}}]
    action = _item(
        "liveChatTextMessageRenderer",
        authorBadges=badges,
        authorName={"simpleText": "@Streamer"},
        message={"runs": [{"text": "hi chat"}]},
    )
    ((message_id, message),) = innertube.action_to_messages(action)
    assert message_id == "m1"
    assert message.is_broadcaster and not message.is_mod
    assert (message.t, message.text, message.kind) == (1700000000.0, "hi chat", "chat")


@pytest.mark.parametrize(
    "renderer,kind",
    [
        ("liveChatTextMessageRenderer", "chat"),
        ("liveChatPaidMessageRenderer", "donation"),
        ("liveChatPaidStickerRenderer", "donation"),
        ("liveChatMembershipItemRenderer", "sub"),
        ("liveChatSponsorshipsGiftPurchaseAnnouncementRenderer", "gift"),
        ("liveChatSponsorshipsGiftRedemptionAnnouncementRenderer", "gift"),
        ("liveChatViewerEngagementMessageRenderer", "system"),
        ("liveChatModeChangeMessageRenderer", "system"),
        ("liveChatSomethingNewRenderer", "notice"),
    ],
)
def test_renderer_kinds(renderer: str, kind: str) -> None:
    ((_, message),) = innertube.action_to_messages(_item(renderer, authorName={"simpleText": "@a"}))
    assert message.kind == kind


def test_paid_message_text_joins_amount_and_message() -> None:
    action = _item(
        "liveChatPaidMessageRenderer",
        authorName={"simpleText": "@a"},
        purchaseAmountText={"simpleText": "$5.00"},
        message={"runs": [{"text": "great stream"}]},
    )
    ((_, message),) = innertube.action_to_messages(action)
    assert message.text == "$5.00 great stream"


@pytest.mark.parametrize(
    "action",
    [
        {"addLiveChatTickerItemAction": {"item": {"liveChatTickerSponsorItemRenderer": {"id": "t"}}}},
        {"removeChatItemAction": {"targetItemId": "m1"}},
        {"liveChatReportModerationStateCommand": {}},
        {
            "addChatItemAction": {
                "item": {"liveChatPlaceholderItemRenderer": {"id": "p", "timestampUsec": "1"}}
            }
        },
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"id": "x"}}}},  # live item, no time
        {"replayChatItemAction": {"actions": [_item("liveChatTextMessageRenderer")]}},  # replay, no offset
        {"clickTrackingParams": "abc"},
    ],
)
def test_actions_that_are_not_messages(action: dict[str, Any]) -> None:
    assert innertube.action_to_messages(action) == []


def test_replay_action_uses_the_video_offset() -> None:
    action = {
        "replayChatItemAction": {
            "actions": [_item("liveChatTextMessageRenderer", authorName={"simpleText": "@a"})],
            "videoOffsetTimeMsec": "83500",
        }
    }
    ((_, message),) = innertube.action_to_messages(action)
    assert message.t == 83.5


# --- HTTP -------------------------------------------------------------------------------------------


async def test_client_sends_browser_headers_and_the_consent_cookie(fixtures_dir: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/watch":
            return httpx.Response(200, text=(fixtures_dir / "youtube_watch_replay.html").read_text("utf-8"))
        return httpx.Response(200, json=chat_response([], "NEXT"))

    async with innertube.new_client(transport=httpx.MockTransport(handler)) as http:
        page = await innertube.fetch_watch_page(http, VIDEO_ID)
        chat = await innertube.post_chat(http, page, innertube.REPLAY_ENDPOINT, "TOKEN")
    assert chat is not None and chat.continuation == "NEXT"
    watch, post = seen
    assert watch.url.params["v"] == VIDEO_ID
    assert "Chrome/" in watch.headers["user-agent"]
    assert "SOCS=CAI" in watch.headers["cookie"]
    assert post.url.path == "/youtubei/v1/live_chat/get_live_chat_replay"
    assert post.url.params["key"] == "FAKE_API_KEY"
    body = json.loads(post.content)
    assert body["continuation"] == "TOKEN"
    assert body["context"]["client"] == {
        "clientName": "WEB",
        "clientVersion": "2.20261116.00.00",
        "hl": "en",
        "gl": "US",
        "visitorData": "FAKE_VISITOR_DATA",
    }


async def test_client_loads_a_cookies_file(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tabc123\n",
        encoding="utf-8",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with innertube.new_client(cookies_file=cookies, transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ChatSourceError, match="HTTP 500"):
            await innertube.fetch_watch_page(http, VIDEO_ID)
    assert "LOGIN_INFO=abc123" in seen[0].headers["cookie"]
    assert "SOCS=CAI" in seen[0].headers["cookie"]


async def test_http_failures_become_chat_source_errors(fixtures_dir: Path) -> None:
    page = _page(fixtures_dir, "youtube_watch_replay.html")
    replies: list[httpx.Response | Exception] = [
        httpx.ConnectTimeout("slow"),
        httpx.Response(429),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"responseContext": {}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async with innertube.new_client(transport=httpx.MockTransport(handler)) as http:
        for expected in ("ConnectTimeout", "HTTP 429", "not JSON", "unexpected JSON"):
            with pytest.raises(ChatSourceError, match=expected):
                await innertube.post_chat(http, page, innertube.LIVE_ENDPOINT, "TOKEN")
        assert await innertube.post_chat(http, page, innertube.LIVE_ENDPOINT, "TOKEN") is None


async def test_consent_redirect_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            return httpx.Response(302, headers={"location": "https://consent.youtube.com/m?continue=x"})
        return httpx.Response(200, text="<html>Before you continue</html>")

    async with innertube.new_client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ChatSourceError, match="consent"):
            await innertube.fetch_watch_page(http, VIDEO_ID)
