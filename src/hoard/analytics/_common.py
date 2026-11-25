"""Shared constants and SQL fragments for the analytics package."""

from __future__ import annotations

ASSET_KINDS = {"depository", "investment"}
LIABILITY_KINDS = {"credit", "loan"}

# Every account's most recent balance row, regardless of whether other accounts
# were synced more recently. Prepend to a query and select FROM latest_balances
# in place of `balances`.
LATEST_BALANCES_CTE = """
WITH latest_balances AS (
    SELECT b.*
    FROM balances b
    JOIN (
        SELECT account_id, MAX(snapshot_date) AS snapshot_date
        FROM balances
        GROUP BY account_id
    ) m ON m.account_id = b.account_id AND m.snapshot_date = b.snapshot_date
)
"""

# Categories that represent balance-sheet shifts (cash → liability reduction,
# transfers between own accounts, ATM withdrawals) rather than consumption.
# Excluded from spending totals to avoid double-counting credit-card payments
# that are already captured on the credit-card side as purchases.
EXCLUDED_SPEND_CATEGORIES = frozenset(
    {
        "LOAN_PAYMENTS",
        "TRANSFER_OUT",
        "TRANSFER_IN",
        "INCOME",
    }
)

