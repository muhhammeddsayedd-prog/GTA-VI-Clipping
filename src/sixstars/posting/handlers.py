"""Job handlers of the posting engine.

- `post.hourly` (queue express), payload `{"slot_at": "<ISO hour>"}`: runs one hourly slot.
- `post.retry` (queue express), payload `{"kind": "publish", "post_id": ..., "clip_id": ...}`: resumes one
  post, that is reads its status or sends it again with the same idempotency key.
- `post.retry` (queue express), payload `{"kind": "spike", "clip_id": ...}`: tries a spike clip again after
  the minimum gap (R4).

Call `register()` once when a worker process starts. Every handler is safe to run twice.
"""

from collections.abc import Callable
from datetime import datetime

from sixstars import jobs
from sixstars.clients.upload_post import UploadPostClient
from sixstars.logging import bind_context
from sixstars.posting.hourly import HOURLY_JOB_TYPE, run_hourly_slot
from sixstars.posting.publisher import Publisher
from sixstars.posting.spike import try_post_spike
from sixstars.posting.store import RETRY_JOB_TYPE, DbStore

ClientFactory = Callable[[], UploadPostClient]


def make_handlers(client_factory: ClientFactory = UploadPostClient) -> dict[str, jobs.Handler]:
    """The posting job handlers. The Upload-Post client is created on the first job and then reused."""
    cache: list[UploadPostClient] = []

    def client() -> UploadPostClient:
        if not cache:
            cache.append(client_factory())
        return cache[0]

    def hourly(job: jobs.Job) -> None:
        run_hourly_slot(datetime.fromisoformat(str(job.payload["slot_at"])), client())

    def retry(job: jobs.Job) -> None:
        kind = job.payload.get("kind")
        clip_id = job.payload.get("clip_id")
        if clip_id is not None:
            bind_context(clip_id=str(clip_id))
        if kind == "publish":
            Publisher(client(), DbStore()).publish(str(job.payload["post_id"]))
        elif kind == "spike":
            try_post_spike(str(job.payload["clip_id"]), client(), job_id=job.id)
        else:
            raise ValueError(f"unknown post.retry kind {kind!r}")

    return {HOURLY_JOB_TYPE: hourly, RETRY_JOB_TYPE: retry}


def register(client_factory: ClientFactory = UploadPostClient) -> None:
    """Register the posting handlers with the worker service."""
    from sixstars.services import worker

    for job_type, handler in make_handlers(client_factory).items():
        worker.register(job_type, handler)
