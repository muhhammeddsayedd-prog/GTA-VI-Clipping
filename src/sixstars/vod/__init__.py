"""VOD lane: chat-first processing of finished streams and videos, downloading only the windows around
detected spikes, with a transcript fallback for videos without a usable chat replay.

Entry points (M08): `register_handlers()` for the worker, `process.process_video(video_id)` for one video.
"""

from sixstars import jobs

JOB_VOD_PROCESS = "vod.process"
JOB_REPLAY_SCAN = "replay.scan"
JOB_CLEANUP = "vod.cleanup"

__all__ = ["JOB_CLEANUP", "JOB_REPLAY_SCAN", "JOB_VOD_PROCESS", "job_handlers", "register_handlers"]


def job_handlers() -> dict[str, jobs.Handler]:
    """The lane's job handlers by job type (all on queue `normal`)."""
    from sixstars.vod import cleanup, process

    return {
        JOB_VOD_PROCESS: process.handle_vod_process,
        JOB_REPLAY_SCAN: process.handle_replay_scan,
        JOB_CLEANUP: cleanup.handle_cleanup,
    }


def register_handlers() -> None:
    """Register `vod.process`, `replay.scan` and `vod.cleanup` with the worker (safe to call more than
    once). The clip factory registers `clip.render` itself (`factory.pipeline.register_handlers`)."""
    from sixstars.services import worker

    for job_type, handler in job_handlers().items():
        if worker.HANDLERS.get(job_type) is not handler:
            worker.register(job_type, handler)
