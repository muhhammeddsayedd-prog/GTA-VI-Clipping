"""Switch a scoring overhaul flag on or off (docs/CHANGELOG-scoring.md).

    uv run python scripts/scoring_flag.py <env> <key> on|off|show

Only flips `enabled` inside the flag's JSON; every other number is kept. Prints the flag and the new scoring
config version. Running services pick the change up within 30 s.
"""

import json
import pathlib
import sys

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3] not in ("on", "off", "show"):
        raise SystemExit(__doc__)
    env, key, action = sys.argv[1:]
    url = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in (ROOT / f".env.{env}").read_text().splitlines()
        if line.startswith("DATABASE_URL=")
    )
    with psycopg.connect(url, autocommit=True) as conn:
        if action != "show":
            changed = conn.execute(
                "update scoring_config set value = jsonb_set(value, '{enabled}', %s::jsonb) where key = %s",
                ("true" if action == "on" else "false", key),
            ).rowcount
            if changed != 1:
                raise SystemExit(f"no scoring_config row {key!r}")
        row = conn.execute("select value, updated_at from scoring_config where key = %s", (key,)).fetchone()
        version = conn.execute("select coalesce(max(version), 0) from scoring_config_history").fetchone()
    assert row is not None and version is not None
    print(json.dumps({"key": key, "value": row[0], "updated_at": str(row[1]), "version": version[0]}))


if __name__ == "__main__":
    main()
