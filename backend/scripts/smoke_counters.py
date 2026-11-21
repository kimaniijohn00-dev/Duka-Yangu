"""Print this database's AI usage counters as one JSON line.

Used by backend/scripts/smoke_http.ps1 to prove that requests rejected
at the Origin check consume no AI budget: the script snapshots the
counters before the missing-Origin probes and asserts they did not move
afterwards. Only stdlib is used.

Run from the repo root, e.g.::

    .venv\\Scripts\\python.exe backend/scripts/smoke_counters.py --db C:\\Temp\\x.db
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print AI usage counters for a database."
    )
    parser.add_argument("--db", required=True, help="Database file to read.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = datetime.now(timezone.utc).date().isoformat()
    conn = sqlite3.connect(args.db)
    try:
        total = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM usage_counters WHERE name = 'ai_calls'"
        ).fetchone()[0]
        today_count = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM usage_counters"
            " WHERE name = 'ai_calls' AND day = ?",
            (today,),
        ).fetchone()[0]
    finally:
        conn.close()
    print(json.dumps(
        {"ai_calls_total": total, "ai_calls_today": today_count, "day": today}
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
