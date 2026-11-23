"""Bearer token authentication: every route but the health check refuses without the right token."""

import json

import pytest
from fastapi.testclient import TestClient

from sixstars import config
from sixstars.api import auth

PROTECTED = [
    ("POST", "/v1/discovery/run"),
    ("POST", "/v1/posting/hourly"),
    ("POST", "/v1/guardian/check"),
    ("POST", "/v1/guardian/takedown"),
    ("POST", "/v1/stats/collect"),
    ("GET", "/v1/reports/daily"),
    ("GET", "/v1/status"),
    ("POST", "/v1/control/stop"),
    ("POST", "/v1/control/start"),
    ("POST", "/v1/control/lane/resume"),
    ("POST", "/v1/control/settings"),
]


def assert_unauthorized(response) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthorized"
    assert body["error"] == "missing or invalid bearer token"
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_a_missing_token_is_refused_on_every_protected_route(
    client: TestClient, token: str, method: str, path: str
) -> None:
    assert_unauthorized(client.request(method, path))


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_a_wrong_token_is_refused_on_every_protected_route(
    client: TestClient, token: str, method: str, path: str
) -> None:
    assert_unauthorized(client.request(method, path, headers={"Authorization": "Bearer not-the-token"}))


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Token {token}",
        "{token}",
        "Bearer {token}x",
        "Bearer x{token}",
        "Bearer {token} extra",
    ],
)
def test_malformed_authorization_headers_are_refused(client: TestClient, token: str, header: str) -> None:
    value = header.format(token=token)
    assert_unauthorized(client.get("/v1/status", headers={"Authorization": value}))


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_with_no_token_configured_on_the_server_every_protected_route_refuses(
    client: TestClient, method: str, path: str
) -> None:
    # No CONTROL_API_TOKEN at all: not even an empty or a guessed token opens the API.
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer anything"}):
        assert_unauthorized(client.request(method, path, headers=headers))


def test_an_empty_configured_token_also_means_refuse(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROL_API_TOKEN", "")
    config.env.cache_clear()
    assert_unauthorized(client.get("/v1/status", headers={"Authorization": "Bearer "}))


def test_the_right_token_reaches_the_route(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/v1/guardian/takedown", headers=auth, json={"message_id": "m1"})
    assert response.status_code == 200  # the route answered, so authentication passed


def test_the_scheme_is_case_insensitive(client: TestClient, token: str) -> None:
    response = client.post(
        "/v1/guardian/takedown", headers={"Authorization": f"bearer {token}"}, json={"message_id": "m1"}
    )
    assert response.status_code == 200


def test_the_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(auth.hmac, "compare_digest", compare)
    assert auth.token_matches("abc", "abc") is True
    assert auth.token_matches("abd", "abc") is False
    assert calls == [(b"abc", b"abc"), (b"abd", b"abc")]


def test_bearer_token_parsing() -> None:
    assert auth.bearer_token("Bearer abc") == "abc"
    assert auth.bearer_token("BEARER abc") == "abc"
    assert auth.bearer_token(None) is None
    assert auth.bearer_token("Bearer") is None
    assert auth.bearer_token("Basic abc") is None


def test_the_token_never_appears_in_the_logs(
    client: TestClient, auth: dict[str, str], token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    client.post(
        "/v1/guardian/takedown", headers=auth, json={"message_id": "m1", "body_text": "email-secret-text-abc"}
    )
    client.get(f"/v1/status?format=json&token={token}", headers={"Authorization": "Bearer wrong-token-value"})
    client.post("/v1/control/settings", headers=auth, content=b'{"key": "a.b", "value": "body-secret-xyz"')
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    requests = [line for line in lines if line["event"] == "request"]
    assert [line["status"] for line in requests] == [200, 401, 422]
    assert all(
        {"env", "service", "ts", "request_id", "method", "path", "duration_ms"} <= set(r) for r in requests
    )
    assert token not in out
    assert "wrong-token-value" not in out
    assert "body-secret-xyz" not in out
    assert "email-secret-text-abc" not in out  # an email body is never logged
    assert "Bearer" not in out


def test_a_missing_or_short_token_is_reported_at_start_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    auth.warn_if_weak_token()
    monkeypatch.setenv("CONTROL_API_TOKEN", "short")
    config.env.cache_clear()
    auth.warn_if_weak_token()
    out = capsys.readouterr().out
    events = [json.loads(line)["event"] for line in out.splitlines() if line.strip()]
    assert events == ["control_api_token_not_configured", "control_api_token_short"]
    assert '"short"' not in out
