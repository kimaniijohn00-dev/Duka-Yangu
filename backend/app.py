"""Duka-Yangu FastAPI backend."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from db import get_connection, init_db
from services.spark import parse_entry


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
    """Mark a draft entry confirmed. 404 when missing, 409 when already confirmed."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (payload.id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="entry not found")
        if row["confirmed"]:
            raise HTTPException(status_code=409, detail="entry already confirmed")
        conn.execute(
            "UPDATE transactions SET confirmed = 1 WHERE id = ?", (payload.id,)
        )
        conn.execute(
            "INSERT INTO audit_log (action, entity, entity_id, detail, created_at)"
            " VALUES ('confirm', 'transaction', ?, ?, ?)",
            (payload.id, row["raw_text"], now),
        )
        updated = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (payload.id,)
        ).fetchone()
    return _entry_from_row(updated)


@app.get("/api/ledger")
def read_ledger():
    """Return confirmed entries only, newest first. Drafts never appear."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE confirmed = 1"
            " ORDER BY occurred_at DESC, id DESC"
        ).fetchall()
    return [_entry_from_row(r) for r in rows]
