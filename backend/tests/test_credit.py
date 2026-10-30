"""Tests for services/credit.py. Every expectation is hand-computed."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

import app as appmod
import db
from services import credit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import seed_demo

AS_OF = date(2026, 3, 31)


def tx(offset, kind="sale", amount=100, item="sugar", unit="1kg", cp=None, confirmed=1):
    """One transaction dict, `offset` days before AS_OF."""
    day = AS_OF - timedelta(days=offset)
    return {
        "shop_id": 1,
        "type": kind,
        "item": item,
        "quantity": 1,
        "unit": unit,
        "amount_kes": amount,
        "counterparty": cp,
        "raw_text": "test",
        "source": "api",
        "confirmed": confirmed,
        "occurred_at": day.isoformat(),
        "created_at": day.isoformat(),
    }


def test_factor_dict_shape():
    factor = credit.factor_regularity([tx(0)], AS_OF)
    assert set(factor) == {"name", "points", "max_points", "value", "explanation"}


def test_regularity_45_of_60_days():
    ledger = [tx(offset, amount=100) for offset in range(45)]
    factor = credit.factor_regularity(ledger, AS_OF)
    # 45 / 60 = 0.75 -> 0.75 * 20 = 15
    assert (factor["points"], factor["max_points"]) == (15, 20)
    assert factor["value"] == 0.75


def test_stability_constant_weeks():
    ledger = [tx(offset, amount=100) for offset in range(56)]
    factor = credit.factor_stability(ledger, AS_OF)
    # Every 7-day week sums to 700, so CV = 0.
    assert (factor["points"], factor["max_points"]) == (20, 20)
    assert factor["value"] == 0.0


def test_stability_alternating_weeks():
    ledger = []
    for week in range(8):
        daily_sales = 6 if week % 2 == 0 else 4
        for day in range(daily_sales):
            ledger.append(tx(week * 7 + day, amount=100))
    factor = credit.factor_stability(ledger, AS_OF)
    # Weeks alternate 600, 400: mean 500, sd 100, CV = 0.2 -> 20 * 0.8 = 16.
    assert (factor["points"], factor["max_points"]) == (16, 20)
    assert factor["value"] == 0.2


def test_profitability_full_marks():
    ledger = [tx(0, "sale", 10000), tx(0, "purchase", 7000), tx(0, "expense", 1000)]
    factor = credit.factor_profitability(ledger, AS_OF)
    # Margin (10000 - 7000 - 1000) / 10000 = 0.20 -> full marks.
    assert (factor["points"], factor["max_points"]) == (25, 25)
    assert factor["value"] == 0.2


def test_profitability_partial():
    ledger = [tx(0, "sale", 10000), tx(0, "purchase", 7800), tx(0, "expense", 1000)]
    factor = credit.factor_profitability(ledger, AS_OF)
    # Margin 0.12 is 0.6 of the way to 0.20 -> 25 * 0.6 = 15.
    assert (factor["points"], factor["max_points"]) == (15, 25)
    assert factor["value"] == 0.12


def test_profitability_no_sales():
    factor = credit.factor_profitability([tx(0, "purchase", 500)], AS_OF)
    assert factor["points"] == 0


def test_repayment_two_of_three():
    ledger = [
        tx(40, "credit_given", 1000, cp="Ann"),
        tx(30, "credit_received", 1000, cp="Ann"),  # 10 days later: counts
        tx(40, "credit_given", 1000, cp="Beatrice"),
        tx(10, "credit_received", 1000, cp="Beatrice"),  # 30 days later: too late
        tx(5, "credit_given", 1000, cp="Carol"),  # never repaid
        tx(0, "sale", 20000),  # monthly sales; outstanding ratio stays low
    ]
    factor = credit.factor_repayment(ledger, AS_OF)
    # Repaid 1000 of 3000 -> 25 / 3 = 8.33 -> 8. Outstanding 1000
    # against monthly sales of 20000 is below the 0.5 penalty line.
    assert (factor["points"], factor["max_points"]) == (8, 25)
    assert factor["value"] == 0.3333


def test_repayment_high_outstanding_penalty():
    ledger = [
        tx(40, "credit_given", 30000, cp="Dan"),
        tx(30, "credit_given", 10000, cp="Eve"),
        tx(20, "credit_received", 10000, cp="Eve"),
        tx(10, "credit_given", 10000, cp="Fay"),
        tx(5, "credit_received", 10000, cp="Fay"),
        tx(0, "sale", 20000),
    ]
    factor = credit.factor_repayment(ledger, AS_OF)
    # Repaid 20000 of 50000 = 0.4 -> 10 points, but outstanding 30000
    # against monthly sales of 20000 (ratio 1.5) costs 10 -> 0.
    assert (factor["points"], factor["max_points"]) == (0, 25)
    assert factor["value"] == 0.4


def test_repayment_no_credit_is_full_marks():
    factor = credit.factor_repayment([tx(0, "sale", 500)], AS_OF)
    assert (factor["points"], factor["max_points"]) == (25, 25)


def test_trend_growth():
    ledger = [tx(40, "sale", 10000), tx(5, "sale", 11000)]
    factor = credit.factor_trend(ledger, AS_OF)
    # Growth 0.10 is half of 0.20 -> 10 * 0.5 = 5.
    assert (factor["points"], factor["max_points"]) == (5, 10)
    assert factor["value"] == 0.1


def test_trend_decline_scores_zero():
    ledger = [tx(40, "sale", 10000), tx(5, "sale", 8000)]
    assert credit.factor_trend(ledger, AS_OF)["points"] == 0


def test_insufficient_days():
    ledger = [tx(offset // 6, amount=50) for offset in range(120)]
    result = credit.assess_shop(ledger, AS_OF)
    assert result["status"] == "insufficient_data"
    assert result["score"] is None
    assert result["band"] is None
    assert result["factors"] == []
    assert result["safe_loan_kes"] == 0
    assert "20 days" in result["explanation"]


def test_insufficient_count():
    ledger = [tx(offset, amount=50) for offset in range(60)]
    result = credit.assess_shop(ledger, AS_OF)
    assert result["status"] == "insufficient_data"
    assert "60 confirmed transactions" in result["explanation"]


def test_loss_making_shop_gets_no_loan():
    ledger = [tx(offset // 3, "sale", 100) for offset in range(120)]
    ledger += [tx(offset, "purchase", 400) for offset in range(40)]
    result = credit.assess_shop(ledger, AS_OF)
    assert result["status"] == "ready"
    assert result["safe_loan_kes"] == 0
    assert result["monthly_repayment_kes"] == 0


def test_safe_loan_math_and_rounding():
    # Lower of 60-day (10000/mo) and 30-day (24000/mo): 10000.
    # 30% -> 3000/mo over 6 months = 18000, x0.8 = 14400 -> 14000.
    assert credit.safe_loan_for_band("Ready", 20000, 24000) == (14000, 2333)
    # 18000 x0.5 = 9000 exact; 9000 // 6 = 1500.
    assert credit.safe_loan_for_band("Developing", 20000, 20000) == (9000, 1500)
    # Lower is the 30-day figure: 6000. 6000 x0.3 x6 = 10800 -> 10500.
    assert credit.safe_loan_for_band("Strong", 12000, 6000) == (10500, 1750)
    assert credit.safe_loan_for_band("Building", 50000, 50000) == (0, 0)
    assert credit.safe_loan_for_band("Ready", -5000, 10000) == (0, 0)


def test_unconfirmed_transactions_ignored():
    base = [tx(offset // 3, amount=100) for offset in range(120)]
    with_noise = base + [
        tx(offset, amount=100000, confirmed=0) for offset in range(200)
    ]
    assert credit.assess_shop(with_noise, AS_OF) == credit.assess_shop(base, AS_OF)


def test_bands():
    assert [credit.band_for_score(s) for s in (0, 39, 40, 59, 60, 79, 80, 100)] == [
        "Building",
        "Building",
        "Developing",
        "Developing",
        "Ready",
        "Ready",
        "Strong",
        "Strong",
    ]


def test_no_llm_or_network_imports():
    imported = set()
    for line in Path(credit.__file__).read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imported.add(stripped.split()[1].split(".")[0])
        elif stripped.startswith("from "):
            imported.add(stripped.split()[1].split(".")[0])
    assert imported <= {"statistics", "datetime"}
    assert imported == {"statistics", "datetime"}


def test_demo_shop_name_matches_seeder():
    assert credit.DEMO_SHOP_NAME == seed_demo.SHOP_NAME


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "credit.db")
    db.init_db()


def insert_shop(name):
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            (name, "test", AS_OF.isoformat()),
        )
        return cur.lastrowid


def insert_tx(shop_id, offset, kind="sale", amount=100, cp=None, confirmed=1):
    day = AS_OF - timedelta(days=offset)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, 'sugar', 1, '1kg', ?, ?, 'test', 'api', ?, ?, ?)""",
            (
                shop_id,
                kind,
                amount,
                cp,
                confirmed,
                day.isoformat(),
                day.isoformat(),
            ),
        )


def test_endpoint_insufficient_data(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    shop_id = insert_shop("Tiny Duka")
    for offset in range(10):
        insert_tx(shop_id, offset)

    result = appmod.credit_summary(shop_id=shop_id, as_of=AS_OF.isoformat())

    assert result["status"] == "insufficient_data"
    assert result["shop_id"] == shop_id
    assert result["disclaimer"] == credit.DISCLAIMER
    assert result["safe_loan_kes"] == 0


def test_endpoint_ready_shape(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    shop_id = insert_shop(credit.DEMO_SHOP_NAME)
    for offset in range(40):
        insert_tx(shop_id, offset, amount=200)
        insert_tx(shop_id, offset, amount=100)
        insert_tx(shop_id, offset, "purchase", 150)

    result = appmod.credit_summary(as_of=AS_OF.isoformat())

    assert result["status"] == "ready"
    assert result["shop_id"] == shop_id
    assert result["band"] in ("Building", "Developing", "Ready", "Strong")
    assert len(result["factors"]) == 5
    assert result["term_months"] == 6
    assert result["tips"]
    assert result["disclaimer"] == credit.DISCLAIMER


def test_endpoint_bad_date_and_missing_shop(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_summary(shop_id=1, as_of="not-a-date")
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_summary(shop_id=999)
    assert exc_info.value.status_code == 404
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_summary()
    assert exc_info.value.status_code == 404
