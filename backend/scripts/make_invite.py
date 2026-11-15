"""Create one single-use invite code and print it exactly once.

The raw code is shown below and never stored: only its SHA-256 hash
goes into the database. Give it to the shopkeeper now — it cannot be
recovered later. Expires in --days days (default 14).

Run from the repo root::

    .\\.venv\\Scripts\\python.exe backend/scripts/make_invite.py --label "Mama Wanjiku"
    .\\.venv\\Scripts\\python.exe backend/scripts/make_invite.py --label "Mama Wanjiku" --days 30 --db C:\\backups\\duka.db
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from services.invites import DEFAULT_EXPIRY_DAYS, create_invite  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a single-use invite code (printed once)."
    )
    parser.add_argument("--label", required=True, help="Who the code is for.")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_EXPIRY_DAYS,
        help="Days until the code expires.",
    )
    parser.add_argument("--db", default=None, help="Database file to write to.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        print("--days must be at least 1.")
        return 2
    db.DB_PATH = Path(args.db) if args.db else db.DB_PATH
    os.environ.setdefault("DUKA_DB_PATH", str(db.DB_PATH))
    db.init_db()
    now = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        invite_id, raw = create_invite(conn, args.label, args.days, now)
    print(f'Invite code for "{args.label}" (id {invite_id}, '
          f"expires in {args.days} days):")
    print(raw)
    print("Give this to the shopkeeper now — it is shown only once and "
          "stored hashed. One code registers exactly one shop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
