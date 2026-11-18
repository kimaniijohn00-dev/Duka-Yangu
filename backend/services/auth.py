"""Phone-plus-PIN authentication for Duka-Yangu v2 (plan step 3).

Scope: registration gated by an invite code, login with lockout and a
per-phone ceiling, server-side sessions in an HttpOnly cookie, logout,
and a reusable current-user dependency. No existing endpoint is touched
by this module; scoping them is a later plan step.

Security notes:
- PINs are hashed with Argon2id (argon2-cffi) and verified only through
  the library, which also performs the comparison in constant time.
  Invite codes are compared with hmac.compare_digest.
- The session cookie carries a signed random id; only its SHA-256 hash
  is stored. The PIN, the raw token and full phone numbers never reach
  the audit log (phones are masked) or any stored field in plain form.
- Login failures always return one generic 401, including unknown
  phones (served by a dummy verify), locked accounts and ceiling hits,
  so the response does not reveal whether a phone is registered.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from db import get_connection
from services.credit import DEMO_SHOP_NAME
from services.invites import GENERIC_INVALID_CODE, InvalidInvite, consume_invite

SESSION_COOKIE = "duka_session"
SESSION_DAYS = 30

GENERIC_LOGIN_FAILURE = "Wrong phone number or PIN. Try again."

# (phone, IP) failures that impose a lock, and for how long. Keying on
# the pair means an attacker who knows only the victim's number locks
# out their own IP, never the victim elsewhere.
LOCKOUT_STEPS = ((3, 60), (5, 300), (8, 900))

# Per-phone ceiling across all IPs in a trailing 24 hours. Beyond it
# every attempt for that phone is blocked until an operator clears it,
# so rotating IPs cannot brute-force a 6-digit PIN.
PER_PHONE_24H_MAX = 30
ATTEMPT_RETENTION_HOURS = 48

# Backstops reusing the existing per-IP sliding-window shape.
REGISTER_IP_MAX_PER_HOUR = 10
LOGIN_IP_MAX_PER_MINUTE = 20

# Failed single-use invite guesses per IP per hour. Checked before the
# generic register limiter so a guessing IP gets this specific 429 once
# it has burned through MAX wrong codes.
INVITE_GUESS_IP_MAX_PER_HOUR = 10
INVITE_GUESS_WINDOW_S = 3600
INVITE_GUESS_LIMIT_MESSAGE = (
    "Too many wrong invite codes. Try again in an hour."
)

PIN_MIN_LEN = 6
PIN_MAX_LEN = 12

_hasher = PasswordHasher()  # Argon2id is the argon2-cffi default.
DUMMY_HASH = _hasher.hash("unregistered-phone-dummy")

_PHONE_RE = re.compile(r"^\+254[17]\d{8}$")


def _utcnow() -> datetime:
    """Current UTC time. Monkeypatchable so tests can control the clock."""
    return datetime.now(timezone.utc)


def client_ip(request: Request | None) -> str:
    """Real client IP behind Modal's proxy.

    The proxy delivers the real address as request.client.host; headers
    such as X-Forwarded-For are never trusted, since a client can forge
    them. Canonical home of the helper used by every rate limiter.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host:
        return host
    return "unknown"


def normalize_phone(raw: str | None) -> str | None:
    """Canonical +254 form for Kenyan mobile numbers, else None.

    Accepts 07../01.. (10 digits), 2547../2541.. (12 digits) and the
    +254.. form, ignoring spaces, dashes and brackets. Anything else
    (landlines, foreign numbers, short codes) is rejected.
    """
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"[\s\-()]", "", raw.strip())
    digits = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not digits or not digits.isdigit():
        return None
    if digits.startswith("254"):
        canonical = "+" + digits
    elif len(digits) == 10 and digits[:2] in ("07", "01"):
        canonical = "+254" + digits[1:]
    else:
        return None
    return canonical if _PHONE_RE.fullmatch(canonical) else None


def _is_sequential(pin: str) -> bool:
    steps = {ord(b) - ord(a) for a, b in zip(pin, pin[1:])}
    return steps == {1} or steps == {-1}


def validate_pin(pin: str | None, phone_canonical: str) -> str | None:
    """PIN policy: 6-12 digits plus a blocklist. None when acceptable."""
    if not isinstance(pin, str) or not pin.isdigit():
        return "PIN must be digits only."
    if not PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN:
        return f"PIN must be {PIN_MIN_LEN} to {PIN_MAX_LEN} digits."
    if len(set(pin)) == 1:
        return "PIN must not repeat one digit."
    if _is_sequential(pin) or "123456" in pin or "654321" in pin:
        return "PIN is too easy to guess. Pick a less obvious number."
    if pin == phone_canonical.lstrip("+")[-len(pin):]:
        return "PIN must not match your phone number."
    return None


def hash_pin(pin: str) -> str:
    return _hasher.hash(pin)


def verify_pin(pin_hash: str, pin: str) -> bool:
    try:
        return bool(_hasher.verify(pin_hash, pin))
    except (VerifyMismatchError, InvalidHash):
        return False


def _dummy_verify(pin: str) -> None:
    """Same-cost verify for unknown phones: result dropped, timing kept."""
    try:
        _hasher.verify(DUMMY_HASH, pin)
    except (VerifyMismatchError, InvalidHash):
        pass


def mask_phone(phone: str) -> str:
    """Masked phone for logs: +25471*****78. Never log the full number."""
    if len(phone) <= 8:
        return "***"
    return phone[:6] + "*" * max(3, len(phone) - 8) + phone[-2:]


def _audit(conn, action: str, user_id: int | None, shop_id: int | None,
           detail: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO audit_log (action, entity, entity_id, shop_id, detail, created_at)"
        " VALUES (?, 'auth', ?, ?, ?, ?)",
        (action, user_id, shop_id, detail, now.isoformat()),
    )


def _session_secret() -> str:
    secret = os.getenv("DUKA_SESSION_SECRET") or ""
    if not secret:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    return secret


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_session_secret())


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _cookie_secure() -> bool:
    """Secure cookies everywhere except explicit development mode.

    The exception works only one way: development mode may drop Secure
    for local http; nothing can drop it in production.
    """
    return os.getenv("DUKA_ENV", "production").lower() != "development"


def _set_session_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=cookie_value,
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE, path="/", httponly=True,
        samesite="lax", secure=_cookie_secure(),
    )


def _check_origin(request: Request) -> None:
    """CSRF check for state-changing auth requests.

    The Origin header must match our own origin (scheme + host from this
    request) or the DUKA_ALLOWED_ORIGINS list. A same-site Referer is
    accepted when Origin is absent; missing or foreign values are
    rejected with 403.
    """
    host = (request.headers.get("host") or "").strip()
    own = f"{request.url.scheme}://{host}".rstrip("/")
    extra = {
        entry.strip().rstrip("/")
        for entry in os.getenv("DUKA_ALLOWED_ORIGINS", "").split(",")
        if entry.strip()
    }
    allowed = {own} | extra
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin:
        if origin in allowed:
            return
        raise HTTPException(status_code=403, detail="Request origin not allowed.")
    referer = (request.headers.get("referer") or "").strip()
    match = re.match(r"^(https?://[^/]+)", referer)
    if match and match.group(1).rstrip("/") in allowed:
        return
    raise HTTPException(status_code=403, detail="Request origin not allowed.")


# In-memory per-IP sliding windows, mirroring the existing limiters.
_ip_hits: dict[tuple[str, str], list[datetime]] = {}


def _check_ip_limit(kind: str, ip: str, now: datetime, limit: int, window_s: int) -> None:
    cutoff = now - timedelta(seconds=window_s)
    slot = (kind, ip)
    hits = [hit for hit in _ip_hits.get(slot, []) if hit > cutoff]
    hits.append(now)
    _ip_hits[slot] = hits
    if len(hits) > limit:
        raise HTTPException(
            status_code=429, detail="Too many requests. Try again in a minute."
        )


def _invite_guess_hits(ip: str, now: datetime) -> int:
    """Failed invite guesses from this IP inside the trailing hour."""
    cutoff = now - timedelta(seconds=INVITE_GUESS_WINDOW_S)
    slot = ("invite_guess", ip)
    hits = [hit for hit in _ip_hits.get(slot, []) if hit > cutoff]
    _ip_hits[slot] = hits
    return len(hits)


def _record_invite_guess(ip: str, now: datetime) -> None:
    slot = ("invite_guess", ip)
    _ip_hits[slot] = _ip_hits.get(slot, []) + [now]


def _clear_invite_guesses(ip: str) -> None:
    _ip_hits.pop(("invite_guess", ip), None)


def _check_invite_guess_limit(ip: str, now: datetime) -> None:
    """429 when this IP already burned its hourly wrong-code budget."""
    if _invite_guess_hits(ip, now) >= INVITE_GUESS_IP_MAX_PER_HOUR:
        raise HTTPException(status_code=429, detail=INVITE_GUESS_LIMIT_MESSAGE)


def _recent_failures(conn, phone: str, now: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM login_attempts"
        " WHERE phone = ? AND success = 0 AND attempted_at > ?",
        (phone, (now - timedelta(hours=24)).isoformat()),
    ).fetchone()
    return row[0]


def _prune_attempts(conn, now: datetime) -> None:
    conn.execute(
        "DELETE FROM login_attempts WHERE attempted_at <= ?",
        ((now - timedelta(hours=ATTEMPT_RETENTION_HOURS)).isoformat(),),
    )


def _check_throttles(conn, phone: str, ip: str, now: datetime) -> None:
    """Raise the generic 401 when this phone+IP is locked or over ceiling."""
    row = conn.execute(
        "SELECT fails, locked_until FROM login_throttles WHERE phone = ? AND ip = ?",
        (phone, ip),
    ).fetchone()
    if row is not None and row["locked_until"] is not None:
        if now < datetime.fromisoformat(row["locked_until"]):
            _audit(conn, "locked_out", None, None,
                   f"locked phone {mask_phone(phone)}", now)
            # Commit before raising: the caller's `with` block would
            # otherwise roll the audit row back.
            conn.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_FAILURE)
    if _recent_failures(conn, phone, now) >= PER_PHONE_24H_MAX:
        _audit(conn, "locked_out", None, None,
               f"ceiling phone {mask_phone(phone)}", now)
        conn.commit()
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_FAILURE)


def _record_failure(conn, phone: str, ip: str, now: datetime) -> None:
    row = conn.execute(
        "SELECT fails FROM login_throttles WHERE phone = ? AND ip = ?",
        (phone, ip),
    ).fetchone()
    fails = (row["fails"] if row is not None else 0) + 1
    locked_until = None
    for threshold, delay in LOCKOUT_STEPS:
        if fails == threshold:
            locked_until = (now + timedelta(seconds=delay)).isoformat()
    if row is None:
        conn.execute(
            "INSERT INTO login_throttles (phone, ip, fails, locked_until, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (phone, ip, fails, locked_until, now.isoformat()),
        )
    elif locked_until is not None:
        conn.execute(
            "UPDATE login_throttles SET fails = ?, locked_until = ?, updated_at = ?"
            " WHERE phone = ? AND ip = ?",
            (fails, locked_until, now.isoformat(), phone, ip),
        )
    else:
        conn.execute(
            "UPDATE login_throttles SET fails = ?, updated_at = ?"
            " WHERE phone = ? AND ip = ?",
            (fails, now.isoformat(), phone, ip),
        )
    if locked_until is not None:
        _audit(conn, "locked_out", None, None,
               f"locked phone {mask_phone(phone)}", now)
    conn.execute(
        "INSERT INTO login_attempts (phone, ip, attempted_at, success)"
        " VALUES (?, ?, ?, 0)",
        (phone, ip, now.isoformat()),
    )
    _prune_attempts(conn, now)


def _record_success(conn, phone: str, ip: str, now: datetime) -> None:
    conn.execute(
        "DELETE FROM login_throttles WHERE phone = ? AND ip = ?", (phone, ip)
    )
    conn.execute(
        "INSERT INTO login_attempts (phone, ip, attempted_at, success)"
        " VALUES (?, ?, ?, 1)",
        (phone, ip, now.isoformat()),
    )
    _prune_attempts(conn, now)


def _create_session(conn, user_id: int, shop_id: int, now: datetime) -> str:
    """New signed session cookie value; only the token hash is stored."""
    raw_token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO sessions
           (token_hash, user_id, shop_id, created_at, expires_at, revoked_at)
           VALUES (?, ?, ?, ?, ?, NULL)""",
        (
            _token_hash(raw_token),
            user_id,
            shop_id,
            now.isoformat(),
            (now + timedelta(days=SESSION_DAYS)).isoformat(),
        ),
    )
    return _signer().dumps(raw_token)


def require_user(request: Request, now: datetime | None = None) -> dict:
    """Reusable dependency: current user/shop from the session cookie.

    Returns 401 for a missing, forged, revoked or expired session. Not
    attached to existing endpoints yet; scoping them is a later step.
    """
    now = now or _utcnow()
    raw_cookie = request.cookies.get(SESSION_COOKIE)
    if not raw_cookie:
        raise HTTPException(status_code=401, detail="Not logged in.")
    try:
        raw_token = _signer().loads(raw_cookie)
    except BadSignature:
        raise HTTPException(status_code=401, detail="Not logged in.")
    if not isinstance(raw_token, str):
        raise HTTPException(status_code=401, detail="Not logged in.")
    with get_connection() as conn:
        row = conn.execute(
            """SELECT s.user_id, s.shop_id, s.expires_at, s.revoked_at,
                      u.phone, sh.name AS shop_name
               FROM sessions s
               JOIN users u ON u.id = s.user_id
               JOIN shops sh ON sh.id = s.shop_id
               WHERE s.token_hash = ?""",
            (_token_hash(raw_token),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Not logged in.")
        if row["revoked_at"] is not None or now > datetime.fromisoformat(row["expires_at"]):
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
                (now.isoformat(), _token_hash(raw_token)),
            )
            # Commit before raising: the `with` block would otherwise
            # roll the revocation back.
            conn.commit()
            raise HTTPException(status_code=401, detail="Not logged in.")
    return {
        "user_id": row["user_id"],
        "shop_id": row["shop_id"],
        "phone": row["phone"],
        "shop_name": row["shop_name"],
    }


def _check_env_invite(provided: str | None) -> bool:
    """Optional DUKA_INVITE_CODE fallback. False when unset or mismatched."""
    expected = os.getenv("DUKA_INVITE_CODE") or ""
    if not expected or not isinstance(provided, str):
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


def register(phone: str, pin: str, shop_name: str, invite_code: str,
             request: Request, response: Response,
             now: datetime | None = None) -> dict:
    """Register a phone + PIN, creating a fresh non-demo shop and session."""
    now = now or _utcnow()
    ip = client_ip(request)
    _check_origin(request)
    canonical = normalize_phone(phone)
    if canonical is None:
        raise HTTPException(
            status_code=422, detail="Enter a Kenyan mobile number, e.g. 0712 345 678."
        )
    pin_error = validate_pin(pin, canonical)
    if pin_error is not None:
        raise HTTPException(status_code=422, detail=pin_error)
    cleaned_name = (shop_name or "").strip()
    if not 1 <= len(cleaned_name) <= 60:
        raise HTTPException(
            status_code=422, detail="shop_name must be 1-60 characters"
        )
    if cleaned_name.casefold() == DEMO_SHOP_NAME.casefold():
        raise HTTPException(
            status_code=422, detail="That name is reserved for the demo shop."
        )
    # The wrong-code budget is checked before the generic register
    # limiter so guessers get this specific 429. Weak PINs, bad phones
    # and bad names already returned above and never count as guesses.
    if _invite_guess_hits(ip, now) >= INVITE_GUESS_IP_MAX_PER_HOUR:
        with get_connection() as conn:
            _audit(conn, "invite_failed", None, None,
                   f"phone {mask_phone(canonical)}", now)
        raise HTTPException(status_code=429, detail=INVITE_GUESS_LIMIT_MESSAGE)
    _check_ip_limit("auth_register", ip, now, REGISTER_IP_MAX_PER_HOUR, 3600)
    try:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE phone = ?", (canonical,)
            ).fetchone()
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail="That number is already registered. Log in instead."
                )
            cur = conn.execute(
                "INSERT INTO shops (name, location, is_demo, created_at) VALUES (?, NULL, 0, ?)",
                (cleaned_name, now.isoformat()),
            )
            shop_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO users (shop_id, phone, pin_hash, created_at) VALUES (?, ?, ?, ?)",
                (shop_id, canonical, hash_pin(pin), now.isoformat()),
            )
            user_id = cur.lastrowid
            if not _check_env_invite(invite_code):
                # Single-use code, consumed in this same transaction: any
                # later failure rolls the mark back, so a failed
                # registration never burns a code.
                invite_row_id = consume_invite(conn, invite_code, now, shop_id)
                _audit(conn, "invite_used", invite_row_id, shop_id,
                       f"invite {invite_row_id} used", now)
            _audit(conn, "registered", user_id, shop_id,
                   f"phone {mask_phone(canonical)}", now)
            cookie_value = _create_session(conn, user_id, shop_id, now)
    except InvalidInvite as exc:
        # The shop/user inserts roll back with the failed transaction,
        # so the code is never consumed without a created account. The
        # failure itself is audited in a fresh transaction so the record
        # survives the rollback; the raw code is never written anywhere.
        assert str(exc) == GENERIC_INVALID_CODE
        _record_invite_guess(ip, now)
        with get_connection() as conn:
            _audit(conn, "invite_failed", None, None,
                   f"phone {mask_phone(canonical)}", now)
        if _invite_guess_hits(ip, now) > INVITE_GUESS_IP_MAX_PER_HOUR:
            raise HTTPException(status_code=429, detail=INVITE_GUESS_LIMIT_MESSAGE)
        raise HTTPException(status_code=403, detail=str(exc))
    _clear_invite_guesses(ip)
    _set_session_cookie(response, cookie_value)
    return {"user_id": user_id, "shop_id": shop_id, "shop_name": cleaned_name}


def login(phone: str, pin: str, request: Request, response: Response,
          now: datetime | None = None) -> dict:
    """Log in with phone + PIN. Every failure returns one generic 401."""
    now = now or _utcnow()
    ip = client_ip(request)
    _check_origin(request)
    _check_ip_limit("auth_login", ip, now, LOGIN_IP_MAX_PER_MINUTE, 60)
    canonical = normalize_phone(phone)
    with get_connection() as conn:
        user = None
        if canonical is not None:
            user = conn.execute(
                """SELECT u.id, u.shop_id, u.pin_hash, sh.name AS shop_name
                   FROM users u JOIN shops sh ON sh.id = u.shop_id
                   WHERE u.phone = ?""",
                (canonical,),
            ).fetchone()
        lookup_phone = canonical if canonical is not None else str(phone)
        if user is None:
            _dummy_verify(pin if isinstance(pin, str) else "")
            _record_failure(conn, lookup_phone, ip, now)
            _audit(conn, "login_failed", None, None,
                   f"phone {mask_phone(lookup_phone)}", now)
            conn.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_FAILURE)
        _check_throttles(conn, canonical, ip, now)
        if not isinstance(pin, str) or not verify_pin(user["pin_hash"], pin):
            _record_failure(conn, canonical, ip, now)
            _audit(conn, "login_failed", user["id"], user["shop_id"],
                   f"phone {mask_phone(canonical)}", now)
            conn.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_FAILURE)
        if _hasher.check_needs_rehash(user["pin_hash"]):
            conn.execute(
                "UPDATE users SET pin_hash = ? WHERE id = ?",
                (hash_pin(pin), user["id"]),
            )
        _record_success(conn, canonical, ip, now)
        _audit(conn, "login", user["id"], user["shop_id"],
               f"phone {mask_phone(canonical)}", now)
        cookie_value = _create_session(conn, user["id"], user["shop_id"], now)
        shop_name = user["shop_name"]
        user_id, shop_id = user["id"], user["shop_id"]
    _set_session_cookie(response, cookie_value)
    return {"user_id": user_id, "shop_id": shop_id, "shop_name": shop_name}


def logout(request: Request, response: Response,
           now: datetime | None = None) -> dict:
    """Revoke the current session server-side and clear the cookie."""
    now = now or _utcnow()
    _check_origin(request)
    user = require_user(request, now)
    with get_connection() as conn:
        raw_cookie = request.cookies.get(SESSION_COOKIE, "")
        try:
            raw_token = _signer().loads(raw_cookie)
        except BadSignature:
            raw_token = None
        if isinstance(raw_token, str):
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?"
                " AND revoked_at IS NULL",
                (now.isoformat(), _token_hash(raw_token)),
            )
        _audit(conn, "logout", user["user_id"], user["shop_id"],
               f"phone {mask_phone(user['phone'])}", now)
    _clear_session_cookie(response)
    return {"status": "logged out"}
