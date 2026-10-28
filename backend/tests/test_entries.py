"""Tests for draft entry parsing, confirmation, and the confirmed-only ledger.

The Spark model is always mocked: route tests stub app.parse_entry, and the
"2k" test fakes the OpenAI client so no network or API key is needed.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app as appmod
import db
from services import spark as sparkmod


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def make_draft(monkeypatch, text="sold 5 kilos of sugar for 800 bob", parsed=None):
    parsed = parsed if parsed is not None else {
        "type": "sale",
        "item": "sugar",
        "quantity": 5,
        "unit": "kilos",
        "amount_kes": 800,
        "counterparty": None,
    }
    monkeypatch.setattr(appmod, "parse_entry", lambda t: dict(parsed))
    return appmod.create_entry(appmod.EntryIn(text=text))


def test_entry_parses_2k_as_2000(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen["messages"] = kwargs["messages"]
            args = {
                "type": "sale",
                "item": "sugar",
                "quantity": 1,
                "unit": "packet",
                "amount_kes": 2000,
                "counterparty": None,
            }
            call = SimpleNamespace(
                function=SimpleNamespace(arguments=json.dumps(args))
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))]
            )

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, base_url=None, api_key=None):
            seen["base_url"] = base_url
            seen["api_key"] = api_key
            self.chat = FakeChat()

    monkeypatch.setattr(sparkmod, "OpenAI", FakeOpenAI)

    body = appmod.create_entry(appmod.EntryIn(text="sold 1 packet of sugar for 2k"))

    assert "2k" in sparkmod.SYSTEM_PROMPT  # the rule the real model sees
    assert seen["base_url"] == "https://api.meta.ai/v1"
    assert body["amount_kes"] == 2000
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT amount_kes, confirmed FROM transactions WHERE id = ?", (body["id"],)
        ).fetchone()
    assert tuple(row) == (2000, 0)


def test_confirm_marks_draft_confirmed(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)
    assert draft["confirmed"] == 0

    out = appmod.confirm_transaction(appmod.ConfirmIn(id=draft["id"]))

    assert out["id"] == draft["id"]
    assert out["confirmed"] == 1
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT confirmed FROM transactions WHERE id = ?", (draft["id"],)
        ).fetchone()
    assert tuple(row) == (1,)


def test_confirm_is_not_repeatable(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)
    appmod.confirm_transaction(appmod.ConfirmIn(id=draft["id"]))

    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(appmod.ConfirmIn(id=draft["id"]))

    assert exc_info.value.status_code == 409


def test_unconfirmed_draft_stays_out_of_ledger(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO transactions
               (type, item, quantity, unit, amount_kes, raw_text,
                source, confirmed, occurred_at, created_at)
               VALUES ('sale', 'tea', 1, 'packet', 100, 'old sale',
                       'api', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"""
        )
        old_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO transactions
               (type, item, quantity, unit, amount_kes, raw_text,
                source, confirmed, occurred_at, created_at)
               VALUES ('sale', 'bread', 2, 'pcs', 140, 'new sale',
                       'api', 1, '2026-10-27T00:00:00+00:00', '2026-10-27T00:00:00+00:00')"""
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    ledger = appmod.read_ledger()

    assert [entry["id"] for entry in ledger] == [new_id, old_id]
    assert draft["id"] not in [entry["id"] for entry in ledger]
    assert all(entry["confirmed"] == 1 for entry in ledger)


def test_confirm_missing_id_returns_404(monkeypatch, isolated_db):
    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(appmod.ConfirmIn(id=9999))

    assert exc_info.value.status_code == 404


def test_confirm_without_edits_keeps_values(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)

    out = appmod.confirm_transaction(appmod.ConfirmIn(id=draft["id"]))

    assert (out["item"], out["quantity"], out["amount_kes"]) == ("sugar", 5, 800)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT item, quantity, amount_kes, confirmed FROM transactions"
            " WHERE id = ?",
            (draft["id"],),
        ).fetchone()
        edit_rows = conn.execute(
            "SELECT id FROM audit_log WHERE action = 'edit' AND entity_id = ?",
            (draft["id"],),
        ).fetchall()
    assert tuple(row) == ("sugar", 5, 800, 1)
    assert edit_rows == []


def test_confirm_with_edits_updates_ledger(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)

    out = appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"], item="tea", quantity=2, amount_kes=150)
    )

    assert (out["item"], out["quantity"], out["amount_kes"]) == ("tea", 2, 150)
    ledger = appmod.read_ledger()
    assert [(entry["item"], entry["quantity"], entry["amount_kes"]) for entry in ledger] == [
        ("tea", 2, 150)
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"item": ""},
        {"item": "   "},
        {"quantity": 0},
        {"quantity": -3},
        {"amount_kes": 0},
        {"amount_kes": -50},
    ],
)
def test_confirm_rejects_invalid_edits(monkeypatch, isolated_db, kwargs):
    draft = make_draft(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        appmod.confirm_transaction(appmod.ConfirmIn(id=draft["id"], **kwargs))

    assert exc_info.value.status_code == 422
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT item, quantity, amount_kes, confirmed FROM transactions"
            " WHERE id = ?",
            (draft["id"],),
        ).fetchone()
    assert tuple(row) == ("sugar", 5, 800, 0)


def test_confirm_with_edits_writes_audit_entry(monkeypatch, isolated_db):
    draft = make_draft(monkeypatch)

    appmod.confirm_transaction(
        appmod.ConfirmIn(id=draft["id"], item="tea", quantity=2, amount_kes=150)
    )

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE entity_id = ? ORDER BY id",
            (draft["id"],),
        ).fetchall()
    by_action = {row[0]: row[1] for row in rows}
    assert "confirm" in by_action
    detail = json.loads(by_action["edit"])
    assert detail["original"] == {"item": "sugar", "quantity": 5, "amount_kes": 800}
    assert detail["edited"] == {"item": "tea", "quantity": 2, "amount_kes": 150}
