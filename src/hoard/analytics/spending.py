"""Spending by category, budget-period helpers, month-vs-average, subscriptions."""

from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from .. import categorize
from ..db import get_conn
from ..models import (
    CategoryComparison,
    CategorySpend,
    MonthComparison,
    Subscription,
    TransactionDetail,
)
from ._common import EXCLUDED_SPEND_CATEGORIES


def _effective_category(override: str | None, primary: str | None) -> str:
    return (override or primary or "Uncategorized") or "Uncategorized"


# Bank-account debits that settle a credit card, matched by lowercase name prefix.
# Plaid sometimes tags these as the card charge's category (e.g. Bilt's housing
# ACH pull is tagged RENT_AND_UTILITIES_RENT), so the same rent would be counted
# once on the card and again on checking. The card-side charge is the real spend.
BANK_SIDE_CARD_PAYMENT_PREFIXES: tuple[str, ...] = ("bilt card des:",)


def _is_bank_side_card_payment(r: sqlite3.Row | dict) -> bool:
    """True for a checking/savings debit that pays off a credit card. A manual
    override always wins, so a user can still count one of these as spend."""
    if r["override_category"] or r["account_kind"] != "depository":
        return False
    name = (r["name"] or "").lower()
    return name.startswith(BANK_SIDE_CARD_PAYMENT_PREFIXES)


def _spend_rows(conn, start: date, end: date) -> list[dict]:
    """Transactions representing true consumption in [start, end].

    Excludes balance-sheet-shift categories (LOAN_PAYMENTS, TRANSFER_OUT, etc.)
    so credit-card payments and ATM withdrawals don't inflate spending totals.
    """
    rows = conn.execute(
        """
        SELECT t.transaction_id, t.account_id, t.date, t.amount,
               t.name, t.merchant, t.category_primary, t.category_detailed,
               t.override_category, a.kind AS account_kind, a.name AS account_name
        FROM transactions t
        JOIN accounts a USING (account_id)
        WHERE t.date >= ? AND t.date <= ? AND t.amount > 0
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    filtered = [
        r
        for r in rows
        if _effective_category(r["override_category"], r["category_primary"])
        not in EXCLUDED_SPEND_CATEGORIES
        and not _is_bank_side_card_payment(r)
    ]

    return [dict(r) for r in filtered]


def _load_merchant_rules(conn) -> list[tuple[str, str]]:
    """Load all (merchant_pattern, target_category) rules. Patterns are stored
    lowercase; matching is substring against merchant or name (lowercased)."""
    return [
        (r["merchant_pattern"], r["target_category"])
        for r in conn.execute("SELECT merchant_pattern, target_category FROM merchant_rules")
    ]


def _row_friendly_category(r: dict, merchant_rules: list[tuple[str, str]] | None = None) -> str:
    """Friendly category for a spend row.

    Priority (highest first):
      1. per-transaction ``override_category`` (manual one-off reclassify)
      2. merchant rules (persistent vendor → category reclassify)
      3. ``categorize.friendly_category`` (keyword + Plaid mapping)
    """
    if r.get("override_category"):
        return r["override_category"]
    if merchant_rules:
        haystack_parts = [p for p in (r.get("merchant"), r.get("name")) if p]
        haystack = " ".join(haystack_parts).lower()
        for pattern, target in merchant_rules:
            if pattern in haystack:
                return target
    return categorize.friendly_category(
        primary=r.get("category_primary"),
        detailed=r.get("category_detailed"),
        merchant=r.get("merchant"),
        name=r.get("name"),
    )


def spending_by_category(start: date, end: date) -> list[CategorySpend]:
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _effective_category(r.get("override_category"), r.get("category_primary"))
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    return [
        CategorySpend(category=cat, total=agg["total"], transaction_count=agg["n"])
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def spending_by_friendly_category(start: date, end: date) -> list[CategorySpend]:
    """Spending grouped by user-facing friendly categories (Tesla Charging,
    Subscriptions, Restaurants, Groceries, Rent, etc.).

    Uses the same exclusions and transfer-dedup logic as the raw view so
    credit-card payments and inter-account transfers aren't counted twice.
    """
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _row_friendly_category(r, rules)
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    return [
        CategorySpend(category=cat, total=agg["total"], transaction_count=agg["n"])
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def transactions_for_friendly_category(
    start: date, end: date, category: str
) -> list[TransactionDetail]:
    """Return the individual transactions that fell into a friendly category
    in [start, end]. Mirrors the exclusion/dedup logic of `_spend_rows` so the
    list matches the totals shown on the budgets page.
    """
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    out: list[TransactionDetail] = []
    for r in rows:
        if _row_friendly_category(r, rules) != category:
            continue
        out.append(
            TransactionDetail(
                transaction_id=r["transaction_id"],
                date=date.fromisoformat(r["date"]),
                name=r.get("name") or "",
                merchant=r.get("merchant"),
                amount=float(r["amount"]),
                category_detailed=r.get("category_detailed"),
                account_name=r.get("account_name"),
            )
        )
    out.sort(key=lambda t: t.date, reverse=True)
    return out


# --- Budget period picker ---------------------------------------------------

_MONTH_LABELS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_end(d: date) -> date:
    """Last calendar day of d's month."""
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


def _add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def _complete_month_range(conn=None) -> tuple[date, date] | None:
    """(start, end) covering all complete months in the data window.

    Start is the first month whose 1st is covered by the data. Plaid history
    begins on an arbitrary day, so if the earliest transaction isn't on the 1st
    that month is partial and skipped; otherwise a few days of data would count
    as a full month and drag every average down. The current calendar month is
    never complete. Returns None if there are no complete months yet.
    """
    today = date.today()
    first_incomplete = date(today.year, today.month, 1)
    last_end = first_incomplete - timedelta(days=1)

    if conn is not None:
        row = conn.execute("SELECT MIN(date) AS d FROM transactions").fetchone()
    else:
        with get_conn() as cn:
            row = cn.execute("SELECT MIN(date) AS d FROM transactions").fetchone()
    if not row or not row["d"]:
        return None
    first_day = date.fromisoformat(row["d"][:10])
    earliest = first_day if first_day.day == 1 else _add_months(first_day, 1)

    if earliest > last_end:
        return None
    return earliest, last_end


def _count_complete_months(rng: tuple[date, date]) -> int:
    """Number of calendar months spanned by [start, end] (inclusive)."""
    start, end = rng
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def average_period_info() -> dict | None:
    """The window behind the ``"avg"`` period, e.g.
    ``{"months": 3, "start": "2026-06", "end": "2026-08", "label": "3 months, Jun to Aug 2026"}``.
    None if there are no complete months yet.
    """
    rng = _complete_month_range()
    if rng is None:
        return None
    start, end = rng
    months = _count_complete_months(rng)
    short = lambda d: _MONTH_LABELS[d.month - 1][:3]  # noqa: E731
    if months == 1:
        span = f"{short(start)} {start.year}"
    elif start.year == end.year:
        span = f"{short(start)} to {short(end)} {end.year}"
    else:
        span = f"{short(start)} {start.year} to {short(end)} {end.year}"
    return {
        "months": months,
        "start": f"{start:%Y-%m}",
        "end": f"{end:%Y-%m}",
        "label": f"{months} month{'s' if months != 1 else ''}, {span}",
    }


def spending_months() -> list[dict]:
    """Distinct YYYY-MM periods present in the data (descending).

    Returns ``[{"period": "2026-06", "label": "June 2026"}, ...]``. Used to
    populate the budget period dropdown.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) AS ym "
            "FROM transactions ORDER BY ym DESC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        ym = r["ym"]
        try:
            y, m = int(ym[:4]), int(ym[5:7])
        except (ValueError, IndexError):
            continue
        if not 1 <= m <= 12:
            continue
        out.append({"period": ym, "label": f"{_MONTH_LABELS[m - 1]} {y}"})
    return out


def spending_for_period(period: str) -> tuple[date, date] | None:
    """(start, end) date range for a period token.

    - ``"avg"``: spans every complete month in the data window (current month
      excluded).
    - ``"YYYY-MM"``: that calendar month.
    Returns None for an unparseable/empty period.
    """
    if period == "avg":
        return _complete_month_range()
    try:
        y, m = int(period[:4]), int(period[5:7])
        start = date(y, m, 1)
        end = _month_end(start)
        return start, end
    except (ValueError, IndexError):
        return None


def average_monthly_spending_by_friendly_category() -> list[CategorySpend]:
    """Average monthly spend per friendly category across complete months.

    Divides each category's total over the complete-month window by the number
    of months in that window (including zero-spend months, so a category that
    only appeared once doesn't get inflated).
    """
    rng = _complete_month_range()
    if rng is None:
        return []
    start, end = rng
    months = _count_complete_months(rng)
    if months <= 0:
        return []

    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _row_friendly_category(r, rules)
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    # Both columns per month: an averaged total beside a multi-month count
    # misread as "$120 over 45 transactions".
    return [
        CategorySpend(
            category=cat,
            total=agg["total"] / months,
            transaction_count=round(agg["n"] / months),
        )
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def month_vs_average(period: str, top_n: int = 10) -> MonthComparison | None:
    """Spending per friendly category in ``period`` (``YYYY-MM``) next to the
    average over every *other* complete month, plus the month's largest
    transactions.

    The month under review is left out of the baseline so an unusual month
    doesn't pull its own average toward itself. Returns None for an
    unparseable period.
    """
    rng = spending_for_period(period)
    if rng is None or period == "avg":
        return None
    start, end = rng
    baseline = _complete_month_range()

    with get_conn() as conn:
        month_rows = _spend_rows(conn, start, end)
        baseline_rows = _spend_rows(conn, *baseline) if baseline else []
        rules = _load_merchant_rules(conn)

    in_month = (start.isoformat(), end.isoformat())
    baseline_rows = [r for r in baseline_rows if not in_month[0] <= r["date"][:10] <= in_month[1]]
    baseline_months = _count_complete_months(baseline) if baseline else 0
    if baseline and baseline[0] <= start <= baseline[1]:
        baseline_months -= 1

    spent: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in month_rows:
        cat = _row_friendly_category(r, rules)
        spent[cat]["total"] += float(r["amount"])
        spent[cat]["n"] += 1
    base_totals: dict[str, float] = defaultdict(float)
    for r in baseline_rows:
        base_totals[_row_friendly_category(r, rules)] += float(r["amount"])

    categories = []
    for cat in set(spent) | set(base_totals):
        total = spent[cat]["total"] if cat in spent else 0.0
        avg = base_totals[cat] / baseline_months if baseline_months else 0.0
        categories.append(
            CategoryComparison(
                category=cat,
                spent=total,
                transaction_count=spent[cat]["n"] if cat in spent else 0,
                avg_monthly=avg,
                difference=total - avg,
                pct_of_avg=total / avg * 100.0 if avg > 0 else None,
            )
        )
    categories.sort(key=lambda c: -c.difference)

    largest = sorted(month_rows, key=lambda r: -float(r["amount"]))[:top_n]
    return MonthComparison(
        period=period,
        month_complete=end < date.today().replace(day=1),
        baseline_months=baseline_months,
        categories=categories,
        largest_transactions=[
            {
                "date": r["date"][:10],
                "amount": float(r["amount"]),
                "merchant": r["merchant"] or r["name"],
                "category": _row_friendly_category(r, rules),
                "account": r["account_name"],
            }
            for r in largest
        ],
    )


def transactions_for_period_category(
    period: str, category: str
) -> list[TransactionDetail]:
    """Transactions matching a friendly category for a period token.

    - ``"avg"``: union of matching transactions across all complete months.
    - ``"YYYY-MM"``: that month's transactions.
    """
    rng = spending_for_period(period)
    if rng is None:
        return []
    start, end = rng
    return transactions_for_friendly_category(start, end, category)

# --- Subscription detection ------------------------------------------------

_SUB_MIN_OCCURRENCES = 3
_SUB_INTERVAL_TOL_DAYS = 7       # cadence must be consistent within ±1 week
_SUB_AMOUNT_TOL_PCT = 0.10       # amounts within ±10% of the median
_SUB_MIN_INTERVAL_DAYS = 20     # ignore near-daily repeats (e.g. coffee)
_SUB_MAX_INTERVAL_DAYS = 45


def _subscription_label(r: sqlite3.Row) -> str:
    """Normalized grouping key: prefer merchant, fall back to name."""
    raw = r["merchant"] or r["name"]
    return " ".join(raw.lower().split())


def detect_subscriptions() -> list[Subscription]:
    """Heuristic detection of recurring charges.

    Groups transactions by normalized merchant/name, then flags a group as a
    subscription if it has >= 3 occurrences with consistent monthly cadence
    (20–45 day spacing, ±7 day tolerance) and amounts within ±10% of the
    median.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT transaction_id, date, amount, name, merchant,
                   category_primary, category_detailed, override_category
            FROM transactions
            WHERE amount > 0
            ORDER BY date
            """
        ).fetchall()
        rules = _load_merchant_rules(conn)

    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[_subscription_label(r)].append(r)

    subs: list[Subscription] = []
    for label, group in groups.items():
        if len(group) < _SUB_MIN_OCCURRENCES:
            continue

        # Sort by date and compute intervals (days) between consecutive occurrences.
        group_sorted = sorted(group, key=lambda r: r["date"])
        dates = [date.fromisoformat(r["date"]) for r in group_sorted]
        intervals = [
            (dates[i] - dates[i - 1]).days for i in range(1, len(dates))
        ]
        if not intervals:
            continue

        # Check cadence: each interval must fall in the monthly band and be
        # consistent with the median interval.
        median_interval = sorted(intervals)[len(intervals) // 2]
        if not (_SUB_MIN_INTERVAL_DAYS <= median_interval <= _SUB_MAX_INTERVAL_DAYS):
            continue
        if any(abs(iv - median_interval) > _SUB_INTERVAL_TOL_DAYS for iv in intervals):
            continue

        # Amount consistency.
        amounts = sorted(float(r["amount"]) for r in group_sorted)
        med_amt = amounts[len(amounts) // 2]
        if med_amt <= 0:
            continue
        if any(abs(a - med_amt) / med_amt > _SUB_AMOUNT_TOL_PCT for a in amounts):
            continue

        # Friendly category for the representative row (most recent).
        rep = group_sorted[-1]
        cat = _row_friendly_category(dict(rep), rules)

        subs.append(
            Subscription(
                label=label,
                amount=med_amt,
                occurrences=len(group_sorted),
                last_date=dates[-1],
                avg_interval_days=sum(intervals) / len(intervals),
                category=cat,
            )
        )

    subs.sort(key=lambda s: s.amount, reverse=True)
    return subs
