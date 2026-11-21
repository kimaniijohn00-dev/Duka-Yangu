"""Origin-gate tests for every state-changing POST endpoint (no sockets).

Each of the eight POST routes is called directly with a real starlette
Request carrying a valid session: missing Origin (and no Referer) must
403, a foreign Origin must 403, and the correct Origin must pass the gate
(the downstream result is asserted per endpoint). The two rejected calls
must leave usage_counters and the database unchanged. The async
transcribe route uses the same send()-pump driver as test_transcribe.py
because the sandbox forbids the sockets an event loop needs.
"""
import io
from tempfile import SpooledTemporaryFile

import pytest
from fastapi import HTTPException, Response
from starlette.datastructures import UploadFile

import app as appmod
import auth_routes
import db
import helpers


@pytest.fixture
def shops(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "origin.db")
    db.init_db()
    a = helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Amina's Duka", ip="10.30.0.1",
    )
    b = helpers.register_account(
        monkeypatch, phone="0722222222", pin="739536",
        shop_name="Brian's Duka", ip="10.30.0.1",
    )
    return a, b


def seed_draft(shop_id):
    with db.get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', 'sugar', 1, 'pcs', 500, NULL,
                       'origin probe', 'smoke', 0, '2026-03-01', '2026-03-01')""",
            (shop_id,),
        )
        return cur.lastrowid


def snapshot():
    """Row counts that rejected requests must never move."""
    with db.get_connection() as conn:
        tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        shops = conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        pp = conn.execute("SELECT COUNT(*) FROM passports").fetchone()[0]
        ai = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM usage_counters"
        ).fetchone()[0]
    return (tx, shops, users, sess, pp, ai)


def bare_request(path, account, origin=None):
    """POST request with a valid session cookie and exactly the given Origin.

    origin=None means no Origin header at all (and no Referer either),
    mirroring a cross-site request with neither header present.
    """
    return helpers.make_request(
        path, method="POST", ip="10.30.0.1",
        origin=origin, cookie=account["cookie"],
    )


def expect_403(fn, *args):
    with pytest.raises(HTTPException) as exc_info:
        fn(*args)
    assert exc_info.value.status_code == 403


def make_upload(data, filename="recording.wav"):
    spool = SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    spool.write(data)
    spool.seek(0)
    return UploadFile(file=spool, filename=filename)


def run_route(coro):
    """Drive the transcribe coroutine without an event loop (no sockets)."""
    try:
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value
    finally:
        coro.close()
    raise AssertionError("route suspended on real async I/O")


def test_entry_origin(shops, monkeypatch):
    a, _ = shops
    monkeypatch.setattr(
        appmod, "parse_entry",
        lambda text: {
            "type": "sale", "item": "sugar", "quantity": 1,
            "unit": "pcs", "amount_kes": 500, "counterparty": None,
        },
    )
    before = snapshot()
    expect_403(
        appmod.create_entry, appmod.EntryIn(text="sold sugar"),
        bare_request("/api/entry", a),
    )
    expect_403(
        appmod.create_entry, appmod.EntryIn(text="sold sugar"),
        bare_request("/api/entry", a, origin="https://evil.example"),
    )
    assert snapshot() == before
    out = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", a, method="POST", ip="10.30.0.1"),
    )
    assert out["shop_id"] == a["shop_id"]


def test_confirm_origin(shops):
    a, _ = shops
    draft = seed_draft(a["shop_id"])
    before = snapshot()
    expect_403(
        appmod.confirm_transaction, appmod.ConfirmIn(id=draft),
        bare_request("/api/transactions/confirm", a),
    )
    expect_403(
        appmod.confirm_transaction, appmod.ConfirmIn(id=draft),
        bare_request("/api/transactions/confirm", a,
                     origin="https://evil.example"),
    )
    assert snapshot() == before
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT confirmed FROM transactions WHERE id = ?", (draft,)
        ).fetchone()[0] == 0
    out = appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft),
        helpers.authed("/api/transactions/confirm", a, method="POST",
                       ip="10.30.0.1"),
    )
    assert out["confirmed"] == 1


def test_transcribe_origin(shops, monkeypatch):
    a, _ = shops
    monkeypatch.setattr(
        appmod, "transcribe_audio", lambda data, name: "sold sugar"
    )
    before = snapshot()
    with pytest.raises(HTTPException) as missing:
        run_route(appmod.transcribe_entry(
            make_upload(b"not-a-real-wav"),
            bare_request("/api/transcribe", a)))
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException) as foreign:
        run_route(appmod.transcribe_entry(
            make_upload(b"not-a-real-wav"),
            bare_request("/api/transcribe", a,
                         origin="https://evil.example")))
    assert foreign.value.status_code == 403
    assert snapshot() == before
    out = run_route(appmod.transcribe_entry(
        make_upload(b"not-a-real-wav"),
        helpers.authed("/api/transcribe", a, method="POST",
                       ip="10.30.0.1")))
    assert out == {"text": "sold sugar"}


def test_passport_create_origin(shops):
    a, _ = shops
    body = appmod.PassportCreateIn(lender_name="Equity", expires_in_days=7)
    before = snapshot()
    expect_403(
        appmod.create_passport, body, bare_request("/api/passports", a))
    expect_403(
        appmod.create_passport, body,
        bare_request("/api/passports", a, origin="https://evil.example"))
    assert snapshot() == before
    out = appmod.create_passport(
        body, helpers.authed("/api/passports", a, method="POST",
                             ip="10.30.0.1"))
    assert out["share_path"] == f"/p/{out['token']}"


def test_passport_revoke_origin(shops):
    a, _ = shops
    created = appmod.create_passport(
        appmod.PassportCreateIn(lender_name="KCB", expires_in_days=7),
        helpers.authed("/api/passports", a, method="POST",
                       ip="10.30.0.1"))
    before = snapshot()
    expect_403(
        appmod.revoke_passport, created["id"],
        bare_request("/revoke", a))
    expect_403(
        appmod.revoke_passport, created["id"],
        bare_request("/revoke", a, origin="https://evil.example"))
    assert snapshot() == before
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT revoked_at FROM passports WHERE id = ?",
            (created["id"],),
        ).fetchone()[0] is None
    out = appmod.revoke_passport(
        created["id"], helpers.authed("/revoke", a, method="POST",
                                       ip="10.30.0.1"))
    assert out["status"] == "revoked"


def test_register_origin(shops):
    before = snapshot()
    with pytest.raises(HTTPException) as missing:
        auth_routes.register(
            auth_routes.RegisterIn(
                phone="0733333333", pin="295184", shop_name="C Duka",
                invite_code=helpers.INVITE),
            helpers.make_request("/api/auth/register", method="POST",
                                 ip="10.30.0.1"),
            Response())
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException) as foreign:
        auth_routes.register(
            auth_routes.RegisterIn(
                phone="0755555555", pin="381926", shop_name="D Duka",
                invite_code=helpers.INVITE),
            helpers.make_request("/api/auth/register", method="POST",
                                 ip="10.30.0.1",
                                 origin="https://evil.example"),
            Response())
    assert foreign.value.status_code == 403
    assert snapshot() == before
    out = auth_routes.register(
        auth_routes.RegisterIn(
            phone="0744444444", pin="519384", shop_name="E Duka",
            invite_code=helpers.INVITE),
        helpers.make_request("/api/auth/register", method="POST",
                             ip="10.30.0.1",
                             origin=helpers.same_origin()),
        Response())
    assert out["shop_name"] == "E Duka"


def test_login_origin(shops):
    a, _ = shops
    before = snapshot()
    expect_403(
        auth_routes.login,
        auth_routes.LoginIn(phone="0711111111", pin="482917"),
        bare_request("/api/auth/login", a), Response())
    expect_403(
        auth_routes.login,
        auth_routes.LoginIn(phone="0711111111", pin="482917"),
        bare_request("/api/auth/login", a,
                     origin="https://evil.example"), Response())
    assert snapshot() == before
    out = auth_routes.login(
        auth_routes.LoginIn(phone="0711111111", pin="482917"),
        helpers.authed("/api/auth/login", a, method="POST",
                       ip="10.30.0.1"),
        Response())
    assert out["shop_id"] == a["shop_id"]


def logout_as(account, phone, pin):
    """Fresh session for the logout legs, so other tests keep theirs."""
    req = helpers.make_request("/api/auth/login", method="POST",
                               ip="10.30.0.1",
                               origin=helpers.same_origin())
    resp = Response()
    auth_routes.login(auth_routes.LoginIn(phone=phone, pin=pin), req, resp)
    return resp.headers["set-cookie"].split(";")[0]


def test_logout_origin(shops):
    a, _ = shops
    cookie = logout_as(a, "0711111111", "482917")

    def logout_request(origin):
        return helpers.make_request("/api/auth/logout", method="POST",
                                    ip="10.30.0.1", origin=origin,
                                    cookie=cookie)

    before = snapshot()
    expect_403(auth_routes.logout, logout_request(None), Response())
    expect_403(auth_routes.logout,
               logout_request("https://evil.example"), Response())
    assert snapshot() == before
    seen = auth_routes.me(helpers.make_request(
        "/api/auth/me", cookie=cookie))
    assert seen["shop_id"] == a["shop_id"]
    out = auth_routes.logout(
        logout_request(helpers.same_origin()), Response())
    assert out == {"status": "logged out"}
    with pytest.raises(HTTPException) as exc_info:
        auth_routes.me(helpers.make_request("/api/auth/me", cookie=cookie))
    assert exc_info.value.status_code == 401
