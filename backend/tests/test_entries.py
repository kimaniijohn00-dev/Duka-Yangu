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
import helpers
from services import spark as sparkmod


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    appmod._rate_limit_hits.clear()
    appmod._api_rate_limit_hits.clear()


def make_account(monkeypatch):
    return helpers.register_account(
        monkeypatch, phone="0711111111", pin="482917",
        shop_name="Test Duka", ip="10.11.0.1",
    )


def make_draft(monkeypatch, account, text="sold 5 kilos of sugar for 800 bob", parsed=None):
    parsed = parsed if parsed is not None else {
        "type": "sale",
        "item": "sugar",
        "quantity": 5,
        "unit": "kilos",
        "amount_kes": 800,
        "counterparty": None,
    }
    monkeypatch.setattr(appmod, "parse_entry", lambda t: dict(parsed))
    return appmod.create_entry(
        appmod.EntryIn(text=text),
        helpers.authed("/api/entry", account, method="POST"),
    )


def confirm(account, entry_id, **edits):
    return appmod.confirm_transaction(
        appmod.ConfirmIn(id=entry_id, **edits),
        helpers.authed("/api/transactions/confirm", account, method="POST"),
    )


def test_entry_parses_2k_as_2000(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    account = make_account(monkeypatch)
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

    body = appmod.create_entry(
        appmod.EntryIn(text="sold 1 packet of sugar for 2k"),
        helpers.authed("/api/entry", account, method="POST"),
    )

    assert "2k" in sparkmod.SYSTEM_PROMPT  # the rule the real model sees
    assert seen["base_url"] == "https://api.meta.ai/v1"
    assert body["amount_kes"] == 2000
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT amount_kes, confirmed FROM transactions WHERE id = ?", (body["id"],)
        ).fetchone()
    assert tuple(row) == (2000, 0)


def test_confirm_marks_draft_confirmed(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)
    assert draft["confirmed"] == 0

    out = confirm(account, draft["id"])

    assert out["id"] == draft["id"]
    assert out["confirmed"] == 1
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT confirmed FROM transactions WHERE id = ?", (draft["id"],)
        ).fetchone()
    assert tuple(row) == (1,)


def test_confirm_is_not_repeatable(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)
    confirm(account, draft["id"])

    with pytest.raises(HTTPException) as exc_info:
        confirm(account, draft["id"])

    assert exc_info.value.status_code == 409


def test_unconfirmed_draft_stays_out_of_ledger(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)
    shop_id = account["shop_id"]
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes, raw_text,
                source, confirmed, occurred_at, created_at)
               VALUES (?, 'sale', 'tea', 1, 'packet', 100, 'old sale',
                       'api', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""",
            (shop_id,),
        )
        old_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes, raw_text,
                source, confirmed, occurred_at, created_at)
               VALUES (?, 'sale', 'bread', 2, 'pcs', 140, 'new sale',
                       'api', 1, '2026-10-27T00:00:00+00:00', '2026-10-27T00:00:00+00:00')""",
            (shop_id,),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    ledger = appmod.read_ledger(helpers.authed("/api/ledger", account))

    assert [entry["id"] for entry in ledger] == [new_id, old_id]
    assert draft["id"] not in [entry["id"] for entry in ledger]
    assert all(entry["confirmed"] == 1 for entry in ledger)


def test_confirm_missing_id_returns_404(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        confirm(account, 9999)

    assert exc_info.value.status_code == 404


def test_confirm_without_edits_keeps_values(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)

    out = confirm(account, draft["id"])

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
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)

    out = confirm(account, draft["id"], item="tea", quantity=2, amount_kes=150)

    assert (out["item"], out["quantity"], out["amount_kes"]) == ("tea", 2, 150)
    ledger = appmod.read_ledger(helpers.authed("/api/ledger", account))
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
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)

    with pytest.raises(HTTPException) as exc_info:
        confirm(account, draft["id"], **kwargs)

    assert exc_info.value.status_code == 422
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT item, quantity, amount_kes, confirmed FROM transactions"
            " WHERE id = ?",
            (draft["id"],),
        ).fetchone()
    assert tuple(row) == ("sugar", 5, 800, 0)


def _insert_confirmed(monkeypatch, shop_id, item="sugar", amount=800):
    import db as dbmod

    with dbmod.get_connection() as conn:
        if shop_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO shops (id, name, location, created_at)"
                " VALUES (?, 'shop', 'test', '2026-01-01T08:00:00+03:00')",
                (shop_id,),
            )
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (?, 'sale', ?, 1, 'pcs', ?, NULL, 'test',
                       'api', 1, '2026-03-01T09:00:00+03:00',
                       '2026-03-01T09:00:00+03:00')""",
            (shop_id, item, amount),
        )


def test_ledger_returns_only_own_entries(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    other = helpers.register_account(
        monkeypatch, phone="0722222222", pin="739536",
        shop_name="Other Duka", ip="10.11.0.2",
    )
    _insert_confirmed(monkeypatch, account["shop_id"], item="sugar")
    _insert_confirmed(monkeypatch, other["shop_id"], item="bread")

    ledger = appmod.read_ledger(helpers.authed("/api/ledger", account))

    assert [entry["item"] for entry in ledger] == ["sugar"]
    assert all(entry["shop_id"] == account["shop_id"] for entry in ledger)


def test_confirm_with_edits_writes_audit_entry(monkeypatch, isolated_db):
    account = make_account(monkeypatch)
    draft = make_draft(monkeypatch, account)

    confirm(account, draft["id"], item="tea", quantity=2, amount_kes=150)

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
