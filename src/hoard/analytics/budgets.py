"""Budgets, merchant rules, per-transaction category overrides."""

from __future__ import annotations

from collections import defaultdict

from .. import categorize
from ..db import get_conn
from ..models import BudgetProgress
from .spending import (
    _complete_month_range,
    _count_complete_months,
    _load_merchant_rules,
    _row_friendly_category,
    _spend_rows,
    spending_for_period,
)


def budget_progress(period: str) -> list[BudgetProgress]:
    """Budget progress for a period token (``"avg"`` or ``"YYYY-MM"``).

    For ``"avg"``, ``spent_so_far`` is the average monthly spend over complete
    months, compared against the per-month ``monthly_limit``.
    """
    with get_conn() as conn:
        budgets = conn.execute("SELECT category, monthly_limit FROM budgets").fetchall()

        if period == "avg":
            rng = _complete_month_range(conn)
            if rng is None:
                rows: list = []
                months = 1
            else:
                months = max(_count_complete_months(rng), 1)
                rows = _spend_rows(conn, *rng)
        else:
            rng = spending_for_period(period)
            if rng is None:
                return []
            months = 1
            rows = _spend_rows(conn, *rng)

        rules = _load_merchant_rules(conn)

    spent_map: dict[str, float] = defaultdict(float)
    for r in rows:
        spent_map[_row_friendly_category(r, rules)] += float(r["amount"])

    out: list[BudgetProgress] = []
    for b in budgets:
        spent_raw = spent_map.get(b["category"], 0.0)
        spent = spent_raw / months if period == "avg" else spent_raw
        limit = float(b["monthly_limit"])
        out.append(
            BudgetProgress(
                category=b["category"],
                monthly_limit=limit,
                spent_so_far=spent,
                remaining=limit - spent,
                pct_used=(spent / limit * 100.0) if limit > 0 else 0.0,
            )
        )
    return out


def list_budgets() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()]


def upsert_budget(category: str, monthly_limit: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
            (category, monthly_limit),
        )


def delete_budget(category: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM budgets WHERE category = ?", (category,))


# --- Merchant rules (persistent vendor → category reclassify) ---------------


def list_merchant_rules() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT merchant_pattern, target_category "
                "FROM merchant_rules ORDER BY merchant_pattern"
            ).fetchall()
        ]


def upsert_merchant_rule(merchant_pattern: str, target_category: str) -> None:
    pattern = merchant_pattern.strip().lower()
    if not pattern:
        raise ValueError("merchant_pattern must not be empty")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO merchant_rules (merchant_pattern, target_category) VALUES (?, ?) "
            "ON CONFLICT(merchant_pattern) DO UPDATE SET target_category=excluded.target_category",
            (pattern, target_category),
        )


def delete_merchant_rule(merchant_pattern: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM merchant_rules WHERE merchant_pattern = ?",
            (merchant_pattern.strip().lower(),),
        )


# --- Per-transaction override -----------------------------------------------


def set_transaction_override(transaction_id: str, override_category: str | None) -> None:
    """Set or clear (None) the per-transaction category override."""
    with get_conn() as conn:
        if override_category:
            conn.execute(
                "UPDATE transactions SET override_category = ? WHERE transaction_id = ?",
                (override_category, transaction_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET override_category = NULL WHERE transaction_id = ?",
                (transaction_id,),
            )


def friendly_category_options() -> list[str]:
    """Distinct friendly category labels currently in play — the static set
    from ``FRIENDLY_CATEGORY_RULES`` plus any in use via budgets/overrides/
    merchant rules. Used to populate the reclassify dropdown."""
    static = sorted({label for label, _ in categorize.FRIENDLY_CATEGORY_RULES})
    with get_conn() as conn:
        used = {
            r["target_category"]
            for r in conn.execute("SELECT DISTINCT target_category FROM merchant_rules")
        }
        used.update(
            r["override_category"]
            for r in conn.execute(
                "SELECT DISTINCT override_category FROM transactions "
                "WHERE override_category IS NOT NULL"
            )
        )
        used.update(r["category"] for r in conn.execute("SELECT DISTINCT category FROM budgets"))
    return sorted(set(static) | used)
