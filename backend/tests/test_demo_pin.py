"""Tests for the demo-shop as_of pin (services/credit.resolve_as_of).

The synthetic demo shop is frozen in time: its score, loan, tips, weekly
dashboard and new passport snapshots are pinned to its latest confirmed
transaction, so moving the real clock changes nothing. Other shops keep
following the real date. The clock moves via app._utcnow.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import app as appmod
import db
from services import credit as creditmod

FROZEN_END_OFFSET = 20


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pin.db")
    db.init_db()
    appmod._rate_limit_hits.clear()


def seed_shop(name, end_offset, days=40):
    """Shop with `days` x 3 confirmed txns ending `end_offset` days ago."""
    today = date.today()
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            (name, "test", today.isoformat()),
        )
        shop_id = cur.lastrowid
        rows = []
        for offset in range(end_offset, end_offset + days):
            day = (today - timedelta(days=offset)).isoformat()
            rows += [
                (shop_id, "sale", "sugar", 200, day),
                (shop_id, "sale", "bread", 100, day),
                (shop_id, "purchase", "stock", 150, day),
            ]
        conn.executemany(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, 1, 'pcs', ?, NULL, 'test',
                       'api', 1, ?, ?)""",
            [(s, t, i, a, d, d) for (s, t, i, a, d) in rows],
        )
    latest = today - timedelta(days=end_offset)
    return shop_id, latest


def move_clock(monkeypatch, days):
    real_now = appmod._utcnow()
    monkeypatch.setattr(
        appmod, "_utcnow", lambda: real_now + timedelta(days=days)
    )


def fake_request():
    return SimpleNamespace(client=SimpleNamespace(host="testclient"))


def ledger_for(shop_id):
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (shop_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def tx(day, confirmed=1):
    return {
        "type": "sale",
        "amount_kes": 100,
        "confirmed": confirmed,
        "occurred_at": day.isoformat(),
    }


def test_resolve_as_of_pins_demo_to_latest_confirmed():
    today = date.today()
    latest = today - timedelta(days=20)
    txns = [
        tx(today - timedelta(days=59)),
        tx(latest),
        tx(today, confirmed=0),  # drafts never move the pin
    ]
    assert (
        creditmod.resolve_as_of(creditmod.DEMO_SHOP_NAME, txns, today) == latest
    )


def test_resolve_as_of_leaves_other_shops_alone():
    today = date.today()
    txns = [tx(today - timedelta(days=20))]
    assert creditmod.resolve_as_of("Amina's Duka", txns, today) == today
    assert creditmod.resolve_as_of(creditmod.DEMO_SHOP_NAME, [], today) == today


def test_demo_score_identical_when_clock_moves(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    seed_shop(creditmod.DEMO_SHOP_NAME, FROZEN_END_OFFSET)
    pinned = (date.today() - timedelta(days=FROZEN_END_OFFSET)).isoformat()

    before = appmod.credit_summary()
    assert before["as_of"] == pinned
    assert before["status"] == "scored"
    weeks_before = appmod.dashboard_weekly()
    assert weeks_before["as_of"] == pinned

    move_clock(monkeypatch, 10)

    after = appmod.credit_summary()
    assert after == before
    assert appmod.dashboard_weekly() == weeks_before


def test_non_demo_window_follows_real_date(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    shop_id, _ = seed_shop("Amina's Duka", 0)

    before = appmod.credit_summary(shop_id=shop_id)
    assert before["as_of"] == date.today().isoformat()

    move_clock(monkeypatch, 10)
    moved = appmod._utcnow().date()

    after = appmod.credit_summary(shop_id=shop_id)
    assert after["as_of"] == moved.isoformat()
    expected = creditmod.assess_shop(ledger_for(shop_id), moved)
    assert after["score"] == expected["score"]
    assert after["factors"] == expected["factors"]


def test_explicit_as_of_still_wins_for_demo(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    shop_id, latest = seed_shop(creditmod.DEMO_SHOP_NAME, FROZEN_END_OFFSET)
    custom = (latest - timedelta(days=10)).isoformat()

    result = appmod.credit_summary(shop_id=shop_id, as_of=custom)

    assert result["as_of"] == custom


def test_explain_uses_pinned_date(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    shop_id, latest = seed_shop(creditmod.DEMO_SHOP_NAME, FROZEN_END_OFFSET)
    seen = {}

    def fake_explain(summary):
        seen["summary"] = summary
        return {"text": "steady shop", "source": "template"}

    monkeypatch.setattr(appmod, "explain_credit", fake_explain)

    result = appmod.credit_explain()

    assert result["as_of"] == latest.isoformat()
    assert seen["summary"] == creditmod.assess_shop(ledger_for(shop_id), latest)


def test_passport_snapshot_uses_pinned_date(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    seed_shop(creditmod.DEMO_SHOP_NAME, FROZEN_END_OFFSET)
    pinned = (date.today() - timedelta(days=FROZEN_END_OFFSET)).isoformat()

    def snapshot_of(lender):
        created = appmod.create_passport(
            appmod.PassportCreateIn(lender_name=lender, expires_in_days=7)
        )
        return appmod.view_passport(created["token"], fake_request())

    first = snapshot_of("Equity Bank")
    assert first["as_of"] == pinned
    assert first["is_demo_shop"] is True

    move_clock(monkeypatch, 10)
    second = snapshot_of("KCB")

    assert second["as_of"] == pinned
    assert second["score"] == first["score"]
    assert second["loan"] == first["loan"]
    assert second["performance"] == first["performance"]
