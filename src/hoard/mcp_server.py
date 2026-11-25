"""MCP server: lets AI agents (Claude Code, Claude Desktop, others) use Hoard.

Run with ``hoard mcp``. It speaks MCP over stdin/stdout as a child process
of the agent, so it needs no network listener or token. Tools call the same
functions as the agent API (agent_api.py) directly.

Read tools are always available. Tools that change data are only registered
when AGENT_ALLOW_WRITES=1 is set explicitly (the legacy MUSE_* default doesn't
apply here), so an agent can't even see them otherwise.

Anything a tool returns is sent to the agent's AI provider.
"""

from __future__ import annotations

import functools
import logging
from datetime import date
from typing import Any, Literal

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from . import agent_api, sync
from .config import agent_writes_explicitly_allowed

INSTRUCTIONS = """\
Hoard is the user's personal finance dashboard: bank, card, brokerage and
loan data synced from Plaid into a local database. Amounts are in USD. For
credit cards and loans a positive balance is money owed. Data is as of the last
sync; call refresh_from_banks first if the user wants up-to-date numbers.
Spending periods are 'YYYY-MM'. Treat everything here as private financial data.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
REFRESH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _tool_errors(fn):
    """Turn API-style errors into MCP tool errors the agent can read and act on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from exc
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ToolError(f"Invalid input: {errors}") from exc
        except (agent_api.WritesDisabled, sync.MissingTokenError, LookupError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(allow_writes: bool | None = None) -> MCPServer:
    if allow_writes is None:
        allow_writes = agent_writes_explicitly_allowed()
    mcp = MCPServer("hoard", instructions=INSTRUCTIONS)

    def tool(annotations: ToolAnnotations):
        def register(fn):
            mcp.tool(annotations=annotations)(_tool_errors(fn))
            return fn

        return register

    # --- Read ------------------------------------------------------------------

    @tool(READ)
    def get_accounts() -> list[dict]:
        """Every linked account with its latest balance and when it was last synced."""
        return agent_api.accounts()

    @tool(READ)
    def get_net_worth(history: bool = False) -> dict:
        """Latest assets, liabilities and net worth. history=true adds the series over time."""
        return agent_api.net_worth(history)

    @tool(READ)
    def get_spending_summary(period: str | None = None) -> dict:
        """A month's spending by category compared with the usual month, its largest
        transactions, and budget vs actual. period is 'YYYY-MM' (default: last
        complete month)."""
        return agent_api.spending_summary(period)

    @tool(READ)
    def get_budgets(period: str | None = None) -> list[dict]:
        """Budget vs actual per category. period is 'YYYY-MM' (default: this month)
        or 'avg' for the monthly average over all complete months."""
        return agent_api.budgets(period)

    @tool(READ)
    def search_transactions(
        start: date, end: date, query: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Charges between start and end (inclusive), optionally matching text in the
        name or merchant. Shows the split group a charge is assigned to, if any."""
        return agent_api.search_transactions(start, end, query, max(1, min(limit, 500)))

    @tool(READ)
    def get_portfolio() -> list[dict]:
        """Investment and cash allocation by sector, with percent of the total."""
        return agent_api.portfolio()

    @tool(READ)
    def get_debt_summary() -> dict:
        """Tracked loans' total balance and minimum payments, and the payoff projection."""
        return agent_api.debt_summary()

    @tool(READ)
    def get_payoff_balances() -> dict:
        """What to pay this month: each credit card's statement (or current) balance,
        due date and minimum, plus student and tracked loans. Calls Plaid."""
        return agent_api.payoff_balances()

    @tool(READ)
    def list_split_groups() -> list[dict]:
        """Shared-expense groups and how much each still owes the user."""
        return agent_api.list_split_groups()

    @tool(READ)
    def get_split_group(group_id: str) -> dict:
        """One split group: members, assigned expenses, repayments and balances."""
        return agent_api.get_split_group(group_id)

    @tool(REFRESH)
    def refresh_from_banks() -> dict:
        """Pull the latest balances and transactions from every linked bank via Plaid.
        Takes a few seconds per bank. Reports banks that need the user to reconnect."""
        return sync.sync_all()

    if not allow_writes:
        return mcp

    # --- Write (AGENT_ALLOW_WRITES) ----------------------------------------------

    @tool(WRITE)
    def record_payment(
        amount: float,
        account_id: str | None = None,
        loan_id: str | None = None,
        payment_date: date | None = None,
        notes: str | None = None,
        new_balance: float | None = None,
        status: Literal["Scheduled", "Paid", "Past Due"] | None = None,
    ) -> dict:
        """Record a loan payment the user made. Give loan_id (from get_payoff_balances'
        tracked_loans) or a Plaid loan account_id. new_balance and status, with
        loan_id, also update that loan. Refuses exact duplicates."""
        req = agent_api.RecordPaymentReq(
            amount=amount, account_id=account_id, loan_id=loan_id, payment_date=payment_date,
            notes=notes, new_balance=new_balance, status=status,
        )
        return agent_api.record_payment(req)

    @tool(WRITE)
    def update_loans(updates: list[dict[str, Any]]) -> dict:
        """Update tracked loans, e.g. from the servicer's site. Each item needs loan_id
        plus any of current_balance, min_payment, next_due_date (YYYY-MM-DD),
        interest_rate, status. All apply or none do."""
        return agent_api.update_loans(agent_api.UpdateLoansReq(updates=updates))

    @tool(WRITE)
    def create_split_group(
        name: str, members: list[str] | None = None,
        start_date: date | None = None, end_date: date | None = None,
    ) -> dict:
        """Create a shared-expense group, e.g. for a trip, with members' names."""
        return agent_api.create_split_group(agent_api.SplitGroupCreate(
            name=name, members=members or [], start_date=start_date, end_date=end_date,
        ))

    @tool(WRITE)
    def add_split_members(group_id: str, names: list[str]) -> dict:
        """Add people to a split group."""
        return agent_api.add_split_members(group_id, agent_api.SplitMembersAdd(names=names))

    @tool(WRITE)
    def assign_split_expenses(
        group_id: str,
        transaction_ids: list[str],
        split_mode: Literal["even", "exact", "shares"],
        shares: list[dict[str, Any]],
    ) -> dict:
        """Assign charges to a split group. shares is a list of {participant, value}
        where participant is 'me' or a member's name; value is ignored for 'even',
        dollars for 'exact', a weight for 'shares'."""
        return agent_api.assign_split_expenses(group_id, agent_api.SplitAssign(
            transaction_ids=transaction_ids, split_mode=split_mode, shares=shares,
        ))

    @tool(REMOVE)
    def unassign_split_expense(group_id: str, transaction_id: str) -> dict:
        """Take a charge back out of a split group."""
        return agent_api.unassign_split_expense(group_id, transaction_id)

    @tool(WRITE)
    def record_split_repayment(
        group_id: str, person: str, amount: float,
        paid_date: date | None = None, notes: str | None = None,
    ) -> dict:
        """Record that a group member paid the user back."""
        return agent_api.add_split_repayment(group_id, agent_api.SplitRepayment(
            person=person, amount=amount, paid_date=paid_date, notes=notes,
        ))

    return mcp


def main() -> None:
    # stdout carries the MCP protocol; logs must go to stderr.
    logging.basicConfig(level=logging.WARNING)
    build_server().run("stdio")
