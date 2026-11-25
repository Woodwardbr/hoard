"""Debt Tracker: manually tracked loans, payments, balance history, payoff projection."""

from __future__ import annotations

import calendar
import sqlite3
import uuid
from datetime import date

from ..db import get_conn
from ..models import (
    DebtLoan,
    LoanPayment,
)
from .networth import _tracked_loan_totals
from .spending import _add_months

# --- Debt Tracker: manually-tracked loans -----------------------------------


def roll_due_date_forward(due: date, today: date) -> date:
    """Advance a monthly due date by whole months until it is on or after ``today``.

    Keeps the original day of month, clamped to short months (Jan 31 -> Feb 28
    -> Mar 31).
    """
    months = 0
    rolled = due
    while rolled < today:
        months += 1
        total = due.month - 1 + months
        year, month = due.year + total // 12, total % 12 + 1
        rolled = due.replace(
            year=year, month=month, day=min(due.day, calendar.monthrange(year, month)[1])
        )
    return rolled


def _row_to_loan(r: sqlite3.Row) -> DebtLoan:
    next_due = date.fromisoformat(r["next_due_date"]) if r["next_due_date"] else None
    # Autopay loans are paid on schedule, so a stored due date in the past just
    # means nobody updated it; show the next occurrence instead. Manual-pay
    # loans keep the stored date so a missed payment still looks overdue.
    if next_due is not None and r["auto_pay"]:
        next_due = roll_due_date_forward(next_due, date.today())
    return DebtLoan(
        loan_id=r["loan_id"],
        name=r["name"],
        loan_type=r["loan_type"],
        interest_rate=r["interest_rate"],
        min_payment=r["min_payment"],
        current_balance=r["current_balance"],
        next_due_date=next_due,
        auto_pay=bool(r["auto_pay"]),
        status=r["status"],
        notes=r["notes"],
    )


def list_loans() -> list[DebtLoan]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM loans ORDER BY name").fetchall()
    return [_row_to_loan(r) for r in rows]


def upsert_loan(
    loan_id: str | None,
    name: str,
    loan_type: str | None,
    interest_rate: float | None,
    min_payment: float | None,
    current_balance: float | None,
    next_due_date: date | None,
    auto_pay: bool,
    status: str,
    notes: str | None,
) -> str:
    """Insert or update a loan. On a balance change, record a snapshot row.

    Returns the loan_id (newly generated if None was passed).
    """

    with get_conn() as conn:
        is_new = not loan_id
        if is_new:
            loan_id = uuid.uuid4().hex[:12]

        if not is_new:
            prev = conn.execute(
                "SELECT current_balance FROM loans WHERE loan_id = ?", (loan_id,)
            ).fetchone()
            prev_balance = prev["current_balance"] if prev else None
        else:
            prev_balance = None

        conn.execute(
            """
            INSERT INTO loans
              (loan_id, name, loan_type, interest_rate, min_payment,
               current_balance, next_due_date, auto_pay, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(loan_id) DO UPDATE SET
                name=excluded.name, loan_type=excluded.loan_type,
                interest_rate=excluded.interest_rate, min_payment=excluded.min_payment,
                current_balance=excluded.current_balance,
                next_due_date=excluded.next_due_date, auto_pay=excluded.auto_pay,
                status=excluded.status, notes=excluded.notes,
                updated_at=datetime('now')
            """,
            (
                loan_id, name, loan_type, interest_rate, min_payment,
                current_balance, next_due_date.isoformat() if next_due_date else None,
                int(auto_pay), status, notes,
            ),
        )

        # Record a balance snapshot whenever the balance actually moves.
        # Skip when current_balance is NULL (user hasn't entered it yet).
        if current_balance is not None and current_balance != prev_balance:
            _snapshot_loan_balance(conn, loan_id, current_balance)
    return loan_id


def _snapshot_loan_balance(conn: sqlite3.Connection, loan_id: str, balance: float) -> None:
    conn.execute(
        """
        INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance)
        VALUES (?, ?, ?)
        ON CONFLICT(loan_id, snapshot_date) DO UPDATE SET balance=excluded.balance
        """,
        (loan_id, date.today().isoformat(), balance),
    )


# Columns update_loans() may change; anything else is rejected.
LOAN_UPDATABLE_FIELDS = frozenset(
    {"current_balance", "min_payment", "next_due_date", "interest_rate", "status"}
)


def update_loans(updates: list[tuple[str, dict]]) -> list[DebtLoan]:
    """Apply partial updates to existing loans in one transaction.

    Each update is ``(loan_id, {column: value})`` with columns from
    ``LOAN_UPDATABLE_FIELDS``. Only the given columns change. A balance change
    is snapshotted like an edit in the Debt Tracker. Raises ``LookupError`` for
    an unknown loan_id, in which case nothing is written.
    """
    with get_conn() as conn:
        for loan_id, fields in updates:
            unknown = set(fields) - LOAN_UPDATABLE_FIELDS
            if unknown:
                raise ValueError(f"Not updatable: {sorted(unknown)}")
            prev = conn.execute(
                "SELECT current_balance FROM loans WHERE loan_id = ?", (loan_id,)
            ).fetchone()
            if prev is None:
                raise LookupError(loan_id)
            if not fields:
                continue
            values = {
                k: v.isoformat() if isinstance(v, date) else v for k, v in fields.items()
            }
            # Column names come from LOAN_UPDATABLE_FIELDS, never from input.
            assignments = ", ".join(f"{col}=?" for col in values)
            conn.execute(
                f"UPDATE loans SET {assignments}, updated_at=datetime('now') WHERE loan_id=?",
                (*values.values(), loan_id),
            )
            balance = fields.get("current_balance")
            if balance is not None and balance != prev["current_balance"]:
                _snapshot_loan_balance(conn, loan_id, balance)
        ids = [loan_id for loan_id, _ in updates]
        rows = conn.execute(
            f"SELECT * FROM loans WHERE loan_id IN ({', '.join('?' * len(ids))})", ids
        ).fetchall()
    by_id = {r["loan_id"]: _row_to_loan(r) for r in rows}
    return [by_id[loan_id] for loan_id in ids]


def delete_loan(loan_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM loans WHERE loan_id = ?", (loan_id,))


def _row_to_payment(r: sqlite3.Row) -> LoanPayment:
    return LoanPayment(
        payment_id=r["payment_id"],
        loan_id=r["loan_id"],
        payment_date=date.fromisoformat(r["payment_date"]),
        amount=r["amount"],
        status=r["status"],
        source=r["source"],
        notes=r["notes"],
    )


def list_loan_payments() -> list[LoanPayment]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM loan_payments ORDER BY payment_date DESC, created_at DESC"
        ).fetchall()
    return [_row_to_payment(r) for r in rows]


def record_loan_payment(
    payment_date: date,
    amount: float,
    loan_id: str | None = None,
    status: str = "Received",
    source: str | None = None,
    notes: str | None = None,
    new_balance: float | None = None,
    loan_status: str | None = None,
) -> str:
    """Log a payment. When loan_id + new_balance are provided, also update that
    loan's current_balance and snapshot it; loan_id + loan_status sets the
    loan's status. All writes share one transaction. A NULL loan_id means a
    combined payment across all loans (logged for history but not auto-split)."""

    payment_id = uuid.uuid4().hex[:12]
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO loan_payments
              (payment_id, loan_id, payment_date, amount, status, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, loan_id, payment_date.isoformat(), amount, status, source, notes),
        )
        if loan_id and new_balance is not None:
            conn.execute(
                "UPDATE loans SET current_balance=?, updated_at=datetime('now') WHERE loan_id=?",
                (new_balance, loan_id),
            )
            conn.execute(
                """
                INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(loan_id, snapshot_date) DO UPDATE SET balance=excluded.balance
                """,
                (loan_id, payment_date.isoformat(), new_balance),
            )
        if loan_id and loan_status is not None:
            conn.execute(
                "UPDATE loans SET status=?, updated_at=datetime('now') WHERE loan_id=?",
                (loan_status, loan_id),
            )
    return payment_id


def delete_loan_payment(payment_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM loan_payments WHERE payment_id = ?", (payment_id,))


def loan_balance_history() -> list[dict]:
    """Total loan balance over time, ascending: one point per snapshot date and
    per payment date up to the latest snapshot.

    Every balance snapshot is an exact anchor. Between two snapshots the total
    steps down on each payment and the unexplained remainder (mostly accrued
    interest) is spread linearly, via ``_tracked_loan_totals`` (the same model
    the net-worth chart uses). Before the first snapshot, payments are added
    back so the line still shows the payoff trend; interest isn't modeled
    there, so those early points are a lower bound. Payments after the latest
    snapshot aren't plotted: the latest snapshot is the current balance.
    """
    with get_conn() as conn:
        snap_dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM loan_balance_snapshots ORDER BY 1"
            )
        ]
        if not snap_dates:
            return []
        first, last = snap_dates[0], snap_dates[-1]
        payments = [
            (r[0], float(r[1] or 0.0))
            for r in conn.execute(
                "SELECT payment_date, amount FROM loan_payments WHERE status != 'Failed'"
            )
        ]
        dates = sorted({d for d, _ in payments if d <= last} | set(snap_dates))
        totals = _tracked_loan_totals(conn, dates)

    points: list[dict] = []
    for d in dates:
        total = totals[d]
        if d < first:
            # Balance at end of day d = first snapshot + payments made after d.
            total += sum(amt for pd, amt in payments if d < pd <= first)
        points.append({"date": d, "total": round(total, 2)})
    return points


PAYOFF_STRATEGIES = ("distributed", "highest_interest", "lowest_balance")


def project_payoff(
    extra_monthly: float = 0.0,
    extra_onetime: float = 0.0,
    strategy: str = "distributed",
) -> dict:
    """Forward projection of total loan balance under a payoff strategy.

    Strategies:
      - ``distributed`` (proportional): split the extra payment across
        projectable loans proportional to each loan's balance share. This is
        the historical default.
      - ``highest_interest`` (avalanche): direct the full extra payment at
        the highest-interest-rate loan each month.
      - ``lowest_balance`` (snowball): direct the full extra payment at the
        smallest-balance loan each month.

    When a loan pays off, its minimum payment rolls into the extra-payment
    pool for the remaining loans (all strategies), accelerating payoff.

    Returns a dict with:
      - schedule: list of {month_offset, date, total_balance} points
      - months_to_payoff: month offset when total hits 0 (or None)
      - payoff_date: ISO date string (or None if not within max_months)
      - total_interest: sum of interest paid across all projectable loans
      - unprojectable: list of loan names missing rate/min/balance

    Loans missing interest_rate, min_payment, or current_balance are skipped
    (reported in `unprojectable`) since we can't amortize them.
    """
    if strategy not in PAYOFF_STRATEGIES:
        raise ValueError(
            f"unknown strategy {strategy!r}; expected one of {PAYOFF_STRATEGIES}"
        )

    loans = list_loans()
    projectable = [
        ln for ln in loans
        if ln.current_balance is not None
        and ln.interest_rate is not None
        and ln.min_payment is not None
        and ln.current_balance > 0
    ]
    projectable_ids = {ln.loan_id for ln in projectable}
    unprojectable = [ln.name for ln in loans if ln.loan_id not in projectable_ids]

    if not projectable:
        return {
            "schedule": [],
            "months_to_payoff": None,
            "payoff_date": None,
            "total_interest": 0.0,
            "unprojectable": unprojectable,
        }

    # Per-loan mutable state for the simulation.
    state = [
        {
            "name": ln.name,
            "balance": float(ln.current_balance or 0.0),
            "rate": float(ln.interest_rate or 0.0),
            "monthly_rate": (float(ln.interest_rate or 0.0) / 100.0) / 12.0,
            "min_payment": float(ln.min_payment or 0.0),
            "interest_paid": 0.0,
            "schedule": [round(float(ln.current_balance or 0.0), 2)],  # index 0 = start
        }
        for ln in projectable
    ]
    # Apply one-time lump payments up front (proportional to balance, matching
    # the historical "distributed" behaviour for the lump sum across all
    # strategies — the strategy only governs the recurring extra).
    total_balance = sum(s["balance"] for s in state)
    if extra_onetime > 0 and total_balance > 0:
        for s in state:
            share = s["balance"] / total_balance
            s["balance"] = max(s["balance"] - extra_onetime * share, 0.0)
            s["schedule"][0] = round(s["balance"], 2)

    max_months = 600
    today = date.today()
    total_schedule: list[dict] = [{
        "month_offset": 0,
        "date": today.isoformat(),
        "total_balance": round(sum(s["balance"] for s in state), 2),
    }]

    if total_schedule[0]["total_balance"] < 0.01:
        return {
            "schedule": total_schedule,
            "months_to_payoff": 0,
            "payoff_date": today.isoformat(),
            "total_interest": 0.0,
            "unprojectable": unprojectable,
        }

    months_to_payoff: int | None = None

    for m in range(1, max_months + 1):
        # Pool of extra payment available this month: the user's extra_monthly
        # plus the freed-up minimums from any loans paid off in prior months.
        freed_min = sum(
            s["min_payment"] for s in state if s["balance"] <= 0.005
        )
        extra_pool = extra_monthly + freed_min

        # Order the still-active loans for this month's extra allocation.
        active = [s for s in state if s["balance"] > 0.005]
        if strategy == "highest_interest":
            active.sort(key=lambda s: (-s["rate"], s["balance"]))
        elif strategy == "lowest_balance":
            active.sort(key=lambda s: (s["balance"], -s["rate"]))
        else:  # distributed — order doesn't matter, split is proportional
            active.sort(key=lambda s: -s["balance"])

        total_active = sum(s["balance"] for s in active)

        # Assign each active loan its slice of the extra payment.
        extra_by_loan: dict[int, float] = {}
        if extra_pool > 0 and active:
            if strategy == "distributed" and total_active > 0:
                for idx, s in enumerate(active):
                    extra_by_loan[idx] = extra_pool * (
                        s["balance"] / total_active
                    )
            else:
                # Avalanche / snowball: stack the whole extra onto the
                # first loan in the ordering; any remainder (when the top
                # loan's balance + interest is less than its payment) spills
                # to the next, and so on.
                remaining_extra = extra_pool
                for idx, s in enumerate(active):
                    if remaining_extra <= 0:
                        break
                    # Cap at what would actually apply (balance + interest
                    # this month minus the min payment's principal portion).
                    interest_this_month = s["balance"] * s["monthly_rate"]
                    cap = max(s["balance"] + interest_this_month - s["min_payment"], 0.0)
                    take = min(remaining_extra, cap)
                    if take > 0:
                        extra_by_loan[idx] = extra_by_loan.get(idx, 0.0) + take
                        remaining_extra -= take

        # Step each active loan forward one month.
        for idx, s in enumerate(active):
            interest = s["balance"] * s["monthly_rate"]
            s["interest_paid"] += interest
            payment = min(
                s["min_payment"] + extra_by_loan.get(idx, 0.0),
                s["balance"] + interest,
            )
            principal = payment - interest
            s["balance"] = max(s["balance"] - principal, 0.0)
            s["schedule"].append(round(s["balance"], 2))

        # Paid-off loans this month append a 0.0 point so their schedule
        # length tracks the others.
        for s in state:
            if s["balance"] <= 0.005:
                if len(s["schedule"]) <= m:
                    s["schedule"].append(0.0)

        total = sum(s["balance"] for s in state)
        d = _add_months(today, m)
        total_schedule.append({
            "month_offset": m,
            "date": d.isoformat(),
            "total_balance": round(total, 2),
        })
        if total < 0.01 and months_to_payoff is None:
            months_to_payoff = m
            break

    payoff_date = None
    if months_to_payoff is not None:
        payoff_date = _add_months(today, months_to_payoff).isoformat()

    total_interest = round(sum(s["interest_paid"] for s in state), 2)

    return {
        "schedule": total_schedule,
        "months_to_payoff": months_to_payoff,
        "payoff_date": payoff_date,
        "total_interest": total_interest,
        "unprojectable": unprojectable,
    }


def debt_summary() -> dict:
    """Headline numbers for the Debt Tracker page."""
    loans = list_loans()
    total_balance = sum((ln.current_balance or 0.0) for ln in loans)
    total_min = sum((ln.min_payment or 0.0) for ln in loans)
    proj = project_payoff(extra_monthly=0.0)
    return {
        "total_balance": round(total_balance, 2),
        "total_min_payment": round(total_min, 2),
        "num_loans": len(loans),
        "projected_payoff_months": proj["months_to_payoff"],
        "projected_payoff_date": proj["payoff_date"],
        "projected_total_interest": proj["total_interest"],
        "unprojectable": proj["unprojectable"],
    }
