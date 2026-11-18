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
- several signed URLs in one request: `POST /storage/v1/object/sign/<bucket>` with
  `{"expiresIn": <seconds>, "paths": [<path>, ...]}` (1 to 1,000 paths, `MAX_OBJECTS_PER_REQUEST` of the
  storage server); answers one item per path, `{"path", "signedURL" | null, "error" | null}`. Read on
  2026-11-18 from supabase/storage `src/http/routes/object/getSignedURLs.ts` and `src/storage/limits.ts`,
  and storage-js `createSignedUrls` (which the JS reference documents as "Creates multiple signed URLs").

A storage key is `clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4` (docs/09 section 9): the bucket name, then
the object path. `clips.storage_path` holds the whole key. A clip thumbnail (console media) sits next to
it as `clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg`, in `clips.thumb_path`.
"""

from collections.abc import Iterable, Sequence
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
THUMB_CONTENT_TYPE = "image/jpeg"
SIGN_PATHS_MAX = 1000  # MAX_OBJECTS_PER_REQUEST of the storage server


class StorageError(ClientError):
    """A Storage call failed, or its answer could not be read."""


def clip_key(clip_id: str, when: datetime | None = None, app_env: str | None = None) -> str:
    """The storage key of a clip: `clips/<env>/<yyyy>/<mm>/<dd>/<clip_id>.mp4` (UTC date)."""
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    return f"{BUCKET}/{app_env or env().APP_ENV}/{moment:%Y/%m/%d}/{clip_id}.mp4"


def thumb_key(clip_id: str, when: datetime | None = None, app_env: str | None = None) -> str:
    """The storage key of a clip thumbnail: `clips/thumbs/<env>/<yyyy>/<mm>/<dd>/<clip_id>.jpg` (UTC
    date of the clip row, like `clip_key`)."""
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    return f"{BUCKET}/thumbs/{app_env or env().APP_ENV}/{moment:%Y/%m/%d}/{clip_id}.jpg"


def group_by_bucket(keys: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """Keys grouped by bucket as `(key, object path)` pairs, duplicates and non-keys dropped."""
    out: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        try:
            bucket, path = split_key(key)
        except ValueError:
            continue
        out.setdefault(bucket, []).append((key, path))
    return out


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

    def signed_urls(self, keys: Sequence[str], expires_in_s: int = SIGNED_URL_TTL_S) -> dict[str, str | None]:
        """Signed download URLs for many objects, one request per bucket (and per 1,000 paths). Returns
        `{key: url}`; a key Storage could not sign (missing object) maps to None. Keys that are not
        storage keys are left out."""
        out: dict[str, str | None] = {}
        endpoint = "POST /object/sign/{bucket}"
        for bucket, pairs in group_by_bucket(keys).items():
            url = f"{self._base()}/object/sign/{bucket}"
            headers = self._headers()
            for start in range(0, len(pairs), SIGN_PATHS_MAX):
                chunk = pairs[start : start + SIGN_PATHS_MAX]
                body = {"expiresIn": expires_in_s, "paths": [path for _, path in chunk]}

                def call(
                    url: str = url, headers: dict[str, str] = headers, body: dict[str, object] = body
                ) -> httpx.Response:
                    return self._http.post(url, headers=headers, json=body)

                response = _http.send(SERVICE, endpoint, call)
                _http.raise_for_status(SERVICE, endpoint, response)
                try:
                    items = response.json()
                except ValueError as exc:
                    raise StorageError("Storage sign answered without JSON") from exc
                if not isinstance(items, list):
                    raise StorageError("Storage sign answered without a list")
                signed_by_path: dict[str, str | None] = {}
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        signed = item.get("signedURL")
                        signed_by_path[item["path"]] = (
                            (f"{self._base()}{signed}" if signed.startswith("/") else signed)
                            if isinstance(signed, str) and signed
                            else None
                        )
                for key, path in chunk:
                    out[key] = signed_by_path.get(path)
        return out


class FakeClipStorage(ClipStorage):
    """Storage without the network, for tests: objects are kept in memory by key."""

    def __init__(self) -> None:
        super().__init__(supabase_url="https://fake.supabase.test", service_role_key="fake")
        self.objects: dict[str, bytes] = {}
        self.error: Exception | None = None
        self.missing: set[str] = set()
        self.sign_batches = 0

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

    def signed_urls(self, keys: Sequence[str], expires_in_s: int = SIGNED_URL_TTL_S) -> dict[str, str | None]:
        """Every key signs (as `signed_url` does) unless `missing` names it; `error` is raised if set.
        Each call is counted in `sign_batches`."""
        if self.error is not None:
            raise self.error
        self.sign_batches += 1
        return {
            key: None if key in self.missing else self.signed_url(key, expires_in_s)
            for pairs in group_by_bucket(keys).values()
            for key, _ in pairs
        }
