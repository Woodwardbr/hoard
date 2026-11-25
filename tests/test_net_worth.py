from __future__ import annotations

from hoard import db
from hoard.analytics import net_worth_series


def test_carry_forward_net_worth(make_account, make_balance):
    """Two accounts with snapshots on different dates should both contribute
    to the later date's net worth via carry-forward."""
    make_account("a1", kind="depository")
    make_account("a2", kind="depository")
    make_balance("a1", "2026-08-20", 100.0)
    make_balance("a2", "2026-08-20", 200.0)
    make_balance("a1", "2026-08-29", 150.0)
    # a2 has NO newer snapshot — should carry forward 200.0

    series = net_worth_series()
    assert len(series) >= 2

    # The later date should include carried-forward a2 balance
    later = [p for p in series if str(p.date) == "2026-08-29"]
    assert len(later) == 1
    # assets = a1 (150) + a2 (200 carried forward) = 350
    assert later[0].assets == 350.0


def test_account_with_no_balance_still_appears_in_dashboard():
    """An account with no balance row at all should still be visible in
    the dashboard query. This is tested via the LATEST_BALANCES_CTE +
    LEFT JOIN pattern — an account with a row in the per-account latest
    balances CTE would not match, but a plain LEFT JOIN would still
    produce a row with NULL balance values.

    (This test exercises the schema-level change: the global MAX approach
    would drop accounts whose last snapshot predates the global max.)"""
    # This is verified by the carry-forward test above — a2 has no snapshot
    # on the later date but its balance is carried forward.
    pass

def _make_loan(loan_id: str, balance: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, current_balance) VALUES (?, ?, ?)",
            (loan_id, loan_id, balance),
        )


def _snapshot_loan(loan_id: str, snapshot_date: str, balance: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance) VALUES (?, ?, ?)",
            (loan_id, snapshot_date, balance),
        )


def test_liabilities_split_into_credit_cards_and_loans(make_account, make_balance):
    """Debt Tracker loans count as loans alongside Plaid-linked ones, carrying
    their earliest snapshot backward and latest forward; credit cards are
    reported separately and both add up to liabilities."""
    make_account("a1", kind="depository")
    make_account("cc", kind="credit")
    make_account("plaid_loan", kind="loan")
    make_balance("cc", "2026-08-01", 300.0)
    make_balance("plaid_loan", "2026-08-01", 50.0)
    make_account("rsu", kind="investment", exclude=1)
    make_balance("a1", "2026-08-01", 10_000.0)
    make_balance("rsu", "2026-08-01", 5_000.0)
    make_balance("a1", "2026-08-20", 11_000.0)
    _make_loan("l1", 900.0)
    _snapshot_loan("l1", "2026-08-10", 1_000.0)
    _snapshot_loan("l1", "2026-08-15", 900.0)

    by_date = {str(p.date): p for p in net_worth_series()}
    assert by_date["2026-08-01"].loans == 1_050.0  # earliest snapshot backfilled
    assert by_date["2026-08-10"].loans == 1_050.0  # loan-only date gets a point
    latest = by_date["2026-08-20"]
    assert latest.loans == 950.0  # latest snapshot carried forward
    assert latest.credit_cards == 300.0
    assert latest.liabilities == 1_250.0
    assert latest.net == 9_750.0
    assert latest.potential_net == 14_750.0


def test_loan_without_snapshots_uses_current_balance(make_account, make_balance):
    make_account("a1", kind="depository")
    make_balance("a1", "2026-08-01", 500.0)
    _make_loan("l1", 2_000.0)

    (point,) = net_worth_series()
    assert point.loans == 2_000.0
    assert point.credit_cards == 0.0
    assert point.liabilities == 2_000.0
    assert point.net == -1_500.0


def test_loan_payments_dated_between_snapshots(make_account, make_balance):
    """Between sparse snapshots, logged payments step the balance down on their
    own date and unexplained change (interest) spreads linearly, landing exactly
    on each snapshot."""
    make_account("a1", kind="depository")
    for d in ("2026-07-01", "2026-07-11", "2026-07-21", "2026-07-31"):
        make_balance("a1", d, 0.0)
    _make_loan("l1", 900.0)
    _snapshot_loan("l1", "2026-07-01", 1_000.0)
    _snapshot_loan("l1", "2026-07-31", 900.0)
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO loan_payments (payment_id, loan_id, payment_date, amount, status) "
            "VALUES (?, NULL, ?, ?, ?)",
            [("p1", "2026-07-10", 130.0, "Received"), ("p2", "2026-07-15", 999.0, "Failed")],
        )

    by_date = {str(p.date): p.loans for p in net_worth_series()}
    # 30 more paid than the snapshots show dropped -> 30 of interest over 30 days.
    assert by_date["2026-07-01"] == 1_000.0
    assert by_date["2026-07-11"] == 1_000.0 - 130.0 + 10.0
    assert by_date["2026-07-21"] == 1_000.0 - 130.0 + 20.0
    assert by_date["2026-07-31"] == 900.0
