# Hoard

**Your finances, guarded on your own computer.**

Hoard is a private personal finance dashboard. It syncs your bank accounts, credit cards,
brokerages and loans through Plaid and shows net worth, spending, budgets, portfolio
allocation, debt payoff and shared-expense splits in your browser. Everything runs and is
stored on your own computer: no company account, no subscription, no one else's server.
Hoard only reads your data; it never moves money.

## Features

- **Net worth over time**: assets (depository + investment) minus liabilities (credit + loan).
- **Spending by category**, with month-vs-average comparisons and subscription detection.
- **Budgets vs actuals**: monthly per-category limits, plus merchant rules to recategorize.
- **Portfolio**: allocation by sector across brokerages, with manual 401(k) fund breakdowns.
- **Debt Tracker**: loans, payments, balance history and payoff projections by strategy.
- **Splits**: assign card transactions to groups and track who owes what.
- **Privacy mode**: one click hides every dollar amount so you can share your screen.
- **AI agents (optional)**: an MCP server for Claude Code / Claude Desktop and a token-protected
  HTTP API, read-only unless you allow writes. See [docs/AGENT_API.md](docs/AGENT_API.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).

```bash
# 1. Install deps
uv sync

# 2. Configure Plaid credentials
cp .env.example .env
#   fill in PLAID_CLIENT_ID and PLAID_SECRET from https://dashboard.plaid.com
#   (or PLAID_SECRET_SANDBOX / PLAID_SECRET_PRODUCTION to keep both)
#   set PLAID_ENV to sandbox or production

# 3. Run (opens the dashboard in your browser)
uv run python main.py            # add --no-browser, or set HOARD_NO_BROWSER=1, to skip
```

To get a `hoard` command you can run from anywhere: `uv tool install .`

## Usage

1. Click **Link Account** to connect a bank, card, brokerage or loan through Plaid Link.
2. Click **Sync** to pull the latest balances and transactions.
3. Visit **Budgets** to set per-category monthly limits.
4. **Settings** lists linked banks (sync, reconnect or remove each), where your data lives,
   and how your keys are stored.
5. The **Privacy** button in the top bar masks amounts on every page.

## Where your data lives

- **Settings (`.env`)**: Hoard looks for `.env` in the current folder first, then in
  the per-user data folder: `%LOCALAPPDATA%\hoard` (Windows),
  `~/Library/Application Support/hoard` (macOS), `~/.local/share/hoard` (Linux).
  Set `HOARD_HOME` to use a different folder.
- **Database**: `DB_PATH` from `.env`. Relative paths are relative to the `.env`'s folder,
  so `DB_PATH=data/hoard.db` in a checkout always means that checkout's `data/`,
  wherever you launch from. If unset, it's `hoard.db` in the data folder.
- **Backups**: a copy of the database is taken once a day on startup, into `backups/` next
  to the database; the latest 14 are kept.
- **Secrets**: Plaid access tokens (one per linked bank) are stored in the OS keychain
  (Windows Credential Manager, macOS Keychain, or Secret Service on Linux). Existing tokens
  move out of the database automatically on first start, after a `-pre-keyring` backup. If
  no keychain is available (e.g. a headless Linux box) they stay in the database and the
  Settings page says so. Plaid client id/secret can also live in the keychain instead of
  `.env` (service `hoard`, keys `plaid_client_id` and `plaid_secret_<env>`).

## Privacy mode

The **Privacy** toggle hides dollar amounts, chart axes, chart tooltips, account last-4
digits and typed amounts. Percentages and the shapes of charts stay real, so you can show
your allocation or spending mix without the numbers. It's a per-browser setting (a cookie)
and only changes what's on screen: the data and the JSON API are unchanged, so the
numbers are still visible in the browser's developer tools.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## Layout

```
src/hoard/
  app.py          FastAPI app, page routes, privacy filters, error handlers, run()
  api.py          JSON API + Plaid Link endpoints
  agent_api.py    optional token-authenticated API for agents (docs/AGENT_API.md)
  mcp_server.py   MCP server for AI agents (`hoard mcp`)
  local_guard.py  blocks cross-site requests and DNS rebinding
  plaid.py        Plaid SDK wrapper
  sync.py         Plaid -> SQLite sync
  analytics/      queries: networth, spending, budgets, portfolio, debt
  splits.py       shared-expense groups
  categorize.py   keyword-based category fallback
  config.py       settings, .env discovery, data folder
  keystore.py     OS keychain wrapper
  backup.py       daily database backups
  db.py           SQLite schema + migrations
  templates/      Jinja2 pages
  static/         CSS + JS (Chart.js)
docs/
  AGENT_API.md    MCP server and agent API
```

## Notes

- In sandbox mode use Plaid's [test credentials](https://plaid.com/docs/sandbox/test-credentials/).
- This is a personal local app with no login. Keep `APP_HOST=127.0.0.1` and don't expose
  it to the internet or your local network. It only answers requests addressed to
  localhost, and refuses changes coming from other websites.
