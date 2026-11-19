"""Worker job `stats.collect` (queue `normal`): a stats pass with the longer `stats.job_budget_s`.

The n8n request (WF05) runs a short inline pass; when fixed-age snapshots are still due at the end of its
budget, it queues this job (deduplicated), which takes the rest without the 30 s HTTP limit.
"""

from datetime import UTC, datetime

from sixstars import jobs, settings
from sixstars.clients.youtube_analytics import YouTubeAnalyticsClient
from sixstars.guardian import service
from sixstars.stats import collector


def handle_collect(job: jobs.Job) -> None:
    used = service.clients()
    collector.collect(
        used.upload_post,
        used.youtube,
        datetime.now(UTC),
        analytics=YouTubeAnalyticsClient(used.youtube),
        budget_s=settings.get_float("stats.job_budget_s"),
        from_job=True,
    )


def register() -> None:
    """Register `stats.collect` with the worker (safe to call more than once)."""
    from sixstars.services import worker

    if worker.HANDLERS.get(collector.JOB_COLLECT) is not handle_collect:
        worker.register(collector.JOB_COLLECT, handle_collect)
