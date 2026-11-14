"""Tests for the consent-based Business Passport feature.

Route functions are called directly (no TestClient: it needs sockets),
matching the style of the existing backend tests. The clock is controlled
by monkeypatching app._utcnow; the share-token rate limiter is reset
before every test via app._rate_limit_hits. The public endpoint takes a
bare request object, so a stub carrying client.host is enough.
"""
import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app as appmod
import db
import helpers
from services import credit as creditmod
from services.passport import DEMO_NOTE

LENDER = "Equity Bank"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "passport.db")
    db.init_db()
    appmod._rate_limit_hits.clear()
    appmod._api_rate_limit_hits.clear()


def make_owner(monkeypatch, phone="0711111111"):
    return helpers.register_account(
        monkeypatch, phone=phone, pin="482917",
        shop_name=creditmod.DEMO_SHOP_NAME, ip="10.20.0.1",
    )


def authed_get(owner, path):
    return helpers.authed(path, owner)


def authed_post(owner, path):
    return helpers.authed(path, owner, method="POST")


def fake_request(host="testclient"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def seed_shop(shop_id, with_credit_names=False):
    """40 days x 3 confirmed txns (scores) for the owner's shop, dated to today."""
    today = date.today()
    with db.get_connection() as conn:
        rows = []
        for offset in range(40):
            day = (today - timedelta(days=offset)).isoformat()
            rows += [
                (shop_id, "sale", "sugar", 200, None, f"sale-{offset}-a", day),
                (shop_id, "sale", "bread", 100, None, f"sale-{offset}-b", day),
                (shop_id, "purchase", "stock", 150, None, f"buy-{offset}", day),
            ]
        if with_credit_names:
            day = (today - timedelta(days=5)).isoformat()
            rows += [
                (
                    shop_id,
                    "credit_given",
                    "sugar",
                    1000,
                    "Alice Mwangi Testname",
                    "SECRET-RAW-marker-advance",
                    day,
                ),
                (
                    shop_id,
                    "credit_received",
                    "repayment",
                    1000,
                    "Alice Mwangi Testname",
                    "SECRET-RAW-marker-receipt",
                    day,
                ),
            ]
        conn.executemany(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, 1, 'pcs', ?, ?, ?, 'api', 1, ?, ?)""",
            [(s, t, i, a, c, r, d, d) for (s, t, i, a, c, r, d) in rows],
        )
    return shop_id


def create_passport(owner, **overrides):
    body = {"lender_name": LENDER, "expires_in_days": 7}
    body.update(overrides)
    return appmod.create_passport(
        appmod.PassportCreateIn(**body), authed_post(owner, "/api/passports")
    )


def test_create_returns_token_that_works(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)

    assert created["token"]
    assert created["share_path"] == f"/p/{created['token']}"
    assert created["expires_at"]
    assert created["id"]

    snapshot = appmod.view_passport(created["token"], fake_request())
    assert snapshot["shop_name"] == creditmod.DEMO_SHOP_NAME
    assert snapshot["generated_at"]
    assert snapshot["is_demo_shop"] is True
    assert snapshot["demo_note"] == DEMO_NOTE
    assert snapshot["disclaimer"] == creditmod.DISCLAIMER
    assert {"score", "band", "factors"} <= set(snapshot["score"])
    assert {
        "safe_loan_kes",
        "monthly_repayment_kes",
        "term_months",
        "loan_reasoning",
    } <= set(snapshot["loan"])
    assert {
        "total_sales_kes",
        "total_purchases_kes",
        "total_expenses_kes",
        "net_margin_pct",
        "outstanding_credit_kes",
        "weekly_sales_kes",
    } <= set(snapshot["performance"])
    assert len(snapshot["performance"]["weekly_sales_kes"]) == 8


def test_db_stores_only_hash(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)
    expected_hash = hashlib.sha256(created["token"].encode()).hexdigest()

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM passports WHERE id = ?", (created["id"],)
        ).fetchone()

    assert row["token_hash"] == expected_hash
    assert row["token_hash"] != created["token"]
    assert created["token"] not in row["snapshot_json"]
    assert created["token"] not in json.dumps(dict(row))


def test_snapshot_has_no_customer_names_or_rows(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"], with_credit_names=True)
    created = create_passport(owner)

    snapshot = appmod.view_passport(created["token"], fake_request())
    dumped = json.dumps(snapshot)

    assert "Alice Mwangi Testname" not in dumped
    assert "SECRET-RAW-marker" not in dumped
    assert "raw_text" not in dumped
    assert "counterparty" not in dumped
    assert "transactions" not in dumped


def test_unchosen_sections_are_omitted(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner, sections=["score"])

    snapshot = appmod.view_passport(created["token"], fake_request())

    assert "score" in snapshot
    assert "loan" not in snapshot
    assert "performance" not in snapshot
    assert snapshot["shop_name"] == creditmod.DEMO_SHOP_NAME
    assert snapshot["disclaimer"] == creditmod.DISCLAIMER


def test_revoked_returns_410_immediately(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)

    revoked = appmod.revoke_passport(
        created["id"], authed_post(owner, "/revoke"))
    assert revoked["status"] == "revoked"

    with pytest.raises(HTTPException) as exc_info:
        appmod.view_passport(created["token"], fake_request())
    assert exc_info.value.status_code == 410

    again = appmod.revoke_passport(
        created["id"], authed_post(owner, "/revoke"))
    assert again["revoked_at"] == revoked["revoked_at"]


def test_expired_returns_410(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)
    future = appmod._utcnow() + timedelta(days=31)
    monkeypatch.setattr(appmod, "_utcnow", lambda: future)

    with pytest.raises(HTTPException) as exc_info:
        appmod.view_passport(created["token"], fake_request())
    assert exc_info.value.status_code == 410

    listed = appmod.list_passports(authed_get(owner, "/api/passports"))
    assert listed[0]["status"] == "expired"


def test_unknown_token_returns_404(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    with pytest.raises(HTTPException) as exc_info:
        appmod.view_passport("no-such-token", fake_request())
    assert exc_info.value.status_code == 404


def test_views_are_counted(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)

    listed = appmod.list_passports(authed_get(owner, "/api/passports"))
    assert listed[0]["view_count"] == 0
    assert listed[0]["last_viewed_at"] is None

    appmod.view_passport(created["token"], fake_request())
    appmod.view_passport(created["token"], fake_request())

    listed = appmod.list_passports(authed_get(owner, "/api/passports"))
    assert listed[0]["view_count"] == 2
    assert listed[0]["last_viewed_at"] is not None
    assert listed[0]["status"] == "active"

    activity = appmod.passport_activity(
        created["id"], authed_get(owner, "/activity"))
    assert activity["passport_id"] == created["id"]
    assert len(activity["views"]) == 2


def test_rate_limit_triggers(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)
    request = fake_request()

    for _ in range(30):
        appmod.view_passport(created["token"], request)
    with pytest.raises(HTTPException) as exc_info:
        appmod.view_passport(created["token"], request)
    assert exc_info.value.status_code == 429


def test_audit_rows_exist_and_contain_no_token(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner)
    appmod.view_passport(created["token"], fake_request())
    appmod.revoke_passport(
        created["id"], authed_post(owner, "/revoke"))

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT action, entity, entity_id, detail FROM audit_log"
            " WHERE entity = 'passport' AND entity_id = ? ORDER BY id",
            (created["id"],),
        ).fetchall()

    actions = [row["action"] for row in rows]
    assert actions == ["passport_created", "passport_viewed", "passport_revoked"]
    assert all(row["entity_id"] == created["id"] for row in rows)
    for row in rows:
        assert created["token"] not in (row["detail"] or "")


def test_list_never_returns_tokens_or_hashes(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    create_passport(owner)

    listed = appmod.list_passports(authed_get(owner, "/api/passports"))

    assert len(listed) == 1
    assert "token" not in listed[0]
    assert "token_hash" not in listed[0]
    assert "token" not in json.dumps(listed)


@pytest.mark.parametrize(
    "body",
    [
        {"lender_name": "   ", "expires_in_days": 7},
        {"lender_name": "x" * 61, "expires_in_days": 7},
        {"lender_name": LENDER, "expires_in_days": 10},
        {"lender_name": LENDER, "expires_in_days": 30, "sections": ["everything"]},
    ],
)
def test_create_rejects_bad_input(isolated_db, monkeypatch, body):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_passport(
            appmod.PassportCreateIn(**body),
            authed_post(owner, "/api/passports"),
        )
    assert exc_info.value.status_code == 422


def test_create_trims_name_and_defaults_to_full_scope(isolated_db, monkeypatch):
    owner = make_owner(monkeypatch)
    seed_shop(owner["shop_id"])
    created = create_passport(owner, lender_name="  KCB  ", expires_in_days=30)

    listed = appmod.list_passports(authed_get(owner, "/api/passports"))
    assert created["id"] == listed[0]["id"]
    assert listed[0]["lender_name"] == "KCB"
    assert listed[0]["scope"] == ["score", "loan", "performance"]


def test_passport_has_no_llm_or_network_imports():
    from services import passport as passportmod

    imported = set()
    for line in open(passportmod.__file__).read().splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imported.add(stripped.split()[1].split(".")[0])
        elif stripped.startswith("from "):
            imported.add(stripped.split()[1].split(".")[0])
    assert imported <= {"hashlib", "datetime", "services"}
