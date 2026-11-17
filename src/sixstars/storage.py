"""Supabase Storage access for the `clips` bucket: upload, delete and signed URL creation using the service
role key (server side only). Implemented in M02 (stub) and M06.

The Storage REST API is called directly with httpx, under the same timeout, retry and logging policy as
the other clients (`sixstars.clients._http`). The request shapes are those of the official `storage3`
client 2.34.1 (read from its source on 2026-11-17, then checked live against the staging project):

- upload: `POST /storage/v1/object/<bucket>/<path>`, multipart field `file` plus `cacheControl`, header
  `x-upsert: true` to replace an existing object; answers `{"Key": "<bucket>/<path>", "Id": ...}`
- delete: `DELETE /storage/v1/object/<bucket>` with `{"prefixes": [<path>, ...]}`
- signed URL: `POST /storage/v1/object/sign/<bucket>/<path>` with `{"expiresIn": <seconds>}`; answers
  `{"signedURL": "/object/sign/..."}`, relative to `/storage/v1`

A storage key is `clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4` (docs/09 section 9): the bucket name, then
the object path. `clips.storage_path` holds the whole key.
"""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from sixstars.clients import _http
from sixstars.clients._http import ClientError, NotConfiguredError
from sixstars.config import env

SERVICE = "supabase_storage"
BUCKET = "clips"
CONTENT_TYPE = "video/mp4"
CACHE_CONTROL_S = "3600"
SIGNED_URL_TTL_S = 3600


class StorageError(ClientError):
    """A Storage call failed, or its answer could not be read."""


def clip_key(clip_id: str, when: datetime | None = None, app_env: str | None = None) -> str:
    """The storage key of a clip: `clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4` (UTC date)."""
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    return f"{BUCKET}/{app_env or env().APP_ENV}/{moment:%Y/%m/%d}/{clip_id}.mp4"


def split_key(key: str) -> tuple[str, str]:
    """`"clips/staging/a.mp4"` to `("clips", "staging/a.mp4")`."""
    bucket, _, path = key.strip("/").partition("/")
    if not bucket or not path:
        raise ValueError(f"not a storage key: {key!r}")
    return bucket, path


class ClipStorage:
    """Upload, delete and sign objects in Supabase Storage with the service role key."""

    def __init__(
        self,
        supabase_url: str | None = None,
        service_role_key: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._url = supabase_url
        self._key = service_role_key
        self._http = http or _http.new_client(_http.UPLOAD_TIMEOUT)

    def _base(self) -> str:
        url = self._url or env().SUPABASE_URL
        if not url:
            raise NotConfiguredError("SUPABASE_URL is not set")
        return url.rstrip("/") + "/storage/v1"

    def _headers(self) -> dict[str, str]:
        secret = env().SUPABASE_SERVICE_ROLE_KEY
        key = self._key or (secret.get_secret_value() if secret is not None else None)
        if not key:
            raise NotConfiguredError("SUPABASE_SERVICE_ROLE_KEY is not set")
        return {"Authorization": f"Bearer {key}", "apikey": key}

    def is_configured(self) -> bool:
        """True when the project URL and the service role key are both set."""
        settings_env = env()
        return bool(self._url or settings_env.SUPABASE_URL) and bool(
            self._key or settings_env.SUPABASE_SERVICE_ROLE_KEY
        )

    def upload(self, local_path: Path, key: str, content_type: str = CONTENT_TYPE) -> str:
        """Upload a file to `key` (bucket name first), replacing an object already there. Returns `key`."""
        bucket, path = split_key(key)
        url = f"{self._base()}/object/{bucket}/{quote(path)}"
        headers = {**self._headers(), "x-upsert": "true"}
        endpoint = "POST /object/{bucket}/{path}"

        def call() -> httpx.Response:
            with local_path.open("rb") as handle:
                return self._http.post(
                    url,
                    headers=headers,
                    data={"cacheControl": CACHE_CONTROL_S},
                    files={"file": (Path(path).name, handle, content_type)},
                )

        response = _http.send(SERVICE, endpoint, call)
        _http.raise_for_status(SERVICE, endpoint, response)
        return key

    def delete(self, key: str) -> bool:
        """Delete one object. Returns True when Storage reports it removed (False: it was not there)."""
        bucket, path = split_key(key)
        url = f"{self._base()}/object/{bucket}"
        endpoint = "DELETE /object/{bucket}"
        headers = self._headers()

        def call() -> httpx.Response:
            return self._http.request("DELETE", url, headers=headers, json={"prefixes": [path]})

        response = _http.send(SERVICE, endpoint, call)
        _http.raise_for_status(SERVICE, endpoint, response)
        try:
            removed = response.json()
        except ValueError as exc:
            raise StorageError("Storage delete answered without JSON") from exc
        return isinstance(removed, list) and len(removed) > 0

    def signed_url(self, key: str, expires_in_s: int = SIGNED_URL_TTL_S) -> str:
        """A time-limited download URL for a private object."""
        bucket, path = split_key(key)
        url = f"{self._base()}/object/sign/{bucket}/{quote(path)}"
        endpoint = "POST /object/sign/{bucket}/{path}"
        headers = self._headers()

        def call() -> httpx.Response:
            return self._http.post(url, headers=headers, json={"expiresIn": expires_in_s})

        response = _http.send(SERVICE, endpoint, call)
        _http.raise_for_status(SERVICE, endpoint, response)
        try:
            signed = response.json()["signedURL"]
        except (ValueError, KeyError, TypeError) as exc:
            raise StorageError("Storage sign answered without signedURL") from exc
        return f"{self._base()}{signed}" if str(signed).startswith("/") else str(signed)


class FakeClipStorage(ClipStorage):
    """Storage without the network, for tests: objects are kept in memory by key."""

    def __init__(self) -> None:
        super().__init__(supabase_url="https://fake.supabase.test", service_role_key="fake")
        self.objects: dict[str, bytes] = {}
        self.error: Exception | None = None

    def is_configured(self) -> bool:
        return True

    def upload(self, local_path: Path, key: str, content_type: str = CONTENT_TYPE) -> str:
        if self.error is not None:
            raise self.error
        split_key(key)
        self.objects[key] = local_path.read_bytes()
        return key

    def delete(self, key: str) -> bool:
        split_key(key)
        return self.objects.pop(key, None) is not None

    def signed_url(self, key: str, expires_in_s: int = SIGNED_URL_TTL_S) -> str:
        bucket, path = split_key(key)
        return f"https://fake.supabase.test/storage/v1/object/sign/{bucket}/{path}?token=fake"
