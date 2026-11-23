"""AI cost plan A7: while the AI budget is used up, a live spike that can only become an hourly clip is
closed before P1 (its render would stop at the budget guard anyway)."""

import pytest

from sixstars.live.session import skip_for_budget


@pytest.mark.parametrize(
    ("tier", "fast_path", "budget_left", "skipped"),
    [
        ("A", False, False, True),
        ("B", False, False, True),
        ("S", False, False, False),  # S goes to the express path, which the budget never stops
        ("A", True, False, False),  # the fast path may make it S
        ("A", False, True, False),  # budget left: business as usual
        ("S", False, True, False),
    ],
)
def test_skip_for_budget(tier: str, fast_path: bool, budget_left: bool, skipped: bool) -> None:
    assert skip_for_budget(tier, fast_path, budget_left) is skipped
