"""One-time local sign-in for the owner's YouTube channel (`sixstars youtube auth`). Runs Google's OAuth
flow for desktop apps: a loopback redirect to `http://127.0.0.1:<port>`, PKCE (S256) and a `state` check,
then exchanges the code for tokens. Implemented in M02.

Source: https://developers.google.com/youtube/v3/guides/auth/installed-apps (checked 2026-11-17). Refresh
tokens are always returned for desktop apps. While the OAuth consent screen is in "Testing" status the
refresh token expires after 7 days, so the owner must set it to "In production".
"""

import secrets
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from sixstars.clients._http import ClientError
from sixstars.clients.youtube import build_auth_url, exchange_code, new_pkce_pair

LOOPBACK_HOST = "127.0.0.1"
_DONE_PAGE = b"<html><body><p>6 Stars Clips: sign-in finished. You can close this tab.</p></body></html>"


class OAuthFlowError(ClientError):
    """The local sign-in did not produce an authorization code."""


def parse_redirect(path: str, expected_state: str) -> str:
    """Return the authorization code from the redirect request path, checking `state` and `error`."""
    query = parse_qs(urlparse(path).query)
    if "error" in query:
        raise OAuthFlowError(f"Google answered: {query['error'][0]}")
    if query.get("state", [""])[0] != expected_state:
        raise OAuthFlowError("state mismatch in the redirect (possible forged request)")
    code = query.get("code", [""])[0]
    if not code:
        raise OAuthFlowError("no authorization code in the redirect")
    return code


class _RedirectServer(HTTPServer):
    """Loopback HTTP server that remembers the first request carrying a query string."""

    redirect_path: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
        if not urlparse(self.path).query:
            self.send_response(404)
            self.end_headers()
            return
        assert isinstance(self.server, _RedirectServer)
        self.server.redirect_path = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log: the request line contains the authorization code."""


def run_local_flow(
    client_id: str,
    client_secret: str,
    port: int = 0,
    open_browser: bool = True,
    timeout_s: int = 300,
    echo: Callable[[str], None] = print,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run the sign-in and return Google's token response (`refresh_token`, `access_token`, ...)."""
    server = _RedirectServer((LOOPBACK_HOST, port), _Handler)
    server.timeout = 1.0
    try:
        redirect_uri = f"http://{LOOPBACK_HOST}:{server.server_address[1]}"
        state = secrets.token_urlsafe(24)
        verifier, challenge = new_pkce_pair()
        url = build_auth_url(client_id, redirect_uri, state, challenge)
        echo("Open this URL in a browser and sign in with the Google account that owns the channel:")
        echo(url)
        if open_browser:
            webbrowser.open(url)
        waited = 0.0
        while server.redirect_path is None and waited < timeout_s:
            server.handle_request()
            waited += server.timeout
        path = server.redirect_path
    finally:
        server.server_close()
    if path is None:
        raise OAuthFlowError(f"no sign-in was completed within {timeout_s} seconds")
    code = parse_redirect(path, state)
    return exchange_code(client_id, client_secret, code, verifier, redirect_uri, http=http)
