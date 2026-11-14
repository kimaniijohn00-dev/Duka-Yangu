"""Shared helpers for scoped-endpoint tests (plan step 4).

Registers real users through the auth endpoints and builds real
starlette Requests carrying the session cookie and Origin header, so
endpoint tests exercise the same cookie + CSRF path as production.
"""
from fastapi import Response
from starlette.requests import Request

import auth_routes
import db
from services import auth as authmod

INVITE = "test-invite-step4"
SECRET = "test-secret-step4"
HOST = "testserver"


def setup_auth_env(monkeypatch):
    """Invite code, session secret and production cookie mode for tests."""
    monkeypatch.setenv("DUKA_INVITE_CODE", INVITE)
    monkeypatch.setenv("DUKA_SESSION_SECRET", SECRET)
    monkeypatch.delenv("DUKA_ENV", raising=False)
    monkeypatch.delenv("DUKA_ALLOWED_ORIGINS", raising=False)
    authmod._ip_hits.clear()


def make_request(path, method="GET", ip="10.9.0.1", origin=None,
                 cookie=None, referer=None, host=HOST):
    """A real Request. State-changing callers must pass an origin."""
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


def same_origin():
    """Bare scheme + host, exactly what browsers send as Origin."""
    return f"http://{HOST}"


def register_account(monkeypatch, phone, pin, shop_name, ip="10.9.0.1"):
    """Register a user; returns account dict with a session cookie."""
    setup_auth_env(monkeypatch)
    # Every registering test starts with empty app rate-limit state, so
    # per-IP assertions never leak across tests or files.
    import app as appmod

    appmod._rate_limit_hits.clear()
    appmod._api_rate_limit_hits.clear()
    req = make_request("/api/auth/register", method="POST", ip=ip,
                       origin=same_origin())
    resp = Response()
    out = auth_routes.register(
        auth_routes.RegisterIn(
            phone=phone, pin=pin, shop_name=shop_name, invite_code=INVITE
        ),
        req,
        resp,
    )
    cookie = resp.headers["set-cookie"].split(";")[0]
    return {
        "user_id": out["user_id"],
        "shop_id": out["shop_id"],
        "shop_name": out["shop_name"],
        "phone": phone,
        "cookie": cookie,
    }


def authed(path, account, method="GET", ip="10.9.0.1", origin=None, **kwargs):
    """Request with the account's session cookie (and Origin for POSTs)."""
    if origin is None and method != "GET":
        origin = same_origin()
    return make_request(path, method=method, ip=ip, origin=origin,
                        cookie=account["cookie"], **kwargs)


def user_dict(account):
    """The require_user payload shape for an account."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT phone FROM users WHERE id = ?", (account["user_id"],)
        ).fetchone()
    return {
        "user_id": account["user_id"],
        "shop_id": account["shop_id"],
        "phone": row["phone"],
        "shop_name": account["shop_name"],
    }
