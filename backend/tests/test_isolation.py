"""Per-shop isolation tests (plan step 4).

Every shop-scoped endpoint is exercised with two shops: shop A must not
read, modify, list, confirm, or revoke anything of shop B, and B's ids
give A a 404 indistinguishable from a missing id. Also covers anonymous
and expired sessions, the demo read path, the per-shop AI quota, the
unchanged lender flow, and the route inventory guard.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from itsdangerous import URLSafeTimedSerializer

import app as appmod
import db
import helpers
from services import auth as authmod

PARSED = {
    "type": "sale",
    "item": "sugar",
    "quantity": 5,
    "unit": "kilos",
    "amount_kes": 800,
    "counterparty": None,
}


@pytest.fixture
def shops(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "iso.db")
    db.init_db()
    a = helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Amina's Duka", ip="10.0.0.1",
    )
    b = helpers.register_account(
        monkeypatch, phone="0722222222", pin="739536",
        shop_name="Brian's Duka", ip="10.0.0.2",
    )
    return a, b


def seed_confirmed(shop_id, day="2026-03-01", item="sugar", amount=800):
    with db.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', ?, 1, 'pcs', ?, NULL, 'test sale',
                       'api', 1, ?, ?)""",
            (shop_id, item, amount, day, day),
        )
        return cur.lastrowid


def seed_demo(day="2026-03-01"):
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shops (name, location, is_demo, created_at)"
            " VALUES (?, ?, 1, ?)",
            ("Demo Duka (synthetic data)", "Nairobi (demo)", day),
        )
        demo_id = cur.lastrowid
    seed_confirmed(demo_id, day=day)
    return demo_id


def stub_parse(monkeypatch):
    monkeypatch.setattr(appmod, "parse_entry", lambda text: dict(PARSED))


# Entry and ledger isolation


def test_entry_and_ledger_are_per_shop(shops, monkeypatch):
    stub_parse(monkeypatch)
    a, b = shops

    created = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", a, method="POST"),
    )
    assert created["shop_id"] == a["shop_id"]

    b_entry = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", b, method="POST"),
    )
    assert b_entry["shop_id"] == b["shop_id"]

    appmod.confirm_transaction(
        appmod.ConfirmIn(id=created["id"]),
        helpers.authed("/api/transactions/confirm", a, method="POST"),
    )
    appmod.confirm_transaction(
        appmod.ConfirmIn(id=b_entry["id"]),
        helpers.authed("/api/transactions/confirm", b, method="POST"),
    )

    ledger_a = appmod.read_ledger(helpers.authed("/api/ledger", a))
    ledger_b = appmod.read_ledger(helpers.authed("/api/ledger", b))
    assert [e["id"] for e in ledger_a] == [created["id"]]
    assert [e["id"] for e in ledger_b] == [b_entry["id"]]


def test_null_shop_rows_unreachable(shops, monkeypatch):
    stub_parse(monkeypatch)
    a, _ = shops
    null_id = seed_confirmed(None)

    ledger = appmod.read_ledger(helpers.authed("/api/ledger", a))
    assert null_id not in [e["id"] for e in ledger]

    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(
            appmod.ConfirmIn(id=null_id),
            helpers.authed("/api/transactions/confirm", a, method="POST"),
        )
    assert exc_info.value.status_code == 404


def test_anonymous_writes_rejected(shops, monkeypatch):
    stub_parse(monkeypatch)
    anon_post = helpers.make_request("/api/entry", method="POST")
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_entry(appmod.EntryIn(text="sold sugar"), anon_post)
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(
            appmod.ConfirmIn(id=1), helpers.make_request(
                "/api/transactions/confirm", method="POST")
        )
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        appmod.read_ledger(helpers.make_request("/api/ledger"))
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        appmod.list_passports(helpers.make_request("/api/passports"))
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException) as exc_info:
        appmod.create_passport(
            appmod.PassportCreateIn(lender_name="Equity", expires_in_days=7),
            helpers.make_request("/api/passports", method="POST"),
        )
    assert exc_info.value.status_code == 401


def test_expired_session_rejected_on_scoped_endpoint(shops):
    a, _ = shops
    past = datetime.now(timezone.utc) - timedelta(days=31)
    sid = "expired-isolation-sid"
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions
               (token_hash, user_id, shop_id, created_at, expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, NULL)""",
            (
                hashlib.sha256(sid.encode()).hexdigest(),
                a["user_id"],
                a["shop_id"],
                past.isoformat(),
                past.isoformat(),
            ),
        )
    cookie = URLSafeTimedSerializer(helpers.SECRET).dumps(sid)
    req = helpers.make_request("/api/ledger", cookie=f"duka_session={cookie}")
    with pytest.raises(HTTPException) as exc_info:
        appmod.read_ledger(req)
    assert exc_info.value.status_code == 401


# Confirm isolation


def test_confirm_other_shop_id_is_404(shops, monkeypatch):
    stub_parse(monkeypatch)
    a, b = shops
    draft = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", b, method="POST"),
    )
    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(
            appmod.ConfirmIn(id=draft["id"]),
            helpers.authed("/api/transactions/confirm", a, method="POST"),
        )
    assert exc_info.value.status_code == 404

    out = appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"]),
        helpers.authed("/api/transactions/confirm", b, method="POST"),
    )
    assert out["confirmed"] == 1


# Credit trio: own shop, demo, view=demo


def test_credit_trio_uses_own_shop_when_logged_in(shops):
    a, b = shops
    seed_confirmed(a["shop_id"], item="sugar")
    seed_confirmed(b["shop_id"], item="bread")

    summary = appmod.credit_summary(
        request=helpers.authed("/api/credit/summary", a))
    assert summary["shop_id"] == a["shop_id"]
    assert summary["status"] == "insufficient_data"

    weeks = appmod.dashboard_weekly(
        request=helpers.authed("/api/dashboard/weekly", b))
    assert weeks["shop_id"] == b["shop_id"]

    explained = appmod.credit_explain(
        request=helpers.authed("/api/credit/explain", a))
    assert explained["as_of"]


def test_credit_trio_anonymous_serves_demo(shops):
    seed_demo()
    a, _ = shops
    seed_confirmed(a["shop_id"])

    summary = appmod.credit_summary()
    assert summary["shop_name"] == "Demo Duka (synthetic data)"
    weeks = appmod.dashboard_weekly()
    assert weeks["shop_name"] == "Demo Duka (synthetic data)"
    explained = appmod.credit_explain()
    assert explained["as_of"]

    demo_view = appmod.credit_summary(
        view="demo", request=helpers.authed("/api/credit/summary", a))
    assert demo_view["shop_name"] == "Demo Duka (synthetic data)"


def test_credit_trio_anonymous_without_demo_is_404(shops):
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_summary()
    assert exc_info.value.status_code == 404


# Passport isolation and lender flow


def make_passport(account, **overrides):
    body = {"lender_name": "Equity Bank", "expires_in_days": 7}
    body.update(overrides)
    return appmod.create_passport(
        appmod.PassportCreateIn(**body),
        helpers.authed("/api/passports", account, method="POST"),
    )


def test_passport_create_list_isolated(shops):
    a, b = shops
    created = make_passport(a)
    assert created["share_path"] == f"/p/{created['token']}"

    listed_a = appmod.list_passports(helpers.authed("/api/passports", a))
    listed_b = appmod.list_passports(helpers.authed("/api/passports", b))
    assert [p["id"] for p in listed_a] == [created["id"]]
    assert listed_b == []


def test_revoke_and_activity_other_shop_is_404(shops):
    a, b = shops
    created = make_passport(a)

    for call in (
        lambda: appmod.revoke_passport(
            created["id"], helpers.authed("/revoke", b, method="POST")),
        lambda: appmod.passport_activity(
            created["id"], helpers.authed("/activity", b)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 404

    revoked = appmod.revoke_passport(
        created["id"], helpers.authed("/revoke", a, method="POST"))
    assert revoked["status"] == "revoked"


def test_lender_flow_unchanged(shops):
    a, b = shops
    created = make_passport(a)

    snapshot = appmod.view_passport(created["token"], helpers.make_request("/x"))
    assert snapshot["shop_name"] == "Amina's Duka"

    appmod.revoke_passport(
        created["id"], helpers.authed("/revoke", a, method="POST"))
    with pytest.raises(HTTPException) as exc_info:
        appmod.view_passport(created["token"], helpers.make_request("/x"))
    assert exc_info.value.status_code == 410

    listed = appmod.list_passports(helpers.authed("/api/passports", b))
    assert listed == []


def test_audit_rows_carry_shop_id(shops, monkeypatch):
    stub_parse(monkeypatch)
    a, _ = shops
    draft = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", a, method="POST"),
    )
    appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"]),
        helpers.authed("/api/transactions/confirm", a, method="POST"),
    )
    created = make_passport(a)

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT action, entity, entity_id, shop_id FROM audit_log"
            " WHERE entity IN ('transaction', 'passport')"
        ).fetchall()
    assert rows
    by_action = {row["action"]: row for row in rows}
    assert by_action["parse"]["shop_id"] == a["shop_id"]
    assert by_action["confirm"]["shop_id"] == a["shop_id"]
    assert by_action["passport_created"]["shop_id"] == a["shop_id"]
    assert created["id"] == by_action["passport_created"]["entity_id"]


# Per-shop AI quota


def test_per_shop_quota_independent(shops, monkeypatch):
    stub_parse(monkeypatch)
    monkeypatch.setenv("DUKA_SHOP_AI_CAP", "1")
    a, b = shops

    appmod.create_entry(
        appmod.EntryIn(text="one"),
        helpers.authed("/api/entry", a, method="POST"),
    )
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_entry(
            appmod.EntryIn(text="two"),
            helpers.authed("/api/entry", a, method="POST"),
        )
    assert exc_info.value.status_code == 429

    second = appmod.create_entry(
        appmod.EntryIn(text="one"),
        helpers.authed("/api/entry", b, method="POST"),
    )
    assert second["shop_id"] == b["shop_id"]


def test_shop_quota_falls_back_to_template(shops, monkeypatch):
    from datetime import date
    a, _ = shops
    today = date.today()
    with db.get_connection() as conn:
        for offset in range(40):
            day = (today - timedelta(days=offset)).isoformat()
            for kind, amount in (("sale", 200), ("sale", 100), ("purchase", 150)):
                conn.execute(
                    """INSERT INTO transactions
                       (shop_id, type, item, quantity, unit, amount_kes,
                        counterparty, raw_text, source, confirmed,
                        occurred_at, created_at)
                       VALUES (?, ?, 'x', 1, 'pcs', ?, NULL, 'seed',
                               'api', 1, ?, ?)""",
                    (a["shop_id"], kind, amount, day, day),
                )
    monkeypatch.setenv("DUKA_SHOP_AI_CAP", "0")

    def explode(summary):
        raise AssertionError("Spark must not be called when capped")

    monkeypatch.setattr(appmod, "explain_credit", explode)
    result = appmod.credit_explain(
        request=helpers.authed("/api/credit/explain", a))
    assert result["source"] == "template"


# Route inventory guard


def test_route_inventory_guard():
    public = {
        ("GET", "/api/health"),
        ("GET", "/api/p/{token}"),
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/credit/summary"),
        ("GET", "/api/credit/explain"),
        ("GET", "/api/dashboard/weekly"),
        ("GET", "/{path:path}"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/redoc"),
    }
    uncovered = []
    for route in appmod.app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = set(route.methods or []) - {"HEAD", "OPTIONS"}
        for method in methods:
            if (method, route.path) in public:
                continue
            deps = list(getattr(route, "dependencies", None) or [])
            sub = list(
                getattr(getattr(route, "dependant", None), "dependencies", None)
                or []
            )
            calls = [getattr(dep, "call", None) for dep in deps + sub]
            if authmod.require_user not in calls:
                uncovered.append(f"{method} {route.path}")
    assert uncovered == []
