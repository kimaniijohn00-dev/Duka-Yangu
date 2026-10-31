"""SQLite schema and helpers for the Duka-Yangu backend."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "duka.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    location TEXT,
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
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
