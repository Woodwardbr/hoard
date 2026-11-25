"""Portfolio allocation by sector, holdings overrides, 401(k) plan allocations."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .. import db
from ..db import (
    CASH_SECURITY_ID,
    get_conn,
)
from ..models import (
    AssetDetail,
    SectorAllocation,
)
from ._common import LATEST_BALANCES_CTE

# --- Portfolio: asset allocation by sector --------------------------------

CASH_SECTOR = "Cash"
_UNKNOWN_SECTOR = "Uncategorized"

# Curated ETF → sector map. Plaid labels every ETF as "Miscellaneous", so we
# derive a meaningful sector from the ticker (which encodes the fund's
# strategy). Add new ETFs here as your portfolio grows.
# Ticker → sector mapping for well-known ETFs. This is public reference data,
# not personal — it stays in source code.
ETF_SECTOR_MAP: dict[str, str] = {
    # US broad market
    "SPY": "US Broad Market",
    "RSP": "US Broad Market",
    "SCHB": "US Broad Market",
    # International
    "VXUS": "International Equity",
    # Dividend income
    "VYM": "Dividend Income",
    "SCHD": "Dividend Income",
    "HDV": "Dividend Income",
    # Technology thematic
    "FTEC": "Technology",
    "BOTZ": "Technology",
    # Thematic
    "ICLN": "Clean Energy",
    "PPA": "Aerospace & Defense",
    "VIOV": "US Small-Cap Value",
    # Fixed income
    "BND": "Bonds",
}

# Consolidate Plaid's granular equity sectors into cleaner GICS-style
# groupings. Only applied to equities (ETFs use the ticker map above;
# unknown ETFs fall through to "Diversified ETF").
PLAID_SECTOR_REMAP: dict[str, str] = {
    "Electronic Technology": "Technology",
    "Technology Services": "Technology",
    "Retail Trade": "Consumer Discretionary",
    "Non-Energy Minerals": "Materials",
    "Process Industries": "Materials",
    # Plaid dumps investment trusts / specialty finance under "Miscellaneous".
    "Miscellaneous": "Finance",
}


def _latest_snapshot_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM balances").fetchone()
    return row["d"] if row else None


def _account_balance_sector(name: str, subtype: str | None) -> str:
    """Sector for an investment account surfaced via its balance (no holdings)."""
    n = (name or "").upper()
    s = (subtype or "").lower()
    if "401" in n or "401" in s or "retirement" in s or "IRA" in n:
        return "Retirement"
    if "RESTRICTED" in n or "stock plan" in s or "ESPP" in n:
        return "Equity Compensation"
    return "Other Investments"


def _latest_holdings_snapshot_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM holdings").fetchone()
    return row["d"] if row else None


# --- 401(k) plan allocations ----------------------------------------------

# Load default allocations from a gitignored JSON file keyed by a
# case-insensitive substring of the account name. The file is optional —
# when absent nothing is seeded.  See data/plan-allocations.example.json
# for the expected format.
def _load_default_plan_allocations() -> dict[str, list[tuple[str, str | None, float, str]]]:
    # Resolved via db.settings (not a direct config import) so it stays in
    # sync with whatever path get_conn() is currently using — tests patch
    # db.settings to point at a throwaway DB, and this must follow it.
    alloc_path = Path(db.settings.db_path).parent / "plan-allocations.json"
    if not alloc_path.exists():
        return {}
    with open(alloc_path) as fh:
        raw = json.load(fh)
    return {
        key: [(r["label"], r.get("ticker"), r["allocation_pct"], r["sector"]) for r in rows]
        for key, rows in raw.items()
    }


def seed_default_plan_allocations() -> int:
    """Idempotently insert the curated default allocations for any matching
    401(k) accounts that currently have none. Returns the number of rows
    inserted."""
    default = _load_default_plan_allocations()
    if not default:
        return 0
    inserted = 0
    with get_conn() as conn:
        accts = conn.execute(
            "SELECT account_id, name FROM accounts WHERE kind = 'investment'"
        ).fetchall()
        for acct in accts:
            name_u = (acct["name"] or "").upper()
            for pattern, rows in default.items():
                if pattern.upper() not in name_u:
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM plan_allocations WHERE account_id = ? LIMIT 1",
                    (acct["account_id"],),
                ).fetchone()
                if existing:
                    continue
                for label, ticker, pct, sector in rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO plan_allocations "
                        "(account_id, label, ticker, allocation_pct, sector) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (acct["account_id"], label, ticker, pct, sector),
                    )
                    inserted += 1
    return inserted


def list_plan_allocations() -> list[dict]:
    """All plan allocations with account/institution context, for the UI."""
    with get_conn() as conn:
        rows = conn.execute(
            LATEST_BALANCES_CTE
            + """
            SELECT pa.account_id, pa.label, pa.ticker, pa.allocation_pct, pa.sector,
                   a.name AS account_name, a.subtype, i.institution,
                   b.current AS balance
            FROM plan_allocations pa
            JOIN accounts a USING (account_id)
            JOIN items i USING (item_id)
            LEFT JOIN latest_balances b ON b.account_id = a.account_id
            ORDER BY i.institution, a.name, pa.allocation_pct DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def plan_allocations_for_account(account_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT label, ticker, allocation_pct, sector "
            "FROM plan_allocations WHERE account_id = ? "
            "ORDER BY allocation_pct DESC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_plan_allocation(
    account_id: str, label: str, ticker: str | None, allocation_pct: float, sector: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO plan_allocations "
            "(account_id, label, ticker, allocation_pct, sector) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, label) DO UPDATE SET "
            "ticker=excluded.ticker, "
            "allocation_pct=excluded.allocation_pct, "
            "sector=excluded.sector",
            (account_id, label, ticker, allocation_pct, sector),
        )


def delete_plan_allocation(account_id: str, label: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM plan_allocations WHERE account_id = ? AND label = ?",
            (account_id, label),
        )


def _plan_allocation_rows(conn) -> list[sqlite3.Row]:
    """Plan allocations joined with the account's latest balance, for roll-up."""
    return conn.execute(
        LATEST_BALANCES_CTE
        + """
        SELECT pa.account_id, pa.label, pa.ticker, pa.allocation_pct, pa.sector,
               a.name AS account_name, a.subtype, i.institution,
               b.current AS balance
        FROM plan_allocations pa
        JOIN accounts a USING (account_id)
        JOIN items i USING (item_id)
        LEFT JOIN latest_balances b ON b.account_id = a.account_id
        """
    ).fetchall()


def _infer_sector(
    *,
    security_type: str | None,
    ticker: str | None,
    plaid_sector: str | None,
    industry: str | None,
    is_cash_equivalent: bool,
    override: str | None,
    security_id: str | None = None,
) -> str:
    """Effective sector for a holding, late-bound at query time.

    Priority (highest first):
      1. ``override_sector`` (manual one-off reclassify)
      2. Cash sentinel rows (no securities join partner)
      3. ETF ticker → curated sector (``ETF_SECTOR_MAP``)
      4. Security ``type`` signals (cryptocurrency / cash)
      5. Industry hint (Aerospace & Defense)
      6. Plaid sector, consolidated via ``PLAID_SECTOR_REMAP``
      7. 'Uncategorized'
    """
    if override:
        return override

    # Cash sentinel rows have no securities join partner.
    if security_id == CASH_SECURITY_ID:
        return CASH_SECTOR

    # ETFs: derive from curated ticker map. Unknown ETFs → "Diversified ETF".
    if security_type == "etf":
        if ticker and ticker in ETF_SECTOR_MAP:
            return ETF_SECTOR_MAP[ticker]
        return "Diversified ETF"

    # Cryptocurrency (Plaid leaves sector blank).
    if security_type == "cryptocurrency":
        return "Cryptocurrency"

    # Cash-type securities (e.g. CUR:USD sweep positions).
    if security_type == "cash" or (is_cash_equivalent and not plaid_sector):
        return CASH_SECTOR

    # Industry hint: aerospace & defense (Plaid sometimes buries these under
    # "Electronic Technology").
    if industry and "Aerospace" in industry:
        return "Aerospace & Defense"

    # Consolidate Plaid's equity sectors.
    if plaid_sector:
        return PLAID_SECTOR_REMAP.get(plaid_sector, plaid_sector)

    return _UNKNOWN_SECTOR


def _portfolio_assets(conn: sqlite3.Connection) -> list[AssetDetail]:
    """Every portfolio position as an AssetDetail, sector already resolved.

    Sources, in order: depository cash, investment holdings, investment accounts
    with no holdings, and manual 401(k) plan allocations. Accounts flagged
    exclude_from_net_worth are omitted (e.g. unvested restricted stock); accounts
    with plan_allocations rows are represented only by their fund breakdown,
    never twice. Non-positive values are skipped.
    """
    if not _latest_snapshot_date(conn):
        return []
    out: list[AssetDetail] = []

    def add(value, **fields) -> None:
        val = float(value or 0.0)
        if val > 0:
            out.append(AssetDetail(value=val, **fields))

    # Cash from depository accounts (latest snapshot per account).
    for r in conn.execute(
        LATEST_BALANCES_CTE
        + """
        SELECT a.account_id, a.name, b.current, i.institution
        FROM accounts a
        JOIN latest_balances b USING (account_id)
        JOIN items i USING (item_id)
        WHERE a.kind = 'depository'
          AND a.exclude_from_net_worth = 0
        """
    ):
        add(
            r["current"], account_id=r["account_id"], account_name=r["name"],
            institution=r["institution"], security_id=None, ticker=None,
            name=f"{r['name']} (cash)", sector=CASH_SECTOR, quantity=None, cost_basis=None,
        )

    # Holdings from investment accounts (latest holdings snapshot), excluding
    # 401(k) accounts covered by plan_allocations.
    for r in conn.execute(
        """
        SELECT h.account_id, h.security_id, h.institution_value,
               h.quantity, h.cost_basis, h.override_sector,
               s.sector, s.industry, s.type, s.is_cash_equivalent,
               s.ticker, s.name,
               a.name AS account_name, i.institution
        FROM holdings h
        JOIN accounts a USING (account_id)
        JOIN items i USING (item_id)
        LEFT JOIN securities s USING (security_id)
        WHERE h.snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
          AND a.exclude_from_net_worth = 0
          AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
        """
    ):
        add(
            r["institution_value"], account_id=r["account_id"],
            account_name=r["account_name"], institution=r["institution"],
            security_id=r["security_id"], ticker=r["ticker"],
            name=r["name"] or r["ticker"] or "Holding",
            sector=_infer_sector(
                security_type=r["type"],
                ticker=r["ticker"],
                plaid_sector=r["sector"],
                industry=r["industry"],
                is_cash_equivalent=bool(r["is_cash_equivalent"]),
                override=r["override_sector"],
                security_id=r["security_id"],
            ),
            quantity=float(r["quantity"]) if r["quantity"] is not None else None,
            cost_basis=float(r["cost_basis"]) if r["cost_basis"] is not None else None,
        )

    # Investment accounts with NO holdings (e.g. 401k, ESPP, restricted stock):
    # surface the account balance instead.
    for r in conn.execute(
        LATEST_BALANCES_CTE
        + """
        SELECT a.account_id, a.name, a.subtype, b.current, i.institution
        FROM accounts a
        JOIN latest_balances b USING (account_id)
        JOIN items i USING (item_id)
        WHERE a.kind = 'investment'
          AND a.exclude_from_net_worth = 0
          AND a.account_id NOT IN (
              SELECT DISTINCT account_id FROM holdings
              WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
          )
          AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
        """
    ):
        add(
            r["current"], account_id=r["account_id"], account_name=r["name"],
            institution=r["institution"], security_id=None, ticker=None, name=r["name"],
            sector=_account_balance_sector(r["name"], r["subtype"]),
            quantity=None, cost_basis=None,
        )

    # Manual 401(k) plan allocations: each fund's slice = balance * pct / 100.
    for r in _plan_allocation_rows(conn):
        add(
            float(r["balance"] or 0.0) * float(r["allocation_pct"]) / 100.0,
            account_id=r["account_id"], account_name=r["account_name"],
            institution=r["institution"], security_id=None, ticker=r["ticker"],
            name=r["label"], sector=r["sector"], quantity=None, cost_basis=None,
        )
    return out


def portfolio_by_sector() -> list[SectorAllocation]:
    """Portfolio assets aggregated by effective sector, largest first."""
    with get_conn() as conn:
        assets = _portfolio_assets(conn)
    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for a in assets:
        totals[a.sector]["total"] += a.value
        totals[a.sector]["n"] += 1
    return [
        SectorAllocation(sector=sec, total=agg["total"], asset_count=agg["n"])
        for sec, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def assets_in_sector(sector: str) -> list[AssetDetail]:
    """Individual assets whose effective sector is ``sector``, largest first."""
    with get_conn() as conn:
        assets = _portfolio_assets(conn)
    return sorted((a for a in assets if a.sector == sector), key=lambda a: -a.value)


def set_holding_sector_override(
    account_id: str, security_id: str | None, sector: str | None
) -> None:
    """Set or clear (None) the per-holding sector override.

    Applies to the latest snapshot's row for this account+security pair; the
    override persists across future syncs because sync_holdings omits
    ``override_sector`` from its UPDATE clause.
    """
    with get_conn() as conn:
        if security_id is None:
            # Cash positions have no security_id and can't be reclassified.
            raise ValueError("Cash positions cannot be reclassified")
        conn.execute(
            """
            UPDATE holdings SET override_sector = ?
            WHERE account_id = ? AND security_id = ?
              AND snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
            """,
            (sector, account_id, security_id),
        )


def sector_options() -> list[str]:
    """Distinct sector labels in play — the curated ETF/sector set plus any
    currently in use via Plaid metadata or overrides. Used to populate the
    reclassify dropdown on the portfolio page."""
    static = sorted(set(ETF_SECTOR_MAP.values()) | {
        CASH_SECTOR,
        "Cryptocurrency",
        "Technology",
        "Finance",
        "Healthcare",
        "Consumer Discretionary",
        "Consumer Staples",
        "Communication Services",
        "Industrials",
        "Energy",
        "Materials",
        "Real Estate",
        "Utilities",
        "Aerospace & Defense",
        "Diversified ETF",
        "Retirement",
        "Equity Compensation",
        "Other Investments",
        "US Mid Cap",
        _UNKNOWN_SECTOR,
    })
    with get_conn() as conn:
        used = {
            r["sector"]
            for r in conn.execute(
                "SELECT DISTINCT sector FROM securities WHERE sector IS NOT NULL"
            )
        }
        used.update(
            r["override_sector"]
            for r in conn.execute(
                "SELECT DISTINCT override_sector FROM holdings "
                "WHERE override_sector IS NOT NULL"
            )
        )
    # Drop raw Plaid sector labels that are remapped at query time — they
    # never surface as final sectors, so offering them in the reclassify
    # dropdown would be misleading and reintroduce the messy taxonomy.
    used -= set(PLAID_SECTOR_REMAP)
    return sorted(set(static) | used)
