"""Duka-Yangu FastAPI backend."""
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from db import get_connection, init_db
from services.credit import DEMO_SHOP_NAME, assess_shop, weekly_series
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
from services.transcribe import (
    MAX_AUDIO_BYTES,
    MAX_DURATION_SECONDS,
    transcribe_audio,
    wav_duration_seconds,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Duka-Yangu API", lifespan=lifespan)


class EntryIn(BaseModel):
    text: str
    shop_id: int | None = None


class ConfirmIn(BaseModel):
    id: int
    item: str | None = None
    quantity: float | None = None
    amount_kes: int | None = None


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
def create_entry(payload: EntryIn):
    """Parse text into a transaction draft. Saved with confirmed=0, never confirmed here."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
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
async def transcribe_entry(audio: UploadFile = File(...)):
    """Transcribe a short voice recording; return {"text": "..."}.

    The upload is kept in memory only: it is never written to disk, the
    database, or logs. 413 when the clip is too large or too long, 502
    when transcription itself fails.
    """
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
    try:
        text = transcribe_audio(data, audio.filename or "recording.wav")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"text": text}


@app.get("/api/credit/summary")
def credit_summary(shop_id: int | None = None, as_of: str | None = None):
    """Credit-readiness estimate for one shop. An estimate, never a decision."""
    from datetime import date

    day = date.today()
    if as_of is not None:
        try:
            day = date.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD")
    with get_connection() as conn:
        row = _resolve_shop_row(conn, shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (row["id"],),
        ).fetchall()
    result = assess_shop([dict(item) for item in ledger], day)
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
    from datetime import date

    day = date.today()
    if as_of is not None:
        try:
            day = date.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD")
    with get_connection() as conn:
        row = _resolve_shop_row(conn, shop_id)
        ledger = conn.execute(
            "SELECT * FROM transactions WHERE shop_id = ? AND confirmed = 1",
            (row["id"],),
        ).fetchall()
    return {
        "shop_id": row["id"],
        "shop_name": row["name"],
        "as_of": day.isoformat(),
        "weeks": weekly_series([dict(item) for item in ledger], day),
    }


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
