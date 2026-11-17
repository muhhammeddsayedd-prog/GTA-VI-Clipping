"""Twitch Clips as a content source (decision D110): viewers already clipped the moment, so there is no
chat-spike detection and no P1 confirmation here.

- `source.run(...)`: one step of a discovery pass. Reads the most viewed clips of the target game, keeps
  the ones that fit, stores a `viewer_clips` row and queues a `viewer_clip.process` job for each.
- `process.handle_job(job)`: downloads the clip, renders it through the clip factory on the hourly lane
  and hands it to the quality gate, like every other clip. The credit names the broadcaster.

`register_handlers()` is the worker's entry point (`services.worker.HANDLER_MODULES`).
"""

from typing import Final

JOB_PROCESS: Final = "viewer_clip.process"
SOURCE_NAME: Final = "twitch_clips"

__all__ = ["JOB_PROCESS", "SOURCE_NAME", "register_handlers"]


def register_handlers() -> None:
    """Register `viewer_clip.process` with the worker (safe to call more than once)."""
    from sixstars.services import worker
    from sixstars.viewer_clips import process

    if worker.HANDLERS.get(JOB_PROCESS) is not process.handle_job:
        worker.register(JOB_PROCESS, process.handle_job)
