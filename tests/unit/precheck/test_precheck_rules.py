"""The pure rules of the YouTube pre-check: when it can run and what a video status means."""

from dataclasses import replace

import pytest

from sixstars.clients.youtube import YouTubeVideoStatus
from sixstars.gate.models import PrecheckState
from sixstars.precheck.state import state_for
from sixstars.precheck.youtube_precheck import Outcome, evaluate_status

PROCESSED = YouTubeVideoStatus(
    video_id="vid1",
    upload_status="processed",
    privacy_status="private",
    rejection_reason=None,
    failure_reason=None,
    license="youtube",
    licensed_content=False,
    region_blocked=(),
    region_allowed=None,
)


@pytest.mark.parametrize(
    ("enabled", "has_oauth", "used", "daily_max", "expected"),
    [
        (True, True, 0, 20, PrecheckState.AVAILABLE),
        (True, True, 19, 20, PrecheckState.AVAILABLE),
        (True, True, 20, 20, PrecheckState.QUOTA_USED),
        (True, True, 21, 20, PrecheckState.QUOTA_USED),
        (True, True, 0, 0, PrecheckState.QUOTA_USED),
        (True, False, 0, 20, PrecheckState.UNAVAILABLE),
        (True, False, 20, 20, PrecheckState.UNAVAILABLE),
        (False, True, 0, 20, PrecheckState.DISABLED),
        (False, False, 0, 20, PrecheckState.DISABLED),
    ],
)
def test_state_for(
    enabled: bool, has_oauth: bool, used: int, daily_max: int, expected: PrecheckState
) -> None:
    assert state_for(enabled=enabled, has_oauth=has_oauth, used_today=used, daily_max=daily_max) is expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, Outcome.PASSED),
        ({"upload_status": "uploaded"}, Outcome.PENDING),
        ({"upload_status": "rejected", "rejection_reason": "claim"}, Outcome.CLAIMED),
        ({"upload_status": "rejected", "rejection_reason": "copyright"}, Outcome.CLAIMED),
        ({"upload_status": "rejected", "rejection_reason": "trademark"}, Outcome.CLAIMED),
        ({"upload_status": "rejected", "rejection_reason": "legal"}, Outcome.CLAIMED),
        ({"upload_status": "rejected", "rejection_reason": None}, Outcome.CLAIMED),
        ({"upload_status": "rejected", "rejection_reason": "duplicate"}, Outcome.INCONCLUSIVE),
        ({"upload_status": "rejected", "rejection_reason": "length"}, Outcome.INCONCLUSIVE),
        ({"region_blocked": ("DE",)}, Outcome.CLAIMED),
        ({"region_blocked": ("DE", "FR"), "upload_status": "uploaded"}, Outcome.CLAIMED),
        ({"region_allowed": ("US",)}, Outcome.CLAIMED),
        ({"region_allowed": ()}, Outcome.CLAIMED),
        ({"upload_status": "failed", "failure_reason": "codec"}, Outcome.INCONCLUSIVE),
        ({"upload_status": "deleted"}, Outcome.INCONCLUSIVE),
        ({"upload_status": None}, Outcome.INCONCLUSIVE),
    ],
)
def test_evaluate_status(changes: dict[str, object], expected: Outcome) -> None:
    outcome, detail = evaluate_status(replace(PROCESSED, **changes))  # type: ignore[arg-type]
    assert outcome is expected and detail


def test_a_video_that_is_gone_counts_as_claimed() -> None:
    outcome, detail = evaluate_status(None)
    assert outcome is Outcome.CLAIMED and "removed" in detail
