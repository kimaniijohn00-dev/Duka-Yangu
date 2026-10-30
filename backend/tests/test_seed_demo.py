"""Tests for scripts/seed_demo.py. The seeder only ever touches a temporary DB here."""
import sys
from pathlib import Path

import db

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import seed_demo

ALLOWED_TYPES = {"sale", "purchase", "expense", "credit_given", "credit_received"}


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "seed.db")
    db.init_db()


def demo_rows():
    with db.get_connection() as conn:
        shop = conn.execute(
            "SELECT * FROM shops WHERE name = ?", (seed_demo.SHOP_NAME,)
        ).fetchone()
        assert shop is not None
        rows = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ?", (shop["id"],)
        ).fetchall()
        return shop, rows


def test_seed_creates_60_days_with_positive_totals(monkeypatch, tmp_path, capsys):
    use_temp_db(monkeypatch, tmp_path)

    summary = seed_demo.main([])

    shop, rows = demo_rows()
    assert shop["name"] == "Demo Duka (synthetic data)"
    assert len(rows) == summary["transactions"] > 0
    assert {row["type"] for row in rows} <= ALLOWED_TYPES
    assert {row["confirmed"] for row in rows} == {1}
    assert all(isinstance(row["amount_kes"], int) for row in rows)
    assert min(row["amount_kes"] for row in rows) >= 0
    days = {row["occurred_at"][:10] for row in rows}
    assert len(days) == 60
    assert summary["days"] == 60
    assert summary["sales"] > 0
    assert summary["purchases"] > 0
    assert summary["expenses"] > 0
    assert 8000 <= summary["outstanding"] <= 15000
    assert summary["shop_id"] == seed_demo.DEMO_SHOP_ID
    # Stock cost ~72-78% of sales; regular expenses leave ~10-20% net.
    assert 22.0 <= summary["gross_margin"] <= 28.0
    assert 10.0 <= summary["net_margin"] <= 20.0
    printed = capsys.readouterr().out
    for label in (
        "Transactions:",
        "Total sales:",
        "Total purchases:",
        "Total expenses:",
        "Outstanding credit:",
        "Gross margin:",
        "Net margin:",
    ):
        assert label in printed


def test_seed_twice_does_not_duplicate(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)

    first = seed_demo.main([])
    second = seed_demo.main([])

    assert second == first
    _, rows = demo_rows()
    assert len(rows) == first["transactions"]


def test_seed_reset_regenerates_and_leaves_real_data_alone(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Real Duka", "Kisumu", "2026-01-01T08:00:00+03:00"),
        )
        real_id = cur.lastrowid
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', 'sugar', 1, '1kg', 180,
                       NULL, 'real sale', 'api', 1,
                       '2026-01-01T09:00:00+03:00', '2026-01-01T09:00:00+03:00')""",
            (real_id,),
        )

    before = seed_demo.main([])
    after_reset = seed_demo.main(["--reset"])

    assert after_reset == before
    with db.get_connection() as conn:
        real_rows = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ?", (real_id,)
        ).fetchall()
        shops = conn.execute("SELECT name FROM shops").fetchall()
    assert len(real_rows) == 1
    assert real_rows[0]["raw_text"] == "real sale"
    assert {row["name"] for row in shops} == {"Real Duka", seed_demo.SHOP_NAME}
