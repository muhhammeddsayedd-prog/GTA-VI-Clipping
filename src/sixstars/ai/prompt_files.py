"""Loads the prompt files in `ai/prompts/`. Each `<prompt id>.md` has a `## System` section (static, cached
by the API) and a `## User template` section; both use `{placeholders}` filled with `str.format_map`.
`_preamble.md` is the common first paragraph of every system prompt and `_score_rubric.md` the scoring
scales shared by the clip writer (P2) and the clip scorers (P9, P9V); a system section includes them with
`{preamble}` and `{score_rubric}`. Implemented in M05.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_IDS = (
    "p1_spike_confirm",
    "p2_clip_writer",
    "p3_vod_moment_picker",
    "p4_layout_detect",
    "p5_visual_check",
    "p6_permission_read",
    "p7_takedown_classify",
    "p8_translate_lines",
    "p9_clip_score",
    "p9_visual_score",
    "p10_sentence_ends",
    "p11_title_writer",
    "p12_cover_hookline",
    "p13_cover_qa",
    "p14_episode_package",
)
SYSTEM_HEADING = "## System"
USER_HEADING = "## User template"


class PromptFileError(ValueError):
    """A prompt file is missing a section or a value for one of its placeholders."""


@dataclass(frozen=True, slots=True)
class Prompt:
    prompt_id: str
    system: str
    user_template: str

    def render_system(self, values: Mapping[str, object]) -> str:
        """System prompt with the preamble and the given values filled in."""
        preamble = _fill(self.prompt_id, preamble_text(), values)
        shared = {"preamble": preamble, "score_rubric": score_rubric_text()}
        return _fill(self.prompt_id, self.system, {**values, **shared})

    def render_user(self, values: Mapping[str, object]) -> str:
        return _fill(self.prompt_id, self.user_template, values)


def _fill(prompt_id: str, template: str, values: Mapping[str, object]) -> str:
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise PromptFileError(f"{prompt_id}: no value for placeholder {exc}") from exc


def parse(prompt_id: str, text: str) -> Prompt:
    """Split a prompt file into its two sections."""
    lines = text.splitlines()
    try:
        system_at = lines.index(SYSTEM_HEADING)
        user_at = lines.index(USER_HEADING)
    except ValueError as exc:
        raise PromptFileError(f"{prompt_id}: needs '{SYSTEM_HEADING}' and '{USER_HEADING}' sections") from exc
    if user_at < system_at:
        raise PromptFileError(f"{prompt_id}: '{SYSTEM_HEADING}' must come before '{USER_HEADING}'")
    system = "\n".join(lines[system_at + 1 : user_at]).strip()
    user = "\n".join(lines[user_at + 1 :]).strip()
    if not system or not user:
        raise PromptFileError(f"{prompt_id}: a section is empty")
    return Prompt(prompt_id, system, user)


@cache
def load(prompt_id: str) -> Prompt:
    """Read and parse `prompts/<prompt_id>.md` (cached for the life of the process)."""
    path = PROMPTS_DIR / f"{prompt_id}.md"
    if not path.is_file():
        raise PromptFileError(f"prompt file not found: {path.name}")
    return parse(prompt_id, path.read_text(encoding="utf-8"))


@cache
def preamble_text() -> str:
    return (PROMPTS_DIR / "_preamble.md").read_text(encoding="utf-8").strip()


@cache
def score_rubric_text() -> str:
    """The scoring scales and bands, word for word the same in P2, P9 and P9V."""
    return (PROMPTS_DIR / "_score_rubric.md").read_text(encoding="utf-8").strip()
