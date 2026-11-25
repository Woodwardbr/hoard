"""Net worth over time: assets minus liabilities across balance snapshots."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date

from ..db import get_conn
from ..models import NetWorthPoint
from ._common import ASSET_KINDS


def net_worth_series() -> list[NetWorthPoint]:
    """Net worth computed from daily balance snapshots across all accounts.

    Accounts flagged ``exclude_from_net_worth`` (e.g. unvested RSUs you don't
    yet own) are kept out of ``assets``/``net`` but still contribute to
    ``potential_net`` so the user can see the would-be figure as those holdings
    vest and move into real accounts.

    ``liabilities`` is the total of ``credit_cards`` and ``loans``. Loans
    include Plaid-linked loan accounts and the Debt Tracker's manually-tracked
    ``loans`` (e.g. federal student loans Plaid can't link); see
    ``_tracked_loan_totals`` for how the latter are dated.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.account_id, b.snapshot_date, b.current,
                   a.kind, a.exclude_from_net_worth
            FROM balances b
            JOIN accounts a USING (account_id)
            ORDER BY b.snapshot_date
            """
        ).fetchall()
        loan_snapshot_dates = {
            r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM loan_balance_snapshots")
        }
        dates = sorted({r["snapshot_date"] for r in rows} | loan_snapshot_dates)
        tracked_loans = _tracked_loan_totals(conn, dates or [date.today().isoformat()])

    rows_by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        rows_by_date[r["snapshot_date"]].append(r)

    # Walk snapshot dates in order, carrying each account's last known balance
    # forward so a date where only one institution synced doesn't zero out the
    # others.
    latest: dict[str, dict] = {}
    points: list[NetWorthPoint] = []
    for snapshot_date in dates:
        for r in rows_by_date[snapshot_date]:
            latest[r["account_id"]] = {
                "current": float(r["current"] or 0.0),
                "kind": r["kind"],
                "excluded": bool(r["exclude_from_net_worth"]),
            }
        assets = sum(
            v["current"] for v in latest.values()
            if v["kind"] in ASSET_KINDS and not v["excluded"]
        )
        credit_cards = sum(v["current"] for v in latest.values() if v["kind"] == "credit")
        loans = sum(
            v["current"] for v in latest.values() if v["kind"] == "loan"
        ) + tracked_loans[snapshot_date]
        liabilities = credit_cards + loans
        excluded = sum(
            v["current"] for v in latest.values()
            if v["kind"] in ASSET_KINDS and v["excluded"]
        )
        net = assets - liabilities
        points.append(
            NetWorthPoint(
                date=date.fromisoformat(snapshot_date),
                assets=assets,
                liabilities=liabilities,
                credit_cards=credit_cards,
                loans=loans,
                net=net,
                potential_net=net + excluded,
            )
        )

    if not points:
        # Seed a starting point so the chart renders; manual loans without
        # snapshots still count.
        debt = next(iter(tracked_loans.values()))
        points.append(
            NetWorthPoint(
                date=date.today(), assets=0.0, liabilities=debt, credit_cards=0.0,
                loans=debt, net=-debt, potential_net=-debt,
            )
        )
    return points


def _tracked_loan_totals(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    """Total Debt Tracker loan balance on each of ``dates`` (ISO strings).

    Balance snapshots are sparse (often months apart), so between two of them
    the total steps down on each logged payment's date, and whatever change the
    payments don't explain (mostly accrued interest) is spread linearly over the
    gap. The result matches every snapshot exactly. Before the first snapshot
    the earliest balance is held flat (so a loan doesn't appear as a sudden drop
    the day it was entered), and after the last one the last balance is held.
    Loans with no snapshots count at their stored balance throughout.
    """
    unsnapshotted = sum(
        float(r[0] or 0.0)
        for r in conn.execute(
            "SELECT current_balance FROM loans"
            " WHERE loan_id NOT IN (SELECT loan_id FROM loan_balance_snapshots)"
        )
    )
    snapshots = conn.execute(
        "SELECT loan_id, snapshot_date, balance FROM loan_balance_snapshots ORDER BY snapshot_date"
    ).fetchall()
    if not snapshots:
        return dict.fromkeys(dates, unsnapshotted)
    # Payments are often logged as one combined amount (loan_id NULL), so work
    # with the total across loans rather than per loan.
    payments = [
        (r[0], float(r[1]))
        for r in conn.execute(
            "SELECT payment_date, amount FROM loan_payments WHERE status != 'Failed'"
        )
    ]

    # Total at each snapshot date, carrying each loan's last balance forward
    # and its earliest one backward.
    per_loan: dict[str, float] = {}
    for r in snapshots:
        per_loan.setdefault(r["loan_id"], float(r["balance"]))
    totals: dict[str, float] = {}
    for r in snapshots:
        per_loan[r["loan_id"]] = float(r["balance"])
        totals[r["snapshot_date"]] = sum(per_loan.values())
    anchors = sorted(totals)

    def paid(after: str, through: str) -> float:
        return sum(amount for d, amount in payments if after < d <= through)

    result: dict[str, float] = {}
    for d in dates:
        if d <= anchors[0]:
            total = totals[anchors[0]]
        elif d >= anchors[-1]:
            total = totals[anchors[-1]]
        else:
            nxt = next(a for a in anchors if a > d)
            prev = anchors[anchors.index(nxt) - 1]
            residual = totals[nxt] - totals[prev] + paid(prev, nxt)
            span = (date.fromisoformat(nxt) - date.fromisoformat(prev)).days
            elapsed = (date.fromisoformat(d) - date.fromisoformat(prev)).days
            total = totals[prev] - paid(prev, d) + residual * elapsed / span
        result[d] = total + unsnapshotted
    return result
