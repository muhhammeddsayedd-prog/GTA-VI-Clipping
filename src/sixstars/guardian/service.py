"""Entry points of the guardian and of stats for the control API, the scheduler and the CLI. They build
the real clients; tests replace `clients` (or call the modules directly with fakes)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sixstars.ai import calls
from sixstars.ai.schemas import TakedownVerdict
from sixstars.clients.upload_post import UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.clients.youtube_analytics import YouTubeAnalyticsClient
from sixstars.guardian import claims, takedowns
from sixstars.guardian import config as guardian_config
from sixstars.stats import collector, report


@dataclass(frozen=True, slots=True)
class Clients:
    upload_post: UploadPostClient
    youtube: YouTubeClient


def clients() -> Clients:
    """The real clients. The Upload-Post posting guard is not needed: the guardian never uploads."""
    return Clients(UploadPostClient(), YouTubeClient())


def utcnow() -> datetime:
    return datetime.now(UTC)


def classify(sender: str, subject: str, body: str) -> TakedownVerdict:
    """Prompt P7 (the fast model; never held back by the AI budget)."""
    return calls.classify_takedown(sender, subject, body)


def run_check(now: datetime | None = None) -> dict[str, Any]:
    used = clients()
    return claims.run_check(used.upload_post, used.youtube, now or utcnow()).as_dict()


def handle_takedown(
    sender: Any, subject: Any, body: Any, message_id: Any, received_at: Any, now: datetime | None = None
) -> dict[str, Any]:
    cfg = guardian_config.load()
    email = takedowns.build_email(sender, subject, body, message_id, received_at, cfg.takedown_body_max_chars)
    used = clients()
    return takedowns.handle_email(email, classify, used.upload_post, used.youtube, now or utcnow())


def collect_stats(now: datetime | None = None) -> dict[str, Any]:
    used = clients()
    return collector.collect(
        used.upload_post, used.youtube, now or utcnow(), analytics=YouTubeAnalyticsClient(used.youtube)
    ).as_dict()


def daily_report(day: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    return report.build(day, now)
