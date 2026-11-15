"""List invite codes and revoke them.

Shows each code's label, status (unused/used/expired/revoked), which
shop used it, and expiry. Raw codes are never shown — they cannot be
recovered. --revoke marks an unused code revoked.

Run from the repo root::

    .\\.venv\\Scripts\\python.exe backend/scripts/list_invites.py
    .\\.venv\\Scripts\\python.exe backend/scripts/list_invites.py --revoke 3
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from services.invites import InvalidInvite, revoke_invite, status_of  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or revoke invite codes.")
    parser.add_argument("--db", default=None, help="Database file to read.")
    parser.add_argument(
        "--revoke", type=int, default=None, help="Revoke the code with this id."
    )
    return parser


def shop_name(conn, shop_id):
    if shop_id is None:
        return "-"
    row = conn.execute(
        "SELECT name FROM shops WHERE id = ?", (shop_id,)
    ).fetchone()
    return row["name"] if row else f"(shop {shop_id} deleted)"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db.DB_PATH = Path(args.db) if args.db else db.DB_PATH
    os.environ.setdefault("DUKA_DB_PATH", str(db.DB_PATH))
    db.init_db()
    now = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        if args.revoke is not None:
            try:
                with conn:
                    revoke_invite(conn, args.revoke, now)
            except InvalidInvite as exc:
                print(exc)
                return 1
            print(f"Revoked invite code {args.revoke}.")
            return 0
        rows = conn.execute(
            "SELECT * FROM invite_codes ORDER BY id"
        ).fetchall()
        if not rows:
            print("No invite codes yet.")
            return 0
        print(f"{'id':>3}  {'label':24}  {'status':8}  used by  expires")
        for row in rows:
            print(
                f"{row['id']:>3}  {row['label'][:24]:24}  "
                f"{status_of(row, now):8}  "
                f"{shop_name(conn, row['used_by_shop_id'])[:20]:20}  "
                f"{row['expires_at'][:10]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
