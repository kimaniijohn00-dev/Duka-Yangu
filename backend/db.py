"""SQLite schema and helpers for the Duka-Yangu backend."""
import os
import sqlite3
from pathlib import Path

# Overridden on Modal (DUKA_DB_PATH=/data/duka.db on the data Volume).
# Local default is unchanged: backend/duka.db next to this file.
DB_PATH = Path(os.getenv("DUKA_DB_PATH") or (Path(__file__).resolve().parent / "duka.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
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

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER REFERENCES shops(id),
    name TEXT NOT NULL,
    unit TEXT,
    price_kes INTEGER,
    stock REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    shop_id INTEGER REFERENCES shops(id),
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passports (
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

CREATE TABLE IF NOT EXISTS passport_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passport_id INTEGER REFERENCES passports(id),
    viewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_counters (
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    shop_id INTEGER NOT NULL DEFAULT 0,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name, shop_id)
);

CREATE TABLE IF NOT EXISTS explain_cache (
    shop_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'template',
    created_at TEXT NOT NULL,
    PRIMARY KEY (shop_id, score, as_of)
);

-- v2 auth tables. One phone maps to one shop; throttling state lives in
-- login_throttles (per phone+IP) and login_attempts (timestamped event
-- log for the per-phone 24h ceiling), never in users.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    phone TEXT NOT NULL UNIQUE,
    pin_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS login_throttles (
    phone TEXT NOT NULL,
    ip TEXT NOT NULL,
    fails INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    updated_at TEXT,
    PRIMARY KEY (phone, ip)
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    ip TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0
);

"""

# Indexes run after the column ensures below: some indexed columns
# (audit_log.shop_id, shops.is_demo) do not exist on pre-v2 databases
# until _ensure_column adds them.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_transactions_shop ON transactions(shop_id);
CREATE INDEX IF NOT EXISTS idx_passports_shop ON passports(shop_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_shop ON audit_log(shop_id);
CREATE INDEX IF NOT EXISTS idx_sessions_shop ON sessions(shop_id);
CREATE INDEX IF NOT EXISTS idx_login_attempts_phone_at ON login_attempts(phone, attempted_at);
"""

# usage_counters.shop_id uses 0 for the shared global aggregate row: real
# shop ids start at 1, so 0 never collides and no FOREIGN KEY applies.
GLOBAL_USAGE_SHOP_ID = 0


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn, table: str) -> list:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_column(conn, table: str, definition: str) -> None:
    """ALTER TABLE ADD COLUMN when a pre-v2 database lacks it. No-op otherwise."""
    column = definition.split()[0]
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate_usage_counters(conn) -> None:
    """Give a pre-v2 usage_counters the shop_id key, preserving every count.

    Old shape PK(day, name); new shape PK(day, name, shop_id) with
    GLOBAL_USAGE_SHOP_ID for the existing global rows. Runs once: when
    shop_id already exists there is nothing to do.
    """
    if "shop_id" in _table_columns(conn, "usage_counters"):
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage_counters_new (
            day TEXT NOT NULL,
            name TEXT NOT NULL,
            shop_id INTEGER NOT NULL DEFAULT 0,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, name, shop_id)
        )"""
    )
    conn.execute(
        "INSERT INTO usage_counters_new (day, name, shop_id, count)"
        f" SELECT day, name, {GLOBAL_USAGE_SHOP_ID}, count FROM usage_counters"
    )
    conn.execute("DROP TABLE usage_counters")
    conn.execute("ALTER TABLE usage_counters_new RENAME TO usage_counters")


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "shops", "is_demo INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "audit_log", "shop_id INTEGER REFERENCES shops(id)")
        _migrate_usage_counters(conn)
        conn.executescript(INDEXES)
