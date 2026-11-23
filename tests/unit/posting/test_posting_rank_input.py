"""AI cost plan A2: the posting rank starts from the clip writer's score, not the visual judge's."""

from sixstars.posting.ranking import rank_input_score

VISUAL = {"source": "visual", "writer": {"score": 8}, "visual": {"score": 6}}


def test_the_writer_score_replaces_the_visual_one() -> None:
    assert rank_input_score(6.0, VISUAL, use_visual=False) == 8.0


def test_points_added_on_top_of_the_stored_score_are_kept() -> None:
    # the first-time boost added 1.5 to the visual score 6 before ranking
    assert rank_input_score(7.5, VISUAL, use_visual=False) == 9.5


def test_the_visual_score_is_used_when_switched_on_or_when_there_is_no_visual_score() -> None:
    assert rank_input_score(6.0, VISUAL, use_visual=True) == 6.0
    assert rank_input_score(6.0, {"source": "writer"}, use_visual=False) == 6.0
    assert rank_input_score(6.0, None, use_visual=False) == 6.0
    assert rank_input_score(6.0, {"source": "visual", "writer": {"score": None}}, use_visual=False) == 6.0
