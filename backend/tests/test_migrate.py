"""Tests for scripts/migrate_v2.py and the demo-flag rules (plan steps 7-8).

A faithful v1 database is built from the old shapes (no is_demo, no
shop_id on audit_log, global-only usage_counters, no auth tables), then:
dry-run changes nothing, --apply backs up and passes the count checks,
apply twice changes nothing more, and the claim command reassigns the
unclaimed rows. Also covered: fresh seeds flag the demo shop, anonymous
demo reads work on a fresh seed, and registration rejects the demo name.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import app as appmod
import auth_routes
import db
import helpers
import migrate_v2
import seed_demo
from services import credit as creditmod
from services.auth import DEMO_SHOP_NAME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

V1_SCHEMA = """
CREATE TABLE shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER REFERENCES shops(id),
    type TEXT NOT NULL CHECK(type IN ('sale', 'purchase', 'expense', 'credit_given', 'credit_received')),
    item TEXT NOT NULL DEFAULT '',
    quantity REAL,
    unit TEXT,
    amount_kes INTEGER NOT NULL,
    counterparty TEXT,
    raw_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    confirmed INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER REFERENCES shops(id),
    name TEXT NOT NULL,
    unit TEXT,
    price_kes INTEGER,
    stock REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE passports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER REFERENCES shops(id),
    lender_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE passport_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passport_id INTEGER REFERENCES passports(id),
    viewed_at TEXT NOT NULL
);

CREATE TABLE usage_counters (
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name)
);

CREATE TABLE explain_cache (
    shop_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'template',
    created_at TEXT NOT NULL,
    PRIMARY KEY (shop_id, score, as_of)
);
"""

DAY = "2026-03-15T09:00:00+03:00"


@pytest.fixture
def v1_db(tmp_path, monkeypatch):
    """A faithful v1 database file: demo shop, NULL rows, passport, counters."""
    path = tmp_path / "v1.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V1_SCHEMA)
    demo = conn.execute(
        "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
        (DEMO_SHOP_NAME, "Nairobi (demo)", DAY),
    ).lastrowid
    real = conn.execute(
        "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
        ("Real Duka", "Kisumu", DAY),
    ).lastrowid
    for shop, item in ((demo, "sugar"), (demo, "bread"), (real, "tea")):
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', ?, 1, 'pcs', 100, NULL, 'seed',
                       'api', 1, ?, ?)""",
            (shop, item, DAY, DAY),
        )
    for item in ("salt", "rice"):
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (NULL, 'sale', ?, 1, 'pcs', 50, NULL, 'mine',
                       'api', 1, ?, ?)""",
            (item, DAY, DAY),
        )
    conn.execute(
        """INSERT INTO passports
           (shop_id, lender_name, scope, snapshot_json, token_hash,
            created_at, expires_at, revoked_at)
           VALUES (?, 'Equity', '["score"]', '{}', 'abc123', ?, ?, NULL)""",
        (demo, DAY, DAY),
    )
    conn.execute(
        "INSERT INTO usage_counters (day, name, count) VALUES (?, 'ai_calls', 4)",
        (DAY[:10],),
    )
    conn.execute(
        """INSERT INTO audit_log (action, entity, entity_id, detail, created_at)
           VALUES ('confirm', 'transaction', 4, 'old sale', ?)""",
        (DAY,),
    )
    conn.commit()
    conn.close()
    return path


def dump_db(path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = {}
        for table in tables:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY ROWID").fetchall()
            out[table] = (cols, [tuple(r) for r in rows])
        return out
    finally:
        conn.close()


def backups_next_to(path):
    return sorted(path.parent.glob(f"{path.stem}.backup-*{path.suffix}"))


def test_dry_run_changes_nothing(v1_db):
    before = dump_db(v1_db)

    assert migrate_v2.main(["--db", str(v1_db)]) == 0
    assert migrate_v2.main(["--db", str(v1_db), "--dry-run"]) == 0

    assert dump_db(v1_db) == before
    assert backups_next_to(v1_db) == []


def test_apply_migrates_and_verifies(v1_db):
    before = dump_db(v1_db)
    before_demo_tx = [r for r in before["transactions"][1] if r[1] == 1]

    assert migrate_v2.main(["--db", str(v1_db), "--apply"]) == 0

    made = backups_next_to(v1_db)
    assert len(made) == 1
    assert dump_db(made[0]) == before

    after = dump_db(v1_db)
    assert after["transactions"][1] != before["transactions"][1]
    with db.get_connection() as conn:
        demo = conn.execute(
            "SELECT * FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()
        assert demo["is_demo"] == 1
        unclaimed = conn.execute(
            "SELECT * FROM shops WHERE name = ?",
            (migrate_v2.UNCLAIMED_SHOP_NAME,),
        ).fetchone()
        assert unclaimed is not None
        assert unclaimed["is_demo"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM users WHERE shop_id = ?", (unclaimed["id"],)
        ).fetchone()[0] == 0
        nulls = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        moved = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?",
            (unclaimed["id"],),
        ).fetchone()[0]
        assert moved == 2
        demo_tx = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? ORDER BY id",
            (demo["id"],),
        ).fetchall()
        assert [tuple(r) for r in demo_tx] == before_demo_tx
        inferred = conn.execute(
            "SELECT shop_id FROM audit_log WHERE action = 'confirm'"
        ).fetchone()[0]
        assert inferred == unclaimed["id"]
        assert "users" in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_apply_twice_changes_nothing_more(v1_db):
    assert migrate_v2.main(["--db", str(v1_db), "--apply"]) == 0
    once = dump_db(v1_db)

    assert migrate_v2.main(["--db", str(v1_db), "--apply"]) == 0

    assert dump_db(v1_db) == once


def test_claim_moves_unclaimed_to_owner(v1_db, monkeypatch):
    assert migrate_v2.main(["--db", str(v1_db), "--apply"]) == 0
    account = helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Claim Duka", ip="10.21.0.1",
    )
    with db.get_connection() as conn:
        unclaimed_id = conn.execute(
            "SELECT id FROM shops WHERE name = ?",
            (migrate_v2.UNCLAIMED_SHOP_NAME,),
        ).fetchone()[0]

    refused = migrate_v2.main(
        ["--db", str(v1_db), "--claim-shop", str(unclaimed_id),
         "--to-user", "+254711111111"]
    )
    assert refused == 2
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?",
            (unclaimed_id,),
        ).fetchone()[0] == 2

    ok = migrate_v2.main(
        ["--db", str(v1_db), "--claim-shop", str(unclaimed_id),
         "--to-user", "+254711111111", "--confirm"]
    )
    assert ok == 0
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?",
            (unclaimed_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?",
            (account["shop_id"],),
        ).fetchone()[0] == 2
        claim = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'claimed'"
        ).fetchone()
        assert claim is not None
        assert "+254711111111" not in claim["detail"]
        assert "0711111111" not in claim["detail"]


def test_claim_refuses_non_unclaimed_shop(v1_db, monkeypatch):
    assert migrate_v2.main(["--db", str(v1_db), "--apply"]) == 0
    helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Claim Duka", ip="10.21.0.1",
    )
    with db.get_connection() as conn:
        demo_id = conn.execute(
            "SELECT id FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()[0]
    refused = migrate_v2.main(
        ["--db", str(v1_db), "--claim-shop", str(demo_id),
         "--to-user", "+254711111111", "--confirm"]
    )
    assert refused == 1


def test_fresh_seed_flags_demo_and_serves_reads(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    summary = seed_demo.main([])
    assert summary["transactions"] > 0

    with db.get_connection() as conn:
        demo = conn.execute(
            "SELECT * FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()
        assert demo["is_demo"] == 1

    result = appmod.credit_summary()
    assert result["shop_name"] == DEMO_SHOP_NAME
    assert result["status"] == "scored"


def test_seed_never_creates_second_demo_shop(tmp_path, monkeypatch):
    path = tmp_path / "reseed.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    seed_demo.main([])
    seed_demo.main(["--reset"])
    seed_demo.main([])

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchall()
        assert len(rows) == 1
        assert conn.execute(
            "SELECT is_demo FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "name",
    [
        "Demo Duka (synthetic data)",
        "  Demo Duka (synthetic data)  ",
        "DEMO DUKA (SYNTHETIC DATA)",
        "demo duka (synthetic data)",
    ],
)
def test_register_rejects_demo_name(tmp_path, monkeypatch, name):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "names.db")
    db.init_db()
    helpers.setup_auth_env(monkeypatch)
    req = helpers.make_request(
        "/api/auth/register", method="POST", origin="http://testserver"
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_routes.register(
            auth_routes.RegisterIn(
                phone="0711111111",
                pin="482917",
                shop_name=name,
                invite_code=helpers.INVITE,
            ),
            req,
            Response(),
        )
    assert exc_info.value.status_code == 422
    assert "reserved" in exc_info.value.detail.lower()


def test_register_allows_similar_name(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "names.db")
    db.init_db()
    account = helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Demo Duka Annex", ip="10.22.0.1",
    )
    assert account["shop_name"] == "Demo Duka Annex"
