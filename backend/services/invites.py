"""Single-use invite codes for pilot registration.

Only the SHA-256 hash of a code is ever stored; the raw code is printed
once by scripts/make_invite.py and never written to the database or
logs. Comparison of a presented code uses hmac.compare_digest against
the stored hash.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

DEFAULT_EXPIRY_DAYS = 14

# Unambiguous characters only: no 0/O, 1/I/L. Grouped XXXX-XXXX-XXXX
# for typing on a phone keypad.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = (4, 4, 4)

GENERIC_INVALID_CODE = "Invalid invite code."


class InvalidInvite(Exception):
    """A code that is wrong, used, expired, or revoked. Message is generic."""


def generate_code() -> str:
    """One fresh human-typable code. Randomness comes only from secrets."""
    chars = [_random_char() for _ in range(sum(_CODE_GROUPS))]
    groups = []
    pos = 0
    for size in _CODE_GROUPS:
        groups.append("".join(chars[pos:pos + size]))
        pos += size
    return "-".join(groups)


def _random_char() -> str:
    return secrets.choice(_CODE_ALPHABET)


def normalize_code(raw: str | None) -> str | None:
    """Canonical form for lookup: uppercase, no dashes or spaces."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper().replace("-", "").replace(" ", "")
    if not cleaned or any(char not in _CODE_ALPHABET for char in cleaned):
        return None
    return cleaned


def hash_code(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_invite(conn, label: str, days: int, now: datetime) -> tuple:
    """Store a new code hash; return (row id, raw code shown exactly once)."""
    raw = generate_code()
    cur = conn.execute(
        """INSERT INTO invite_codes
           (code_hash, label, created_at, expires_at,
            used_at, used_by_shop_id, revoked_at)
           VALUES (?, ?, ?, ?, NULL, NULL, NULL)""",
        (
            hash_code(normalize_code(raw)),
            (label or "").strip(),
            now.isoformat(),
            (now + timedelta(days=days)).isoformat(),
        ),
    )
    return cur.lastrowid, raw


def find_invite(conn, code_hash: str):
    return conn.execute(
        "SELECT * FROM invite_codes WHERE code_hash = ?", (code_hash,)
    ).fetchone()


def status_of(row, now: datetime) -> str:
    """Human status for operators: unused, used, expired, or revoked."""
    if row["revoked_at"] is not None:
        return "revoked"
    if row["used_at"] is not None:
        return "used"
    if row["expires_at"] <= now.isoformat():
        return "expired"
    return "unused"


def consume_invite(conn, raw_code: str | None, now: datetime, shop_id: int) -> int:
    """Mark one code used by this shop, or raise InvalidInvite.

    Wrong, used, expired, and revoked codes all raise the same generic
    error so registration reveals nothing about any code. Must run inside
    the registration transaction: a failed registration rolls the mark
    back, so the code is never consumed without a created account.
    """
    canonical = normalize_code(raw_code)
    row = find_invite(conn, hash_code(canonical)) if canonical else None
    if row is None or not hmac.compare_digest(row["code_hash"], hash_code(canonical)):
        raise InvalidInvite(GENERIC_INVALID_CODE)
    if (
        row["used_at"] is not None
        or row["revoked_at"] is not None
        or row["expires_at"] <= now.isoformat()
    ):
        raise InvalidInvite(GENERIC_INVALID_CODE)
    conn.execute(
        "UPDATE invite_codes SET used_at = ?, used_by_shop_id = ? WHERE id = ?",
        (now.isoformat(), shop_id, row["id"]),
    )
    return row["id"]


def revoke_invite(conn, invite_id: int, now: datetime) -> dict:
    """Revoke an unused code. Used codes have nothing left to revoke."""
    row = conn.execute(
        "SELECT * FROM invite_codes WHERE id = ?", (invite_id,)
    ).fetchone()
    if row is None:
        raise InvalidInvite(f"No invite code with id {invite_id}.")
    if row["used_at"] is not None:
        raise InvalidInvite(f"Invite code {invite_id} is already used.")
    conn.execute(
        "UPDATE invite_codes SET revoked_at = ? WHERE id = ?",
        (now.isoformat(), invite_id),
    )
    return dict(row)
