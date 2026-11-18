"""Checks that the residential proxy of YouTube downloads answers: one tiny HTTPS request through it.

Used by the guardian every `proxy.health_interval_s` (a few KB each time, so the paid traffic stays
negligible). The answer says whether the proxy works and, when it does not, why: what the proxy answered
(407 wrong credentials or suspended, 402 no traffic left, ...) or that it could not be reached. The proxy
URL embeds credentials, so it is never logged; error text is scrubbed with `redact_proxy`.
"""

import time
from dataclasses import dataclass
from typing import Final

import httpx

from sixstars.chat.net import proxy_refusal, redact_proxy
from sixstars.clients import _http
from sixstars.logging import get_logger

SERVICE: Final = "proxy"
PROBE_URL: Final = "https://api.ipify.org"  # answers with the caller's IP as plain text
TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)
MAX_ATTEMPTS: Final = 3
BACKOFF_S: Final = 2.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    reason: str  # "ok", "407 Proxy Authentication Required", "no answer (timed out)", ...
    exit_ip: str | None = None
    duration_ms: int = 0
    account_failure: bool = False  # 401 / 402 / 407: credentials or traffic, not a passing fault


class ProxyProbe:
    """Real probe (network)."""

    def probe(self, proxy_url: str) -> ProbeResult:
        log = get_logger(SERVICE)
        started = time.monotonic()
        result = ProbeResult(False, "not tried")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = self._once(proxy_url, started)
            log.info(
                "proxy_probe",
                attempt=attempt,
                ok=result.ok,
                reason=result.reason,
                duration_ms=result.duration_ms,
            )
            if result.ok or result.account_failure:
                return result
            if attempt < MAX_ATTEMPTS:
                _http.sleep(BACKOFF_S * 2 ** (attempt - 1))
        return result

    @staticmethod
    def _once(proxy_url: str, started: float) -> ProbeResult:
        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            with _http.new_client(TIMEOUT, proxy=proxy_url) as client:
                response = client.get(PROBE_URL)
        except httpx.HTTPError as exc:
            # Any failure here is the proxy's: name it so `proxy_refusal` reads the status or the cause.
            refusal = proxy_refusal(redact_proxy(f"ProxyError: {exc}", proxy_url))
            reason = refusal.summary if refusal is not None else type(exc).__name__
            status = refusal.status if refusal is not None else None
            return ProbeResult(
                False, reason, duration_ms=elapsed(), account_failure=status in (401, 402, 407)
            )
        if response.status_code != 200:
            return ProbeResult(False, f"HTTP {response.status_code} from {PROBE_URL}", duration_ms=elapsed())
        return ProbeResult(True, "ok", exit_ip=response.text.strip()[:64] or None, duration_ms=elapsed())
