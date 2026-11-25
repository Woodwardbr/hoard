from __future__ import annotations

import dataclasses
import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import analytics, splits, sync
from . import plaid as plaid_wrapper
from .config import settings
from .plaid import create_link_token as create_plaid_link_token

router = APIRouter()

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_period(period: str | None) -> str:
    """Validate a period query param. Defaults to the current YYYY-MM.

    Accepts ``"avg"`` or a ``YYYY-MM`` string; anything else raises 400.
    """
    if period is None or period == "":
        today = date.today()
        return f"{today.year:04d}-{today.month:02d}"
    if period == "avg":
        return period
    if _PERIOD_RE.match(period):
        return period
    raise HTTPException(
        status_code=400,
        detail="period must be 'avg' or 'YYYY-MM'",
    )


def _serialize(obj) -> dict:
    """dataclasses.asdict with dates rendered as ISO strings."""
    return {
        k: v.isoformat() if isinstance(v, date) else v
        for k, v in dataclasses.asdict(obj).items()
    }


class LinkTokenResp(BaseModel):
    link_token: str


class PublicTokenReq(BaseModel):
    public_token: str
    institution: str | None = None


class BudgetReq(BaseModel):
    category: str
    monthly_limit: float


class MerchantRuleReq(BaseModel):
    merchant_pattern: str
    target_category: str


class TransactionOverrideReq(BaseModel):
    override_category: str | None = None


@router.get("/api/health")
def health() -> dict:
    return {"status": "ok", "plaid_env": settings.plaid_env_value}


@router.get("/api/link-token", response_model=LinkTokenResp)
def create_link_token(item_id: str | None = None) -> LinkTokenResp:
    """Create a Plaid Link token for the browser Link flow.

    Pass ``item_id`` to get an update-mode token that reconnects that existing
    item instead of linking a new one.
    """
    if not plaid_wrapper.credentials_configured():
        raise plaid_wrapper.CredentialsMissing()
    if item_id is None:
        return LinkTokenResp(link_token=create_plaid_link_token())
    try:
        access_token = sync.access_token_for(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown item_id") from None
    return LinkTokenResp(link_token=create_plaid_link_token(access_token=access_token))


@router.post("/api/exchange-public-token")
def exchange_public_token(req: PublicTokenReq) -> dict:
    item_id = sync.link_item(req.public_token, institution=req.institution)
    return {"item_id": item_id, "status": "linked"}


@router.post("/api/sync")
def trigger_sync() -> dict:
    return sync.sync_all()


@router.post("/api/items/{item_id}/sync")
def sync_one_item(item_id: str) -> dict:
    """Sync a single item, e.g. right after reconnecting it via Link update mode."""
    try:
        return sync.sync_item(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown item_id") from None


@router.get("/api/items")
def items() -> list[dict]:
    return sync.list_items()


@router.delete("/api/items/{item_id}")
def remove_item(item_id: str) -> dict:
    sync.unlink_item(item_id)
    return {"status": "removed"}


@router.get("/api/net-worth")
def net_worth() -> list[dict]:
    return [p.__dict__ | {"date": p.date.isoformat()} for p in analytics.net_worth_series()]


@router.get("/api/spending")
def spending(start: date | None = None, end: date | None = None) -> list[dict]:
    today = date.today()
    start_d = start or today.replace(day=1)
    end_d = end or today
    return [
        {"category": c.category, "total": c.total, "count": c.transaction_count}
        for c in analytics.spending_by_category(start_d, end_d)
    ]


@router.get("/api/spending-categories")
def spending_categories(period: str | None = None) -> list[dict]:
    """Friendly-category spending breakdown for a period token.

    ``period`` is ``"avg"`` (average monthly spend over complete months) or a
    ``YYYY-MM`` string. Defaults to the current month.
    """
    p = _parse_period(period)
    if p == "avg":
        cats = analytics.average_monthly_spending_by_friendly_category()
    else:
        rng = analytics.spending_for_period(p)
        if rng is None:
            return []
        start, end = rng
        cats = analytics.spending_by_friendly_category(start, end)
    return [
        {"category": c.category, "total": c.total, "count": c.transaction_count}
        for c in cats
    ]


@router.get("/api/spending-months")
def spending_months() -> list[dict]:
    """Distinct YYYY-MM periods present in the data (descending)."""
    return analytics.spending_months()


@router.get("/api/spending-average-period")
def spending_average_period() -> dict:
    """The complete-month window the ``avg`` period averages over."""
    return analytics.average_period_info() or {"months": 0, "label": "no complete months yet"}


@router.get("/api/spending-categories/{category}/transactions")
def spending_category_transactions(
    category: str, period: str | None = None
) -> list[dict]:
    """Individual transactions for a friendly category in a period."""
    p = _parse_period(period)
    return [
        {
            "transaction_id": t.transaction_id,
            "date": t.date.isoformat(),
            "name": t.name,
            "merchant": t.merchant,
            "amount": t.amount,
            "category_detailed": t.category_detailed,
            "account_name": t.account_name,
        }
        for t in analytics.transactions_for_period_category(p, category)
    ]


@router.get("/api/budgets")
def budgets(period: str | None = None) -> list[dict]:
    p = _parse_period(period)
    return [_serialize(b) for b in analytics.budget_progress(p)]


@router.post("/api/budgets")
def set_budget(req: BudgetReq) -> dict:
    analytics.upsert_budget(req.category, req.monthly_limit)
    return {"status": "saved"}


@router.delete("/api/budgets/{category}")
def remove_budget(category: str) -> dict:
    analytics.delete_budget(category)
    return {"status": "removed"}


# --- Reclassify: merchant rules + per-transaction override -----------------


@router.get("/api/category-options")
def category_options() -> list[str]:
    return analytics.friendly_category_options()


@router.get("/api/merchant-rules")
def list_merchant_rules() -> list[dict]:
    return analytics.list_merchant_rules()


@router.post("/api/merchant-rules")
def add_merchant_rule(req: MerchantRuleReq) -> dict:
    analytics.upsert_merchant_rule(req.merchant_pattern, req.target_category)
    return {"status": "saved"}


@router.delete("/api/merchant-rules/{merchant_pattern}")
def remove_merchant_rule(merchant_pattern: str) -> dict:
    analytics.delete_merchant_rule(merchant_pattern)
    return {"status": "removed"}


@router.patch("/api/transactions/{transaction_id}/category")
def reclassify_transaction(transaction_id: str, req: TransactionOverrideReq) -> dict:
    analytics.set_transaction_override(transaction_id, req.override_category)
    return {"status": "saved"}


@router.get("/api/subscriptions")
def subscriptions() -> list[dict]:
    """Detected recurring transactions (subscriptions)."""
    return [_serialize(s) for s in analytics.detect_subscriptions()]


# --- Portfolio: asset allocation by sector ---------------------------------


class HoldingSectorOverrideReq(BaseModel):
    account_id: str
    security_id: str | None = None
    sector: str | None = None


@router.get("/api/portfolio")
def portfolio() -> list[dict]:
    """Asset allocation grouped by sector (cash + investment holdings)."""
    return [_serialize(s) for s in analytics.portfolio_by_sector()]


@router.get("/api/portfolio/{sector}/assets")
def portfolio_assets(sector: str) -> list[dict]:
    """Individual assets in a given portfolio sector."""
    return [
        {
            "account_id": a.account_id,
            "account_name": a.account_name,
            "institution": a.institution,
            "security_id": a.security_id,
            "ticker": a.ticker,
            "name": a.name,
            "sector": a.sector,
            "value": a.value,
            "quantity": a.quantity,
            "cost_basis": a.cost_basis,
        }
        for a in analytics.assets_in_sector(sector)
    ]


@router.get("/api/sector-options")
def sector_options() -> list[str]:
    return analytics.sector_options()


@router.patch("/api/portfolio/assets/sector")
def reclassify_holding_sector(req: HoldingSectorOverrideReq) -> dict:
    try:
        analytics.set_holding_sector_override(req.account_id, req.security_id, req.sector)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "saved"}


# --- 401(k) plan allocations -----------------------------------------------


class PlanAllocationReq(BaseModel):
    account_id: str
    label: str
    ticker: str | None = None
    allocation_pct: float
    sector: str


@router.get("/api/plan-allocations")
def plan_allocations() -> list[dict]:
    """All 401(k) plan allocations with account context + current balance."""
    return analytics.list_plan_allocations()


@router.get("/api/plan-allocations/{account_id}")
def plan_allocations_for_account(account_id: str) -> list[dict]:
    return analytics.plan_allocations_for_account(account_id)


@router.post("/api/plan-allocations")
def upsert_plan_allocation(req: PlanAllocationReq) -> dict:
    analytics.upsert_plan_allocation(
        req.account_id, req.label, req.ticker, req.allocation_pct, req.sector
    )
    return {"status": "saved"}


@router.delete("/api/plan-allocations/{account_id}/{label}")
def delete_plan_allocation(account_id: str, label: str) -> dict:
    analytics.delete_plan_allocation(account_id, label)
    return {"status": "removed"}


# --- Debt Tracker: manually-tracked loans -----------------------------------


class LoanReq(BaseModel):
    loan_id: str | None = None
    name: str
    loan_type: str | None = None
    interest_rate: float | None = None
    min_payment: float | None = None
    current_balance: float | None = None
    next_due_date: date | None = None
    auto_pay: bool = False
    status: str = "Scheduled"
    notes: str | None = None


class LoanPaymentReq(BaseModel):
    loan_id: str | None = None
    payment_date: date
    amount: float
    status: str = "Received"
    source: str | None = None
    notes: str | None = None
    new_balance: float | None = None


@router.get("/api/loans")
def loans() -> list[dict]:
    return [
        {
            "loan_id": ln.loan_id,
            "name": ln.name,
            "loan_type": ln.loan_type,
            "interest_rate": ln.interest_rate,
            "min_payment": ln.min_payment,
            "current_balance": ln.current_balance,
            "next_due_date": ln.next_due_date.isoformat() if ln.next_due_date else None,
            "auto_pay": ln.auto_pay,
            "status": ln.status,
            "notes": ln.notes,
        }
        for ln in analytics.list_loans()
    ]


@router.post("/api/loans")
def save_loan(req: LoanReq) -> dict:
    loan_id = analytics.upsert_loan(
        loan_id=req.loan_id,
        name=req.name,
        loan_type=req.loan_type,
        interest_rate=req.interest_rate,
        min_payment=req.min_payment,
        current_balance=req.current_balance,
        next_due_date=req.next_due_date,
        auto_pay=req.auto_pay,
        status=req.status,
        notes=req.notes,
    )
    return {"status": "saved", "loan_id": loan_id}


@router.delete("/api/loans/{loan_id}")
def remove_loan(loan_id: str) -> dict:
    analytics.delete_loan(loan_id)
    return {"status": "removed"}


@router.get("/api/loan-payments")
def loan_payments() -> list[dict]:
    return [
        {
            "payment_id": p.payment_id,
            "loan_id": p.loan_id,
            "payment_date": p.payment_date.isoformat(),
            "amount": p.amount,
            "status": p.status,
            "source": p.source,
            "notes": p.notes,
        }
        for p in analytics.list_loan_payments()
    ]


@router.post("/api/loan-payments")
def save_loan_payment(req: LoanPaymentReq) -> dict:
    payment_id = analytics.record_loan_payment(
        payment_date=req.payment_date,
        amount=req.amount,
        loan_id=req.loan_id,
        status=req.status,
        source=req.source,
        notes=req.notes,
        new_balance=req.new_balance,
    )
    return {"status": "saved", "payment_id": payment_id}


@router.delete("/api/loan-payments/{payment_id}")
def remove_loan_payment(payment_id: str) -> dict:
    analytics.delete_loan_payment(payment_id)
    return {"status": "removed"}


@router.get("/api/debt/summary")
def debt_summary() -> dict:
    return analytics.debt_summary()


@router.get("/api/debt/balance-history")
def debt_balance_history() -> list[dict]:
    return analytics.loan_balance_history()


@router.get("/api/debt/projection")
def debt_projection(
    extra_monthly: float = 0.0,
    extra_onetime: float = 0.0,
    strategy: str = Query(
        "distributed",
        description="Payoff strategy: distributed, highest_interest, or lowest_balance",
    ),
) -> dict:
    if strategy not in analytics.PAYOFF_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"strategy must be one of {analytics.PAYOFF_STRATEGIES}",
        )
    return analytics.project_payoff(
        extra_monthly=extra_monthly,
        extra_onetime=extra_onetime,
        strategy=strategy,
    )


# --- Splits: Splitwise-style expense groups ---------------------------------


class SplitGroupReq(BaseModel):
    name: str
    start_date: date | None = None
    end_date: date | None = None
    members: list[str] = []


class SplitMembersReq(BaseModel):
    names: list[str]


class SplitShare(BaseModel):
    participant: str  # person_id, or "me"
    value: float = 1.0


class SplitAssignReq(BaseModel):
    transaction_ids: list[str]
    split_mode: Literal["even", "exact", "shares"]
    shares: list[SplitShare]


class SplitRepaymentReq(BaseModel):
    person_id: str
    amount: float
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


@router.get("/api/splits/transactions")
def split_transactions(start: date, end: date, q: str | None = None) -> list[dict]:
    return splits.search_transactions(start, end, q)


@router.get("/api/splits/people")
def split_people() -> list[dict]:
    return splits.list_people()


@router.get("/api/splits/groups")
def split_groups() -> list[dict]:
    return splits.list_groups()


@router.post("/api/splits/groups")
def create_split_group(req: SplitGroupReq) -> dict:
    group_id = _split_call(splits.create_group, req.name, req.start_date, req.end_date, req.members)
    return {"group_id": group_id}


@router.get("/api/splits/groups/{group_id}")
def split_group(group_id: str) -> dict:
    return _split_call(splits.get_group, group_id)


@router.delete("/api/splits/groups/{group_id}")
def remove_split_group(group_id: str) -> dict:
    splits.delete_group(group_id)
    return {"status": "removed"}


@router.post("/api/splits/groups/{group_id}/members")
def add_split_members(group_id: str, req: SplitMembersReq) -> dict:
    _split_call(splits.add_members, group_id, req.names)
    return {"status": "saved"}


@router.post("/api/splits/groups/{group_id}/expenses")
def assign_split_expenses(group_id: str, req: SplitAssignReq) -> dict:
    _split_call(
        splits.assign_expenses,
        group_id,
        req.transaction_ids,
        req.split_mode,
        {s.participant: s.value for s in req.shares},
    )
    return {"status": "saved"}


@router.delete("/api/splits/groups/{group_id}/expenses/{transaction_id}")
def unassign_split_expense(group_id: str, transaction_id: str) -> dict:
    _split_call(splits.unassign_expense, group_id, transaction_id)
    return {"status": "removed"}


@router.post("/api/splits/groups/{group_id}/repayments")
def add_split_repayment(group_id: str, req: SplitRepaymentReq) -> dict:
    repayment_id = _split_call(
        splits.record_repayment,
        group_id,
        req.person_id,
        req.amount,
        req.paid_date or date.today(),
        req.notes,
    )
    return {"status": "saved", "repayment_id": repayment_id}


@router.delete("/api/splits/repayments/{repayment_id}")
def remove_split_repayment(repayment_id: str) -> dict:
    splits.delete_repayment(repayment_id)
    return {"status": "removed"}
