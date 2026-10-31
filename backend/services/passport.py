"""Consent-based Business Passport snapshots for Duka-Yangu.

Plain deterministic Python: no LLM calls, no network, no randomness, no
clock reads. Builds a snapshot of summary numbers (never raw transactions
or customer names) from the existing credit engine in services/credit.
"""

import hashlib
from datetime import datetime, timedelta

from services.credit import (
    DEMO_SHOP_NAME,
    DISCLAIMER,
    WINDOW_DAYS,
    assess_shop,
    weekly_series,
)

VALID_SECTIONS = ("score", "loan", "performance")
VALID_EXPIRY_DAYS = (7, 30)
DEMO_NOTE = "Demo shop with synthetic data"

# Snapshot keys that are always present, even when a section is not shared.
BASE_KEYS = frozenset(
    {
        "shop_name",
        "generated_at",
        "as_of",
        "is_demo_shop",
        "demo_note",
        "disclaimer",
    }
)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a share token. The raw token is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tx_date(tx):
    value = tx["occurred_at"]
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(value).date()


def _window_totals(ledger, as_of):
    """60-day summary numbers: sales, purchases, expenses, margin, outstanding."""
    start = as_of - timedelta(days=WINDOW_DAYS - 1)
    window_tx = [
        tx
        for tx in ledger
        if tx.get("confirmed") == 1 and start <= _tx_date(tx) <= as_of
    ]
    totals = {"sale": 0, "purchase": 0, "expense": 0}
    given = received = 0
    for tx in window_tx:
        if tx["type"] in totals:
            totals[tx["type"]] += tx["amount_kes"]
        if tx["type"] == "credit_given":
            given += tx["amount_kes"]
        elif tx["type"] == "credit_received":
            received += tx["amount_kes"]
    sales = totals["sale"]
    net = sales - totals["purchase"] - totals["expense"]
    return {
        "total_sales_kes": int(sales),
        "total_purchases_kes": int(totals["purchase"]),
        "total_expenses_kes": int(totals["expense"]),
        "net_margin_pct": round(net / sales * 100, 2) if sales > 0 else 0.0,
        "outstanding_credit_kes": int(max(0, given - received)),
    }


def build_snapshot(shop_name, ledger, as_of, sections, generated_at):
    """Snapshot of summary numbers only: no transactions, no customer names."""
    result = assess_shop(ledger, as_of)
    is_demo = shop_name == DEMO_SHOP_NAME
    snapshot = {
        "shop_name": shop_name,
        "generated_at": generated_at,
        "as_of": as_of.isoformat(),
        "is_demo_shop": is_demo,
        "disclaimer": DISCLAIMER,
    }
    if is_demo:
        snapshot["demo_note"] = DEMO_NOTE
    if "score" in sections:
        snapshot["score"] = {
            "score": result["score"],
            "band": result["band"],
            "factors": [
                {
                    "name": factor["name"],
                    "points": factor["points"],
                    "max_points": factor["max_points"],
                    "value": factor["value"],
                    "explanation": factor["explanation"],
                }
                for factor in result["factors"]
            ],
        }
    if "loan" in sections:
        snapshot["loan"] = {
            "safe_loan_kes": result["safe_loan_kes"],
            "monthly_repayment_kes": result["monthly_repayment_kes"],
            "term_months": result["term_months"],
            "loan_reasoning": result.get("loan_reasoning", ""),
        }
    if "performance" in sections:
        performance = _window_totals(ledger, as_of)
        performance["weekly_sales_kes"] = [
            {"week_start": week["week_start"], "sales_kes": week["sales_kes"]}
            for week in weekly_series(ledger, as_of)
        ]
        snapshot["performance"] = performance
    return snapshot


def filter_snapshot(snapshot, scope):
    """Public view: always-present fields plus only the chosen sections."""
    wanted = set(scope) | BASE_KEYS
    return {key: value for key, value in snapshot.items() if key in wanted}
