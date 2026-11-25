# Contributing to Hoard

## Development setup

Requires [uv](https://docs.astral.sh/uv/); it installs a suitable Python for you.

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Run from a checkout:

```bash
cp .env.example .env        # optional: put Plaid keys here instead of using the wizard
uv run hoard                # opens the browser; --no-browser or HOARD_NO_BROWSER=1 to skip
```

With no Plaid keys, pages redirect to the setup wizard at `/setup`. Plaid's
[Sandbox](https://plaid.com/docs/sandbox/test-credentials/) works for development:
`PLAID_ENV=sandbox` with your Sandbox secret, and log in to test banks as
`user_good` / `pass_good`.

### Demo data

To try the app or take screenshots without a bank:

```bash
HOARD_HOME=/tmp/hoard-demo uv run python scripts/demo_data.py
HOARD_HOME=/tmp/hoard-demo PLAID_CLIENT_ID=demo PLAID_SECRET=demo uv run hoard
```

Everything in the demo data is made up. The fake keys skip the setup wizard; syncing and
linking won't work with them.

## Configuration

Settings come from environment variables or a `.env` file. Hoard looks for `.env` in the
current folder first, then in the data folder (`HOARD_HOME`, or the OS default:
`%LOCALAPPDATA%\hoard`, `~/Library/Application Support/hoard`, `~/.local/share/hoard`).
See [.env.example](.env.example) for every option.

- `DB_PATH`: relative paths are relative to the `.env`'s folder, so a checkout's
  `DB_PATH=data/hoard.db` always means that checkout's `data/`. Default: `hoard.db` in the
  data folder.
- Plaid keys: `.env` first, then the OS keychain (where the setup wizard saves them:
  service `hoard`, keys `plaid_client_id`, `plaid_secret_<env>`, `plaid_env`). Without a
  keychain, the wizard writes a private `.env` in the data folder instead.
- Bank access tokens live in the keychain; `items.access_token = ''` in the database means
  "in the keychain". Go through `sync.resolve_token()` / `sync.store_token()`.
- Data from the app's earlier name (money-mover) is adopted automatically: the data folder
  is moved and keychain entries are migrated on first read.

## Layout

```
src/hoard/
  app.py          FastAPI app, pages, privacy filters, error handlers, CLI (run())
  api.py          JSON API: Plaid Link, setup wizard, budgets, portfolio, debt, quit
  agent_api.py    optional token-authenticated API for agents (docs/AGENT_API.md)
  mcp_server.py   MCP server for AI agents (`hoard mcp`)
  local_guard.py  blocks cross-site requests and DNS rebinding
  plaid.py        Plaid SDK wrapper
  sync.py         Plaid -> SQLite sync
  analytics/      queries: networth, spending, budgets, portfolio, debt
  splits.py       shared-expense groups
  categorize.py   keyword-based category fallback
  config.py       settings, .env discovery, data folder, saving Plaid keys
  keystore.py     OS keychain wrapper
  backup.py       daily database backups
  shortcuts.py    Desktop/Start menu/Applications shortcuts (`hoard shortcuts`)
  db.py           SQLite schema + migrations
  templates/      Jinja2 pages (setup.html is the first-run wizard)
  static/         CSS, JS (Chart.js), icons
install/          one-line installers and uninstallers
scripts/          demo data, installed-app smoke test
docs/             user and agent docs, screenshots
```

AI coding agents: see [AGENTS.md](AGENTS.md).

## Conventions

- Ruff, line length 100. Type hints everywhere; `from __future__ import annotations`.
- No ORM: SQL through `db.get_conn()`. Schema changes go through the idempotent
  migrations in `db.init_db()`.
- Format money with `fmt()` in JS or the `money` Jinja filter, so privacy mode masks it.
- Anything that changes data from the browser must be a POST/PATCH/DELETE (the local guard
  blocks cross-site ones).

## Releases

1. Bump `version` in `pyproject.toml` and commit.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. The Release workflow runs CI on Linux, Windows and macOS (including a real install with
   the installer scripts), then publishes a GitHub Release with the wheel and the install
   scripts. The one-line installers always fetch the latest release.
