"""M20 L4: TUS resumable upload to Supabase Storage (create, 6 MB chunks, resume after a refused chunk)."""

import base64
from pathlib import Path

import httpx

from sixstars import storage
from sixstars.storage import ClipStorage


def test_upload_resumable_creates_patches_and_resumes(tmp_path: Path, monkeypatch: object) -> None:
    data = b"x" * (storage.TUS_CHUNK_BYTES + 1000)
    src = tmp_path / "ep.mp4"
    src.write_bytes(data)
    received = bytearray()
    calls: list[tuple[str, str]] = []
    refused_once = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            meta = dict(part.split(" ") for part in request.headers["Upload-Metadata"].split(","))
            assert base64.b64decode(meta["bucketName"]) == b"episodes"
            assert base64.b64decode(meta["objectName"]) == b"staging/2026/ep.mp4"
            assert request.headers["Upload-Length"] == str(len(data))
            return httpx.Response(201, headers={"Location": "/storage/v1/upload/resumable/abc"})
        if request.method == "PATCH":
            offset = int(request.headers["Upload-Offset"])
            if offset > 0 and not refused_once["done"]:
                refused_once["done"] = True
                return httpx.Response(409)
            assert offset == len(received)
            received.extend(request.content)
            return httpx.Response(204, headers={"Upload-Offset": str(len(received))})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Upload-Offset": str(len(received))})
        return httpx.Response(404)

    client = ClipStorage(
        supabase_url="https://p.supabase.co",
        service_role_key="k",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    key = client.upload_resumable(src, "episodes/staging/2026/ep.mp4")
    assert key == "episodes/staging/2026/ep.mp4"
    assert bytes(received) == data
    assert ("HEAD", "/storage/v1/upload/resumable/abc") in calls
    first_patch = next(c for c in calls if c[0] == "PATCH")
    assert first_patch[1] == "/storage/v1/upload/resumable/abc"
