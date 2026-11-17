"""Retry policy shared by the HTTP clients: three attempts, only on network errors, 429 and 5xx."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from sixstars.clients import _http

URL = "https://api.example.test/thing"


def _get() -> httpx.Response:
    return httpx.get(URL)


@respx.mock
def test_success_is_not_retried(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert _http.send("svc", "GET /thing", _get).status_code == 200
    assert route.call_count == 1 and sleeps == []


@respx.mock
def test_5xx_is_retried_with_exponential_backoff(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200)])
    assert _http.send("svc", "GET /thing", _get).status_code == 200
    assert route.call_count == 3
    assert sleeps == [2.0, 4.0]


@respx.mock
def test_429_honours_retry_after(sleeps: list[float]) -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    assert _http.send("svc", "GET /thing", _get).status_code == 200
    assert sleeps == [7.0]


@respx.mock
def test_last_response_is_returned_when_attempts_run_out(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))
    response = _http.send("svc", "GET /thing", _get)
    assert response.status_code == 500 and route.call_count == 3 and len(sleeps) == 2
    with pytest.raises(_http.ApiError) as raised:
        _http.raise_for_status("svc", "GET /thing", response)
    assert raised.value.status == 500 and "boom" in str(raised.value)


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status: int, sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    assert _http.send("svc", "GET /thing", _get).status_code == status
    assert route.call_count == 1 and sleeps == []


@respx.mock
def test_network_error_is_retried_then_raised(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        _http.send("svc", "GET /thing", _get)
    assert route.call_count == 3


@respx.mock
def test_network_error_then_success(sleeps: list[float]) -> None:
    respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
    assert _http.send("svc", "GET /thing", _get).status_code == 200
    assert sleeps == [2.0]


@respx.mock
def test_custom_retry_statuses(sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    assert _http.send("svc", "GET /thing", _get, retry_statuses=(500,)).status_code == 429
    assert route.call_count == 1


def test_retry_after_seconds_forms() -> None:
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
    assert _http.retry_after_seconds(httpx.Response(429, headers={"Retry-After": "30"}), now) == 30.0
    http_date = httpx.Response(429, headers={"Retry-After": "Tue, 17 Nov 2026 12:01:00 GMT"})
    assert _http.retry_after_seconds(http_date, now) == 60.0
    reset = httpx.Response(429, headers={"X-RateLimit-Reset": str(int(now.timestamp()) + 45)})
    assert _http.retry_after_seconds(reset, now) == 45.0
    assert _http.retry_after_seconds(httpx.Response(429), now) is None


@respx.mock
def test_retry_after_is_capped(sleeps: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200)]
    )
    _http.send("svc", "GET /thing", _get)
    assert sleeps == [_http.RETRY_AFTER_CAP_S]


@respx.mock
def test_logs_never_contain_query_or_headers(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200))
    _http.send("svc", "GET /thing", lambda: httpx.get(URL, params={"key": "s3cret"}, headers={"X": "t0ken"}))
    out = capsys.readouterr().out
    assert "api_call" in out and "s3cret" not in out and "t0ken" not in out
