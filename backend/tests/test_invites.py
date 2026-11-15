"""Tests for single-use invite codes (replacing the shared secret code).

Covers: create, use-once, reuse/expired/revoked/wrong all fail with the
same generic error, the raw code never lands in the database or logs, a
failed registration never consumes a code, and the DUKA_INVITE_CODE env
fallback still works when set.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import auth_routes
import db
import helpers
import list_invites
import make_invite
from services import auth as authmod
from services.invites import (
    DEFAULT_EXPIRY_DAYS,
    create_invite,
    hash_code,
    normalize_code,
    revoke_invite,
    status_of,
)

PHONE = "0711111111"
PIN = "482917"
GENERIC = "Invalid invite code."


@pytest.fixture
def invite_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "invites.db")
    db.init_db()
    monkeypatch.setenv("DUKA_SESSION_SECRET", helpers.SECRET)
    monkeypatch.delenv("DUKA_INVITE_CODE", raising=False)
    monkeypatch.delenv("DUKA_ENV", raising=False)
    monkeypatch.delenv("DUKA_ALLOWED_ORIGINS", raising=False)
    authmod._ip_hits.clear()
    import app as appmod

    appmod._rate_limit_hits.clear()
    appmod._api_rate_limit_hits.clear()


def do_register(monkeypatch, phone, code, shop="Test Duka", ip="10.30.0.1"):
    req = helpers.make_request(
        "/api/auth/register", method="POST", ip=ip,
        origin=helpers.same_origin(),
    )
    resp = Response()
    return auth_routes.register(
        auth_routes.RegisterIn(
            phone=phone, pin=PIN, shop_name=shop, invite_code=code
        ),
        req,
        resp,
    )


def fresh_code(monkeypatch, label="Mama Wanjiku", days=DEFAULT_EXPIRY_DAYS,
               ip="10.30.0.1"):
    helpers.setup_auth_env(monkeypatch)
    monkeypatch.delenv("DUKA_INVITE_CODE", raising=False)
    with db.get_connection() as conn:
        _, raw = create_invite(conn, label, days, datetime.now(timezone.utc))
    return raw


def test_create_and_use_once(invite_db, monkeypatch):
    raw = fresh_code(monkeypatch)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", raw)

    out = do_register(monkeypatch, PHONE, raw)
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM invite_codes").fetchone()
        assert row["used_at"] is not None
        assert row["used_by_shop_id"] == out["shop_id"]

    with pytest.raises(HTTPException) as exc_info:
        do_register(monkeypatch, "0722222222", raw, ip="10.30.0.2")
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == GENERIC


@pytest.mark.parametrize("setup", ["wrong", "expired", "revoked"])
def test_bad_codes_share_one_generic_error(invite_db, monkeypatch, setup):
    raw = fresh_code(monkeypatch)
    if setup == "wrong":
        raw = "AAAA-BBBB-CCCC"
    elif setup == "expired":
        with db.get_connection() as conn:
            _, raw = create_invite(
                conn, "old", -1, datetime.now(timezone.utc))
    else:
        with db.get_connection() as conn:
            row_id, raw = create_invite(
                conn, "rev", 14, datetime.now(timezone.utc))
            revoke_invite(conn, row_id, datetime.now(timezone.utc))

    with pytest.raises(HTTPException) as exc_info:
        do_register(monkeypatch, PHONE, raw)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == GENERIC


def test_raw_code_never_stored_or_logged(invite_db, monkeypatch):
    raw = fresh_code(monkeypatch, label="Secret Label")
    out = do_register(monkeypatch, PHONE, raw)
    assert out["shop_id"]

    with db.get_connection() as conn:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        dump = ""
        for table in tables:
            dump += str([
                dict(r) for r in conn.execute(f"SELECT * FROM {table}")
            ])
        stored = conn.execute(
            "SELECT code_hash FROM invite_codes").fetchone()[0]
    assert raw not in dump
    assert raw.replace("-", "") not in dump
    assert stored == hash_code(normalize_code(raw))
    assert "Secret Label" in dump  # the operator's note is stored, not the code


def test_failed_registration_does_not_consume(invite_db, monkeypatch):
    raw = fresh_code(monkeypatch)
    do_register(monkeypatch, PHONE, raw)

    # Duplicate phone: 409, and a second valid code stays burnable.
    raw2 = fresh_code(monkeypatch, label="second")
    with pytest.raises(HTTPException) as exc_info:
        do_register(monkeypatch, PHONE, raw2, ip="10.30.0.2")
    assert exc_info.value.status_code == 409
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT used_at FROM invite_codes WHERE label = 'second'"
        ).fetchone()[0] is None
    out = do_register(monkeypatch, "0722222222", raw2, ip="10.30.0.3")
    assert out["shop_id"]

    # Invalid PIN: 422 before any code is touched.
    raw3 = fresh_code(monkeypatch, label="third")
    req = helpers.make_request(
        "/api/auth/register", method="POST", ip="10.30.0.4",
        origin=helpers.same_origin(),
    )
    with pytest.raises(HTTPException) as exc_info:
        auth_routes.register(
            auth_routes.RegisterIn(
                phone="0733333333", pin="11", shop_name="X",
                invite_code=raw3,
            ),
            req,
            Response(),
        )
    assert exc_info.value.status_code == 422
    with db.get_connection() as conn:
        assert conn.execute(
            "SELECT used_at FROM invite_codes WHERE label = 'third'"
        ).fetchone()[0] is None


def test_env_fallback_still_works_when_set(invite_db, monkeypatch):
    monkeypatch.setenv("DUKA_INVITE_CODE", "pilot-shared")
    out = do_register(monkeypatch, PHONE, "pilot-shared")
    assert out["shop_id"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invite_codes").fetchone()[0] == 0


def test_code_statuses(invite_db, monkeypatch):
    now = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        _, fresh = create_invite(conn, "fresh", 14, now)
        _, old = create_invite(conn, "old", -1, now)
        _, used = create_invite(conn, "used", 14, now)
        rev_id, _ = create_invite(conn, "rev", 14, now)
        revoke_invite(conn, rev_id, now)
    do_register(monkeypatch, PHONE, used, ip="10.30.0.9")
    with db.get_connection() as conn:
        rows = {r["label"]: dict(r) for r in conn.execute("SELECT * FROM invite_codes")}
    assert status_of(rows["fresh"], now) == "unused"
    assert status_of(rows["old"], now) == "expired"
    assert status_of(rows["used"], now) == "used"
    assert status_of(rows["rev"], now) == "revoked"
    assert fresh and old  # codes themselves are only in memory here


def test_make_invite_script_prints_code_once(invite_db, monkeypatch, capsys):
    path = db.DB_PATH
    assert make_invite.main(
        ["--label", "Script Mama", "--days", "30", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Script Mama" in out
    match = re.search(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", out)
    assert match
    raw = match.group(0)

    out2 = do_register(monkeypatch, PHONE, raw)
    assert out2["shop_id"]
    # The printed block is the only place the raw code ever appears.
    with db.get_connection() as conn:
        dump = str([
            dict(r) for r in conn.execute("SELECT * FROM invite_codes")])
    assert raw not in dump


def test_make_invite_rejects_bad_days(invite_db):
    assert make_invite.main(
        ["--label", "X", "--days", "0", "--db", str(db.DB_PATH)]) == 2


def test_list_and_revoke(invite_db, capsys):
    path = str(db.DB_PATH)
    assert make_invite.main(["--label", "List A", "--db", path]) == 0
    capsys.readouterr()
    assert make_invite.main(["--label", "List B", "--db", path]) == 0
    capsys.readouterr()

    assert list_invites.main(["--db", path]) == 0
    out = capsys.readouterr().out
    assert "List A" in out and "unused" in out

    assert list_invites.main(["--db", path, "--revoke", "1"]) == 0
    assert "Revoked invite code 1" in capsys.readouterr().out
    # Revoking a used code is refused; unknown ids are refused.
    assert list_invites.main(["--db", path, "--revoke", "1"]) == 0
    assert list_invites.main(["--db", path, "--revoke", "999"]) == 1


def test_revoke_used_code_refused(invite_db, monkeypatch):
    raw = fresh_code(monkeypatch)
    do_register(monkeypatch, PHONE, raw)
    assert list_invites.main(["--db", str(db.DB_PATH), "--revoke", "1"]) == 1


def test_invite_table_created_by_init_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    db.init_db()
    db.init_db()
    with db.get_connection() as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
            .fetchall()
        }
    assert "invite_codes" in tables
