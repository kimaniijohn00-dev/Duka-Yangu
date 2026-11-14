"""Idempotent v2 data migration (plan step 8).

What it does on --apply:
  1. Copies the database file to a timestamped backup next to it and
     refuses to continue unless the copy exists with the same size.
  2. Runs init_db() to add the v2 tables, columns and indexes.
  3. Sets is_demo = 1 on the existing demo shop (found by its known
     name; zero matches is fine, more than one aborts as ambiguous).
  4. Moves every transaction with shop_id NULL into a reserved shop
     named "Pre-v2 entries (unclaimed)" (is_demo = 0, never publicly
     listed, no user attached) and fills audit_log.shop_id wherever
     the owning row can be inferred.
  5. Prints before/after counts and exits non-zero if anything that
     must not change differs (demo rows byte-identical, per-shop
     totals preserved).

The default is --dry-run: report everything above without changing a
byte. A separate operator command reassigns the unclaimed rows:
  --claim-shop <id> --to-user <phone> --confirm

Run from the repo root, always against a COPY first::

    .\\.venv\\Scripts\\python.exe backend/scripts/migrate_v2.py --db C:\\backups\\duka-copy.db
    .\\.venv\\Scripts\\python.exe backend/scripts/migrate_v2.py --db C:\\backups\\duka-copy.db --apply
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from services.auth import mask_phone, normalize_phone  # noqa: E402
from services.credit import DEMO_SHOP_NAME  # noqa: E402

UNCLAIMED_SHOP_NAME = "Pre-v2 entries (unclaimed)"


def resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.getenv("DUKA_DB_PATH") or (db.DB_PATH))


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table: str) -> list:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def schema_gaps(conn) -> list:
    """v2 schema pieces a pre-v2 database is missing (fixed by init_db)."""
    gaps = []
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in ("users", "sessions", "login_throttles", "login_attempts"):
        if table not in tables:
            gaps.append(f"missing table {table}")
    if "shops" in tables and "is_demo" not in table_columns(conn, "shops"):
        gaps.append("missing column shops.is_demo")
    if "audit_log" in tables and "shop_id" not in table_columns(conn, "audit_log"):
        gaps.append("missing column audit_log.shop_id")
    if "usage_counters" in tables and "shop_id" not in table_columns(
        conn, "usage_counters"
    ):
        gaps.append("missing column usage_counters.shop_id")
    return gaps


def snapshot_counts(conn) -> dict:
    """Counts and demo payloads used to prove the migration is lossless."""
    per_shop = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT shop_id, COUNT(*) FROM transactions GROUP BY shop_id"
        ).fetchall()
    }
    null_tx = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE shop_id IS NULL"
    ).fetchone()[0]
    demo = conn.execute(
        "SELECT * FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
    ).fetchall()
    demo_tx = []
    if len(demo) == 1:
        demo_tx = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transactions WHERE shop_id = ? ORDER BY id",
                (demo[0]["id"],),
            ).fetchall()
        ]
    return {
        "null_tx": null_tx,
        "per_shop": per_shop,
        "total_tx": sum(per_shop.values()),
        "demo_rows": len(demo),
        "demo_shop": dict(demo[0]) if len(demo) == 1 else None,
        "demo_tx": demo_tx,
        "passports": conn.execute("SELECT COUNT(*) FROM passports").fetchone()[0],
    }


def print_snapshot(title: str, snap: dict) -> None:
    print(f"--- {title} ---")
    print(f"transactions with NULL shop_id: {snap['null_tx']}")
    print(f"total transactions: {snap['total_tx']}")
    for shop_id in sorted(snap["per_shop"], key=lambda v: (v is None, v)):
        print(f"  shop {shop_id}: {snap['per_shop'][shop_id]} transactions")
    print(f"demo shop rows: {snap['demo_rows']}")
    print(f"demo transactions: {len(snap['demo_tx'])}")
    print(f"passports: {snap['passports']}")


def find_demo_shop(conn):
    """Demo-name rows as (id, is_demo or None when the column is missing)."""
    has_flag = "is_demo" in table_columns(conn, "shops")
    query = (
        "SELECT id, is_demo FROM shops WHERE name = ?"
        if has_flag
        else "SELECT id, NULL AS is_demo FROM shops WHERE name = ?"
    )
    return conn.execute(query, (DEMO_SHOP_NAME,)).fetchall()


def get_or_create_unclaimed(conn, now: str) -> int:
    row = conn.execute(
        "SELECT id, is_demo FROM shops WHERE name = ?", (UNCLAIMED_SHOP_NAME,)
    ).fetchone()
    if row is not None:
        if row["is_demo"]:
            raise RuntimeError("Unclaimed shop must never be flagged as demo.")
        return row["id"]
    cur = conn.execute(
        "INSERT INTO shops (name, location, is_demo, created_at)"
        " VALUES (?, ?, 0, ?)",
        (UNCLAIMED_SHOP_NAME, "migration", now),
    )
    return cur.lastrowid


def infer_audit_shops(conn) -> dict:
    """Backfill audit_log.shop_id from the referenced transaction/passport/user."""
    moved = {}
    cur = conn.execute(
        """UPDATE audit_log SET shop_id =
           (SELECT shop_id FROM transactions
            WHERE transactions.id = audit_log.entity_id)
           WHERE entity = 'transaction' AND entity_id IS NOT NULL
           AND shop_id IS NULL"""
    )
    moved["transaction"] = cur.rowcount
    cur = conn.execute(
        """UPDATE audit_log SET shop_id =
           (SELECT shop_id FROM passports
            WHERE passports.id = audit_log.entity_id)
           WHERE entity = 'passport' AND entity_id IS NOT NULL
           AND shop_id IS NULL"""
    )
    moved["passport"] = cur.rowcount
    cur = conn.execute(
        """UPDATE audit_log SET shop_id =
           (SELECT shop_id FROM users
            WHERE users.id = audit_log.entity_id)
           WHERE entity = 'auth' AND entity_id IS NOT NULL
           AND shop_id IS NULL"""
    )
    moved["auth"] = cur.rowcount
    return moved


def verify_migration(before: dict, after: dict, unclaimed_id: int) -> list:
    """Return a list of violations; empty means the migration is lossless."""
    problems = []
    if after["null_tx"] != 0:
        problems.append(f"{after['null_tx']} transactions still have NULL shop_id")
    if after["total_tx"] != before["total_tx"]:
        problems.append(
            f"transaction total changed: {before['total_tx']} -> {after['total_tx']}"
        )
    if before["demo_rows"] == 1:
        # is_demo = 1 is the intended change; everything else must match.
        before_shop = {k: v for k, v in before["demo_shop"].items() if k != "is_demo"}
        after_shop = {k: v for k, v in after["demo_shop"].items() if k != "is_demo"}
        if after_shop != before_shop:
            problems.append("demo shop row changed")
        if after["demo_shop"].get("is_demo") != 1:
            problems.append("demo shop is not flagged is_demo = 1")
        if after["demo_tx"] != before["demo_tx"]:
            problems.append("demo transactions changed")
    for shop_id, count in before["per_shop"].items():
        if shop_id is None:
            continue
        if shop_id == unclaimed_id:
            continue
        if after["per_shop"].get(shop_id) != count:
            problems.append(
                f"shop {shop_id} total changed: "
                f"{count} -> {after['per_shop'].get(shop_id)}"
            )
    expected_unclaimed = before["per_shop"].get(None, 0) + before["per_shop"].get(
        unclaimed_id, 0
    )
    if after["per_shop"].get(unclaimed_id, 0) != expected_unclaimed:
        problems.append(
            f"unclaimed shop should hold {expected_unclaimed} transactions, "
            f"holds {after['per_shop'].get(unclaimed_id, 0)}"
        )
    return problems


def backup_database(src: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.stem}.backup-{stamp}{src.suffix}")
    shutil.copy2(src, dst)
    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
        raise RuntimeError(f"Backup copy failed: {dst}")
    return dst


def describe_plan(conn, snap: dict) -> list:
    """Human-readable list of what --apply would change."""
    lines = []
    for gap in schema_gaps(conn):
        lines.append(f"schema: add {gap}")
    demo = find_demo_shop(conn)
    if len(demo) > 1:
        lines.append("BLOCKED: more than one shop carries the demo name")
    elif len(demo) == 1 and not demo[0]["is_demo"]:
        lines.append(f"demo: set is_demo = 1 on shop {demo[0]['id']}")
    lines.append(
        f"data: move {snap['null_tx']} NULL-shop transactions "
        f"into '{UNCLAIMED_SHOP_NAME}'"
    )
    lines.append("data: backfill audit_log.shop_id where inferable")
    return lines


def do_apply(conn, now: str) -> tuple:
    """Run the migration inside one connection. Returns (summary lines)."""
    lines = []
    demo = find_demo_shop(conn)
    if len(demo) > 1:
        raise RuntimeError("More than one shop carries the demo name; aborting.")
    if len(demo) == 1:
        if not demo[0]["is_demo"]:
            conn.execute(
                "UPDATE shops SET is_demo = 1 WHERE id = ?", (demo[0]["id"],)
            )
            lines.append(f"demo: set is_demo = 1 on shop {demo[0]['id']}")
        else:
            lines.append(f"demo: shop {demo[0]['id']} already flagged")
    else:
        lines.append("demo: no demo shop found; fresh seeds get the flag")
    unclaimed_id = get_or_create_unclaimed(conn, now)
    lines.append(f"data: unclaimed shop id is {unclaimed_id}")
    cur = conn.execute(
        "UPDATE transactions SET shop_id = ? WHERE shop_id IS NULL",
        (unclaimed_id,),
    )
    lines.append(f"data: moved {cur.rowcount} NULL-shop transactions")
    moved = infer_audit_shops(conn)
    lines.append(
        "data: backfilled audit_log.shop_id "
        f"(transaction={moved['transaction']}, "
        f"passport={moved['passport']}, auth={moved['auth']})"
    )
    return lines, unclaimed_id


def do_claim(conn, source_id: int, phone: str, now: str) -> tuple:
    """Reassign every transaction of one shop to the phone owner's shop."""
    canonical = normalize_phone(phone)
    if canonical is None:
        raise RuntimeError(f"Not a Kenyan mobile number: {phone}")
    owner = conn.execute(
        "SELECT id, shop_id FROM users WHERE phone = ?", (canonical,)
    ).fetchone()
    if owner is None:
        raise RuntimeError(f"No registered user for {mask_phone(canonical)}")
    source = conn.execute(
        "SELECT id, name, is_demo FROM shops WHERE id = ?", (source_id,)
    ).fetchone()
    if source is None:
        raise RuntimeError(f"No shop with id {source_id}")
    if source["name"] != UNCLAIMED_SHOP_NAME:
        raise RuntimeError(
            f"Only '{UNCLAIMED_SHOP_NAME}' can be claimed, "
            f"not '{source['name']}'."
        )
    attached = conn.execute(
        "SELECT COUNT(*) FROM users WHERE shop_id = ?", (source_id,)
    ).fetchone()[0]
    if attached:
        raise RuntimeError("Unclaimed shop has a user attached; aborting.")
    rows = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE shop_id = ?", (source_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE transactions SET shop_id = ? WHERE shop_id = ?",
        (owner["shop_id"], source_id),
    )
    conn.execute(
        """UPDATE audit_log SET shop_id = ?
           WHERE entity = 'transaction' AND shop_id = ?""",
        (owner["shop_id"], source_id),
    )
    detail = json.dumps(
        {
            "from_shop": source_id,
            "to_shop": owner["shop_id"],
            "rows": rows,
            "phone": mask_phone(canonical),
        }
    )
    conn.execute(
        """INSERT INTO audit_log
           (action, entity, entity_id, shop_id, detail, created_at)
           VALUES ('claimed', 'shop', ?, ?, ?, ?)""",
        (owner["shop_id"], owner["shop_id"], detail, now),
    )
    return rows, owner["shop_id"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotent v2 data migration. Dry run unless --apply."
    )
    parser.add_argument("--db", default=None, help="Database file to migrate.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up the database, then migrate. Without it: dry run only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only (the default; accepted for clarity).",
    )
    parser.add_argument(
        "--claim-shop",
        type=int,
        default=None,
        help="Move every transaction of this (unclaimed) shop id...",
    )
    parser.add_argument(
        "--to-user", default=None, help="...to the shop of this phone number."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for --claim-shop to write anything.",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(args.db)
    now = datetime.now(timezone.utc).isoformat()
    if args.claim_shop is not None or args.to_user is not None:
        return run_claim(args, db_path, now)
    if not args.apply:
        return run_dry(args, db_path)
    return run_apply(args, db_path, now)


def run_dry(args, db_path: Path) -> int:
    if not db_path.is_file():
        print(f"No database at {db_path}; --apply would create the v2 schema.")
        return 0
    with connect(db_path) as conn:
        snap = snapshot_counts(conn)
        print(f"Dry run on {db_path}: nothing will be changed.")
        for line in describe_plan(conn, snap):
            print(f"  would: {line}")
        print_snapshot("current state", snap)
        if len(find_demo_shop(conn)) > 1:
            print("BLOCKED: more than one shop carries the demo name.")
            return 1
    return 0


def run_apply(args, db_path: Path, now: str) -> int:
    if not db_path.is_file():
        db.DB_PATH = db_path
        db.init_db()
        print(f"No database at {db_path}; created the v2 schema on a fresh file.")
        return 0
    try:
        backup = backup_database(db_path)
    except (OSError, RuntimeError) as exc:
        print(f"Refusing to migrate: {exc}")
        return 1
    print(f"Backup: {backup}")
    db.DB_PATH = db_path
    db.init_db()
    with connect(db_path) as conn:
        before = snapshot_counts(conn)
        print_snapshot("before", before)
        try:
            with conn:
                lines, unclaimed_id = do_apply(conn, now)
        except RuntimeError as exc:
            print(f"Refusing to migrate: {exc}")
            return 1
        for line in lines:
            print(f"  did: {line}")
        after = snapshot_counts(conn)
        print_snapshot("after", after)
        problems = verify_migration(before, after, unclaimed_id)
        if problems:
            print("MIGRATION FAILED VERIFICATION:")
            for problem in problems:
                print(f"  - {problem}")
            print(f"Database left as-is except the backup; restore from {backup}")
            return 1
    print("Migration verified: demo rows identical, totals preserved.")
    return 0


def run_claim(args, db_path: Path, now: str) -> int:
    if args.claim_shop is None or args.to_user is None:
        print("--claim-shop and --to-user go together.")
        return 2
    if not db_path.is_file():
        print(f"No database at {db_path}.")
        return 1
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?",
            (args.claim_shop,),
        ).fetchone()[0]
        print(f"Claim would move {rows} transactions from shop {args.claim_shop} "
              f"to the shop of {mask_phone(args.to_user)}.")
        if not args.confirm:
            print("Refusing without --confirm.")
            return 2
        try:
            backup = backup_database(db_path)
        except (OSError, RuntimeError) as exc:
            print(f"Refusing to claim: {exc}")
            return 1
        print(f"Backup: {backup}")
        try:
            with conn:
                moved, target = do_claim(conn, args.claim_shop, args.to_user, now)
        except RuntimeError as exc:
            print(f"Refusing to claim: {exc}")
            return 1
    print(f"Claimed {moved} transactions to shop {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

