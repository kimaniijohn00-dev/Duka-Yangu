"""Tests for services/explain.py. Spark is always mocked: no network."""
import pytest
from fastapi import HTTPException

import app as appmod
import db
from services import explain as explainmod

SCORED = {
    "status": "scored",
    "explanation": "Scored 70/100 from 120 confirmed transactions across 40 days.",
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
            "name": "Sales stability",
            "points": 17,
            "max_points": 20,
            "value": 0.1688,
            "explanation": "Weekly sales average KES 42,068.",
        },
        {
            "name": "Profitability",
            "points": 19,
            "max_points": 25,
            "value": 0.1546,
            "explanation": "Net margin of 15.5% over the last 60 days.",
        },
        {
            "name": "Repayment behaviour",
            "points": 14,
            "max_points": 25,
            "value": 0.5654,
            "explanation": "56.5% repaid within 21 days; KES 9,300 outstanding.",
        },
        {
            "name": "Trend",
            "points": 0,
            "max_points": 10,
            "value": -0.133,
            "explanation": "Sales moved down from the previous 30 days.",
        },
    ],
    "safe_loan_kes": 14000,
    "monthly_repayment_kes": 2333,
    "term_months": 6,
    "loan_reasoning": "Lower of 60-day and last-30-day net profit.",
    "tips": ["Grow this month over last month: push a little extra each week."],
    "disclaimer": "Estimate only, not a lending decision.",
}

GOOD_REPLY = (
    "Your shop scored 70 out of 100, in the Ready band."
    " Record-keeping regularity earned 20 out of 20 points,"
    " while Trend earned 0 out of 10."
    " A safe loan estimate is KES 14,000,"
    " about KES 2,333 a month for 6 months."
    " This is only an estimate."
)


def install_fake_openai(monkeypatch, text=None, error=None):
    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen["create"] = kwargs
            if error is not None:
                raise error
            message = type("Message", (), {"content": text})
            return type("Response", (), {"choices": [type("Choice", (), {"message": message})]})

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen["init"] = kwargs
            self.chat = type("Chat", (), {"completions": FakeCompletions()})

    monkeypatch.setattr(explainmod, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("SPARK_MODEL", "test-spark-model")
    return seen


def test_number_tokenizer():
    values = explainmod._token_values("KES 23,500 and 15.5% and 70/100 and 6")
    assert values == [23500.0, 15.5, 70.0, 100.0, 6.0]


def test_good_reply_passes(monkeypatch):
    seen = install_fake_openai(monkeypatch, text=GOOD_REPLY)

    result = explainmod.explain_credit(SCORED)

    assert result == {"text": GOOD_REPLY, "source": "spark"}
    assert seen["init"]["timeout"] == 20.0
    assert seen["create"]["model"] == "test-spark-model"
    system = seen["create"]["messages"][0]["content"]
    assert "estimate" in system.lower()


def test_invented_number_falls_back(monkeypatch):
    install_fake_openai(monkeypatch, text=GOOD_REPLY + " You could get KES 99,999!")

    result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert result["text"] == explainmod.template_explanation(SCORED)


def test_spark_failure_falls_back(monkeypatch):
    install_fake_openai(monkeypatch, error=ConnectionError("down"))

    result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "Ready" in result["text"]


def test_insufficient_data_skips_spark(monkeypatch):
    install_fake_openai(monkeypatch, error=AssertionError("must not call Spark"))

    result = explainmod.explain_credit({"status": "insufficient_data"})

    assert result["source"] == "template"
    assert "history" in result["text"].lower()


def test_template_uses_only_input_numbers():
    import json

    text = explainmod.template_explanation(SCORED)
    allowed = explainmod._allowed_numbers(json.dumps(SCORED))
    assert all(n in allowed for n in explainmod._token_values(text))
    assert "estimate" in text.lower()
    assert "approv" not in text.lower()


def test_template_weakest_matches_tip():
    # Most missing points: Repayment behaviour (25 - 14 = 11).
    text = explainmod.template_explanation(SCORED)
    assert "Your weakest area is Repayment behaviour with 14 out of 25 points." in text
    assert "Follow up small credit quickly" in text


def test_timeout_logs_reason(monkeypatch, caplog):
    class FakeTimeout(Exception):
        pass

    monkeypatch.setattr(explainmod, "APITimeoutError", FakeTimeout)
    install_fake_openai(monkeypatch, error=FakeTimeout("slow"))

    with caplog.at_level("INFO", logger="services.explain"):
        result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "reason=timeout" in caplog.text
    assert "model=test-spark-model" in caplog.text
    assert "elapsed_s=" in caplog.text


def test_spark_error_logs_type_and_status(monkeypatch, caplog):
    class FakeBadRequest(Exception):
        def __init__(self):
            self.status_code = 400
            super().__init__("bad request")

    install_fake_openai(monkeypatch, error=FakeBadRequest())

    with caplog.at_level("INFO", logger="services.explain"):
        result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "reason=spark_error:FakeBadRequest:400" in caplog.text


def test_empty_reply_logs_reason(monkeypatch, caplog):
    install_fake_openai(monkeypatch, text="   ")

    with caplog.at_level("INFO", logger="services.explain"):
        result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "reason=empty_reply" in caplog.text


def test_invalid_numbers_logged_without_sensitive_data(monkeypatch, caplog):
    install_fake_openai(monkeypatch, text="You could borrow KES 99,999 over 99 moons!")

    with caplog.at_level("INFO", logger="services.explain"):
        result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "reason=invalid_numbers:99,99999" in caplog.text
    for secret in ("moons", "warm", "test-key", "KES 14,000"):
        assert secret not in caplog.text


def test_out_of_100_always_allowed(monkeypatch):
    install_fake_openai(monkeypatch, text="Your shop scored 70 out of 100.")
    summary = {
        "status": "scored",
        "score": 70,
        "band": "Ready",
        "factors": [],
        "safe_loan_kes": 0,
        "monthly_repayment_kes": 0,
        "term_months": 6,
        "tips": [],
    }

    result = explainmod.explain_credit(summary)

    assert result["source"] == "spark"


def test_contributor_model_refused(monkeypatch, caplog):
    install_fake_openai(monkeypatch, text=GOOD_REPLY)
    monkeypatch.setenv("SPARK_MODEL", "muse-contributor-9.9")

    with caplog.at_level("INFO", logger="services.explain"):
        result = explainmod.explain_credit(SCORED)

    assert result["source"] == "template"
    assert "reason=spark_error:RuntimeError" in caplog.text


def use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "explain.db")
    db.init_db()


def test_endpoint_explain_spark(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with db.get_connection() as conn:
        shop_id = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Big Duka", "test", "2026-01-01T08:00:00+03:00"),
        ).lastrowid
        for day in range(40):
            stamp = f"2026-{1 + (day // 20):02d}-{(day % 20) + 1:02d}T09:00:00+03:00"
            for _ in range(3):
                conn.execute(
                    """INSERT INTO transactions
                       (shop_id, type, item, quantity, unit, amount_kes,
                        counterparty, raw_text, source, confirmed,
                        occurred_at, created_at)
                       VALUES (?, 'sale', 'sugar', 1, '1kg', 200, NULL,
                               'test', 'api', 1, ?, ?)""",
                    (shop_id, stamp, stamp),
                )
    summary = appmod.credit_summary(shop_id=shop_id)
    assert summary["status"] == "scored"
    reply = (
        f"Your shop scored {summary['score']} out of 100"
        f" in the {summary['band']} band. This is only an estimate."
    )
    install_fake_openai(monkeypatch, text=reply)

    result = appmod.credit_explain(shop_id=shop_id)

    assert result["source"] == "spark"
    assert result["text"] == reply


def test_endpoint_explain_insufficient(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    install_fake_openai(monkeypatch, error=AssertionError("must not call Spark"))
    with db.get_connection() as conn:
        shop_id = conn.execute(
            "INSERT INTO shops (name, location, created_at) VALUES (?, ?, ?)",
            ("Tiny Duka", "test", "2026-01-01T08:00:00+03:00"),
        ).lastrowid

    result = appmod.credit_explain(shop_id=shop_id)

    assert result["text"] == explainmod.INSUFFICIENT_MESSAGE
    assert result["source"] == "template"
    assert result["as_of"] == appmod._utcnow().date().isoformat()


def test_endpoint_explain_errors(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_explain(shop_id=1, as_of="yesterday")
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        appmod.credit_explain(shop_id=4242)
    assert exc_info.value.status_code == 404
