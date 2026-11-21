"""YouTube lane tuning C: the gate's hourly verdict allows the union of the per-platform windows."""

from gate_helpers import DEFAULT_VALUES

from sixstars.gate.profiles import settings_from_values, with_platform_lengths

OVERRIDE = {"youtube": {"min_len": 8, "max_len": 30}}


def test_the_hourly_verdict_allows_the_union_and_express_is_unchanged() -> None:
    plain = settings_from_values(DEFAULT_VALUES)

    tuned = with_platform_lengths(plain, OVERRIDE)

    assert (tuned.hourly.min_len_s, tuned.hourly.max_len_s) == (8, plain.hourly.max_len_s)
    assert tuned.express == plain.express
    assert tuned.hourly.min_score == plain.hourly.min_score


def test_the_loaded_profile_itself_is_unchanged() -> None:
    """Sources that read gate.hourly for their own limits (Twitch clips) keep them."""
    values = {**DEFAULT_VALUES, "gate.hourly_len_by_platform": OVERRIDE}
    assert settings_from_values(values).hourly == settings_from_values(DEFAULT_VALUES).hourly


def test_without_the_setting_nothing_changes() -> None:
    plain = settings_from_values(DEFAULT_VALUES)
    assert with_platform_lengths(plain, None) == plain
