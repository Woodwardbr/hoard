"""Splitwise-style expense groups: assign card transactions to a group, split them
between people, and track what each person still owes.

Amounts are never copied; they are read from ``transactions.amount`` on demand so
a re-sync stays correct. ``'me'`` is the reserved participant id for the user.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from fractions import Fraction
from math import floor

from . import analytics
from .db import get_conn

ME = "me"
MODES = ("even", "exact", "shares")


class SplitConflict(Exception):
    """A transaction is already assigned to a different group."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def compute_shares(amount: float, mode: str, shares: dict[str, float]) -> dict[str, float]:
    """Dollar amount owed by each participant for one expense.

    ``even`` and ``shares`` split cents with the largest-remainder method so the
    parts always add up to exactly ``amount``. ``exact`` validates that the typed
    dollar values add up to ``amount`` (within a cent).
    """
    if mode not in MODES:
        raise ValueError(f"split_mode must be one of {MODES}")
    if not shares:
        raise ValueError("at least one participant is required")

    if mode == "exact":
        result = {p: round(v, 2) for p, v in shares.items()}
        if any(v < 0 for v in result.values()):
            raise ValueError("exact amounts cannot be negative")
        if abs(sum(result.values()) - amount) > 0.01:
            raise ValueError(
                f"exact amounts add up to {sum(result.values()):.2f}, expected {amount:.2f}"
            )
        return result

    if mode == "even":
        weights = {p: Fraction(1) for p in shares}
    else:
        if any(v <= 0 for v in shares.values()):
            raise ValueError("share weights must be greater than 0")
        weights = {p: Fraction(v).limit_denominator(10**6) for p, v in shares.items()}

    total_cents = round(amount * 100)
    total_weight = sum(weights.values())
    raw = {p: Fraction(total_cents) * w / total_weight for p, w in weights.items()}
    cents = {p: floor(r) for p, r in raw.items()}
    leftover = total_cents - sum(cents.values())
    # Hand leftover cents to the largest fractional remainders (ties: by name).
    order = sorted(raw, key=lambda p: (-(raw[p] - cents[p]), p))
    for p in order[:leftover]:
        cents[p] += 1
    return {p: c / 100 for p, c in cents.items()}


def search_transactions(
    start: date, end: date, q: str | None = None, limit: int = 500
) -> list[dict]:
    """Charges in [start, end], newest first, with any group they're assigned to."""
    sql = """
        SELECT t.transaction_id, t.date, t.name, t.merchant, t.amount,
               t.category_primary, t.category_detailed, t.override_category,
               a.name AS account_name, e.group_id, g.name AS group_name
        FROM transactions t
        JOIN accounts a USING (account_id)
        LEFT JOIN split_expenses e USING (transaction_id)
        LEFT JOIN split_groups g ON g.group_id = e.group_id
        WHERE t.date >= ? AND t.date <= ? AND t.amount > 0
    """
    params: list = [start.isoformat(), end.isoformat()]
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        sql += " AND (lower(t.name) LIKE ? OR lower(coalesce(t.merchant, '')) LIKE ?)"
        params += [like, like]
    sql += " ORDER BY t.date DESC, t.transaction_id LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rules = analytics._load_merchant_rules(conn)
        rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "transaction_id": d["transaction_id"],
                "date": d["date"],
                "name": d["name"],
                "merchant": d["merchant"],
                "amount": d["amount"],
                "account_name": d["account_name"],
                "category": analytics._row_friendly_category(d, rules),
                "group_id": d["group_id"],
                "group_name": d["group_name"],
            }
        )
    return out


# --- People & groups --------------------------------------------------------


def get_or_create_person(conn: sqlite3.Connection, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name cannot be empty")
    row = conn.execute("SELECT person_id FROM split_people WHERE name = ?", (name,)).fetchone()
    if row:
        return row["person_id"]
    person_id = _new_id()
    conn.execute("INSERT INTO split_people (person_id, name) VALUES (?, ?)", (person_id, name))
    return person_id


def list_people() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT person_id, name FROM split_people ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def _require_group(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM split_groups WHERE group_id = ?", (group_id,)).fetchone()
    if row is None:
        raise LookupError(f"Unknown group_id: {group_id}")
    return row


def _add_members(conn: sqlite3.Connection, group_id: str, names: list[str]) -> None:
    for name in names:
        if not (name or "").strip():
            continue
        person_id = get_or_create_person(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO split_group_members (group_id, person_id) VALUES (?, ?)",
            (group_id, person_id),
        )


def create_group(
    name: str,
    start_date: date | None,
    end_date: date | None,
    member_names: list[str],
) -> str:
    if not (name or "").strip():
        raise ValueError("group name cannot be empty")
    group_id = _new_id()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO split_groups (group_id, name, start_date, end_date) VALUES (?, ?, ?, ?)",
            (
                group_id,
                name.strip(),
                start_date.isoformat() if start_date else None,
                end_date.isoformat() if end_date else None,
            ),
        )
        _add_members(conn, group_id, member_names)
    return group_id


def add_members(group_id: str, names: list[str]) -> None:
    with get_conn() as conn:
        _require_group(conn, group_id)
        _add_members(conn, group_id, names)


def delete_group(group_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM split_groups WHERE group_id = ?", (group_id,))


# --- Expenses ---------------------------------------------------------------


def _expense_details(conn: sqlite3.Connection, group_id: str) -> list[dict]:
    """Expenses in a group with each participant's computed dollar share."""
    names = {r["person_id"]: r["name"] for r in conn.execute("SELECT * FROM split_people")}
    names[ME] = "Me"
    expenses = conn.execute(
        """
        SELECT e.expense_id, e.transaction_id, e.split_mode,
               t.date, t.name, t.amount, a.name AS account_name
        FROM split_expenses e
        JOIN transactions t USING (transaction_id)
        JOIN accounts a USING (account_id)
        WHERE e.group_id = ?
        ORDER BY t.date DESC, e.transaction_id
        """,
        (group_id,),
    ).fetchall()
    out = []
    for e in expenses:
        rows = conn.execute(
            "SELECT participant, value FROM split_expense_shares WHERE expense_id = ?",
            (e["expense_id"],),
        ).fetchall()
        values = {r["participant"]: r["value"] for r in rows}
        try:
            amounts = compute_shares(e["amount"], e["split_mode"], values)
        except ValueError:
            # e.g. an exact split whose transaction amount changed after a re-sync.
            amounts = {p: round(v, 2) for p, v in values.items()}
        out.append(
            {
                "expense_id": e["expense_id"],
                "transaction_id": e["transaction_id"],
                "date": e["date"],
                "name": e["name"],
                "account_name": e["account_name"],
                "amount": e["amount"],
                "split_mode": e["split_mode"],
                "shares": [
                    {
                        "participant": p,
                        "name": names.get(p, p),
                        "value": values[p],
                        "amount": amounts.get(p, 0.0),
                    }
                    for p in sorted(values, key=lambda p: (p != ME, names.get(p, p).lower()))
                ],
            }
        )
    return out


def _balances(conn: sqlite3.Connection, group_id: str, expenses: list[dict]) -> dict:
    members = conn.execute(
        """
        SELECT p.person_id, p.name
        FROM split_group_members m JOIN split_people p USING (person_id)
        WHERE m.group_id = ?
        ORDER BY p.name COLLATE NOCASE
        """,
        (group_id,),
    ).fetchall()
    owed: dict[str, float] = {}
    my_share = 0.0
    total_spent = 0.0
    for e in expenses:
        total_spent += e["amount"]
        for s in e["shares"]:
            if s["participant"] == ME:
                my_share += s["amount"]
            else:
                owed[s["participant"]] = owed.get(s["participant"], 0.0) + s["amount"]
    repaid = {
        r["person_id"]: r["total"]
        for r in conn.execute(
            "SELECT person_id, SUM(amount) AS total FROM split_repayments "
            "WHERE group_id = ? GROUP BY person_id",
            (group_id,),
        )
    }
    people = []
    for m in members:
        o = round(owed.get(m["person_id"], 0.0), 2)
        r = round(repaid.get(m["person_id"], 0.0), 2)
        people.append(
            {
                "person_id": m["person_id"],
                "name": m["name"],
                "owed": o,
                "repaid": r,
                "outstanding": round(o - r, 2),
            }
        )
    return {
        "people": people,
        "my_share": round(my_share, 2),
        "total_spent": round(total_spent, 2),
    }


def group_balances(group_id: str) -> dict:
    with get_conn() as conn:
        _require_group(conn, group_id)
        return _balances(conn, group_id, _expense_details(conn, group_id))


def list_groups() -> list[dict]:
    with get_conn() as conn:
        groups = conn.execute(
            "SELECT * FROM split_groups ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        out = []
        for g in groups:
            bal = _balances(conn, g["group_id"], _expense_details(conn, g["group_id"]))
            out.append(
                {
                    "group_id": g["group_id"],
                    "name": g["name"],
                    "start_date": g["start_date"],
                    "end_date": g["end_date"],
                    "total_outstanding": round(sum(p["outstanding"] for p in bal["people"]), 2),
                }
            )
    return out


def get_group(group_id: str) -> dict:
    with get_conn() as conn:
        g = _require_group(conn, group_id)
        members = conn.execute(
            """
            SELECT p.person_id, p.name
            FROM split_group_members m JOIN split_people p USING (person_id)
            WHERE m.group_id = ?
            ORDER BY p.name COLLATE NOCASE
            """,
            (group_id,),
        ).fetchall()
        expenses = _expense_details(conn, group_id)
        repayments = conn.execute(
            """
            SELECT r.repayment_id, r.person_id, p.name, r.amount, r.paid_date, r.notes
            FROM split_repayments r JOIN split_people p USING (person_id)
            WHERE r.group_id = ?
            ORDER BY r.paid_date DESC, r.rowid DESC
            """,
            (group_id,),
        ).fetchall()
        return {
            "group_id": g["group_id"],
            "name": g["name"],
            "start_date": g["start_date"],
            "end_date": g["end_date"],
            "members": [dict(m) for m in members],
            "expenses": expenses,
            "repayments": [dict(r) for r in repayments],
            "balances": _balances(conn, group_id, expenses),
        }


def assign_expenses(
    group_id: str,
    transaction_ids: list[str],
    mode: str,
    shares: dict[str, float],
) -> None:
    """Assign transactions to a group with a split, or re-split ones already in it.

    All-or-nothing: any error leaves the database untouched.
    """
    if mode not in MODES:
        raise ValueError(f"split_mode must be one of {MODES}")
    if not transaction_ids:
        raise ValueError("at least one transaction is required")
    if len(set(transaction_ids)) != len(transaction_ids):
        raise ValueError("each transaction may appear only once")
    if mode == "exact" and len(transaction_ids) > 1:
        raise ValueError("exact splits can only be applied to one transaction at a time")

    with get_conn() as conn:
        _require_group(conn, group_id)
        member_ids = {
            r["person_id"]
            for r in conn.execute(
                "SELECT person_id FROM split_group_members WHERE group_id = ?", (group_id,)
            )
        }
        for participant in shares:
            if participant != ME and participant not in member_ids:
                raise ValueError(f"{participant} is not a member of this group")

        for tx_id in transaction_ids:
            tx = conn.execute(
                "SELECT amount FROM transactions WHERE transaction_id = ?", (tx_id,)
            ).fetchone()
            if tx is None:
                raise LookupError(f"Unknown transaction_id: {tx_id}")
            if tx["amount"] <= 0:
                raise ValueError(f"Transaction {tx_id} is not a charge")
            compute_shares(tx["amount"], mode, shares)  # validates

            existing = conn.execute(
                "SELECT expense_id, group_id FROM split_expenses WHERE transaction_id = ?",
                (tx_id,),
            ).fetchone()
            if existing and existing["group_id"] != group_id:
                raise SplitConflict(f"Transaction {tx_id} is already in another group")
            if existing:
                expense_id = existing["expense_id"]
                conn.execute(
                    "UPDATE split_expenses SET split_mode = ? WHERE expense_id = ?",
                    (mode, expense_id),
                )
                conn.execute(
                    "DELETE FROM split_expense_shares WHERE expense_id = ?", (expense_id,)
                )
            else:
                expense_id = _new_id()
                conn.execute(
                    "INSERT INTO split_expenses (expense_id, group_id, transaction_id, split_mode) "
                    "VALUES (?, ?, ?, ?)",
                    (expense_id, group_id, tx_id, mode),
                )
            for participant, value in shares.items():
                conn.execute(
                    "INSERT INTO split_expense_shares (expense_id, participant, value) "
                    "VALUES (?, ?, ?)",
                    (expense_id, participant, 1.0 if mode == "even" else value),
                )


def unassign_expense(group_id: str, transaction_id: str) -> None:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM split_expenses WHERE group_id = ? AND transaction_id = ?",
            (group_id, transaction_id),
        )
        if cur.rowcount == 0:
            raise LookupError("That transaction is not assigned to this group")


# --- Repayments -------------------------------------------------------------


def record_repayment(
    group_id: str,
    person_id: str,
    amount: float,
    paid_date: date,
    notes: str | None = None,
) -> str:
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    with get_conn() as conn:
        _require_group(conn, group_id)
        member = conn.execute(
            "SELECT 1 FROM split_group_members WHERE group_id = ? AND person_id = ?",
            (group_id, person_id),
        ).fetchone()
        if member is None:
            raise ValueError(f"{person_id} is not a member of this group")
        repayment_id = _new_id()
        conn.execute(
            "INSERT INTO split_repayments "
            "(repayment_id, group_id, person_id, amount, paid_date, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repayment_id, group_id, person_id, amount, paid_date.isoformat(), notes),
        )
    return repayment_id


def delete_repayment(repayment_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM split_repayments WHERE repayment_id = ?", (repayment_id,))


def resolve_member(group: dict, ref: str) -> str:
    """Map a member's person_id or (case-insensitive) name to their person_id."""
    ref_clean = (ref or "").strip()
    for m in group["members"]:
        if m["person_id"] == ref_clean or m["name"].lower() == ref_clean.lower():
            return m["person_id"]
    raise LookupError(f"Unknown member: {ref}")
