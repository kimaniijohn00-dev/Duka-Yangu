"""Deterministic demo-data seeder for the Duka-Yangu credit-score demo.

Run from the backend directory::

    python scripts/seed_demo.py            # create demo data (no-op if present)
    python scripts/seed_demo.py --reset    # delete only the demo shop's data, then seed

Shop handling: normal typed and voice entries are stored with shop_id NULL
(the app's EntryIn leaves shop_id unset and the frontend never sends one),
so they can never mix with demo rows. The demo shop additionally gets the
fixed id DEMO_SHOP_ID (999999) plus its unmistakable name, and --reset only
ever deletes transactions belonging to that one shop id.

Economics (tuned for a small Kenyan duka): stock purchases run about
72-78% of sales, with cheaper wholesale ratios on staples (sugar, flour,
cooking oil ~85-90% of retail) and airtime (~95%) than on soap and other
goods (~70%); bread and milk sit around 80%. Regular expenses (rent,
electricity, transport, trading licence, packaging) leave a net profit of
roughly 10-20% of sales.

The run is deterministic (fixed random seed) and idempotent: without
--reset an already-seeded database is left untouched, and --reset wipes
just the demo shop's transactions before regenerating the exact same rows.
Only the five transaction types from the schema CHECK constraint are used;
the schema itself is never changed.
"""
import argparse
import calendar
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

SHOP_NAME = "Demo Duka (synthetic data)"
SHOP_LOCATION = "Nairobi (demo)"
DEMO_SHOP_ID = 999999
SEED = 42
DAYS = 60
SOURCE = "seed"
NAIROBI = timezone(timedelta(hours=3))

RENT_KES = 8000
ELECTRICITY_KES = 1500
TRANSPORT_KES = 400
LICENCE_KES = 6000
PACKAGING_KES = 350

# (item, unit, retail KES, wholesale cost as a fraction of retail, line weight)
PRODUCTS = [
    ("sugar", "1kg", 180, 0.86, 1.0),
    ("flour", "2kg", 310, 0.86, 1.0),
    ("cooking oil", "1L", 360, 0.86, 1.0),
    ("bread", "pcs", 75, 0.80, 2.5),
    ("milk", "500ml", 70, 0.80, 2.5),
    ("airtime", "KES", 50, 0.95, 1.0),
    ("soap", "bar", 140, 0.70, 3.0),
    ("salt", "500g", 55, 0.70, 3.0),
    ("eggs", "pcs", 15, 0.70, 4.0),
    ("rice", "1kg", 170, 0.70, 3.0),
    ("beans", "1kg", 190, 0.70, 3.0),
    ("tea", "100g", 130, 0.70, 2.0),
]
# Restock rhythm in days: perishables and staples often, float weekly.
RESTOCK_EVERY = {
    "sugar": 2,
    "flour": 2,
    "cooking oil": 2,
    "bread": 2,
    "milk": 2,
    "airtime": 7,
    "soap": 3,
    "salt": 3,
    "eggs": 3,
    "rice": 3,
    "beans": 3,
    "tea": 3,
}
SLOW_DAY_OFFSETS = {11, 32, 46}
MID_DIP_OFFSETS = range(20, 40)
AIRTIME_DENOMINATIONS = [50, 100, 200]

# (given day offset, customer, amount KES, item, repay after days, repaid KES;
#  repaid 0 means still outstanding at the end of the window)
CREDIT_PLAN = [
    (3, "Mary", 900, "sugar", 11, 900),
    (6, "John", 1400, "flour", 16, 1400),
    (10, "Wanjiku", 700, "cooking oil", 9, 700),
    (13, "Otieno", 1800, "sugar", 20, 1800),
    (17, "Kamau", 1100, "rice", 13, 1100),
    (21, "Achieng", 1500, "flour", 17, 1500),
    (25, "Mary", 800, "bread", 8, 800),
    (29, "John", 2200, "cooking oil", 12, 1000),
    (34, "Wanjiku", 1300, "sugar", 15, 1300),
    (39, "Otieno", 2600, "flour", 35, 0),
    (44, "Kamau", 1600, "beans", 12, 1600),
    (49, "Achieng", 1200, "milk", 16, 0),
    (54, "Mary", 1900, "cooking oil", 13, 0),
    (57, "John", 2400, "sugar", 10, 0),
]


def _iso(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NAIROBI).isoformat()


def _get_shop(conn):
    row = conn.execute("SELECT * FROM shops WHERE name = ?", (SHOP_NAME,)).fetchone()
    if row is not None:
        return row["id"]
    now = _iso(date.today(), 8)
    try:
        conn.execute(
            "INSERT INTO shops (id, name, location, created_at) VALUES (?, ?, ?, ?)",
            (DEMO_SHOP_ID, SHOP_NAME, SHOP_LOCATION, now),
        )
        return DEMO_SHOP_ID
    except sqlite3.IntegrityError:
        # Id already taken by another shop: fall back to autoincrement.
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            (SHOP_NAME, SHOP_LOCATION, now),
        )
        return cur.lastrowid


def _demo_row_count(conn, shop_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE shop_id = ?", (shop_id,)
    ).fetchone()
    return row["n"]


def _clear_demo_data(conn, shop_id):
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM transactions WHERE shop_id = ?", (shop_id,)
        ).fetchall()
    ]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            "DELETE FROM audit_log WHERE entity = 'transaction'"
            f" AND entity_id IN ({placeholders})",
            ids,
        )
        conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)


def _generate_rows(shop_id, start):
    """Build every demo transaction row. Deterministic for a fixed start date."""
    rng = random.Random(SEED)
    rows = []
    end = start + timedelta(days=DAYS - 1)

    def add(day, hour, minute, kind, item, quantity, unit, amount, counterparty=None):
        occurred = _iso(day, hour, minute)
        if kind == "sale":
            raw = f"sold {quantity:g} {unit} {item} for {amount} bob"
        elif kind == "purchase":
            raw = f"restocked {quantity:g} {unit} {item} for {amount} bob"
        elif kind == "expense":
            raw = f"paid {amount} bob: {item}"
        elif kind == "credit_given":
            raw = f"{counterparty} took {quantity:g} {unit} {item} on credit ({amount} bob)"
        else:
            raw = f"{counterparty} repaid {amount} bob"
        rows.append(
            (
                shop_id,
                kind,
                item,
                quantity,
                unit,
                int(amount),
                counterparty,
                raw,
                SOURCE,
                1,
                occurred,
                occurred,
            )
        )

    names = [p[0] for p in PRODUCTS]
    weights = [p[4] for p in PRODUCTS]
    retail = {p[0]: p[2] for p in PRODUCTS}
    wholesale_ratio = {p[0]: p[3] for p in PRODUCTS}
    unit_of = {p[0]: p[1] for p in PRODUCTS}
    pending = {name: 0 for name in names}

    def restock(day, item, factor_low=0.95, factor_high=1.05):
        qty = pending[item]
        if qty <= 0:
            return
        qty = max(1, round(qty * rng.uniform(factor_low, factor_high)))
        pending[item] = 0
        amount = max(1, round(qty * retail[item] * wholesale_ratio[item]))
        add(day, rng.randint(9, 11), rng.randint(0, 59), "purchase",
            item, qty, unit_of[item], amount)

    for offset in range(DAYS):
        day = start + timedelta(days=offset)
        last_day = calendar.monthrange(day.year, day.month)[1]
        busy = 1.0
        if day.weekday() >= 5:
            busy *= 1.5
        if day.day >= last_day - 2:
            busy *= 1.4
        if offset in MID_DIP_OFFSETS:
            busy *= 0.85
        if offset in SLOW_DAY_OFFSETS:
            lines = 3
        else:
            lines = max(3, int(rng.randint(14, 24) * busy))
        for _ in range(lines):
            item = rng.choices(names, weights=weights)[0]
            if item == "airtime":
                quantity = rng.choice(AIRTIME_DENOMINATIONS) // retail[item]
                amount = quantity * retail[item]
            else:
                quantity = rng.randint(1, 4)
                amount = quantity * retail[item]
            pending[item] += quantity
            add(
                day, rng.randint(7, 20), rng.randint(0, 59), "sale",
                item, quantity, unit_of[item], amount,
            )
        for item, every in RESTOCK_EVERY.items():
            if offset % every == 0:
                restock(day, item)
        if day.day == 1:
            add(day, 8, 5, "expense", "shop rent", 1, "month", RENT_KES)
        if day.day == 15:
            add(day, 10, 30, "expense", "electricity tokens", 1, "month", ELECTRICITY_KES)
        if day.weekday() == 0:
            add(day, 8, 45, "expense", "transport", 1, "trip", TRANSPORT_KES)
        if day.weekday() == 5:
            add(day, 9, 15, "expense", "packaging and misc", 1, "week", PACKAGING_KES)
        if offset == 10:
            add(day, 9, 30, "expense", "trading licence", 1, "year", LICENCE_KES)

    for item in names:
        if pending[item] > 0:
            qty = pending[item]
            amount = max(1, round(qty * retail[item] * wholesale_ratio[item]))
            add(end, 10, 0, "purchase", item, qty, unit_of[item], amount)

    for given_offset, name, amount, item, repay_in_days, repaid in CREDIT_PLAN:
        given_day = start + timedelta(days=given_offset)
        quantity = max(1, round(amount / retail[item]))
        add(
            given_day, 18, rng.randint(0, 59), "credit_given",
            item, quantity, unit_of[item], amount, name,
        )
        if repaid > 0:
            repay_day = given_day + timedelta(days=repay_in_days)
            if repay_day <= end:
                add(
                    repay_day, 18, rng.randint(0, 59), "credit_received",
                    item, 1, unit_of[item], repaid, name,
                )
    return rows


def _summarize(conn, shop_id):
    totals = {}
    for kind in ("sale", "purchase", "expense", "credit_given", "credit_received"):
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_kes), 0) AS total FROM transactions"
            " WHERE shop_id = ? AND type = ?",
            (shop_id, kind),
        ).fetchone()
        totals[kind] = row["total"]
    outstanding_rows = conn.execute(
        "SELECT counterparty, COALESCE(SUM(CASE WHEN type = 'credit_given'"
        " THEN amount_kes ELSE -amount_kes END), 0) AS balance"
        " FROM transactions WHERE shop_id = ? AND type IN ('credit_given', 'credit_received')"
        " GROUP BY counterparty HAVING balance > 0",
        (shop_id,),
    ).fetchall()
    count = _demo_row_count(conn, shop_id)
    days = conn.execute(
        "SELECT COUNT(DISTINCT substr(occurred_at, 1, 10)) AS n"
        " FROM transactions WHERE shop_id = ?",
        (shop_id,),
    ).fetchone()["n"]
    sales = totals["sale"]
    gross_margin = round((sales - totals["purchase"]) / sales * 100, 1) if sales else 0.0
    net_margin = (
        round((sales - totals["purchase"] - totals["expense"]) / sales * 100, 1)
        if sales
        else 0.0
    )
    return {
        "shop_id": shop_id,
        "transactions": count,
        "days": days,
        "sales": totals["sale"],
        "purchases": totals["purchase"],
        "expenses": totals["expense"],
        "outstanding": totals["credit_given"] - totals["credit_received"],
        "customers_outstanding": len(outstanding_rows),
        "gross_margin": gross_margin,
        "net_margin": net_margin,
    }


def _print_summary(summary, shop_id, fresh):
    state = "Seeded" if fresh else "Already seeded (use --reset to regenerate)"
    print(f"{state}: {SHOP_NAME} (id {shop_id})")
    print(f"Transactions: {summary['transactions']} across {summary['days']} days")
    print(f"Total sales: KES {summary['sales']:,}")
    print(f"Total purchases: KES {summary['purchases']:,}")
    print(f"Total expenses: KES {summary['expenses']:,}")
    print(
        f"Outstanding credit: KES {summary['outstanding']:,}"
        f" across {summary['customers_outstanding']} customers"
    )
    print(f"Gross margin: {summary['gross_margin']}%")
    print(f"Net margin: {summary['net_margin']}%")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed demo data for the credit-score demo.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete only the demo shop's data before seeding.",
    )
    args = parser.parse_args(argv)
    db.init_db()
    with db.get_connection() as conn:
        shop_id = _get_shop(conn)
        if _demo_row_count(conn, shop_id) and not args.reset:
            summary = _summarize(conn, shop_id)
            _print_summary(summary, shop_id, fresh=False)
            return summary
        _clear_demo_data(conn, shop_id)
        start = date.today() - timedelta(days=DAYS - 1)
        conn.executemany(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _generate_rows(shop_id, start),
        )
        summary = _summarize(conn, shop_id)
        _print_summary(summary, shop_id, fresh=True)
        return summary


if __name__ == "__main__":
    main()
