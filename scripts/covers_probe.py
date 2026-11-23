"""Cover engine C0 probe (docs/29b-cover-probe.md): what Upload-Post returns for cover fields.

    SIXSTARS_ENV_FILE=.env.staging uv run python scripts/covers_probe.py prepare  <clip_id> <out_dir>
    SIXSTARS_ENV_FILE=.env.staging uv run python scripts/covers_probe.py post     <out_dir> [probe ...]
    SIXSTARS_ENV_FILE=.env.staging uv run python scripts/covers_probe.py status   <out_dir>
    SIXSTARS_ENV_FILE=.env.staging uv run python scripts/covers_probe.py yt_thumb <out_dir>

A one-off probe, not a posting path. It talks to the raw API (the client's status parser drops the fields
we want to see: thumbnail_pending, warnings) and has its own safety rails:
- staging only (APP_ENV must be `staging`);
- private posts only: TikTok `privacy_level=SELF_ONLY`, YouTube `privacyStatus=private`, hard-coded;
- Instagram is refused (it can't post privately and a cover can't be changed after posting);
- `settings` is never read or written; staging `launch.dry_run` stays as it is.
Every request and raw response is appended to <out_dir>/probe.jsonl with the API key left out.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from sixstars.config import env
from sixstars.storage import ClipStorage

API = "https://api.upload-post.com"
USER = "default"
COVER_TS_MS = 3000

# name -> (platform, extra fields). The cover image URL is filled in from prepare's output.
PROBES: dict[str, tuple[str, dict[str, str]]] = {
    "tiktok_timestamp": ("tiktok", {"cover_timestamp": str(COVER_TS_MS)}),
    "tiktok_image": ("tiktok", {"tiktok_cover_image_url": "{cover_url}"}),
    "youtube_thumbnail": ("youtube", {"thumbnail_url": "{cover_url}"}),
}
PRIVATE_FIELDS: dict[str, dict[str, str]] = {
    "tiktok": {"privacy_level": "SELF_ONLY"},
    "youtube": {"privacyStatus": "private", "categoryId": "20", "selfDeclaredMadeForKids": "false"},
}


def _headers() -> dict[str, str]:
    key = env().UPLOAD_POST_API_KEY
    if key is None:
        raise SystemExit("UPLOAD_POST_API_KEY is not set")
    return {"Authorization": f"Apikey {key.get_secret_value()}"}


def _guard() -> None:
    if env().APP_ENV != "staging":
        raise SystemExit(f"covers_probe runs on staging only (APP_ENV={env().APP_ENV})")


def _log(out: Path, entry: dict[str, Any]) -> None:
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    with (out / "probe.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False)[:1500])


def _scrub(value: Any) -> Any:
    """Drop personal fields (the account email) from a response before it is logged."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in ("user_email", "email") else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _body(response: httpx.Response) -> Any:
    try:
        return _scrub(response.json())
    except ValueError:
        return response.text[:2000]


def prepare(clip_id: str, out: Path) -> None:
    """Download the clip, make a dummy 1080x1920 cover from its frame at 3 s, upload it, sign it for 24 h."""
    out.mkdir(parents=True, exist_ok=True)
    storage = ClipStorage()
    with httpx.Client(timeout=120) as http:
        import psycopg

        with psycopg.connect(env().require("DATABASE_URL")) as conn:
            row = conn.execute("select storage_path from clips where id = %s", (clip_id,)).fetchone()
        if row is None or not row[0]:
            raise SystemExit(f"clip {clip_id} has no storage_path")
        video = out / "clip.mp4"
        video.write_bytes(http.get(storage.signed_url(row[0])).content)
    frame = out / "frame.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            str(COVER_TS_MS / 1000),
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )
    image = Image.open(frame).convert("RGB").resize((1080, 1920))
    draw = ImageDraw.Draw(image)
    try:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype("arialbd.ttf", 120)
    except OSError:
        font = ImageFont.load_default()
    draw.text(
        (540, 1000),
        "COVER PROBE",
        font=font,
        fill="#FFD400",
        anchor="mm",
        stroke_width=10,
        stroke_fill="#000000",
    )
    cover = out / "cover.jpg"
    image.save(cover, "JPEG", quality=90)
    key = f"clips/covers-probe/staging/{clip_id}.jpg"
    storage.upload(cover, key, content_type="image/jpeg")
    cover_url = storage.signed_url(key, expires_in_s=24 * 3600)
    (out / "prepare.json").write_text(
        json.dumps({"clip_id": clip_id, "cover_key": key, "cover_url": cover_url})
    )
    _log(out, {"step": "prepare", "clip_id": clip_id, "cover_key": key, "cover_bytes": cover.stat().st_size})


def post(out: Path, names: list[str]) -> None:
    """Send the chosen probes (default: all three). Each is its own private upload."""
    prep = json.loads((out / "prepare.json").read_text())
    video = out / "clip.mp4"
    for name in names or list(PROBES):
        platform, extra = PROBES[name]
        if platform not in PRIVATE_FIELDS:
            raise SystemExit(f"refusing platform {platform}: private posts only")
        request_id = f"covers-probe-{prep['clip_id'][:8]}-{name}-{int(time.time())}"
        fields = {
            "user": USER,
            "platform[]": platform,
            "async_upload": "true",
            "request_id": request_id,
            "title": "Cover probe (staging test, private)",
            **PRIVATE_FIELDS[platform],
            **{k: v.format(cover_url=prep["cover_url"]) for k, v in extra.items()},
        }
        if platform == "youtube":
            fields["youtube_title"] = "Cover probe (staging test, private)"
        with video.open("rb") as handle, httpx.Client(timeout=300) as http:
            response = http.post(
                f"{API}/api/upload",
                data=fields,
                files={"video": ("clip.mp4", handle, "video/mp4")},
                headers={**_headers(), "Idempotency-Key": request_id},
            )
        shown = {k: ("<signed cover url>" if "url" in k else v) for k, v in fields.items()}
        _log(
            out,
            {
                "step": "post",
                "probe": name,
                "request": shown,
                "http": response.status_code,
                "response": _body(response),
            },
        )


def status(out: Path) -> None:
    """Poll the status endpoint once for every request id posted so far."""
    lines = [json.loads(line) for line in (out / "probe.jsonl").read_text(encoding="utf-8").splitlines()]
    with httpx.Client(timeout=60) as http:
        for entry in lines:
            if entry.get("step") != "post":
                continue
            request_id = entry["request"]["request_id"]
            response = http.get(
                f"{API}/api/uploadposts/status", params={"request_id": request_id}, headers=_headers()
            )
            _log(
                out,
                {
                    "step": "status",
                    "probe": entry["probe"],
                    "request_id": request_id,
                    "http": response.status_code,
                    "response": _body(response),
                },
            )


def yt_thumb(out: Path) -> None:
    """Call the set-thumbnail endpoint for the YouTube probe video (after processing)."""
    prep = json.loads((out / "prepare.json").read_text())
    lines = [json.loads(line) for line in (out / "probe.jsonl").read_text(encoding="utf-8").splitlines()]
    video_ids = [
        result.get("platform_post_id") or result.get("post_id")
        for entry in lines
        if entry.get("step") == "status"
        and entry.get("probe") == "youtube_thumbnail"
        and isinstance(entry.get("response"), dict)
        for result in entry["response"].get("results") or []
        if isinstance(result, dict)
    ]
    video_ids = [v for v in video_ids if v]
    if not video_ids:
        raise SystemExit("no YouTube video id in the status log yet; run `status` first")
    with httpx.Client(timeout=120) as http:
        response = http.post(
            f"{API}/api/uploadposts/youtube/thumbnail",
            json={"user": USER, "video_id": video_ids[-1], "thumbnail_url": prep["cover_url"]},
            headers=_headers(),
        )
    _log(
        out,
        {
            "step": "yt_thumb",
            "video_id": video_ids[-1],
            "http": response.status_code,
            "response": _body(response),
        },
    )


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    _guard()
    command = sys.argv[1]
    if command == "prepare" and len(sys.argv) == 4:
        prepare(sys.argv[2], Path(sys.argv[3]))
    elif command == "post":
        post(Path(sys.argv[2]), sys.argv[3:])
    elif command == "status":
        status(Path(sys.argv[2]))
    elif command == "yt_thumb":
        yt_thumb(Path(sys.argv[2]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
