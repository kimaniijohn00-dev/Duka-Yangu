"""Duka-Yangu FastAPI backend."""
import json
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import get_connection, init_db
from services.credit import (
    DEMO_SHOP_NAME,
    assess_shop,
    resolve_as_of,
    weekly_series,
)
from services.explain import explain_credit, template_explanation
from services.passport import (
    VALID_EXPIRY_DAYS,
    VALID_SECTIONS,
    build_snapshot,
    filter_snapshot,
    hash_token,
)
from services.spark import parse_entry


def _resolve_shop_row(conn, shop_id):
    """Fetch one shop: explicit id, or the demo shop when omitted."""
    if shop_id is None:
        row = conn.execute(
            "SELECT * FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail="Demo shop not found; pass shop_id."
            )
        return row
    row = conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Shop not found.")
    return row


def _parse_as_of(as_of: str | None):
    """Explicit ?as_of=YYYY-MM-DD, or None when the caller passed none.

    400 on a bad date. A parsed date always wins over the demo-shop pin.
    """
    from datetime import date

    if as_of is None:
        return None
    try:
        return date.fromisoformat(as_of)
    except ValueError:
        raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD")
from services.transcribe import (
    MAX_AUDIO_BYTES,
    MAX_DURATION_SECONDS,
    transcribe_audio,
    wav_duration_seconds,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_demo_seed()
    yield


def ensure_demo_seed() -> None:
    """Seed the demo shop once, on first start.

    Runs only when the demo shop row is missing. The seeder itself is
    idempotent and touches only the demo shop's rows, so existing data
    (demo or real) is never modified or duplicated.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
        ).fetchone()
        if row is not None:
            return
    import importlib.util

    seed_path = Path(__file__).resolve().parent / "scripts" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("duka_seed_demo", seed_path)
    seed_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_demo)
    seed_demo.main([])


app = FastAPI(title="Duka-Yangu API", lifespan=lifespan)


class EntryIn(BaseModel):
    text: str
    shop_id: int | None = None


class ConfirmIn(BaseModel):
    id: int
    item: str | None = None
    quantity: float | None = None
    amount_kes: int | None = None


class PassportCreateIn(BaseModel):
    lender_name: str
    expires_in_days: int
    sections: list[str] | None = None
    shop_id: int | None = None


RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

# Client IP -> timestamps of recent GET /api/p/{token} hits, pruned on use.
_rate_limit_hits: dict[str, list[datetime]] = {}

# Per-endpoint per-IP limits: key -> (max requests, window seconds).
RATE_LIMITS = {
    "entry": (20, 60),
    "transcribe": (10, 60),
    "explain": (10, 60),
    "passports": (10, 60),
}

# (endpoint key, client IP) -> timestamps of recent hits, pruned on use.
_api_rate_limit_hits: dict[tuple[str, str], list[datetime]] = {}

# Global daily budget shared by all Spark and voice calls. The counter
# lives in SQLite (usage_counters) so it survives restarts. Overridable
# with DUKA_DAILY_AI_CAP; the Credit tab never 429s (see credit_explain).
DAILY_AI_COUNTER = "ai_calls"
DAILY_CAP_DEFAULT = 400
DAILY_CAP_MESSAGE = "Demo limit reached for today, please try again tomorrow"

# Built frontend bundle. Served as static files when present (production);
# absent during local development, where Vite serves the frontend instead.
# On Modal the image sets DUKA_STATIC_DIR to the bundle's absolute path.
def _frontend_dist() -> Path:
    override = os.getenv("DUKA_STATIC_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _utcnow() -> datetime:
    """Current UTC time. Monkeypatchable so tests can control the clock."""
    return datetime.now(timezone.utc)


def _client_ip(request: Request | None) -> str:
    """Real client IP behind Modal's proxy.

    Modal's docs ("Client IP address" under Web Functions) give exactly
    this: the proxy delivers the real client address as request.client.host.
    Proxy headers such as X-Forwarded-For are never trusted here, since a
    client can forge them.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host:
        return host
    return "unknown"


def _rate_limited(ip: str, now: datetime) -> bool:
    cutoff = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    hits = [hit for hit in _rate_limit_hits.get(ip, []) if hit > cutoff]
    hits.append(now)
    _rate_limit_hits[ip] = hits
    return len(hits) > RATE_LIMIT_MAX_REQUESTS


def _check_rate_limited(key: str, ip: str, now: datetime) -> bool:
    """Per-endpoint per-IP sliding-window check. True when over the limit."""
    limit, window = RATE_LIMITS[key]
    cutoff = now - timedelta(seconds=window)
    slot = (key, ip)
    hits = [hit for hit in _api_rate_limit_hits.get(slot, []) if hit > cutoff]
    hits.append(now)
    _api_rate_limit_hits[slot] = hits
    return len(hits) > limit


def _guard(key: str, request: Request | None, now: datetime) -> None:
    """Enforce the per-IP rate limit for served HTTP traffic.

    request is None only for in-process (test) calls, which bypass the
    HTTP layer and are never rate limited.
    """
    if request is None:
        return
    if _check_rate_limited(key, _client_ip(request), now):
        raise HTTPException(
            status_code=429, detail="Too many requests. Try again in a minute."
        )


def _daily_ai_cap() -> int:
    try:
        return max(0, int(os.getenv("DUKA_DAILY_AI_CAP", str(DAILY_CAP_DEFAULT))))
    except ValueError:
        return DAILY_CAP_DEFAULT


def _try_consume_ai_call(conn, now: datetime) -> bool:
    """Consume one AI call from today's shared budget. False when exhausted.

    The caller must already hold a connection; the increment commits with
    the surrounding transaction.
    """
    day = now.date().isoformat()
    row = conn.execute(
        "SELECT count FROM usage_counters WHERE day = ? AND name = ?",
        (day, DAILY_AI_COUNTER),
    ).fetchone()
    used = row["count"] if row is not None else 0
    if used >= _daily_ai_cap():
        return False
    conn.execute(
        "INSERT INTO usage_counters (day, name, count) VALUES (?, ?, 1)"
        " ON CONFLICT(day, name) DO UPDATE SET count = count + 1",
        (day, DAILY_AI_COUNTER),
    )
    return True


def _charge_ai_call(request: Request | None, now: datetime) -> None:
    """Charge one Spark/voice call, or raise the friendly 429 when capped.

    Skipped when request is None (in-process test calls bypass the HTTP
    layer, so they neither consume budget nor touch the database here).
    """
    if request is None:
        return
    with get_connection() as conn:
        if not _try_consume_ai_call(conn, now):
            raise HTTPException(status_code=429, detail=DAILY_CAP_MESSAGE)


def _cached_explanation(conn, shop_id: int, score, as_of: str) -> dict | None:
    row = conn.execute(
        "SELECT text, source FROM explain_cache"
        " WHERE shop_id = ? AND score = ? AND as_of = ?",
        (shop_id, score, as_of),
    ).fetchone()
    if row is None:
        return None
    return {"text": row["text"], "source": row["source"]}


def _store_explanation(conn, shop_id: int, score, as_of: str,
                       text: str, source: str, now: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO explain_cache"
        " (shop_id, score, as_of, text, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (shop_id, score, as_of, text, source, now),
    )


def _passport_status(row, now: datetime) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    if now > datetime.fromisoformat(row["expires_at"]):
        return "expired"
    return "active"


def _passport_summary(conn, row, now: datetime) -> dict:
    views = conn.execute(
        "SELECT COUNT(*), MAX(viewed_at) FROM passport_views WHERE passport_id = ?",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "lender_name": row["lender_name"],
        "scope": json.loads(row["scope"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "status": _passport_status(row, now),
        "view_count": views[0],
        "last_viewed_at": views[1],
    }


def _audit_passport(conn, action: str, passport_id: int, detail: str | None, now: str):
    conn.execute(
        "INSERT INTO audit_log (action, entity, entity_id, detail, created_at)"
        " VALUES (?, 'passport', ?, ?, ?)",
        (action, passport_id, detail, now),
    )


def _entry_from_row(row) -> dict:
    return {
        "id": row["id"],
        "shop_id": row["shop_id"],
        "type": row["type"],
        "item": row["item"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "amount_kes": row["amount_kes"],
        "counterparty": row["counterparty"],
        "raw_text": row["raw_text"],
        "source": row["source"],
        "confirmed": row["confirmed"],
        "occurred_at": row["occurred_at"],
    }


@app.post("/api/entry")
def create_entry(payload: EntryIn, request: Request = None):
    """Parse text into a transaction draft. Saved with confirmed=0, never confirmed here."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    now_for_guards = _utcnow()
    _guard("entry", request, now_for_guards)
    _charge_ai_call(request, now_for_guards)
    try:
        parsed = parse_entry(text)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'api', 0, ?, ?)""",
            (
                payload.shop_id,
                parsed.get("type"),
                parsed.get("item", ""),
                parsed.get("quantity", 1),
                parsed.get("unit", "pcs"),
                int(parsed.get("amount_kes", 0)),
                parsed.get("counterparty"),
                text,
                now,
                now,
            ),
        )
        entry_id = cur.lastrowid
        conn.execute(
            "INSERT INTO audit_log (action, entity, entity_id, detail, created_at)"
            " VALUES ('parse', 'transaction', ?, ?, ?)",
            (entry_id, text, now),
        )
    return {
        "id": entry_id,
        "shop_id": payload.shop_id,
        "type": parsed.get("type"),
        "item": parsed.get("item", ""),
        "quantity": parsed.get("quantity", 1),
        "unit": parsed.get("unit", "pcs"),
        "amount_kes": int(parsed.get("amount_kes", 0)),
        "counterparty": parsed.get("counterparty"),
        "raw_text": text,
        "source": "api",
        "confirmed": 0,
        "occurred_at": now,
    }


@app.post("/api/transactions/confirm")
def confirm_transaction(payload: ConfirmIn):
    """Mark a draft entry confirmed, applying any edited fields first.

    404 when missing, 409 when already confirmed, 422 when an edited
    field is invalid. With only {id} it behaves exactly as before.
    """
    edits = _validate_confirm_edits(payload)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (payload.id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="entry not found")
        if row["confirmed"]:
            raise HTTPException(status_code=409, detail="entry already confirmed")
        changed = {
            field: value
            for field, value in edits.items()
            if value != row[field]
        }
        set_clause = ", ".join([f"{field} = ?" for field in edits] + ["confirmed = 1"])
        conn.execute(
            f"UPDATE transactions SET {set_clause} WHERE id = ?",
            (*edits.values(), payload.id),
        )
        conn.execute(
            "INSERT INTO audit_log (action, entity, entity_id, detail, created_at)"
            " VALUES ('confirm', 'transaction', ?, ?, ?)",
            (payload.id, row["raw_text"], now),
        )
        if changed:
            detail = json.dumps(
                {
                    "original": {field: row[field] for field in changed},
                    "edited": changed,
                }
            )
            conn.execute(
                "INSERT INTO audit_log (action, entity, entity_id, detail, created_at)"
                " VALUES ('edit', 'transaction', ?, ?, ?)",
                (payload.id, detail, now),
            )
        updated = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (payload.id,)
        ).fetchone()
    return _entry_from_row(updated)


def _validate_confirm_edits(payload: ConfirmIn) -> dict:
    """Collect provided edit fields, raising 422 with a clear message if invalid."""
    edits: dict = {}
    if payload.item is not None:
        cleaned = payload.item.strip()
        if not cleaned:
            raise HTTPException(
                status_code=422, detail="item must be a non-empty string"
            )
        edits["item"] = cleaned
    if payload.quantity is not None:
        quantity = payload.quantity
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity)
            or quantity <= 0
        ):
            raise HTTPException(
                status_code=422, detail="quantity must be a positive number"
            )
        edits["quantity"] = quantity
    if payload.amount_kes is not None:
        if isinstance(payload.amount_kes, bool) or payload.amount_kes < 1:
            raise HTTPException(
                status_code=422, detail="amount_kes must be a positive integer"
            )
        edits["amount_kes"] = payload.amount_kes
    return edits


@app.post("/api/transcribe")
async def transcribe_entry(
    audio: UploadFile = File(...), request: Request = None
):
    """Transcribe a short voice recording; return {"text": "..."}.

    The upload is kept in memory only: it is never written to disk, the
    database, or logs. 413 when the clip is too large or too long, 502
    when transcription itself fails.
    """
    _guard("transcribe", request, _utcnow())
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Recording is too large. Keep it under about 2 MB / 60 seconds.",
        )
    if not data:
        raise HTTPException(status_code=400, detail="No audio received.")
    duration = wav_duration_seconds(data)
    if duration is not None and duration > MAX_DURATION_SECONDS:
        raise HTTPException(
            status_code=413,
            detail="Recording is longer than 60 seconds. Record a shorter clip.",
        )
    _charge_ai_call(request, _utcnow())
    try:
        text = transcribe_audio(data, audio.filename or "recording.wav")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"text": text}


@app.get("/api/credit/summary")
def credit_summary(shop_id: int | None = None, as_of: str | None = None):
    """Credit-readiness estimate for one shop. An estimate, never a decision."""
    explicit = _parse_as_of(as_of)
    with get_connection() as conn:
        row = _resolve_shop_row(conn, shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (row["id"],),
        ).fetchall()
        items = [dict(item) for item in ledger]
        day = (
            explicit
            if explicit is not None
            else resolve_as_of(row["name"], items, _utcnow().date())
        )
    result = assess_shop(items, day)
    return {
        "shop_id": row["id"],
        "shop_name": row["name"],
        "as_of": day.isoformat(),
        **result,
    }


@app.get("/api/dashboard/weekly")
def dashboard_weekly(shop_id: int | None = None, as_of: str | None = None):
    """Last 8 weeks of confirmed sales, purchases and expenses.

    Oldest week first, integer KES. Defaults to the demo shop.
    """
    explicit = _parse_as_of(as_of)
    with get_connection() as conn:
        row = _resolve_shop_row(conn, shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (row["id"],),
        ).fetchall()
        items = [dict(item) for item in ledger]
        day = (
            explicit
            if explicit is not None
            else resolve_as_of(row["name"], items, _utcnow().date())
        )
    return {
        "shop_id": row["id"],
        "shop_name": row["name"],
        "as_of": day.isoformat(),
        "weeks": weekly_series(items, day),
    }


@app.get("/api/credit/explain")
def credit_explain(
    shop_id: int | None = None,
    as_of: str | None = None,
    request: Request = None,
):
    """Plain-English credit explanation. Written by Spark, checked in Python.

    Below a scored summary this never calls Spark: a fixed friendly
    message is returned instead. Spark's reply is number-checked and any
    failure falls back to a deterministic template; see services/explain.
    Scored explanations are cached per (shop, score, as_of) in SQLite, so
    repeated opens never call Spark again. When the daily AI budget is
    exhausted this still returns 200 with the template explanation, so the
    Credit tab never breaks.
    """
    now_for_guards = _utcnow()
    _guard("explain", request, now_for_guards)
    explicit = _parse_as_of(as_of)
    with get_connection() as conn:
        row = _resolve_shop_row(conn, shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (row["id"],),
        ).fetchall()
        items = [dict(item) for item in ledger]
        day = (
            explicit
            if explicit is not None
            else resolve_as_of(row["name"], items, now_for_guards.date())
        )
    result = assess_shop(items, day)
    if result.get("status") != "scored":
        return {"as_of": day.isoformat(), **explain_credit(result)}
    day_iso = day.isoformat()
    with get_connection() as conn:
        cached = _cached_explanation(conn, row["id"], result["score"], day_iso)
        if cached is not None:
            return {"as_of": day_iso, **cached}
        if request is not None and not _try_consume_ai_call(conn, now_for_guards):
            return {
                "as_of": day_iso,
                "text": template_explanation(result),
                "source": "template",
            }
        explanation = explain_credit(result)
        _store_explanation(
            conn,
            row["id"],
            result["score"],
            day_iso,
            explanation["text"],
            explanation.get("source", "template"),
            now_for_guards.isoformat(),
        )
    return {"as_of": day_iso, **explanation}


@app.get("/api/ledger")
def read_ledger(shop_id: int | None = None):
    """Return confirmed entries only, newest first. Drafts never appear.

    Without shop_id this returns the shopkeeper's own entries
    (shop_id IS NULL), so seeded demo data never mixes in. Pass an
    explicit shop_id for that shop's dashboard slice instead.
    """
    with get_connection() as conn:
        if shop_id is None:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE confirmed = 1 AND shop_id IS NULL"
                " ORDER BY occurred_at DESC, id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE confirmed = 1 AND shop_id = ?"
                " ORDER BY occurred_at DESC, id DESC",
                (shop_id,),
            ).fetchall()
    return [_entry_from_row(r) for r in rows]


@app.post("/api/passports")
def create_passport(payload: PassportCreateIn, request: Request = None):
    """Share a snapshot of the shop's credit summary with one lender.

    The raw share token is returned exactly once, here. Only its
    SHA-256 hash is stored; it never appears in logs or audit rows.
    """
    _guard("passports", request, _utcnow())
    lender_name = payload.lender_name.strip()
    if not 1 <= len(lender_name) <= 60:
        raise HTTPException(
            status_code=422, detail="lender_name must be 1-60 characters"
        )
    if payload.expires_in_days not in VALID_EXPIRY_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"expires_in_days must be one of {list(VALID_EXPIRY_DAYS)}",
        )
    sections = list(VALID_SECTIONS) if payload.sections is None else payload.sections
    if not isinstance(sections, list) or any(s not in VALID_SECTIONS for s in sections):
        raise HTTPException(
            status_code=422,
            detail=f"sections must be a list drawn from {list(VALID_SECTIONS)}",
        )
    scope = list(dict.fromkeys(sections))
    now = _utcnow()
    with get_connection() as conn:
        shop = _resolve_shop_row(conn, payload.shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (shop["id"],),
        ).fetchall()
        items = [dict(item) for item in ledger]
        snapshot = build_snapshot(
            shop["name"],
            items,
            resolve_as_of(shop["name"], items, now.date()),
            scope,
            now.isoformat(),
        )
        token = secrets.token_urlsafe(32)
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=payload.expires_in_days)).isoformat()
        cur = conn.execute(
            """INSERT INTO passports
               (shop_id, lender_name, scope, snapshot_json, token_hash,
                created_at, expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                shop["id"],
                lender_name,
                json.dumps(scope),
                json.dumps(snapshot),
                hash_token(token),
                created_at,
                expires_at,
            ),
        )
        passport_id = cur.lastrowid
        _audit_passport(conn, "passport_created", passport_id, lender_name, created_at)
    return {
        "id": passport_id,
        "token": token,
        "share_path": f"/p/{token}",
        "expires_at": expires_at,
    }


@app.get("/api/passports")
def list_passports(shop_id: int | None = None):
    """The shop's passports with view counts. Never tokens or hashes."""
    now = _utcnow()
    with get_connection() as conn:
        if shop_id is not None:
            _resolve_shop_row(conn, shop_id)
            rows = conn.execute(
                "SELECT * FROM passports WHERE shop_id = ? ORDER BY id DESC",
                (shop_id,),
            ).fetchall()
        else:
            demo = conn.execute(
                "SELECT * FROM shops WHERE name = ?", (DEMO_SHOP_NAME,)
            ).fetchone()
            if demo is None:
                rows = conn.execute(
                    "SELECT * FROM passports ORDER BY id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM passports WHERE shop_id = ? ORDER BY id DESC",
                    (demo["id"],),
                ).fetchall()
        return [_passport_summary(conn, row, now) for row in rows]


@app.post("/api/passports/{passport_id}/revoke")
def revoke_passport(passport_id: int):
    """Revoke a passport. Idempotent: revoking twice is still a 200."""
    now = _utcnow().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM passports WHERE id = ?", (passport_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Passport not found.")
        if row["revoked_at"] is None:
            conn.execute(
                "UPDATE passports SET revoked_at = ? WHERE id = ?",
                (now, passport_id),
            )
            _audit_passport(
                conn, "passport_revoked", passport_id, row["lender_name"], now
            )
            revoked_at = now
        else:
            revoked_at = row["revoked_at"]
    return {"id": passport_id, "status": "revoked", "revoked_at": revoked_at}


@app.get("/api/passports/{passport_id}/activity")
def passport_activity(passport_id: int):
    """View timestamps for one passport, oldest first."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM passports WHERE id = ?", (passport_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Passport not found.")
        views = conn.execute(
            "SELECT viewed_at FROM passport_views WHERE passport_id = ?"
            " ORDER BY viewed_at ASC, id ASC",
            (passport_id,),
        ).fetchall()
    return {"passport_id": passport_id, "views": [view["viewed_at"] for view in views]}


@app.get("/api/p/{token}")
def view_passport(token: str, request: Request):
    """Public lender view: the snapshot filtered to the chosen sections."""
    now = _utcnow()
    if _rate_limited(_client_ip(request), now):
        raise HTTPException(
            status_code=429, detail="Too many requests. Try again in a minute."
        )
    stamped = now.isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM passports WHERE token_hash = ?", (hash_token(token),)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Passport not found.")
        if row["revoked_at"] is not None:
            raise HTTPException(
                status_code=410, detail="This link has been revoked by the shop."
            )
        if now > datetime.fromisoformat(row["expires_at"]):
            raise HTTPException(
                status_code=410, detail="This link has expired."
            )
        conn.execute(
            "INSERT INTO passport_views (passport_id, viewed_at) VALUES (?, ?)",
            (row["id"], stamped),
        )
        _audit_passport(conn, "passport_viewed", row["id"], None, stamped)
        snapshot = filter_snapshot(
            json.loads(row["snapshot_json"]), json.loads(row["scope"])
        )
        snapshot["expires_at"] = row["expires_at"]
    return snapshot


@app.get("/api/health")
def health():
    """Liveness probe. Always 200 with {"status": "ok"}."""
    return {"status": "ok"}


@app.get("/{path:path}")
def serve_frontend(path: str):
    """Serve the built frontend, with an SPA fallback to index.html.

    Registered after every /api route, so /api keeps priority. Any GET
    that is not an API route serves the matching bundle file when one
    exists, otherwise index.html, so /p/<token> opens directly and
    reloads work. Unknown /api paths still 404. Only active when the
    frontend has been built (frontend/dist exists); local Vite
    development is unaffected.
    """
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found.")
    dist = _frontend_dist()
    root = dist.resolve() if dist.is_dir() else None
    if root is None:
        raise HTTPException(status_code=404, detail="Not found.")
    target = (root / path).resolve() if path else root
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail="Not found.")
    if path and target.is_file():
        return FileResponse(str(target))
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(str(index))
