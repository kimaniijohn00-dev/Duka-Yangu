"""Tests for Modal deployment support: rate limits, daily AI cap, cache,
static frontend fallback, health, first-start seeding, and client IP.

Route functions are called directly (no TestClient: it needs sockets),
matching the style of the existing backend tests. Guards apply to served
HTTP traffic, so every guarded call here passes an explicit stub request;
calls without one bypass the guards exactly as before.
"""
import io
import wave
from datetime import date, timedelta
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import app as appmod
import db
from services import credit as creditmod
from services.explain import template_explanation

HOST_A = "10.0.0.1"
HOST_B = "10.0.0.2"

SCORED = {
    "status": "scored",
    "score": 70,
    "band": "Ready",
    "factors": [
        {
            "name": "Record-keeping regularity",
            "points": 20,
            "max_points": 20,
            "value": 1.0,
            "explanation": "60 of the last 60 days have a confirmed entry.",
        },
        {
            "name": "Trend",
            "points": 0,
            "max_points": 10,
            "value": -0.1,
            "explanation": "Sales moved down from the previous 30 days.",
        },
    ],
    "safe_loan_kes": 14000,
    "monthly_repayment_kes": 2333,
    "term_months": 6,
    "tips": ["Grow this month over last month."],
}

PARSED = {
    "type": "sale",
    "item": "sugar",
    "quantity": 5,
    "unit": "kilos",
    "amount_kes": 800,
    "counterparty": None,
}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "deploy.db")
    db.init_db()
    appmod._rate_limit_hits.clear()
    appmod._api_rate_limit_hits.clear()
    monkeypatch.delenv("DUKA_DAILY_AI_CAP", raising=False)


def fake_request(host=HOST_A):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def seed_demo_shop():
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            (creditmod.DEMO_SHOP_NAME, "test", date.today().isoformat()),
        )
        return cur.lastrowid


def make_wav_bytes(seconds, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def make_upload(data, filename="recording.wav"):
    spool = SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    spool.write(data)
    spool.seek(0)
    return UploadFile(file=spool, filename=filename)


def run_route(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("route coroutine did not finish synchronously")


def stub_parse(monkeypatch, calls=None):
    def fake_parse(text):
        if calls is not None:
            calls.append(text)
        return dict(PARSED)

    monkeypatch.setattr(appmod, "parse_entry", fake_parse)


# Rate limits


def test_entry_rate_limit_20_per_minute(isolated_db, monkeypatch):
    stub_parse(monkeypatch)
    request = fake_request()
    for _ in range(20):
        appmod.create_entry(appmod.EntryIn(text="sold sugar"), request)
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_entry(appmod.EntryIn(text="sold sugar"), request)
    assert exc_info.value.status_code == 429
    # A different IP still has its own budget.
    appmod.create_entry(appmod.EntryIn(text="sold sugar"), fake_request(HOST_B))


def test_transcribe_rate_limit_10_per_minute(isolated_db, monkeypatch):
    monkeypatch.setattr(
        appmod, "transcribe_audio", lambda data, filename: "sold sugar"
    )
    request = fake_request()
    for _ in range(10):
        result = run_route(
            appmod.transcribe_entry(make_upload(make_wav_bytes(1)), request)
        )
        assert result == {"text": "sold sugar"}
    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1)), request))
    assert exc_info.value.status_code == 429


def test_explain_rate_limit_10_per_minute(isolated_db, monkeypatch):
    seed_demo_shop()
    monkeypatch.setattr(appmod, "assess_shop", lambda items, day: dict(SCORED))
    monkeypatch.setattr(
        appmod, "explain_credit", lambda summary: {"text": "steady", "source": "spark"}
    )
    request = fake_request()
    for _ in range(10):
        appmod.credit_explain(as_of="2026-01-01", request=request)
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_explain(as_of="2026-01-01", request=request)
    assert exc_info.value.status_code == 429


def test_passports_rate_limit_10_per_minute(isolated_db):
    seed_demo_shop()
    request = fake_request()
    for _ in range(10):
        created = appmod.create_passport(
            appmod.PassportCreateIn(lender_name="Equity Bank", expires_in_days=7),
            request,
        )
        assert created["token"]
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_passport(
            appmod.PassportCreateIn(lender_name="Equity Bank", expires_in_days=7),
            request,
        )
    assert exc_info.value.status_code == 429


# Daily AI cap


def test_daily_cap_blocks_third_call_with_friendly_429(
    isolated_db, monkeypatch
):
    monkeypatch.setenv("DUKA_DAILY_AI_CAP", "2")
    calls = []
    stub_parse(monkeypatch, calls)
    request = fake_request()
    appmod.create_entry(appmod.EntryIn(text="one"), request)
    appmod.create_entry(appmod.EntryIn(text="two"), request)
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_entry(appmod.EntryIn(text="three"), request)
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == appmod.DAILY_CAP_MESSAGE
    assert calls == ["one", "two"]
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT count FROM usage_counters WHERE day = ? AND name = ?",
            (date.today().isoformat(), appmod.DAILY_AI_COUNTER),
        ).fetchone()
    assert row["count"] == 2


def test_daily_cap_counts_transcribe_and_entry_together(
    isolated_db, monkeypatch
):
    monkeypatch.setenv("DUKA_DAILY_AI_CAP", "2")
    stub_parse(monkeypatch)
    monkeypatch.setattr(
        appmod, "transcribe_audio", lambda data, filename: "sold sugar"
    )
    request = fake_request()
    appmod.create_entry(appmod.EntryIn(text="one"), request)
    run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1)), request))
    with pytest.raises(HTTPException) as exc_info:
        appmod.create_entry(appmod.EntryIn(text="three"), request)
    assert exc_info.value.status_code == 429


def test_explain_never_429s_falls_back_to_template(isolated_db, monkeypatch):
    monkeypatch.setenv("DUKA_DAILY_AI_CAP", "0")
    seed_demo_shop()
    monkeypatch.setattr(appmod, "assess_shop", lambda items, day: dict(SCORED))

    def explode(summary):
        raise AssertionError("Spark must not be called when capped")

    monkeypatch.setattr(appmod, "explain_credit", explode)
    result = appmod.credit_explain(as_of="2026-01-01", request=fake_request())
    assert result["as_of"] == "2026-01-01"
    assert result["source"] == "template"
    assert result["text"] == template_explanation(SCORED)


# Explain cache


def test_explain_cache_avoids_repeat_spark_calls(isolated_db, monkeypatch):
    shop_id = seed_demo_shop()
    calls = []

    def fake_explain(summary):
        calls.append(summary["score"])
        return {"text": f"scored {summary['score']}", "source": "spark"}

    monkeypatch.setattr(appmod, "explain_credit", fake_explain)
    monkeypatch.setattr(appmod, "assess_shop", lambda items, day: dict(SCORED))

    first = appmod.credit_explain(shop_id=shop_id, as_of="2026-01-01")
    second = appmod.credit_explain(shop_id=shop_id, as_of="2026-01-01")
    assert calls == [70]
    assert second == first

    monkeypatch.setattr(
        appmod, "assess_shop", lambda items, day: {**SCORED, "score": 71}
    )
    third = appmod.credit_explain(shop_id=shop_id, as_of="2026-01-01")
    assert calls == [70, 71]
    assert third["text"] == "scored 71"


# Static fallback and health


def make_dist(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>duka</html>")
    (assets / "app.js").write_text("console.log(1)")
    return dist


def test_static_fallback_serves_files_and_index(isolated_db, monkeypatch, tmp_path):
    dist = make_dist(tmp_path)
    monkeypatch.setattr(appmod, "FRONTEND_DIST", dist)

    assert appmod.serve_frontend("").path == str(dist / "index.html")
    assert appmod.serve_frontend("p/abc123").path == str(dist / "index.html")
    assert appmod.serve_frontend("assets/app.js").path == str(
        dist / "assets" / "app.js"
    )
    with pytest.raises(HTTPException) as exc_info:
        appmod.serve_frontend("api/ledger")
    assert exc_info.value.status_code == 404
    with pytest.raises(HTTPException) as exc_info:
        appmod.serve_frontend("../outside")
    assert exc_info.value.status_code == 404


def test_static_fallback_404_without_build(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "FRONTEND_DIST", tmp_path / "no-dist")
    with pytest.raises(HTTPException) as exc_info:
        appmod.serve_frontend("p/abc123")
    assert exc_info.value.status_code == 404


def test_frontend_routes_registered_after_api():
    methods = {
        route.path: sorted(route.methods)
        for route in appmod.app.routes
        if hasattr(route, "methods")
    }
    assert methods.get("/api/health") == ["GET"]
    assert "/{path:path}" in methods
    api_index = next(
        i for i, route in enumerate(appmod.app.routes)
        if getattr(route, "path", None) == "/api/health"
    )
    fallback_index = next(
        i for i, route in enumerate(appmod.app.routes)
        if getattr(route, "path", None) == "/{path:path}"
    )
    assert api_index < fallback_index


def test_health():
    assert appmod.health() == {"status": "ok"}


# First-start seeding


def test_first_start_seeds_once_and_leaves_other_data_alone(
    isolated_db, monkeypatch, tmp_path
):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Amina's Duka", "test", date.today().isoformat()),
        )
        conn.execute(
            """INSERT INTO transactions
               (shop_id, type, item, quantity, unit, amount_kes,
                counterparty, raw_text, source, confirmed,
                occurred_at, created_at)
               VALUES (NULL, 'sale', 'sugar', 1, 'pcs', 200,
                       NULL, 'mine', 'api', 1, ?, ?)""",
            (date.today().isoformat(), date.today().isoformat()),
        )

    appmod.ensure_demo_seed()
    with db.get_connection() as conn:
        demo = conn.execute(
            "SELECT * FROM shops WHERE name = ?", (creditmod.DEMO_SHOP_NAME,)
        ).fetchone()
        assert demo is not None
        demo_txns = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?", (demo["id"],)
        ).fetchone()[0]
        other_txns = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id IS NULL"
        ).fetchone()[0]
        shops = conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
    assert demo_txns > 0
    assert other_txns == 1
    assert shops == 2

    appmod.ensure_demo_seed()
    with db.get_connection() as conn:
        again = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE shop_id = ?", (demo["id"],)
        ).fetchone()[0]
    assert again == demo_txns


# Client IP


def test_client_ip_uses_proxy_address_and_ignores_forged_headers():
    assert appmod._client_ip(fake_request(HOST_A)) == HOST_A
    assert appmod._client_ip(None) == "unknown"
    forged = SimpleNamespace(
        client=SimpleNamespace(host="9.9.9.9"),
        headers={"x-forwarded-for": "1.2.3.4"},
    )
    assert appmod._client_ip(forged) == "9.9.9.9"
