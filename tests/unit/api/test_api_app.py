"""The application shell: health, error bodies, request ids, the body limit, placeholders and discovery."""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sixstars import notify
from sixstars.api import errors, middleware, routes_health, routes_triggers
from sixstars.api.main import create_app
from sixstars.api.middleware import RequestMiddleware

# ── health ────────────────────────────────────────────────────────────────────────────────────────────────


def test_health_needs_no_token_and_reports_the_environment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_health, "database_ok", lambda: True)
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "env": "dev"}


def test_health_is_503_when_the_database_does_not_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_health, "database_ok", lambda: False)
    response = client.get("/v1/health")
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "env": "dev",
        "error": "database unreachable",
        "code": "db_unreachable",
    }


def test_health_is_503_without_a_database_url(client: TestClient) -> None:
    # No DATABASE_URL in the unit test environment: the real check must fail closed, not crash.
    response = client.get("/v1/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_the_documentation_routes_do_not_exist(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ── error bodies and request ids ──────────────────────────────────────────────────────────────────────────


def test_unknown_paths_and_wrong_methods_answer_in_the_error_shape(client: TestClient) -> None:
    missing = client.get("/v1/nothing-here")
    assert missing.status_code == 404
    assert missing.json() == {
        "error": "Not Found",
        "code": "not_found",
        "request_id": missing.headers["x-request-id"],
    }
    wrong = client.get("/v1/control/stop")
    assert wrong.status_code == 405
    assert wrong.json()["code"] == "method_not_allowed"


def test_every_answer_carries_a_request_id_and_the_fixed_headers(client: TestClient) -> None:
    first, second = client.get("/v1/nothing"), client.get("/v1/nothing")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["cache-control"] == "no-store"
    assert "server" not in first.headers


def test_a_plain_request_id_from_the_caller_is_kept_and_anything_else_replaced(client: TestClient) -> None:
    kept = client.get("/v1/nothing", headers={"X-Request-ID": "n8n-exec-12345"})
    assert kept.headers["x-request-id"] == "n8n-exec-12345"
    for bad in ("short", "has spaces in it", "x" * 65, 'quote"injection{}'):
        replaced = client.get("/v1/nothing", headers={"X-Request-ID": bad})
        assert replaced.headers["x-request-id"] != bad


def test_a_validation_error_names_the_field_and_does_not_echo_the_input(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post(
        "/v1/control/settings", headers=auth, json={"key": "SECRET VALUE !!", "value": 1, "more": "x"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert {item["field"] for item in body["details"]} == {"key", "more"}
    assert "SECRET VALUE" not in response.text


def test_a_body_that_is_not_json_is_a_validation_error(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/v1/control/settings",
        headers={**auth, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def _failing_app() -> FastAPI:
    app = FastAPI()
    errors.install(app)
    app.add_middleware(RequestMiddleware)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("database password is hunter2")

    return app


def test_an_unexpected_exception_is_a_generic_500_logged_and_alerted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notify, "send", lambda severity, title, body="": sent.append((severity, title, body)))
    with TestClient(_failing_app(), raise_server_exceptions=False) as failing:
        response = failing.get("/boom")
    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    assert response.json() == {"error": "internal error", "code": "internal_error", "request_id": request_id}
    assert "hunter2" not in response.text
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    failed = next(line for line in lines if line["event"] == "request_failed")
    assert failed["request_id"] == request_id and failed["error"] == "RuntimeError"
    done = next(line for line in lines if line["event"] == "request")
    assert done["status"] == 500
    assert sent == [
        ("warn", "Control API error: GET /boom", f"RuntimeError (request {request_id}). See the api logs.")
    ]


# ── body size limit ───────────────────────────────────────────────────────────────────────────────────────


def test_a_body_over_the_limit_is_refused_by_its_declared_length(
    client: TestClient, auth: dict[str, str]
) -> None:
    big = json.dumps({"reason": "x" * (middleware.MAX_BODY_BYTES + 1)})
    response = client.post("/v1/control/stop", headers=auth, content=big)
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_a_streamed_body_over_the_limit_is_refused_while_it_is_read(
    client: TestClient, auth: dict[str, str]
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"key": "a.b", "value": "'
        for _ in range(middleware.MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024
        yield b'"}'

    response = client.post(
        "/v1/control/settings", headers={**auth, "Content-Type": "application/json"}, content=chunks()
    )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_the_size_limit_applies_before_authentication(client: TestClient) -> None:
    response = client.post("/v1/control/stop", content=b"x" * (middleware.MAX_BODY_BYTES + 1))
    assert response.status_code == 413


# ── placeholders (M13) ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "heartbeat"),
    [
        ("POST", "/v1/guardian/check", "trigger:guardian"),
        ("POST", "/v1/guardian/takedown", None),
        ("POST", "/v1/stats/collect", "trigger:stats"),
        ("GET", "/v1/reports/daily?format=telegram", None),
    ],
)
def test_the_m13_routes_answer_501_with_a_clear_body(
    client: TestClient,
    auth: dict[str, str],
    heartbeats: list[str],
    method: str,
    path: str,
    heartbeat: str | None,
) -> None:
    response = client.request(method, path, headers=auth)
    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "not_implemented"
    assert body["milestone"] == "M13"
    assert "not implemented yet" in body["error"]
    assert heartbeats == ([heartbeat] if heartbeat else [])


# ── discovery ─────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Summary:
    live_assigned: int
    videos_queued: int
    finished_at: datetime


def use_discovery(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    monkeypatch.setattr(routes_triggers, "find_run_discovery", lambda: run)


def test_discovery_answers_501_while_the_module_has_no_run_discovery(
    client: TestClient, auth: dict[str, str], heartbeats: list[str]
) -> None:
    # The real lookup: `sixstars.discovery` exists but has no `run_discovery` yet.
    import sixstars.discovery

    if hasattr(sixstars.discovery, "run_discovery"):
        pytest.skip("discovery is implemented")
    response = client.post("/v1/discovery/run", headers=auth)
    assert response.status_code == 501
    assert response.json()["code"] == "not_implemented"
    assert heartbeats == ["trigger:discovery"]


def test_find_run_discovery_survives_a_missing_or_broken_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_triggers, "DISCOVERY_MODULE", "sixstars.no_such_package")
    assert routes_triggers.find_run_discovery() is None


def test_discovery_passes_a_dataclass_summary_through_as_json(
    client: TestClient, auth: dict[str, str], heartbeats: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    def run() -> Summary:
        threads.append(threading.current_thread().name)
        return Summary(2, 5, datetime(2026, 11, 18, 11, 0, tzinfo=UTC))

    use_discovery(monkeypatch, run)
    response = client.post("/v1/discovery/run", headers=auth)
    assert response.status_code == 200
    assert response.json() == {
        "live_assigned": 2,
        "videos_queued": 5,
        "finished_at": "2026-11-18T11:00:00+00:00",
    }
    assert heartbeats == ["trigger:discovery"]
    assert threads and threads[0] != threading.main_thread().name


def test_discovery_summary_shapes() -> None:
    assert routes_triggers.summary_to_json({"a": 1}) == {"a": 1}
    assert routes_triggers.summary_to_json(None) == {"summary": None}
    assert routes_triggers.summary_to_json([1, 2]) == {"summary": [1, 2]}


def test_a_failing_discovery_pass_is_a_500_and_frees_the_lock(
    client: TestClient, auth: dict[str, str], heartbeats: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: False)

    def run() -> None:
        raise RuntimeError("twitch said no")

    use_discovery(monkeypatch, run)
    response = client.post("/v1/discovery/run", headers=auth)
    assert response.status_code == 500
    assert "twitch" not in response.text
    use_discovery(monkeypatch, lambda: {"ok": 1})
    assert client.post("/v1/discovery/run", headers=auth).status_code == 200


def test_a_slow_discovery_pass_answers_504_and_a_second_call_409_until_it_ends(
    auth: dict[str, str], heartbeats: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    finished = threading.Event()

    def run() -> dict[str, int]:
        release.wait(10)
        finished.set()
        return {"ok": 1}

    use_discovery(monkeypatch, run)
    monkeypatch.setattr(routes_triggers, "DISCOVERY_TIMEOUT_S", 0.2)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        started = time.monotonic()
        slow = client.post("/v1/discovery/run", headers=auth)
        assert slow.status_code == 504
        assert slow.json()["code"] == "discovery_timeout"
        assert time.monotonic() - started < 5
        busy = client.post("/v1/discovery/run", headers=auth)
        assert busy.status_code == 409
        assert busy.json()["code"] == "discovery_running"
        release.set()
        assert finished.wait(5)
        deadline = time.monotonic() + 5
        while routes_triggers._discovery_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.post("/v1/discovery/run", headers=auth).status_code == 200
