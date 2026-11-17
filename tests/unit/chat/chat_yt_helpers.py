"""Helpers for the YouTube chat tests: a scripted stand-in for youtube.com (an httpx mock transport) and
builders for chat API responses in the shape recorded from the real endpoint on 2026-11-17."""

import json
from pathlib import Path
from typing import Any

import httpx

Reply = dict[str, Any] | int | Exception


def load_actions(path: Path) -> list[dict[str, Any]]:
    """Read a fixture of raw chat actions (one JSON object per line)."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def chat_response(
    actions: list[dict[str, Any]],
    continuation: str | None = None,
    *,
    timeout_ms: int | None = 10000,
    kind: str = "invalidationContinuationData",
    views: tuple[str, str] | None = None,
    all_chat_selected: bool = False,
) -> dict[str, Any]:
    """Build a `live_chat/get_live_chat[_replay]` response body.

    `views` adds the header listing the "Top chat" and "Live chat" views with those continuation tokens.
    """
    chat: dict[str, Any] = {"actions": actions}
    if continuation is not None:
        body: dict[str, Any] = {"continuation": continuation}
        if timeout_ms is not None:
            body["timeoutMs"] = timeout_ms
        chat["continuations"] = [{"playerSeekContinuationData": {"continuation": "SEEK"}}, {kind: body}]
    if views is not None:
        items = [
            {
                "title": title,
                "selected": selected,
                "continuation": {"reloadContinuationData": {"continuation": token}},
            }
            for title, selected, token in (
                ("Top chat", not all_chat_selected, views[0]),
                ("Live chat", all_chat_selected, views[1]),
            )
        ]
        chat["header"] = {
            "liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": items}}}
        }
    return {"continuationContents": {"liveChatContinuation": chat}}


class FakeYouTube:
    """Scripted youtube.com. `watch` is the list of watch-page replies (the last one repeats); `chat` maps a
    continuation token to its replies (the last one repeats). A reply is a JSON body or HTML text (200),
    an int (that HTTP status) or an exception to raise (a network failure)."""

    def __init__(self, watch: list[str | int | Exception], chat: dict[str, list[Reply]]) -> None:
        self.watch = list(watch)
        self.chat = {token: list(replies) for token, replies in chat.items()}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def continuations(self) -> list[str]:
        """The continuation tokens requested from the chat endpoints, in order."""
        return [token for endpoint, token in self.calls if endpoint != "watch"]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if request.url.path == "/watch":
            self.calls.append(("watch", request.url.params["v"]))
            reply: str | int | Exception = self.watch.pop(0) if len(self.watch) > 1 else self.watch[0]
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, int):
                return httpx.Response(reply, text="error")
            return httpx.Response(200, text=reply)
        body = json.loads(request.content)
        self.bodies.append(body)
        token = body["continuation"]
        self.calls.append((request.url.path.rsplit("/", 1)[-1], token))
        replies = self.chat[token]
        chat_reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(chat_reply, Exception):
            raise chat_reply
        if isinstance(chat_reply, int):
            return httpx.Response(chat_reply, text="error")
        return httpx.Response(200, json=chat_reply)


class SleepRecorder:
    """An async `sleep` that returns at once and records the requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
