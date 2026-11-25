"""Agent API: a small token-authenticated API for AI agents and automations.

Routes live under ``/api/agent`` (and, for older Muse setups, ``/api/muse``) and
require an ``X-Agent-Token`` header (``X-Muse-Token`` is also accepted).

``agent_app`` is a standalone app served on its own listener (AGENT_HOST) and
contains only these routes, so an agent cannot reach the dashboard or the rest
of ``/api`` regardless of the token. Routes that change data also need
AGENT_ALLOW_WRITES; see docs/AGENT_API.md. The MCP server (mcp_server.py)
exposes the same functions as tools.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import analytics, plaid, splits, sync
from .config import settings
from .db import get_conn

log = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def require_token(
    x_agent_token: str | None = Header(default=None),
    x_muse_token: str | None = Header(default=None),
) -> None:
    expected = settings.agent_api_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="AGENT_API_TOKEN not configured. Set it in .env.",
        )
    given = x_agent_token or x_muse_token
    if not given or not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid agent token")


class WritesDisabled(PermissionError):
    """The agent tried to change data while AGENT_ALLOW_WRITES is off."""


WRITES_DISABLED_MESSAGE = (
    "This action changes your data and agent writes are turned off. "
    "Set AGENT_ALLOW_WRITES=1 in .env and restart Hoard to allow it."
)


def check_writes_allowed() -> None:
    if not settings.agent_allow_writes:
        raise WritesDisabled(WRITES_DISABLED_MESSAGE)


def require_writes() -> None:
    try:
        check_writes_allowed()
    except WritesDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


WRITE = [Depends(require_writes)]

router = APIRouter(dependencies=[Depends(require_token)])


class RecordPaymentReq(BaseModel):
    # Optional with loan_id: tracked loans (e.g. a federal student loan servicer) usually have no
    # Plaid account. Required otherwise, to tell loan payments from card ones.
    account_id: str | None = Field(default=None, min_length=1)
    amount: float = Field(gt=0)
    payment_date: date | None = None
    loan_id: str | None = None
    notes: str | None = None
    # Loan's balance after the payment, as the loan servicer reports it (part of each
    # payment goes to interest, so it isn't simply balance - amount).
    new_balance: float | None = Field(default=None, ge=0)
    status: Literal["Scheduled", "Paid", "Past Due"] | None = None

    @model_validator(mode="after")
    def _loan_fields_need_loan_id(self) -> RecordPaymentReq:
        if self.loan_id is None and self.account_id is None:
            raise ValueError("account_id is required when loan_id is not given")
        if self.loan_id is None and (self.new_balance is not None or self.status is not None):
            raise ValueError("new_balance and status require loan_id")
        return self


class LoanUpdate(BaseModel):
    """Partial update to a tracked loan. Only fields that are sent change."""

    model_config = ConfigDict(extra="forbid")

    loan_id: str = Field(min_length=1)
    current_balance: float | None = Field(default=None, ge=0)
    min_payment: float | None = Field(default=None, ge=0)
    next_due_date: date | None = None
    interest_rate: float | None = Field(default=None, ge=0, le=100)
    status: Literal["Scheduled", "Paid", "Past Due"] | None = None

    @model_validator(mode="after")
    def _sent_fields_not_null(self) -> LoanUpdate:
        fields = self.model_fields_set - {"loan_id"}
        if not fields:
            raise ValueError("at least one field to update is required")
        nulls = sorted(f for f in fields if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"fields cannot be null: {nulls}")
        return self

    def changes(self) -> dict:
        return {f: getattr(self, f) for f in self.model_fields_set - {"loan_id"}}


class UpdateLoansReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: list[LoanUpdate] = Field(min_length=1, max_length=50)


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "plaid_env": settings.plaid_env_value,
        "writes_allowed": settings.agent_allow_writes,
    }


@router.get("/accounts")
def accounts() -> list[dict]:
    """Every linked account with its latest balance (positive = owed for credit/loan)."""
    with get_conn() as conn:
        rows = conn.execute(
            analytics.LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, a.name, a.mask, a.kind, a.subtype, i.institution,
                   a.exclude_from_net_worth, lb.current AS balance,
                   lb.available, lb.snapshot_date AS balance_as_of
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            LEFT JOIN latest_balances lb ON lb.account_id = a.account_id
            ORDER BY a.kind, i.institution, a.name
            """
        ).fetchall()
    return [{**dict(r), "exclude_from_net_worth": bool(r["exclude_from_net_worth"])} for r in rows]


@router.get("/net-worth")
def net_worth(history: bool = False) -> dict:
    """Latest net worth (assets, liabilities, net); ``history=true`` adds the series."""
    points = [
        {k: (v.isoformat() if isinstance(v, date) else v) for k, v in vars(p).items()}
        for p in analytics.net_worth_series()
    ]
    out: dict = {"latest": points[-1] if points else None}
    if history:
        out["history"] = points
    return out


@router.get("/budgets")
def budgets(period: str | None = None) -> list[dict]:
    """Budget vs actual for ``period`` (``YYYY-MM``, default this month, or ``avg``)."""
    if period is None:
        period = f"{date.today():%Y-%m}"
    elif period != "avg" and not _PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period must be 'YYYY-MM' or 'avg'")
    return [
        {
            "category": b.category,
            "limit": b.monthly_limit,
            "spent": b.spent_so_far,
            "remaining": b.remaining,
            "pct_used": b.pct_used,
        }
        for b in analytics.budget_progress(period)
    ]


@router.get("/portfolio")
def portfolio() -> list[dict]:
    """Investment and cash allocation by sector, largest first, with percent of total."""
    sectors = analytics.portfolio_by_sector()
    grand = sum(s.total for s in sectors) or 1.0
    return [
        {
            "sector": s.sector,
            "total": round(s.total, 2),
            "pct": round(100 * s.total / grand, 1),
            "assets": s.asset_count,
        }
        for s in sectors
    ]


@router.get("/debt-summary")
def debt_summary() -> dict:
    """Tracked-loan totals and the payoff projection at minimum payments."""
    return analytics.debt_summary()


# Plaid errors meaning "this item has no liabilities data" rather than "something
# broke". Cards on these items fall back to their synced current balance.
_NO_LIABILITIES_CODES = {"ADDITIONAL_CONSENT_REQUIRED", "PRODUCTS_NOT_SUPPORTED"}


@router.get("/payoff-balances")
def payoff_balances() -> dict:
    """Return what to pay for the payoff workflow.

    Credit cards: one entry per card. ``payoff_amount`` is the last statement
    balance when Plaid Liabilities is available for the card
    (``balance_source: "statement"``), otherwise the current balance from the
    last sync (``balance_source: "current"``). Due date and minimum payment
    are only known with Liabilities.

    Student loans: Plaid Liabilities data, when any linked item has it.
    Tracked loans: the Debt Tracker's loans, with monthly payment, due date
    and ``auto_pay``. Their ``loan_id`` can be passed to ``record-payment``.

    Unexpected Plaid failures are listed under ``errors``; the affected cards
    still appear with their current balance, so a card is never silently
    dropped or mistaken for a zero balance.
    """
    with get_conn() as conn:
        cards = conn.execute(
            analytics.LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, a.name, a.mask, a.item_id, i.institution,
                   lb.current AS current_balance, lb.snapshot_date AS balance_as_of
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            LEFT JOIN latest_balances lb ON lb.account_id = a.account_id
            WHERE a.kind = 'credit'
            ORDER BY i.institution, a.name
            """
        ).fetchall()
        # Only items holding credit/loan accounts can have liabilities; querying
        # brokerage-only items would fail every time and bury real errors.
        items = conn.execute(
            """
            SELECT DISTINCT i.item_id, i.access_token, i.institution
            FROM items i JOIN accounts a ON a.item_id = i.item_id
            WHERE a.kind IN ('credit', 'loan')
            """
        ).fetchall()
        account_names = {
            r["account_id"]: r for r in conn.execute("SELECT account_id, name, mask FROM accounts")
        }

    credit_liabs: dict[str, plaid.CreditLiability] = {}
    student_loans: list[dict] = []
    errors: list[dict] = []

    for item in items:
        try:
            token = sync.resolve_token(item["item_id"], item["access_token"])
            credit, student = plaid.get_liabilities(token)
        except Exception as exc:
            code = plaid.error_code(exc)
            if code not in _NO_LIABILITIES_CODES:
                log.warning("Liabilities fetch failed for %s: %s", item["institution"], exc)
                errors.append(
                    {"institution": item["institution"], "error_code": code or type(exc).__name__}
                )
            continue

        credit_liabs.update((c.account_id, c) for c in credit)
        for s in student:
            acct = account_names.get(s.account_id)
            student_loans.append(
                {
                    "account_id": s.account_id,
                    "name": acct["name"] if acct else None,
                    "mask": acct["mask"] if acct else None,
                    "institution": item["institution"],
                    "loan_name": s.loan_name,
                    "monthly_payment_due": s.next_monthly_payment,
                    "due_date": s.next_payment_due_date,
                    "minimum_payment": s.minimum_payment_amount,
                    "last_payment_amount": s.last_payment_amount,
                    "last_payment_date": s.last_payment_date,
                }
            )

    credit_cards = []
    for card in cards:
        liab = credit_liabs.get(card["account_id"])
        statement = liab.last_statement_balance if liab else None
        if statement is not None:
            payoff, source = statement, "statement"
        elif card["current_balance"] is not None:
            payoff, source = card["current_balance"], "current"
        else:
            payoff, source = None, None
        credit_cards.append(
            {
                "account_id": card["account_id"],
                "name": card["name"],
                "mask": card["mask"],
                "institution": card["institution"],
                "payoff_amount": payoff,
                "balance_source": source,
                "statement_balance": statement,
                "current_balance": card["current_balance"],
                "balance_as_of": card["balance_as_of"],
                "statement_issue_date": liab.last_statement_issue_date if liab else None,
                "minimum_payment": liab.minimum_payment_amount if liab else None,
                "due_date": liab.next_payment_due_date if liab else None,
                "last_payment_amount": liab.last_payment_amount if liab else None,
                "last_payment_date": liab.last_payment_date if liab else None,
            }
        )

    tracked_loans = [_loan_dict(ln) for ln in analytics.list_loans()]

    return {
        "credit_cards": credit_cards,
        "student_loans": student_loans,
        "tracked_loans": tracked_loans,
        "errors": errors,
    }


def _loan_dict(ln) -> dict:
    return {
        "loan_id": ln.loan_id,
        "name": ln.name,
        "monthly_payment": ln.min_payment,
        "next_due_date": ln.next_due_date.isoformat() if ln.next_due_date else None,
        "current_balance": ln.current_balance,
        "interest_rate": ln.interest_rate,
        "auto_pay": ln.auto_pay,
        "status": ln.status,
    }


@router.get("/spending-summary")
def spending_summary(period: str | None = None) -> dict:
    """A month's spending next to the usual, for the unusual-spending check.

    ``period`` is ``YYYY-MM`` and defaults to the last complete month.

    - ``categories``: every category's spend that month vs its average over the
      other complete months (``difference``, ``pct_of_avg``), biggest overspend first.
    - ``largest_transactions``: the month's 10 largest purchases.
    - ``budgets``: budget vs actual, for categories that have a budget.
    - ``average_monthly``: averages over all complete months (kept for compatibility).
    """
    if period is None:
        prev = date.today().replace(day=1) - timedelta(days=1)
        period = f"{prev.year:04d}-{prev.month:02d}"
    elif not _PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period must be 'YYYY-MM'")

    budgets = [
        {
            "category": b.category,
            "limit": b.monthly_limit,
            "spent": b.spent_so_far,
            "remaining": b.remaining,
        }
        for b in analytics.budget_progress(period)
    ]
    average_monthly = [
        {"category": c.category, "avg_monthly": c.total, "count": c.transaction_count}
        for c in analytics.average_monthly_spending_by_friendly_category()
    ]
    comparison = analytics.month_vs_average(period)
    return {
        "period": period,
        "month_complete": comparison.month_complete,
        "baseline_months": comparison.baseline_months,
        "categories": [
            {
                "category": c.category,
                "spent": round(c.spent, 2),
                "count": c.transaction_count,
                "avg_monthly": round(c.avg_monthly, 2),
                "difference": round(c.difference, 2),
                "pct_of_avg": round(c.pct_of_avg, 1) if c.pct_of_avg is not None else None,
            }
            for c in comparison.categories
        ],
        "largest_transactions": comparison.largest_transactions,
        "budgets": budgets,
        "average_monthly": average_monthly,
    }


_DUPLICATE_DETAIL = (
    "A payment for this loan with the same date and amount is already recorded. "
    "Not recording, to avoid duplicates."
)


@router.post("/record-payment", dependencies=WRITE)
def record_payment(payload: RecordPaymentReq) -> dict:
    """Record a manual payoff payment so it shows in hoard.

    With ``loan_id``, the payment is logged against that tracked loan. Without
    it, a payment to a Plaid loan account (e.g. a federal student loan servicer) is logged as a
    combined payment across all loans (``loan_id`` NULL). Payments to other
    accounts are not stored (card payments arrive via Plaid sync) and the
    response says so with ``"stored": false``. ``account_id`` may be omitted
    when ``loan_id`` is given.

    Autopay loans are accepted too (autopay can fail and the user pays by
    hand). To avoid double-counting, a payment is refused with 409 when one
    already exists for the same loan (or combined, ``loan_id`` NULL), on the
    same date, for the same amount.

    With ``loan_id``, optional ``new_balance`` (balance after the payment, from
    a loan servicer) and ``status`` update that loan in the same transaction as the
    payment, and the balance is snapshotted for the history chart.
    """
    with get_conn() as conn:
        acct = None
        if payload.account_id is not None:
            acct = conn.execute(
                "SELECT name, kind FROM accounts WHERE account_id = ?", (payload.account_id,)
            ).fetchone()
            if acct is None:
                raise HTTPException(status_code=404, detail="Unknown account_id")
        loan_name: str | None = None
        if payload.loan_id is not None:
            loan = conn.execute(
                "SELECT name, auto_pay FROM loans WHERE loan_id = ?", (payload.loan_id,)
            ).fetchone()
            if loan is None:
                raise HTTPException(status_code=404, detail="Unknown loan_id")
            loan_name = loan["name"]

    if payload.loan_id is None and acct["kind"] != "loan":  # acct set: validator
        return {"status": "noted", "stored": False, "account": acct["name"]}

    payment_date = payload.payment_date or date.today()
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT payment_id FROM loan_payments
            WHERE loan_id IS ? AND payment_date = ? AND ABS(amount - ?) < 0.005
            """,
            (payload.loan_id, payment_date.isoformat(), payload.amount),
        ).fetchone()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": _DUPLICATE_DETAIL,
                "existing_payment_id": existing["payment_id"],
            },
        )

    payment_id = analytics.record_loan_payment(
        payment_date=payment_date,
        amount=payload.amount,
        loan_id=payload.loan_id,
        status="Received",
        source=f"Agent payoff ({acct['name'] if acct else loan_name})",
        notes=payload.notes or "Monthly payoff (agent API)",
        new_balance=payload.new_balance,
        loan_status=payload.status,
    )
    resp: dict = {"status": "recorded", "stored": True, "payment_id": payment_id, "loan": loan_name}
    if payload.loan_id is not None and (payload.new_balance is not None or payload.status):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT loan_id, name, current_balance, status, next_due_date FROM loans "
                "WHERE loan_id = ?",
                (payload.loan_id,),
            ).fetchone()
        resp["updated_loan"] = dict(row)
    return resp


@router.post("/update-loans", dependencies=WRITE)
def update_loans(payload: UpdateLoansReq) -> dict:
    """Update balance, payment, due date, rate or status on existing tracked loans.

    Only fields that are sent change. All updates apply or none do: an unknown
    ``loan_id`` returns 404 and nothing is written. Loans cannot be created,
    deleted or renamed, and ``auto_pay`` cannot be changed, through this API.
    Returns the updated loans in request order.
    """
    ids = [u.loan_id for u in payload.updates]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Each loan_id may appear only once")
    try:
        loans = analytics.update_loans([(u.loan_id, u.changes()) for u in payload.updates])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown loan_id: {exc.args[0]}") from exc
    log.info(
        "Agent updated loans: %s",
        ", ".join(f"{u.loan_id}={sorted(u.changes())}" for u in payload.updates),
    )
    return {"loans": [_loan_dict(ln) for ln in loans]}


# --- Splits: expense groups --------------------------------------------------
# Agents can read, create groups/people, assign and re-split expenses, unassign
# expenses and record repayments. It cannot delete or rename groups or people,
# or delete repayments.


class SplitGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    start_date: date | None = None
    end_date: date | None = None
    members: list[str] = Field(default_factory=list, max_length=50)


class SplitMembersAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(min_length=1, max_length=50)


class SplitShareReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant: str = Field(min_length=1)  # "me", or a member's name or person_id
    value: float = 1.0


class SplitAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[str] = Field(min_length=1, max_length=100)
    split_mode: Literal["even", "exact", "shares"]
    shares: list[SplitShareReq] = Field(min_length=1)


class SplitRepayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1)  # a member's name or person_id
    amount: float = Field(gt=0)
    paid_date: date | None = None
    notes: str | None = None


def _split_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except splits.SplitConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/transactions")
def search_transactions(
    start: date,
    end: date,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=500),
) -> list[dict]:
    """Search charges by date range (and optional name/merchant text).

    Each row shows ``group_id``/``group_name`` if it is already assigned to a
    split group.
    """
    return splits.search_transactions(start, end, q, limit)


@router.get("/split-groups")
def list_split_groups() -> list[dict]:
    """Split groups with the total still owed to the user."""
    return splits.list_groups()


@router.get("/split-groups/{group_id}")
def get_split_group(group_id: str) -> dict:
    """Members, assigned expenses with per-person shares, repayments and balances."""
    return _split_call(splits.get_group, group_id)


@router.post("/split-groups", dependencies=WRITE)
def create_split_group(payload: SplitGroupCreate) -> dict:
    """Create a group. People are created by name if they don't exist yet."""
    group_id = _split_call(
        splits.create_group, payload.name, payload.start_date, payload.end_date, payload.members
    )
    log.info("Agent created split group %s (%s)", group_id, payload.name)
    return {"group_id": group_id}


@router.post("/split-groups/{group_id}/members", dependencies=WRITE)
def add_split_members(group_id: str, payload: SplitMembersAdd) -> dict:
    """Add people to a group (add only; members cannot be removed through this API)."""
    _split_call(splits.add_members, group_id, payload.names)
    log.info("Agent added members to split group %s: %s", group_id, payload.names)
    return {"status": "saved"}


@router.post("/split-groups/{group_id}/expenses", dependencies=WRITE)
def assign_split_expenses(group_id: str, payload: SplitAssign) -> dict:
    """Assign transactions to a group with a split, or re-split ones already in it.

    Participants are ``"me"`` or a group member's name or person_id. Modes: ``even``
    (values ignored), ``exact`` (dollars; one transaction only) and ``shares``
    (weights). All transactions are assigned or none are.
    """

    def run() -> None:
        group = splits.get_group(group_id)
        shares = {}
        for s in payload.shares:
            who = s.participant if s.participant == splits.ME else splits.resolve_member(
                group, s.participant
            )
            shares[who] = s.value
        splits.assign_expenses(group_id, payload.transaction_ids, payload.split_mode, shares)

    _split_call(run)
    log.info(
        "Agent assigned %d transaction(s) to split group %s (%s)",
        len(payload.transaction_ids),
        group_id,
        payload.split_mode,
    )
    return {"status": "saved"}


@router.delete("/split-groups/{group_id}/expenses/{transaction_id}", dependencies=WRITE)
def unassign_split_expense(group_id: str, transaction_id: str) -> dict:
    """Take a transaction out of a group (the transaction itself is untouched)."""
    _split_call(splits.unassign_expense, group_id, transaction_id)
    log.info("Agent unassigned %s from split group %s", transaction_id, group_id)
    return {"status": "removed"}


@router.post("/split-groups/{group_id}/repayments", dependencies=WRITE)
def add_split_repayment(group_id: str, payload: SplitRepayment) -> dict:
    """Record that a member paid the user back."""

    def run() -> str:
        person_id = splits.resolve_member(splits.get_group(group_id), payload.person)
        return splits.record_repayment(
            group_id, person_id, payload.amount, payload.paid_date or date.today(), payload.notes
        )

    repayment_id = _split_call(run)
    log.info("Agent recorded a %.2f repayment in split group %s", payload.amount, group_id)
    return {"status": "saved", "repayment_id": repayment_id}


# No interactive docs/OpenAPI schema: agents get exactly the routes above.
# Keep this at the end of the file so every route is registered before the router is included.
agent_app = FastAPI(title="Hoard agent API", docs_url=None, redoc_url=None, openapi_url=None)
agent_app.include_router(router, prefix="/api/agent")
agent_app.include_router(router, prefix="/api/muse")  # older Muse setups
