from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import plaid as plaid_sdk
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from . import analytics, backup, config, keystore, local_guard, plaid, sync
from .api import router as api_router
from .config import settings
from .db import get_conn

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=BASE_DIR / "templates")

PRIVACY_COOKIE = "hoard_privacy"
MASKED_MONEY = "$•••••"


def privacy_on(request: Request) -> bool:
    return request.cookies.get(PRIVACY_COOKIE) == "1"


@pass_context
def _money_filter(ctx, value) -> str:
    """``$1,234.56``, or a mask when privacy mode is on. Sign is dropped (callers use abs)."""
    if privacy_on(ctx["request"]):
        return MASKED_MONEY
    return f"${abs(value or 0):,.2f}"


@pass_context
def _last4_filter(ctx, value) -> str:
    if not value:
        return "—"
    return "••••" if privacy_on(ctx["request"]) else str(value)


TEMPLATES.env.filters["money"] = _money_filter
TEMPLATES.env.filters["last4"] = _last4_filter
TEMPLATES.env.globals["privacy_on"] = privacy_on
TEMPLATES.env.globals["plaid_env"] = settings.plaid_env_value


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Take the daily DB backup, then idempotently seed curated 401(k) allocations."""
    backup.backup_now()
    analytics.seed_default_plan_allocations()
    yield


app = FastAPI(title="Hoard", lifespan=lifespan)
app.middleware("http")(local_guard.middleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(api_router)


# Errors the browser shows to the user: always JSON {detail, error_code}, never a
# stack trace. app.js's api() surfaces ``detail`` in its alerts.
@app.exception_handler(plaid.CredentialsMissing)
async def _credentials_missing(request: Request, exc: plaid.CredentialsMissing) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"detail": str(exc), "error_code": "CREDENTIALS_MISSING"}
    )


@app.exception_handler(plaid_sdk.ApiException)
async def _plaid_api_error(request: Request, exc: plaid_sdk.ApiException) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": plaid.error_message(exc), "error_code": plaid.error_code(exc)},
    )


@app.exception_handler(sync.MissingTokenError)
async def _missing_token(request: Request, exc: sync.MissingTokenError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "error_code": "ACCESS_TOKEN_MISSING"}
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with get_conn() as conn:
        acct_rows = conn.execute(
            analytics.LATEST_BALANCES_CTE
            + """
            SELECT a.name, a.mask, a.kind, a.subtype,
                   a.exclude_from_net_worth,
                   b.current,
                   b.snapshot_date
            FROM accounts a
            LEFT JOIN latest_balances b ON a.account_id = b.account_id
            ORDER BY a.kind, a.name
            """
        ).fetchall()
        # Manually-tracked loans (Debt Tracker) aren't Plaid accounts but are
        # liabilities all the same; list them alongside.
        loan_rows = conn.execute(
            """
            SELECT name, NULL AS mask, 'loan' AS kind, loan_type AS subtype,
                   0 AS exclude_from_net_worth,
                   current_balance AS current,
                   date(updated_at) AS snapshot_date
            FROM loans
            ORDER BY name
            """
        ).fetchall()
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": [dict(r) for r in acct_rows] + [dict(r) for r in loan_rows],
            "plaid_env": settings.plaid_env_value,
        },
    )


@app.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "budgets.html", {"budgets": analytics.list_budgets()}
    )


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request) -> HTMLResponse:
    with get_conn() as conn:
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT item_id, institution FROM items ORDER BY institution"
            ).fetchall()
        ]
        invest_accts = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.account_id, a.name, a.subtype, i.institution
                FROM accounts a
                JOIN items i USING (item_id)
                WHERE a.kind = 'investment'
                ORDER BY i.institution, a.name
                """
            ).fetchall()
        ]
    return TEMPLATES.TemplateResponse(
        request,
        "portfolio.html",
        {"items": items, "investment_accounts": invest_accts},
    )


@app.get("/debt", response_class=HTMLResponse)
def debt_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "debt.html",
        {"plaid_env": settings.plaid_env_value},
    )


@app.get("/splits", response_class=HTMLResponse)
def splits_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "splits.html", {"plaid_env": settings.plaid_env_value}
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    import keyring

    backups = backup.list_backups()
    source = config.PLAID_CREDENTIALS_SOURCE
    env_label = (
        f"your .env file ({config.ENV_FILE})" if config.ENV_FILE else "environment variables"
    )
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "data_folder": settings.db_path.parent,
            "db_path": settings.db_path,
            "env_file": config.ENV_FILE,
            "backup_dir": backup.backup_dir(),
            "backup_count": len(backups),
            "latest_backup": backups[-1].name if backups else None,
            "credentials_source": {
                "env": env_label,
                "keychain": "the OS keychain",
            }.get(source, "not set"),
            "keychain_available": keystore.available(),
            "keychain_backend": type(keyring.get_keyring()).__name__,
            "items": sync.list_items(),
            "agent_api": (
                f"on, at {settings.agent_host}:{settings.agent_port}"
                if settings.agent_host else "off"
            ),
            "agent_api_writes": settings.agent_allow_writes,
            "mcp_writes": config.agent_writes_explicitly_allowed(),
        },
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def dashboard_url() -> str:
    host = settings.app_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.app_port}/"


async def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    for _ in range(300):  # up to ~30s
        if server.started:
            webbrowser.open(url)
            return
        if server.should_exit:
            return
        await asyncio.sleep(0.1)


def run(argv: list[str] | None = None) -> None:
    """Serve the dashboard, plus the agent API on its own listener if AGENT_HOST is set.

    The two are separate apps so that the address an agent can reach only serves
    ``/api/agent/*`` (and ``/api/muse/*``); the dashboard and the rest of ``/api``
    stay on APP_HOST. Opens the dashboard in the default browser once it is up,
    unless ``--no-browser`` is passed or ``HOARD_NO_BROWSER`` is set.

    ``hoard mcp`` instead runs the MCP server on stdin/stdout, for AI
    agents such as Claude Code or Claude Desktop.
    """
    parser = argparse.ArgumentParser(prog="hoard", description="Run Hoard.")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "mcp"], default="serve",
        help="serve (default): run the dashboard; mcp: run the MCP server for AI agents",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="don't open the dashboard in a browser"
    )
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return
    open_browser = not (args.no_browser or os.environ.get("HOARD_NO_BROWSER"))

    configs = [
        uvicorn.Config("hoard.app:app", host=settings.app_host, port=settings.app_port)
    ]
    if settings.agent_host:
        if not settings.agent_api_token:
            raise SystemExit("AGENT_HOST is set but AGENT_API_TOKEN is empty; refusing to start.")
        if not _is_loopback(settings.app_host):
            raise SystemExit(
                "AGENT_HOST is set, so APP_HOST must be a loopback address (e.g. 127.0.0.1); "
                "otherwise an agent could reach the dashboard and the rest of /api."
            )
        configs.append(
            uvicorn.Config(
                "hoard.agent_api:agent_app",
                host=settings.agent_host,
                port=settings.agent_port,
            )
        )

    async def serve_all() -> None:
        servers = [uvicorn.Server(c) for c in configs]
        tasks = [s.serve() for s in servers]
        if open_browser:
            print(f"Opening {dashboard_url()} in your browser…")
            tasks.append(_open_browser_when_ready(servers[0], dashboard_url()))
        await asyncio.gather(*tasks)

    asyncio.run(serve_all())
