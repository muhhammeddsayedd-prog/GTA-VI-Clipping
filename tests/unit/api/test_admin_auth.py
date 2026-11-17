"""The two tokens (docs/26): n8n's `CONTROL_API_TOKEN` opens everything, the console's `DASHBOARD_API_TOKEN`
everything except the trigger routes. Which one matched is logged as `actor`."""

import contextlib
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sixstars import config
from sixstars.api import admin_queries, auth, routes_admin

DASHBOARD = "dashboard-test-token-0123456789abcdef0123456789abcdef"
TRIGGERS = [
    ("POST", "/v1/discovery/run"),
    ("POST", "/v1/posting/hourly"),
    ("POST", "/v1/guardian/check"),
    ("POST", "/v1/guardian/takedown"),
    ("POST", "/v1/stats/collect"),
]


@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", DASHBOARD)
    config.env.cache_clear()
    return {"Authorization": f"Bearer {DASHBOARD}"}


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin settings route without a database."""

    @contextlib.contextmanager
    def fake_tx() -> Iterator[None]:
        yield

    monkeypatch.setattr(routes_admin, "read_tx", fake_tx)
    monkeypatch.setattr(admin_queries, "settings_rows", lambda: [{"key": "a.b", "value": 1}])


# ── the pure rule ────────────────────────────────────────────


def test_select_actor() -> None:
    select = auth.select_actor
    assert select("n8n-token", "n8n-token", "dash-token", allow_dashboard=False) == "n8n"
    assert select("n8n-token", "n8n-token", "dash-token", allow_dashboard=True) == "n8n"
    assert select("dash-token", "n8n-token", "dash-token", allow_dashboard=True) == "dashboard"
    assert select("dash-token", "n8n-token", "dash-token", allow_dashboard=False) is None
    assert select(None, "n8n-token", "dash-token", allow_dashboard=True) is None
    assert select("other", "n8n-token", "dash-token", allow_dashboard=True) is None
    # An unset token never matches, not even an empty or a guessed value.
    assert select("", "", "", allow_dashboard=True) is None
    assert select(None, "", "", allow_dashboard=True) is None
    assert select("\x00not-configured", "", "", allow_dashboard=True) is None
    assert select("dash-token", "", "dash-token", allow_dashboard=True) == "dashboard"
    assert select("n8n-token", "n8n-token", "", allow_dashboard=True) == "n8n"


def test_both_comparisons_always_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []

    def compare(a: bytes, b: bytes) -> bool:
        calls.append(b)
        return a == b

    monkeypatch.setattr(auth.hmac, "compare_digest", compare)
    auth.select_actor("n8n-token", "n8n-token", "dash-token", allow_dashboard=False)
    assert calls == [b"n8n-token", b"dash-token"]


# ── through the application ──────────────────────────────────


@pytest.mark.parametrize(("method", "path"), TRIGGERS)
def test_the_dashboard_token_is_refused_on_the_trigger_routes(
    client: TestClient, token: str, dashboard: dict[str, str], method: str, path: str
) -> None:
    response = client.request(method, path, headers=dashboard, json={"message_id": "m1"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_the_n8n_token_still_opens_the_trigger_routes(
    client: TestClient, auth: dict[str, str], dashboard: dict[str, str]
) -> None:
    response = client.post("/v1/guardian/takedown", headers=auth, json={"message_id": "m1"})
    assert response.status_code == 200


def test_the_dashboard_token_opens_the_admin_routes(client: TestClient, dashboard: dict[str, str]) -> None:
    # No CONTROL_API_TOKEN configured at all: the console still works with its own token.
    response = client.get("/v1/admin/settings", headers=dashboard)
    assert response.status_code == 200
    assert response.json() == {"items": [{"key": "a.b", "value": 1}]}


def test_the_n8n_token_opens_the_admin_routes(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/v1/admin/settings", headers=auth).status_code == 200


def test_the_dashboard_token_reads_the_daily_report(client: TestClient, dashboard: dict[str, str]) -> None:
    assert client.get("/v1/reports/daily", headers=dashboard).status_code == 200


def test_the_dashboard_token_may_use_the_control_routes(
    client: TestClient, dashboard: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sixstars.api import routes_control

    monkeypatch.setattr(routes_control.status_view, "collect", lambda: {"ok": True})
    assert client.get("/v1/status", headers=dashboard).status_code == 200


@pytest.mark.parametrize("path", ["/v1/admin/settings", "/v1/admin/overview", "/v1/reports/daily"])
def test_no_token_is_refused(client: TestClient, token: str, dashboard: dict[str, str], path: str) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("path", ["/v1/admin/settings", "/v1/reports/daily", "/v1/status"])
def test_with_both_tokens_unset_everything_refuses(client: TestClient, path: str) -> None:
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": f"Bearer {DASHBOARD}"}):
        assert client.get(path, headers=headers).status_code == 401


def test_the_request_log_names_the_actor(
    client: TestClient,
    auth: dict[str, str],
    dashboard: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.get("/v1/admin/settings", headers=auth)
    client.get("/v1/admin/settings", headers=dashboard)
    client.get("/v1/admin/settings")
    out = capsys.readouterr().out
    requests = [json.loads(line) for line in out.splitlines() if line.strip()]
    actors = [line.get("actor") for line in requests if line["event"] == "request"]
    assert actors == ["n8n", "dashboard", None]
    assert DASHBOARD not in out


def test_a_short_dashboard_token_is_reported_at_start_up(
    token: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "short")
    config.env.cache_clear()
    auth.warn_if_weak_token()
    out = capsys.readouterr().out
    assert [json.loads(line)["event"] for line in out.splitlines() if line.strip()] == [
        "dashboard_api_token_short"
    ]
    assert '"short"' not in out
