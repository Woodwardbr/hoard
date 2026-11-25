# Letting AI agents use Hoard

Hoard can be used by AI agents in two ways:

- **MCP server** (`hoard mcp`): for agents running on the same computer, such as
  Claude Code or the Claude desktop app. No network setup or token needed.
- **Agent API** (`/api/agent/*`): a token-protected HTTP API on its own listener, for
  agents and automations on another machine (for example over Tailscale).

Both are **off by default**. Both offer the same actions, and both are **read-only unless
you allow writes** (see [Writes](#writes)).

> **Privacy:** anything an agent reads is sent to that agent's AI provider. Your data stays
> on your computer, but asking an agent "what did I spend on food?" sends the answer to
> its servers. Only connect agents you'd trust with your financial details.

## MCP server (Claude Code, Claude Desktop, other MCP clients)

The agent starts `hoard mcp` itself and talks to it over stdin/stdout.

**Claude Code** (from a checkout, so it finds your `.env` and data):

```bash
claude mcp add hoard -- uv --directory /path/to/hoard run hoard mcp
```

If you installed the `hoard` command (`uv tool install .`) and keep your `.env` in
the per-user data folder, this is enough:

```bash
claude mcp add hoard -- hoard mcp
```

**Claude Desktop** (Settings, Developer, Edit Config), same command as JSON:

```json
{
  "mcpServers": {
    "hoard": {
      "command": "uv",
      "args": ["--directory", "/path/to/hoard", "run", "hoard", "mcp"]
    }
  }
}
```

Tools: `get_accounts`, `get_net_worth`, `get_spending_summary`, `get_budgets`,
`search_transactions`, `get_portfolio`, `get_debt_summary`, `get_payoff_balances`,
`list_split_groups`, `get_split_group`, `refresh_from_banks` (pulls the latest data from
Plaid), and with writes allowed: `record_payment`, `update_loans`, `create_split_group`,
`add_split_members`, `assign_split_expenses`, `unassign_split_expense`,
`record_split_repayment`.

The MCP server only offers write tools when `AGENT_ALLOW_WRITES=1` is set explicitly.

## Agent API (HTTP)

The agent API runs as a **separate listener** that serves only the routes below. The
dashboard and the rest of `/api` stay on `127.0.0.1`, so an agent can't reach them even
with a valid token. To give agents a new capability, add a route to `agent_api.py` (and a
tool to `mcp_server.py`); nothing else is exposed.

Every request needs the header `X-Agent-Token: <AGENT_API_TOKEN>`.

### Read

- `GET /api/agent/health`: status, Plaid environment, and whether writes are allowed.
- `GET /api/agent/accounts`: every account with its latest balance.
- `GET /api/agent/net-worth?history=false`: latest assets, liabilities and net worth
  (`history=true` adds the series).
- `GET /api/agent/spending-summary?period=YYYY-MM`: a month's spending vs normal:
  `categories` (each category's spend that month vs its average over the *other* complete
  months, with `difference` and `pct_of_avg`, biggest overspend first), the month's 10
  `largest_transactions`, and `budgets`. `period` defaults to the last complete month;
  `month_complete: false` means the month is still in progress.
- `GET /api/agent/budgets?period=YYYY-MM|avg`: budget vs actual.
- `GET /api/agent/portfolio`: allocation by sector with percent of total.
- `GET /api/agent/debt-summary`: tracked-loan totals and payoff projection.
- `GET /api/agent/payoff-balances`: what to pay on each credit card (`payoff_amount`), plus
  tracked loans (`tracked_loans`: monthly payment, due date, `auto_pay`, `loan_id`). A
  card's `balance_source` is `"statement"` when Plaid Liabilities is available for it,
  otherwise `"current"` (balance from the last sync; no due date or minimum payment).
- `GET /api/agent/transactions?start=&end=&q=`: search charges.
- `GET /api/agent/split-groups`, `GET /api/agent/split-groups/{id}`: shared-expense groups.

### Write (needs writes allowed)

- `POST /api/agent/record-payment`: `{"account_id": "...", "amount": 123.45,
  "payment_date": "2026-10-01"}`, or with `"loan_id"` to target one tracked loan
  (`account_id` can then be omitted). With `loan_id` you can also send `"new_balance"`
  (the balance your loan servicer shows after the payment) and `"status"`
  (`Scheduled`/`Paid`/`Past Due`); the response then includes `updated_loan`.
- `POST /api/agent/update-loans`: `{"updates": [{"loan_id": "...", "current_balance":
  4950.12, "min_payment": 55.98, "next_due_date": "2026-11-15", "interest_rate": 5.5,
  "status": "Scheduled"}]}`. Every field except `loan_id` is optional; only fields sent
  change.
- `POST /api/agent/split-groups`, `.../members`, `.../expenses`, `.../repayments`, and
  `DELETE .../expenses/{transaction_id}`: manage split groups.

### Setup

1. Generate a token and add it to `.env`:
   ```bash
   openssl rand -hex 32
   # add to .env:
   AGENT_API_TOKEN=<output>
   ```

2. Install Tailscale on the machine running Hoard and on the agent's machine, and
   join both to the same tailnet:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

3. Start Hoard with the agent listener on your Tailscale IP:
   ```bash
   AGENT_HOST=$(tailscale ip -4) uv run python main.py
   ```
   (or set `AGENT_HOST` / `AGENT_PORT` in `.env`; the port defaults to 8001). The
   dashboard stays at `http://127.0.0.1:8000`. Startup refuses to run if `AGENT_HOST` is
   set while `AGENT_API_TOKEN` is empty or `APP_HOST` isn't a loopback address. Leave
   `AGENT_HOST` empty to turn the agent API off.

4. Give the agent the address and token (privately, never in git):
   `http://100.x.y.z:8001/api/agent/...` with the `X-Agent-Token` header.

## Writes

Agent writes only change your local records; nothing moves money. They're off unless you
set `AGENT_ALLOW_WRITES=1` in `.env`. One exception for older setups: if you configured
the HTTP API with the old `MUSE_API_TOKEN` name and never set `AGENT_ALLOW_WRITES`, writes
stay on so an existing Muse setup keeps working. The MCP server ignores that exception.

When writes are off, write routes answer `403` and the MCP write tools aren't offered.

- `update-loans` changes balance, monthly payment, due date, interest rate or status on
  **existing** loans. It cannot create, delete or rename loans, or change autopay. Unknown
  fields are rejected, values are validated, and a batch applies entirely or not at all
  (unknown `loan_id` gives 404 and nothing is written). Balance changes are snapshotted for
  the balance-history chart, same as editing in the Debt Tracker. Each call is logged.
- `record-payment` stores a `loan_payments` row when `loan_id` is given, or a combined
  payment across all loans when the account is a Plaid loan account. Otherwise it returns
  `"stored": false` (card payments arrive via Plaid sync). Autopay loans are accepted
  (autopay can fail and you pay by hand). A payment is refused with `409` only when one is
  already recorded for the same loan (or combined), date and amount; the response's
  `detail.existing_payment_id` names it.
- Split groups: agents can create groups and members, assign and unassign expenses, and
  record repayments. They can't delete or rename groups or people, or delete repayments.

## Notes

- Tracked loans on autopay show their next due date even if the stored one has passed (it
  is advanced month by month when read). Manual-pay loans keep the stored date, so a
  missed payment still shows as overdue.
- `payoff-balances` works without Plaid Liabilities: cards fall back to their current
  balance. Liabilities only adds statement balances, due dates and minimums. Unexpected
  Plaid failures (e.g. `ITEM_LOGIN_REQUIRED`) are listed under `errors`, and the affected
  cards still appear with their current balance.
- The listener serves no `/docs` or OpenAPI schema; everything else answers 404.
- Optionally restrict the agent port to the agent's device with Tailscale ACLs.
- Never commit `.env`. Rotate the token with `openssl rand -hex 32` if it leaks.

## Older Muse setups

The previous names still work: `/api/muse/*` routes, the `X-Muse-Token` header, and
`MUSE_API_TOKEN` / `MUSE_HOST` / `MUSE_PORT` in `.env`. New setups should use the `AGENT_*`
names.

A monthly payoff workflow, for example:
1. `GET payoff-balances` to build a payment plan (skip tracked loans with `auto_pay: true`).
2. `GET spending-summary` to flag categories well above their average.
3. Present the plan for approval, then pay.
4. `POST record-payment` for each payment.
5. After checking the loan servicer, `POST update-loans` with current balances.
