"""Seed throwaway smoke-test data directly into a database file.

Used by backend/scripts/smoke_http.ps1. Writes rows with plain SQL so the
smoke test never calls the AI parser (no API key needed, no network):

- Shop A: 60 days x 3 confirmed rows (180 total, enough for a credit
  score: >=30 days of history and >=100 confirmed transactions) plus one
  unconfirmed draft.
- Shop B: one unconfirmed draft.

Prints one JSON line: {"shop_a_confirmed": N, "draft_a": id, "draft_b": id}.
Shop ids come from the registration responses; the script takes them as
arguments. Only stdlib is used.

Run from the repo root, e.g.::

    .venv\\Scripts\\python.exe backend/scripts/smoke_seed.py --db C:\\Temp\\x.db --shop-a 1 --shop-b 2
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed smoke-test rows for two shops."
    )
    parser.add_argument("--db", required=True, help="Database file to write to.")
    parser.add_argument("--shop-a", required=True, type=int, help="Shop A id.")
    parser.add_argument("--shop-b", required=True, type=int, help="Shop B id.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    rows = []
    for offset in range(60):
        day = (now - timedelta(days=offset)).date().isoformat()
        # Two sales and one purchase per day: 180 confirmed rows for Shop A.
        rows.append(
            (args.shop_a, "sale", "sugar", 2, "pcs", 200, None,
             "smoke sale sugar", "smoke", 1,
             f"{day}T10:00:00+00:00", f"{day}T10:00:00+00:00")
        )
        rows.append(
            (args.shop_a, "sale", "bread", 3, "pcs", 150, None,
             "smoke sale bread", "smoke", 1,
             f"{day}T14:00:00+00:00", f"{day}T14:00:00+00:00")
        )
        rows.append(
            (args.shop_a, "purchase", "flour", 2, "2kg", 120, None,
             "smoke restock flour", "smoke", 1,
             f"{day}T09:00:00+00:00", f"{day}T09:00:00+00:00")
        )
    stamp = now.isoformat()
    draft_a = (
        args.shop_a, "sale", "sugar", 5, "pcs", 500, None,
        "smoke draft A", "smoke", 0, stamp, stamp,
    )
    draft_b = (
        args.shop_b, "sale", "sugar", 1, "pcs", 300, None,
        "smoke draft B", "smoke", 0, stamp, stamp,
    )
    conn = sqlite3.connect(args.db)
    try:
        cur = conn.executemany(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        confirmed = cur.rowcount
        draft_a_id = conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            draft_a,
        ).lastrowid
        draft_b_id = conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            draft_b,
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(
        {"shop_a_confirmed": confirmed,
         "draft_a": draft_a_id, "draft_b": draft_b_id}
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
