"""Removes what the VOD lane leaves on disk: the downloaded window of a spike once its render has ended,
and chat replay files past their retention. Implemented in M08.

A window file must outlive `process_video`: the `clip.render` job that reads it runs later. So each video
gets a `vod.cleanup` job that looks at its windows every `vod.cleanup_interval_s`, deletes those whose
spike is finished (clipped, rejected or failed) or that are older than `vod.section_retention_s`, and
queues itself again while files remain.
"""

import time
from datetime import timedelta
from pathlib import Path
from typing import Final

from sixstars import jobs
from sixstars.logging import get_logger
from sixstars.vod import store
from sixstars.vod.common import Deps
from sixstars.vod.config import VodConfig

JOB_CLEANUP: Final = "vod.cleanup"
REQUEUE_SUFFIX: Final = ":again"
# Spike statuses after which nothing reads the window file any more.
FINISHED: Final = ("clipped", "rejected", "failed", "merged")


def removable(status: str | None, age_s: float, retention_s: float) -> bool:
    """True when a window file may go: its spike is finished or unknown, or the file is too old. Pure."""
    return status is None or status in FINISHED or age_s > retention_s


def clean_sections(video_id: str, retention_s: float, *, now_s: float | None = None) -> int:
    """Delete the window files of a video that are no longer needed. Returns how many remain."""
    log = get_logger("vod")
    folder = store.sections_dir(video_id)
    if not folder.is_dir():
        return 0
    files = [path for path in folder.iterdir() if path.is_file()]
    states = store.spike_states(sorted({path.name.split(".")[0] for path in files}))
    current = time.time() if now_s is None else now_s
    remaining = 0
    for path in files:
        age_s = current - path.stat().st_mtime
        if removable(states.get(path.name.split(".")[0]), age_s, retention_s):
            path.unlink(missing_ok=True)
            log.info("section_removed", file=path.name, age_s=round(age_s))
        else:
            remaining += 1
    if remaining == 0:
        for empty in (folder, store.video_dir(video_id)):
            try:
                empty.rmdir()
            except OSError:
                break
    return remaining


def clean_chat_files(retention_s: float) -> int:
    """Delete chat replay files older than the retention; the `chat_files` rows keep their numbers."""
    removed = 0
    for chat_file_id, path in store.chat_files_older_than(retention_s):
        Path(path).unlink(missing_ok=True)
        store.clear_chat_path(chat_file_id)
        removed += 1
    return removed


def handle_cleanup(job: jobs.Job, deps: Deps | None = None) -> None:
    """Job `vod.cleanup` (queue `normal`): payload `{"video_id": <videos.id>}`."""
    deps = deps or Deps()
    cfg = VodConfig.load()
    video_id = str(job.payload.get("video_id") or "")
    remaining = clean_sections(video_id, cfg.section_retention_s) if video_id else 0
    removed_chats = clean_chat_files(cfg.chat_retention_s)
    get_logger("vod").info(
        "vod_cleanup", video_id=video_id, sections_left=remaining, chat_files_removed=removed_chats
    )
    if remaining:
        key = job.dedupe_key
        if key is not None:
            key = key[: -len(REQUEUE_SUFFIX)] if key.endswith(REQUEUE_SUFFIX) else key + REQUEUE_SUFFIX
        jobs.enqueue(
            "normal",
            JOB_CLEANUP,
            job.payload,
            priority=job.priority,
            run_after=deps.now() + timedelta(seconds=cfg.cleanup_interval_s),
            dedupe_key=key,
        )
