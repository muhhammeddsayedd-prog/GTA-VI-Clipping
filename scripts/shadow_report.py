"""Read-only report of the shadow runs (AI cost plan B1): how close each shadow model came to production.

    uv run python scripts/shadow_report.py --days 7
    uv run python scripts/shadow_report.py --days 7 --pairs pairs.json   # 50 P2 title/hook pairs, shuffled

Reads `ai_shadow` (and `clips.gate_result` for each clip's pass line) through DATABASE_URL. Writes nothing to
the database. The pairs file is the input of the blind copy review; its key (which side is production) is
written next to it as `<name>.key.json`.
"""

import argparse
import json
import random
from pathlib import Path
from typing import Any

from sixstars import db
from sixstars.ai import calls
from sixstars.ai.shadow_metrics import VisualPair, shadow_visual_score, visual_report, writer_report

PAIRS = 50


def _pass_line(gate_result: Any) -> float | None:
    try:
        return float(gate_result["checks"]["score"]["limit"])
    except (KeyError, TypeError, ValueError):
        return None


def visual(days: int) -> None:
    rows = db.fetch_all(
        """
        select s.shadow_model, s.primary_output, s.shadow_output, c.gate_result
        from ai_shadow s left join clips c on c.id::text = s.ref_id
        where s.prompt_id = %s and s.created_at > now() - make_interval(days => %s)
        """,
        (calls.P9V, days),
    )
    by_model: dict[str, list[VisualPair]] = {}
    for row in rows:
        line = _pass_line(row["gate_result"])
        primary = (row["primary_output"] or {}).get("score")
        if line is None or not isinstance(primary, int):
            continue
        pair = VisualPair(primary, shadow_visual_score(row["shadow_output"]), line)
        by_model.setdefault(row["shadow_model"], []).append(pair)
    for model, pairs in sorted(by_model.items()):
        report = visual_report(pairs)
        print(f"P9V {model}: {report}  meets criteria: {report.meets_criteria}")


def writer(days: int, pairs_path: Path | None) -> None:
    rows = db.fetch_all(
        """
        select ref_id, shadow_model, primary_output, shadow_output from ai_shadow
        where prompt_id = %s and created_at > now() - make_interval(days => %s)
        """,
        (calls.P2, days),
    )
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_model.setdefault(row["shadow_model"], []).append(row)
    for model, items in sorted(by_model.items()):
        report = writer_report([(item["primary_output"] or {}, item["shadow_output"]) for item in items])
        print(f"P2 {model}: {report}  meets criteria (without the copy review): {report.meets_criteria}")
        if pairs_path is not None:
            _write_pairs(pairs_path.with_name(f"{pairs_path.stem}.{model}{pairs_path.suffix}"), items)


def _write_pairs(path: Path, items: list[dict[str, Any]]) -> None:
    usable = [item for item in items if isinstance(item["shadow_output"], dict)]
    chosen = random.sample(usable, min(PAIRS, len(usable)))
    pairs, key = [], []
    for index, item in enumerate(chosen):
        sides = [("production", item["primary_output"]), ("shadow", item["shadow_output"])]
        random.shuffle(sides)
        pairs.append(
            {
                "pair": index,
                "A": {"title": sides[0][1].get("title"), "hook": sides[0][1].get("hook")},
                "B": {"title": sides[1][1].get("title"), "hook": sides[1][1].get("hook")},
            }
        )
        key.append({"pair": index, "clip": item["ref_id"], "A": sides[0][0], "B": sides[1][0]})
    path.write_text(json.dumps(pairs, indent=2, ensure_ascii=False), encoding="utf-8")
    path.with_name(f"{path.stem}.key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"  wrote {len(pairs)} blind pairs to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--pairs", type=Path, default=None)
    args = parser.parse_args()
    visual(args.days)
    writer(args.days, args.pairs)


if __name__ == "__main__":
    main()
