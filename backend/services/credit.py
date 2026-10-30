"""Credit-readiness engine for Duka-Yangu.

Plain deterministic Python: no LLM calls, no network, no randomness, no
clock reads. Every function takes a shop's transactions (mappings with the
real schema's keys) and an explicit "as of" date, so results are fully
reproducible and unit-testable.

Only confirmed transactions (confirmed == 1) are ever counted; drafts are
ignored. Money is integer KES everywhere.
"""
import statistics
from datetime import date, datetime, timedelta

DISCLAIMER = "Estimate only, not a lending decision."
DEMO_SHOP_NAME = "Demo Duka (synthetic data)"

# ---- Tunable thresholds -------------------------------------------------
WINDOW_DAYS = 60
MIN_HISTORY_DAYS = 30
MIN_TRANSACTIONS = 100

REGULARITY_MAX = 20
STABILITY_MAX = 20
PROFIT_MAX = 25
REPAY_MAX = 25
TREND_MAX = 10

WEEKS_FOR_STABILITY = 8
STABILITY_CV_ZERO_POINTS = 1.0  # CV at or above this earns 0 stability points

PROFIT_FULL_MARGIN = 0.20  # net margin earning full profitability points
PROFIT_ZERO_MARGIN = 0.0  # net margin at or below this earns 0 points

REPAY_WINDOW_DAYS = 21  # a repayment counts when it lands within this long
OUTSTANDING_PENALTY_THRESHOLD = 0.5  # outstanding vs monthly sales above this...
OUTSTANDING_PENALTY_POINTS = 10  # ...costs this many repayment points

TREND_FULL_GROWTH = 0.20  # 30-day sales growth earning full trend points

BAND_SHARES = {
    "Building": (0, 1),
    "Developing": (1, 2),
    "Ready": (4, 5),
    "Strong": (1, 1),
}  # band scaling as exact fractions: 0, 0.5, 0.8, 1.0
REPAY_NUMERATOR = 3
REPAY_DENOMINATOR = 10  # repayments never exceed 3/10 of monthly profit
LOAN_TERM_MONTHS = 6
LOAN_ROUND_KES = 500

TIPS = {
    "Record-keeping regularity": (
        "Record at least one confirmed sale every day, including slow days."
    ),
    "Sales stability": (
        "Smooth out quiet spells: steady weekly sales score better than spikes."
    ),
    "Profitability": (
        "Lift your net margin: restock staples at wholesale and watch daily costs."
    ),
    "Repayment behaviour": (
        "Follow up small credit quickly so advances are repaid within three weeks."
    ),
    "Trend": "Grow this month over last month: push a little extra each week.",
}


def _tx_date(tx):
    value = tx["occurred_at"]
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(value).date()


def _confirmed_upto(transactions, as_of):
    """Confirmed transactions dated on or before as_of."""
    return [
        tx
        for tx in transactions
        if tx.get("confirmed") == 1 and _tx_date(tx) <= as_of
    ]


def _in_window(transactions, start, end):
    return [tx for tx in transactions if start <= _tx_date(tx) <= end]


def _sum_by_type(transactions):
    totals = {}
    for tx in transactions:
        totals[tx["type"]] = totals.get(tx["type"], 0) + tx["amount_kes"]
    return totals


def _factor(name, points, max_points, value, explanation):
    return {
        "name": name,
        "points": int(points),
        "max_points": max_points,
        "value": value,
        "explanation": explanation,
    }


def factor_regularity(window_tx, as_of):
    """Share of the last WINDOW_DAYS days with a confirmed transaction."""
    start = as_of - timedelta(days=WINDOW_DAYS - 1)
    active = {_tx_date(tx) for tx in window_tx if _tx_date(tx) >= start}
    share = len(active) / WINDOW_DAYS
    points = round(share * REGULARITY_MAX)
    return _factor(
        "Record-keeping regularity",
        points,
        REGULARITY_MAX,
        round(share, 4),
        f"{len(active)} of the last {WINDOW_DAYS} days have a confirmed entry.",
    )


def factor_stability(window_tx, as_of):
    """Coefficient of variation of weekly sales: lower is better.

    Uses the most recent WEEKS_FOR_STABILITY complete 7-day weeks.
    """
    weeks = []
    for back in range(WEEKS_FOR_STABILITY):
        end = as_of - timedelta(days=7 * back)
        start = end - timedelta(days=6)
        weeks.append(
            sum(
                tx["amount_kes"]
                for tx in window_tx
                if tx["type"] == "sale" and start <= _tx_date(tx) <= end
            )
        )
    mean = sum(weeks) / len(weeks)
    if mean <= 0:
        return _factor(
            "Sales stability",
            0,
            STABILITY_MAX,
            None,
            "No sales in the recent weeks, so stability cannot be measured.",
        )
    cv = statistics.pstdev(weeks) / mean
    points = round(STABILITY_MAX * max(0.0, 1.0 - cv / STABILITY_CV_ZERO_POINTS))
    return _factor(
        "Sales stability",
        points,
        STABILITY_MAX,
        round(cv, 4),
        f"Weekly sales average KES {mean:,.0f} with a coefficient of variation of {cv:.2f}.",
    )


def factor_profitability(window_tx, as_of):
    """Net margin = (sales - purchases - expenses) / sales over the window."""
    del as_of  # the caller passes already-windowed transactions
    totals = _sum_by_type(window_tx)
    sales = totals.get("sale", 0)
    if sales <= 0:
        return _factor(
            "Profitability",
            0,
            PROFIT_MAX,
            0.0,
            "No sales in the last 60 days, so margin cannot be measured.",
        )
    margin = (sales - totals.get("purchase", 0) - totals.get("expense", 0)) / sales
    span = PROFIT_FULL_MARGIN - PROFIT_ZERO_MARGIN
    points = round(PROFIT_MAX * min(1.0, max(0.0, (margin - PROFIT_ZERO_MARGIN) / span)))
    return _factor(
        "Profitability",
        points,
        PROFIT_MAX,
        round(margin, 4),
        f"Net margin of {margin * 100:.1f}% over the last 60 days.",
    )


def factor_repayment(window_tx, as_of):
    """Share of credit given that was repaid within REPAY_WINDOW_DAYS.

    A repayment counts for an advance when it comes from the same customer
    after the advance and no later than REPAY_WINDOW_DAYS after it. Each
    advance is judged on its own, capped at fully repaid. A penalty applies
    when outstanding credit is high next to one month of sales.
    """
    advances = [tx for tx in window_tx if tx["type"] == "credit_given"]
    receipts = [tx for tx in window_tx if tx["type"] == "credit_received"]
    if not advances:
        return _factor(
            "Repayment behaviour",
            REPAY_MAX,
            REPAY_MAX,
            1.0,
            "No credit was given in the last 60 days, so there is nothing overdue.",
        )
    repaid_total = 0
    given_total = 0
    for advance in advances:
        given_total += advance["amount_kes"]
        given_day = _tx_date(advance)
        collected = sum(
            rx["amount_kes"]
            for rx in receipts
            if rx.get("counterparty") == advance.get("counterparty")
            and given_day < _tx_date(rx) <= given_day + timedelta(days=REPAY_WINDOW_DAYS)
        )
        repaid_total += min(collected, advance["amount_kes"])
    rate = repaid_total / given_total if given_total else 1.0
    points = round(REPAY_MAX * rate)
    month_start = as_of - timedelta(days=29)
    monthly_sales = sum(
        tx["amount_kes"]
        for tx in window_tx
        if tx["type"] == "sale" and _tx_date(tx) >= month_start
    )
    outstanding = max(
        0,
        sum(tx["amount_kes"] for tx in advances)
        - sum(tx["amount_kes"] for tx in receipts),
    )
    if monthly_sales > 0:
        ratio = outstanding / monthly_sales
    else:
        ratio = float("inf") if outstanding > 0 else 0.0
    if ratio > OUTSTANDING_PENALTY_THRESHOLD:
        points = max(0, points - OUTSTANDING_PENALTY_POINTS)
    return _factor(
        "Repayment behaviour",
        points,
        REPAY_MAX,
        round(rate, 4),
        f"{rate * 100:.1f}% of credit given was repaid within"
        f" {REPAY_WINDOW_DAYS} days; KES {outstanding:,} is still outstanding.",
    )


def factor_trend(window_tx, as_of):
    """Last 30 days of sales compared with the previous 30."""
    recent_start = as_of - timedelta(days=29)
    prev_start = as_of - timedelta(days=WINDOW_DAYS - 1)
    prev_end = as_of - timedelta(days=30)
    recent = sum(
        tx["amount_kes"]
        for tx in window_tx
        if tx["type"] == "sale" and _tx_date(tx) >= recent_start
    )
    previous = sum(
        tx["amount_kes"]
        for tx in window_tx
        if tx["type"] == "sale" and prev_start <= _tx_date(tx) <= prev_end
    )
    if previous <= 0:
        if recent > 0:
            return _factor(
                "Trend",
                TREND_MAX,
                TREND_MAX,
                None,
                "Sales restarted after a quiet spell.",
            )
        return _factor("Trend", 0, TREND_MAX, 0.0, "No sales in either 30-day stretch.")
    growth = (recent - previous) / previous
    points = round(TREND_MAX * min(1.0, max(0.0, growth / TREND_FULL_GROWTH)))
    return _factor(
        "Trend",
        points,
        TREND_MAX,
        round(growth, 4),
        f"Sales moved {growth * 100:+.1f}% from the previous 30 days.",
    )


def weekly_series(transactions, as_of):
    """Last 8 complete 7-day weeks, oldest first.

    Each week reports integer-KES sums of confirmed sales, purchases and
    expenses. Uses the same most-recent-56-days window as the stability
    factor so the chart and the score agree.
    """
    usable = _confirmed_upto(transactions, as_of)
    weeks = []
    for index in range(WEEKS_FOR_STABILITY):
        start = as_of - timedelta(days=55 - 7 * index)
        end = start + timedelta(days=6)
        totals = _sum_by_type(_in_window(usable, start, end))
        weeks.append(
            {
                "week_start": start.isoformat(),
                "sales_kes": int(totals.get("sale", 0)),
                "purchases_kes": int(totals.get("purchase", 0)),
                "expenses_kes": int(totals.get("expense", 0)),
            }
        )
    return weeks


def band_for_score(score):
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Ready"
    if score >= 40:
        return "Developing"
    return "Building"


def safe_loan_for_band(band, net_last_60, net_last_30):
    """Conservative loan from monthly net profit. Integer KES throughout.

    Takes the lower of the 60-day and last-30-day average monthly net
    profit (the 60-day figure is floored to whole shillings), allows at
    most REPAY_NUMERATOR/REPAY_DENOMINATOR of it as a monthly repayment,
    stretches that over LOAN_TERM_MONTHS, scales by band, and rounds the
    loan down to the nearest LOAN_ROUND_KES. A single floor division keeps
    the arithmetic exact: no float dust near the rounding boundary.
    """
    base = min(net_last_60 // 2, net_last_30)
    if base <= 0:
        return 0, 0
    num, den = BAND_SHARES[band]
    units = (
        base * REPAY_NUMERATOR * LOAN_TERM_MONTHS * num
    ) // (REPAY_DENOMINATOR * den * LOAN_ROUND_KES)
    loan = units * LOAN_ROUND_KES
    return loan, loan // LOAN_TERM_MONTHS if loan else 0


def pick_tips(factors):
    """One tip per factor with the most missing points, capped at three."""
    ranked = sorted(
        (f for f in factors if f["max_points"] - f["points"] > 0),
        key=lambda f: (f["max_points"] - f["points"], f["name"]),
        reverse=True,
    )
    return [TIPS[f["name"]] for f in ranked[:3]] or [
        "Records are strong across the board: keep recording every day."
    ]


def assess_shop(transactions, as_of):
    """Score one shop. Returns status, score, band, factors, loan and tips."""
    usable = _confirmed_upto(transactions, as_of)
    history_days = {_tx_date(tx) for tx in usable}
    if len(history_days) < MIN_HISTORY_DAYS or len(usable) < MIN_TRANSACTIONS:
        reasons = []
        if len(history_days) < MIN_HISTORY_DAYS:
            reasons.append(
                f"only {len(history_days)} days of history"
                f" (needs {MIN_HISTORY_DAYS})"
            )
        if len(usable) < MIN_TRANSACTIONS:
            reasons.append(
                f"only {len(usable)} confirmed transactions (needs {MIN_TRANSACTIONS})"
            )
        return {
            "status": "insufficient_data",
            "explanation": "Not enough history yet: " + " and ".join(reasons) + ".",
            "score": None,
            "band": None,
            "factors": [],
            "safe_loan_kes": 0,
            "monthly_repayment_kes": 0,
            "term_months": LOAN_TERM_MONTHS,
            "tips": [
                "Keep recording every confirmed sale until there are"
                f" {MIN_HISTORY_DAYS} days and {MIN_TRANSACTIONS} entries."
            ],
            "disclaimer": DISCLAIMER,
        }
    start = as_of - timedelta(days=WINDOW_DAYS - 1)
    window_tx = _in_window(usable, start, as_of)
    factors = [
        factor_regularity(window_tx, as_of),
        factor_stability(window_tx, as_of),
        factor_profitability(window_tx, as_of),
        factor_repayment(window_tx, as_of),
        factor_trend(window_tx, as_of),
    ]
    score = sum(f["points"] for f in factors)
    band = band_for_score(score)
    totals = _sum_by_type(window_tx)
    month_start = as_of - timedelta(days=29)
    month_tx = [tx for tx in window_tx if _tx_date(tx) >= month_start]
    month_totals = _sum_by_type(month_tx)
    net_60 = totals.get("sale", 0) - totals.get("purchase", 0) - totals.get("expense", 0)
    net_30 = (
        month_totals.get("sale", 0)
        - month_totals.get("purchase", 0)
        - month_totals.get("expense", 0)
    )
    loan, monthly = safe_loan_for_band(band, net_60, net_30)
    share_num, share_den = BAND_SHARES[band]
    return {
        "status": "scored",
        "explanation": (
            f"Scored {score}/100 from {len(usable)} confirmed transactions"
            f" across {len(history_days)} days."
        ),
        "score": score,
        "band": band,
        "factors": factors,
        "safe_loan_kes": loan,
        "monthly_repayment_kes": monthly,
        "term_months": LOAN_TERM_MONTHS,
        "loan_reasoning": (
            f"Lower of 60-day (KES {net_60 // 2:,}/month) and last-30-day"
            f" (KES {net_30:,}/month) net profit; repayments capped at"
            f" {REPAY_NUMERATOR}/{REPAY_DENOMINATOR} over {LOAN_TERM_MONTHS} months,"
            f" scaled by {share_num / share_den:g} for the {band} band"
            " and rounded down to the nearest KES 500."
        ),
        "tips": pick_tips(factors),
        "disclaimer": DISCLAIMER,
    }
