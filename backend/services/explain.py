"""Plain-English credit explanations, written by Spark but never calculated by it.

Flow: the caller passes an already-computed credit summary (see
services/credit.py). If its status is not "scored", a fixed friendly
message is returned and Spark is never called. Otherwise Spark
(SPARK_MODEL from .env, the standard chat-completions call) explains the
numbers in warm, simple English.

Guardrail: every number in Spark's reply must already appear in the input
JSON (formats like "23,500", "KES 23,500", "70", "70/100" and "15.5%" all
count). Any invented number, failure, or over-10-second call falls back
to a deterministic template built in Python from the same data.

Transaction data is never logged here: fallback log lines carry only a
reason code, the model name and the elapsed seconds.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APITimeoutError, OpenAI

from services.credit import TIPS as CREDIT_TIPS

logger = logging.getLogger(__name__)

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
TIMEOUT_SECONDS = 20.0
SCORE_SCALE = 100.0  # scores are always out of 100, so it is always allowed

SYSTEM_PROMPT = (
    "You are explaining a small shop's credit-readiness result to the"
    " shopkeeper in simple, warm, plain English. Write about one hundred"
    " and twenty words in short sentences. No jargon, no bullet points, no"
    " markdown. Use ONLY the numbers in the JSON provided: do not calculate,"
    " estimate, round differently, or add any number of your own. Mention the"
    " strongest factor, the weakest factor, and one practical next step from"
    " the tips. Do not promise a loan and do not say the shop will or will"
    " not be approved. Say clearly this is only an estimate."
)

INSUFFICIENT_MESSAGE = (
    "There is not enough history yet to explain your score."
    " Keep recording every confirmed sale, then check back soon."
)

NUMBER_TOKEN = re.compile(
    r"KES\s*\d[\d,]*\.?\d*"
    r"|\d[\d,]*\.?\d*\s*%"
    r"|\d[\d,]*\.?\d*/\d[\d,]*\.?\d*"
    r"|\d[\d,]*\.?\d*"
)


def _token_values(text):
    """Every number in text as floats: "KES 23,500" -> 23500.0."""
    values = []
    for match in NUMBER_TOKEN.finditer(text):
        token = match.group(0)
        token = re.sub(r"(?i)kes", "", token)
        token = token.replace(",", "").replace("%", "").strip()
        for part in token.split("/"):
            part = part.strip()
            if part:
                values.append(float(part))
    return values


def _allowed_numbers(payload_json):
    """Every number in the input JSON, plus the 100 of "out of 100"."""
    return set(_token_values(payload_json)) | {SCORE_SCALE}


def _format_number(value):
    return f"{value:g}"


def _log_fallback(reason, model, elapsed):
    logger.info(
        "credit_explain fallback reason=%s model=%s elapsed_s=%.2f",
        reason,
        model,
        elapsed,
    )


class _SparkFailure(Exception):
    """A Spark call that must fall back, carrying its log-safe reason."""

    def __init__(self, reason, model):
        super().__init__(reason)
        self.reason = reason
        self.model = model


def template_explanation(summary):
    """Deterministic fallback built only from the summary's own numbers.

    The weakest factor is the one missing the most points, and its own
    tip is the next step, so the two always agree.
    """
    factors = summary["factors"]
    strongest = max(factors, key=lambda f: f["points"] / f["max_points"])
    weakest = max(factors, key=lambda f: f["max_points"] - f["points"])
    tip = CREDIT_TIPS.get(weakest["name"]) or (
        summary["tips"][0] if summary["tips"] else "Keep recording every day."
    )
    return (
        f"Your shop scored {summary['score']} out of 100,"
        f" which puts you in the {summary['band']} band."
        f" Your strongest area is {strongest['name']}"
        f" with {strongest['points']} out of {strongest['max_points']} points."
        f" Your weakest area is {weakest['name']}"
        f" with {weakest['points']} out of {weakest['max_points']} points."
        f" A safe loan estimate is KES {summary['safe_loan_kes']:,},"
        f" or about KES {summary['monthly_repayment_kes']:,}"
        f" a month for {summary['term_months']} months."
        f" A good next step: {tip}"
        f" This is only an estimate, not a lending decision."
    )


def _credentials():
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set. Add it to .env.")
    model = os.getenv("SPARK_MODEL", DEFAULT_MODEL)
    if "contributor" in model.lower():
        raise RuntimeError("Contributor models are not allowed here.")
    return api_key, model


def _spark_reply(payload_json):
    """Ask Spark to explain the JSON. Raises _SparkFailure with a reason."""
    try:
        api_key, model = _credentials()
    except RuntimeError as exc:
        raise _SparkFailure(f"spark_error:{type(exc).__name__}", "unknown") from exc
    client = OpenAI(base_url=BASE_URL, api_key=api_key, timeout=TIMEOUT_SECONDS)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Here is the result as JSON:\n" + payload_json},
            ],
        )
    except APITimeoutError as exc:
        raise _SparkFailure("timeout", model) from exc
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        suffix = f":{status}" if status is not None else ""
        raise _SparkFailure(
            f"spark_error:{type(exc).__name__}{suffix}", model
        ) from exc
    try:
        return response.choices[0].message.content or "", model
    except (AttributeError, IndexError, TypeError) as exc:
        raise _SparkFailure("spark_error:BadResponse", model) from exc


def explain_credit(summary):
    """Explain a credit summary. Returns {"text": ..., "source": ...}."""
    if summary.get("status") != "scored":
        return {"text": INSUFFICIENT_MESSAGE, "source": "template"}
    payload_json = json.dumps(summary)
    allowed = _allowed_numbers(payload_json)
    start = time.monotonic()
    try:
        reply, model = _spark_reply(payload_json)
    except _SparkFailure as exc:
        _log_fallback(exc.reason, exc.model, time.monotonic() - start)
        return {"text": template_explanation(summary), "source": "template"}
    elapsed = time.monotonic() - start
    if not reply.strip():
        _log_fallback("empty_reply", model, elapsed)
        return {"text": template_explanation(summary), "source": "template"}
    bad = sorted({number for number in _token_values(reply) if number not in allowed})
    if bad:
        _log_fallback(
            "invalid_numbers:" + ",".join(_format_number(n) for n in bad),
            model,
            elapsed,
        )
        return {"text": template_explanation(summary), "source": "template"}
    return {"text": reply.strip(), "source": "spark"}
