"""Tests for the v2 schema additions (plan build steps 1-2).

No endpoints are scoped yet and no auth behavior exists: these tests
prove the new tables/columns are created on fresh databases, upgrade a
v1-shaped database without losing data, survive re-runs, and leave v1
behavior (entry/confirm/ledger flow, global AI cap) exactly unchanged.
"""
from datetime import datetime, timezone

import pytest

import app as appmod
import db
import helpers

# The exact pre-v2 shapes for shops, audit_log and usage_counters; every
# other table is unchanged from the current schema. Used to build a
# faithful v1 database to upgrade.
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

# invite_codes holds single-use registration codes (hash only); the raw
# code is never stored. Added with the invite-code change alongside auth.
V2_TABLES = {"users", "sessions", "login_throttles", "login_attempts",
             "invite_codes"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v2schema.db")


def table_names():
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return {row[0] for row in rows}


def columns(table):
    with db.get_connection() as conn:
        return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def seed_v1_db():
    """A small v1 database: demo shop, one NULL-shop entry, one counter."""
    day = "2026-11-01"
    with db.get_connection() as conn:
        conn.executescript(V1_SCHEMA)
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Demo Duka (synthetic data)", "Nairobi (demo)", day),
        )
        demo_id = cur.lastrowid
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', 'sugar', 5, 'kilos', 800,
                       NULL, 'sold sugar', 'api', 1, ?, ?)""",
            (demo_id, day, day),
        )
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (NULL, 'sale', 'tea', 1, 'packet', 100,
                       NULL, 'sold tea', 'api', 1, ?, ?)""",
            (day, day),
        )
        conn.execute(
            "INSERT INTO usage_counters (day, name, count) VALUES (?, 'ai_calls', 7)",
            (day,),
        )
        conn.execute(
            """INSERT INTO audit_log (action, entity, entity_id, detail, created_at)
               VALUES ('confirm', 'transaction', 1, 'sold tea', ?)""",
            (day,),
        )
    return demo_id


def dump_all():
    """Every user table's columns and rows, for before/after comparison."""
    with db.get_connection() as conn:
        tables = sorted(table_names())
        out = {}
        for table in tables:
            cols = columns(table)
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY ROWID").fetchall()
            out[table] = (cols, [tuple(row) for row in rows])
    return out


def test_fresh_db_has_v2_schema(tmp_db):
    db.init_db()

    assert V2_TABLES <= table_names()
    assert "is_demo" in columns("shops")
    assert "shop_id" in columns("audit_log")
    assert columns("usage_counters") == ["day", "name", "shop_id", "count"]
    assert "phone" in columns("users")
    assert "token_hash" in columns("sessions")
    assert "attempted_at" in columns("login_attempts")
    assert columns("invite_codes") == [
        "id", "code_hash", "label", "created_at", "expires_at",
        "used_at", "used_by_shop_id", "revoked_at",
    ]

    with db.get_connection() as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert {
        "idx_transactions_shop",
        "idx_passports_shop",
        "idx_audit_log_shop",
        "idx_sessions_shop",
        "idx_login_attempts_phone_at",
    } <= indexes

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Amina's Duka", "Kisumu", "2026-11-01"),
        )
        row = conn.execute("SELECT is_demo FROM shops").fetchone()
    assert tuple(row) == (0,)


def test_v1_db_upgrades_without_data_loss(tmp_db):
    seed_v1_db()
    before_tx = dump_all()["transactions"]

    db.init_db()

    assert V2_TABLES <= table_names()
    # Old rows untouched; new columns filled with their defaults.
    assert dump_all()["transactions"] == before_tx
    with db.get_connection() as conn:
        shops = conn.execute("SELECT id, name, is_demo FROM shops").fetchall()
        assert [tuple(row) for row in shops] == [
            (1, "Demo Duka (synthetic data)", 0)
        ]
        counters = conn.execute("SELECT day, name, shop_id, count FROM usage_counters").fetchall()
        assert [tuple(row) for row in counters] == [("2026-11-01", "ai_calls", 0, 7)]
        audits = conn.execute("SELECT action, entity, shop_id FROM audit_log").fetchall()
        assert [tuple(row) for row in audits] == [("confirm", "transaction", None)]
        for table in V2_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_init_db_rerun_is_harmless(tmp_db):
    seed_v1_db()
    db.init_db()
    upgraded = dump_all()

    db.init_db()

    assert dump_all() == upgraded


def test_v1_entry_confirm_ledger_flow_unchanged(tmp_db, monkeypatch):
    seed_v1_db()
    db.init_db()
    monkeypatch.setattr(
        appmod,
        "parse_entry",
        lambda text: {
            "type": "sale",
            "item": "sugar",
            "quantity": 5,
            "unit": "kilos",
            "amount_kes": 800,
            "counterparty": None,
        },
    )

    account = helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Schema Duka", ip="10.13.0.1",
    )
    draft = appmod.create_entry(
        appmod.EntryIn(text="sold 5 kilos of sugar"),
        helpers.authed("/api/entry", account, method="POST"),
    )
    assert draft["confirmed"] == 0
    confirmed = appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"]),
        helpers.authed("/api/transactions/confirm", account, method="POST"),
    )
    assert confirmed["confirmed"] == 1

    ledger = appmod.read_ledger(helpers.authed("/api/ledger", account))
    assert [entry["id"] for entry in ledger] == [draft["id"]]
    assert all(entry["confirmed"] == 1 for entry in ledger)


def test_global_ai_cap_counter_unchanged_after_upgrade(tmp_db):
    seed_v1_db()
    db.init_db()
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    with db.get_connection() as conn:
        assert appmod._try_consume_ai_call(conn, now, 1) is True
        row = conn.execute(
            "SELECT count FROM usage_counters"
            " WHERE day = '2026-11-01' AND name = 'ai_calls' AND shop_id = 0"
        ).fetchone()
    assert tuple(row) == (8,)


def test_pinned_auth_libraries_work(tmp_db):
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
    from itsdangerous import URLSafeTimedSerializer

    pin_hash = PasswordHasher().hash("482917")
    assert PasswordHasher().verify(pin_hash, "482917")
    try:
        PasswordHasher().verify(pin_hash, "482918")
    except VerifyMismatchError:
        pass
    else:  # pragma: no cover
        raise AssertionError("wrong PIN must not verify")

    signer = URLSafeTimedSerializer("test-secret")
    token = signer.dumps({"session_id": 1, "shop_id": 2})
    assert signer.loads(token, max_age=60) == {"session_id": 1, "shop_id": 2}
