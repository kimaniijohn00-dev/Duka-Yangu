"""Tests for plan step 3: phone-plus-PIN authentication.

Route functions are called directly (no TestClient: it needs sockets),
matching the existing backend style. Real starlette Request/Response
objects carry cookies, the Origin header and the client IP, so cookie
flags, the CSRF check and throttling are exercised without HTTP.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response
from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request

import app as appmod
import auth_routes
import db
import helpers
from services import auth as authmod

INVITE = "test-invite-1"
SECRET = "test-secret-for-pytest-only"
PHONE = "0712345678"  # canonical +254712345678
PIN = "482917"


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "auth.db")
    db.init_db()
    monkeypatch.setenv("DUKA_INVITE_CODE", INVITE)
    monkeypatch.setenv("DUKA_SESSION_SECRET", SECRET)
    monkeypatch.delenv("DUKA_ENV", raising=False)
    monkeypatch.delenv("DUKA_ALLOWED_ORIGINS", raising=False)
    authmod._ip_hits.clear()


def make_request(path="/api/auth/login", method="POST", ip="1.2.3.4",
                 origin="http://testserver", referer=None, cookie=None,
                 host="testserver"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": headers,
        "client": (ip, 5000),
        "server": (host, 80),
    })


def register(phone=PHONE, pin=PIN, shop="Amina's Duka", ip="1.2.3.4",
             origin="http://testserver", invite=INVITE):
    req = make_request("/api/auth/register", ip=ip, origin=origin)
    resp = Response()
    out = auth_routes.register(
        auth_routes.RegisterIn(
            phone=phone, pin=pin, shop_name=shop, invite_code=invite
        ),
        req,
        resp,
    )
    return out, resp


def login(phone=PHONE, pin=PIN, ip="1.2.3.4", origin="http://testserver"):
    req = make_request("/api/auth/login", ip=ip, origin=origin)
    resp = Response()
    out = auth_routes.login(auth_routes.LoginIn(phone=phone, pin=pin), req, resp)
    return out, resp


def session_cookie(resp):
    header = resp.headers.get("set-cookie", "")
    return header.split(";")[0]


def cookie_flags(resp):
    return resp.headers.get("set-cookie", "").lower()


# Registration


def test_register_happy_path_sets_secure_cookie(auth_env):
    out, resp = register()

    assert out["shop_name"] == "Amina's Duka"
    flags = cookie_flags(resp)
    assert "duka_session=" in flags
    assert "httponly" in flags
    assert "samesite=lax" in flags
    assert "secure" in flags
    assert "path=/" in flags
    with db.get_connection() as conn:
        user = conn.execute("SELECT * FROM users").fetchone()
        assert user["phone"] == "+254712345678"
        assert user["pin_hash"].startswith("$argon2id$")
        assert user["pin_hash"] != PIN
        shop = conn.execute(
            "SELECT * FROM shops WHERE id = ?", (user["shop_id"],)
        ).fetchone()
        assert shop["is_demo"] == 0
        session = conn.execute("SELECT * FROM sessions").fetchone()
        assert len(session["token_hash"]) == 64
        assert session["token_hash"] != resp.headers["set-cookie"]
        audit = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'registered'"
        ).fetchall()
    assert len(audit) == 1
    assert "+254712345678" not in audit[0]["detail"]
    assert "***" in audit[0]["detail"]


def test_register_rejects_wrong_or_missing_invite(auth_env):
    with pytest.raises(HTTPException) as exc_info:
        register(invite="wrong-code")
    assert exc_info.value.status_code == 403

    with pytest.raises(HTTPException) as exc_info:
        register(invite="")
    assert exc_info.value.status_code == 403


def test_register_without_any_valid_code_fails_generic(auth_env, monkeypatch):
    monkeypatch.delenv("DUKA_INVITE_CODE")
    with pytest.raises(HTTPException) as exc_info:
        register()
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid invite code."


def test_register_duplicate_phone_conflicts(auth_env):
    register()
    with pytest.raises(HTTPException) as exc_info:
        register(shop="Another Duka")
    assert exc_info.value.status_code == 409


def test_register_per_ip_limit(auth_env):
    for second in range(10):
        register(phone=f"07000000{second:02d}", ip="9.9.9.9")
    with pytest.raises(HTTPException) as exc_info:
        register(phone="0700000099", ip="9.9.9.9")
    assert exc_info.value.status_code == 429


@pytest.mark.parametrize(
    "pin",
    [
        "12345",  # too short
        "1234567890123",  # too long
        "abcdef",  # not digits
        "12ab34",  # not digits
        "111111",  # repeated digit
        "000000",  # repeated digit
        "123456",  # sequence
        "654321",  # reverse sequence
        "234567",  # sequence
        "987654",  # reverse sequence
        "345678",  # phone's last digits
        "1234567",  # contains a sequence
    ],
)
def test_register_rejects_weak_pins(auth_env, pin):
    with pytest.raises(HTTPException) as exc_info:
        register(pin=pin)
    assert exc_info.value.status_code == 422


def test_register_rejects_bad_shop_name(auth_env):
    with pytest.raises(HTTPException) as exc_info:
        register(shop="   ")
    assert exc_info.value.status_code == 422


def test_dev_mode_drops_secure_flag(auth_env, monkeypatch):
    monkeypatch.setenv("DUKA_ENV", "development")
    _, resp = register()
    assert "secure" not in cookie_flags(resp)


# Phone normalization


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "+254712345678"),
        ("0112345678", "+254112345678"),
        ("254712345678", "+254712345678"),
        ("+254712345678", "+254712345678"),
        ("0712 345 678", "+254712345678"),
        ("0712-345-678", "+254712345678"),
        ("(0712) 345678", "+254712345678"),
    ],
)
def test_normalize_accepts_kenyan_forms(raw, expected):
    assert authmod.normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "071234567",  # too short
        "07123456789",  # too long
        "020123456",  # landline
        "+255712345678",  # foreign
        "12345",  # short code
        "0712abc678",  # letters
        "",  # empty
        None,  # not a string
    ],
)
def test_normalize_rejects_non_mobile(raw):
    assert authmod.normalize_phone(raw) is None


# Login, lockout, ceiling


def test_login_success_sets_cookie_and_me(auth_env):
    register()
    out, resp = login()

    assert out["shop_name"] == "Amina's Duka"
    assert "duka_session=" in cookie_flags(resp)
    req = make_request(
        "/api/auth/me", method="GET", cookie=session_cookie(resp)
    )
    seen = auth_routes.me(req)
    assert seen == {
        "user_id": out["user_id"],
        "shop_id": out["shop_id"],
        "shop_name": "Amina's Duka",
    }
    assert "pin_hash" not in seen


def test_wrong_pin_matches_unknown_phone(auth_env):
    register()
    with pytest.raises(HTTPException) as wrong:
        login(pin="000001")
    with pytest.raises(HTTPException) as unknown:
        login(phone="0799999999", pin="000001")
    assert wrong.value.status_code == unknown.value.status_code == 401
    assert wrong.value.detail == unknown.value.detail
    assert wrong.value.detail == authmod.GENERIC_LOGIN_FAILURE


def test_me_without_cookie_is_401(auth_env):
    with pytest.raises(HTTPException) as exc_info:
        auth_routes.me(make_request("/api/auth/me", method="GET"))
    assert exc_info.value.status_code == 401


def test_lockout_blocks_pair_but_not_other_ips(auth_env, monkeypatch):
    register()
    base = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(authmod, "_utcnow", lambda: base)

    for _ in range(3):
        with pytest.raises(HTTPException):
            login(pin="000001", ip="10.0.0.1")
    with pytest.raises(HTTPException) as locked:
        login(pin=PIN, ip="10.0.0.1")
    assert locked.value.status_code == 401

    out, _ = login(pin=PIN, ip="10.0.0.2")
    assert out["shop_name"] == "Amina's Duka"

    monkeypatch.setattr(
        authmod, "_utcnow", lambda: base + timedelta(seconds=61)
    )
    out, _ = login(pin=PIN, ip="10.0.0.1")
    assert out["shop_name"] == "Amina's Duka"


def test_per_phone_ceiling_blocks_rotating_ips(auth_env):
    register()
    for third in range(30):
        with pytest.raises(HTTPException):
            login(pin="000001", ip=f"10.1.0.{third}")
    with pytest.raises(HTTPException) as exc_info:
        login(pin=PIN, ip="10.2.0.1")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == authmod.GENERIC_LOGIN_FAILURE
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'locked_out'"
        ).fetchall()
    assert rows
    assert all("+254712345678" not in row["detail"] for row in rows)


# Logout and expiry


def test_logout_revokes_session(auth_env):
    register()
    _, login_resp = login()
    cookie = session_cookie(login_resp)

    req = make_request("/api/auth/logout", cookie=cookie)
    out = auth_routes.logout(req, Response())
    assert out == {"status": "logged out"}

    with db.get_connection() as conn:
        # The login session is the newest row; the register session stays live.
        row = conn.execute(
            "SELECT revoked_at FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["revoked_at"] is not None

    with pytest.raises(HTTPException) as exc_info:
        auth_routes.me(make_request("/api/auth/me", method="GET", cookie=cookie))
    assert exc_info.value.status_code == 401


def test_expired_session_rejected_and_revoked(auth_env):
    out, _ = register()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sid = "expired-raw-sid"
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO sessions
               (token_hash, user_id, shop_id, created_at, expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, NULL)""",
            (
                hashlib.sha256(sid.encode()).hexdigest(),
                out["user_id"],
                out["shop_id"],
                past.isoformat(),
                past.isoformat(),
            ),
        )
    cookie = URLSafeTimedSerializer(SECRET).dumps(sid)
    req = make_request(
        "/api/auth/me", method="GET", cookie=f"duka_session={cookie}"
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_routes.me(req)
    assert exc_info.value.status_code == 401
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT revoked_at FROM sessions WHERE token_hash = ?",
            (hashlib.sha256(sid.encode()).hexdigest(),),
        ).fetchone()
    assert row["revoked_at"] is not None


# Origin check


def test_origin_missing_or_foreign_rejected(auth_env):
    register()
    with pytest.raises(HTTPException) as missing:
        login(origin=None)
    assert missing.value.status_code == 403
    with pytest.raises(HTTPException) as foreign:
        login(origin="https://evil.example")
    assert foreign.value.status_code == 403


def test_same_site_referer_accepted(auth_env):
    register()
    req = make_request(
        "/api/auth/login", origin=None, referer="http://testserver/entries"
    )
    out = auth_routes.login(
        auth_routes.LoginIn(phone=PHONE, pin=PIN), req, Response()
    )
    assert out["shop_name"] == "Amina's Duka"


def test_allowed_origins_env_accepted(auth_env, monkeypatch):
    register()
    monkeypatch.setenv("DUKA_ALLOWED_ORIGINS", "https://shop.example")
    req = make_request("/api/auth/login", origin="https://shop.example")
    out = auth_routes.login(
        auth_routes.LoginIn(phone=PHONE, pin=PIN), req, Response()
    )
    assert out["shop_name"] == "Amina's Duka"


# Secrets hygiene and v1 behavior


def test_pin_and_token_never_stored_or_logged(auth_env):
    register()
    _, login_resp = login()
    cookie_value = session_cookie(login_resp).split("=", 1)[1]
    with pytest.raises(HTTPException):
        login(pin="000001")

    with db.get_connection() as conn:
        dump = ""
        for table in ("users", "sessions", "audit_log", "login_attempts",
                      "login_throttles"):
            dump += str([
                dict(row) for row in conn.execute(f"SELECT * FROM {table}")
            ])
        pin_hash = conn.execute("SELECT pin_hash FROM users").fetchone()[0]
    assert PIN not in dump
    assert cookie_value not in dump
    assert pin_hash.startswith("$argon2id$")
    with db.get_connection() as conn:
        details = conn.execute("SELECT detail FROM audit_log").fetchall()
    assert details
    assert all("+254712345678" not in row["detail"] for row in details)


def test_v1_calling_convention_unchanged(auth_env, monkeypatch):
    # v1 calling convention is unchanged (payloads, return shapes); only
    # the caller's session is now required alongside.
    account = helpers.register_account(
        monkeypatch, phone="0799999999", pin="739536",
        shop_name="V1 Duka", ip="10.14.0.1",
    )
    monkeypatch.setattr(
        appmod, "parse_entry",
        lambda text: {
            "type": "sale",
            "item": "sugar",
            "quantity": 5,
            "unit": "kilos",
            "amount_kes": 800,
            "counterparty": None,
        },
    )
    draft = appmod.create_entry(
        appmod.EntryIn(text="sold sugar"),
        helpers.authed("/api/entry", account, method="POST"),
    )
    confirmed = appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"]),
        helpers.authed("/api/transactions/confirm", account, method="POST"),
    )
    assert confirmed["confirmed"] == 1
    assert appmod.read_ledger(helpers.authed("/api/ledger", account))[0]["id"] == draft["id"]
    assert appmod.health() == {"status": "ok"}
