from __future__ import annotations

import pytest

from hoard.analytics import spending_by_friendly_category, spending_for_period


def test_no_transfer_dedup(make_account, make_txn):
    """A debit-card charge and a credit-card charge of the same amount on
    different days should both appear in spending totals."""
    make_account("chk", kind="depository", institution="Bank A")
    make_account("cc", kind="credit", institution="CreditCo", item_id="item2")
    make_txn("t1", "chk", "2026-06-10", 42.0, name="Debit Purchase",
             primary="GENERAL_MERCHANDISE")
    make_txn("t2", "cc", "2026-06-12", 42.0, name="Credit Purchase",
             primary="GENERAL_MERCHANDISE")

    rng = spending_for_period("2026-06")
    result = spending_by_friendly_category(*rng)
    merchandise = [c for c in result if c.category == "Merchandise"]
    assert len(merchandise) == 1
    assert merchandise[0].total == 84.0

def test_bilt_rent_counted_once(make_account, make_txn):
    """Bilt rent posts as a card charge and as a checking ACH pull that pays
    the card. Only the card charge is spending."""
    make_account("chk", kind="depository")
    make_account("bilt", kind="credit", item_id="item2", institution="Bilt")
    make_txn("t1", "bilt", "2026-06-02", 2350.0, name="Bilt Housing Payment",
             primary="RENT_AND_UTILITIES", detailed="RENT_AND_UTILITIES_RENT")
    make_txn("t2", "chk", "2026-06-02", 2350.0,
             name="BILT CARD DES:HOUSING ID: INDN:Someone CO ID:XXXXX00000 PPD",
             primary="RENT_AND_UTILITIES", detailed="RENT_AND_UTILITIES_RENT")

    result = spending_by_friendly_category(*spending_for_period("2026-06"))
    rent = [c for c in result if c.category == "Rent"]
    assert len(rent) == 1
    assert rent[0].total == 2350.0


def test_bank_side_card_payment_override_still_counts(make_account, make_txn):
    """A manual override on the bank-side debit means the user wants it counted."""
    make_account("chk", kind="depository")
    make_txn("t1", "chk", "2026-06-02", 500.0, name="BILT CARD DES:HOUSING",
             primary="RENT_AND_UTILITIES", override="Rent")

    result = spending_by_friendly_category(*spending_for_period("2026-06"))
    assert [(c.category, c.total) for c in result] == [("Rent", 500.0)]


def test_partial_first_month_excluded_from_average(make_account, make_txn):
    """History that starts mid-month shouldn't count that month as a full one."""
    from hoard.analytics import (
        _complete_month_range,
        average_monthly_spending_by_friendly_category,
    )

    make_account("cc", kind="credit")
    make_txn("t0", "cc", "2025-03-23", 30.0, primary="GENERAL_MERCHANDISE")
    make_txn("t1", "cc", "2025-04-10", 100.0, primary="GENERAL_MERCHANDISE")
    make_txn("t2", "cc", "2025-05-10", 200.0, primary="GENERAL_MERCHANDISE")

    start, end = _complete_month_range()
    assert start.isoformat() == "2025-04-01"
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    merch = [c for c in average_monthly_spending_by_friendly_category()
             if c.category == "Merchandise"][0]
    # The March 23 charge is outside the window: 300 not 330.
    assert merch.total == pytest.approx(300.0 / months)


def test_first_month_kept_when_history_starts_on_the_1st(make_account, make_txn):
    from hoard.analytics import _complete_month_range

    make_account("cc", kind="credit")
    make_txn("t0", "cc", "2025-03-01", 30.0, primary="GENERAL_MERCHANDISE")

    start, _ = _complete_month_range()
    assert start.isoformat() == "2025-03-01"


def test_average_count_is_per_month_like_the_total(make_account, make_txn, monkeypatch):
    """In "avg" mode the count and the total are on the same per-month basis."""
    import datetime as dt

    from hoard.analytics import (
        average_monthly_spending_by_friendly_category,
        average_period_info,
        spending,
    )

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 15)

    monkeypatch.setattr(spending, "date", FixedDate)
    make_account("cc", kind="credit")
    for i, month in enumerate(["01", "02", "03"]):
        for j in range(4):
            make_txn(f"t{i}{j}", "cc", f"2026-{month}-{j + 1:02d}", 25.0,
                     primary="GENERAL_MERCHANDISE")

    merch = [c for c in average_monthly_spending_by_friendly_category()
             if c.category == "Merchandise"][0]
    assert merch.total == pytest.approx(100.0)
    assert merch.transaction_count == 4

    assert average_period_info() == {
        "months": 3, "start": "2026-01", "end": "2026-03",
        "label": "3 months, Jan to Mar 2026",
    }
