from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import keystore, plaid
from .db import CASH_SECURITY_ID, get_conn

log = logging.getLogger(__name__)


class MissingTokenError(RuntimeError):
    """The item's access token is in neither the DB nor the OS keychain."""

    def __init__(self, item_id: str) -> None:
        super().__init__(
            f"No stored access token for item {item_id}; reconnect this bank to fix it."
        )
        self.item_id = item_id


def store_token(item_id: str, token: str) -> str:
    """Put ``token`` in the keychain; return what the ``items.access_token`` column holds.

    ``""`` means "in the keychain". If no keychain is usable the token stays in
    the column, as before.
    """
    return "" if keystore.set(keystore.item_key(item_id), token) else token


def resolve_token(item_id: str, column_value: str | None) -> str:
    """The real access token, given the ``items.access_token`` column value."""
    if column_value:
        return column_value
    token = keystore.get(keystore.item_key(item_id))
    if not token:
        raise MissingTokenError(item_id)
    return token


def link_item(public_token: str, institution: str | None = None) -> str:
    """Exchange a public token from Plaid Link and persist the item."""
    linked = plaid.exchange_public_token(public_token)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO items (item_id, access_token, institution) VALUES (?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                access_token=excluded.access_token,
                institution=COALESCE(excluded.institution, items.institution)
            """,
            (linked.item_id, store_token(linked.item_id, linked.access_token), institution),
        )
    sync_item(linked.item_id)
    return linked.item_id


def sync_all() -> dict[str, Any]:
    """Re-sync every linked item's accounts, balances, and transactions.

    A failure on one item (most often ``ITEM_LOGIN_REQUIRED`` when a bank
    revokes OAuth consent) is recorded in ``errors`` and the remaining items
    still sync, so one broken connection doesn't block the rest.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_id, access_token, institution FROM items"
        ).fetchall()
    totals: dict[str, Any] = {"accounts": 0, "transactions": 0, "holdings": 0, "errors": []}
    for row in rows:
        try:
            counts = sync_item(
                row["item_id"], access_token=resolve_token(row["item_id"], row["access_token"])
            )
        except Exception as exc:
            log.exception("Sync failed for item %s (%s)", row["item_id"], row["institution"])
            totals["errors"].append({
                "item_id": row["item_id"],
                "institution": row["institution"],
                "error_code": plaid.error_code(exc),
                "message": plaid.error_message(exc),
            })
            continue
        totals["accounts"] += counts["accounts"]
        totals["transactions"] += counts["transactions"]
        totals["holdings"] += counts.get("holdings", 0)
    return totals


def access_token_for(item_id: str) -> str:
    """Stored access token for an item; raises KeyError if it isn't linked."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT access_token FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
    if row is None:
        raise KeyError(item_id)
    return resolve_token(item_id, row["access_token"])


def sync_item(item_id: str, access_token: str | None = None) -> dict[str, int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT access_token, cursor FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        if access_token is None:
            access_token = resolve_token(item_id, row["access_token"])
        cursor = row["cursor"]

    accounts = plaid.get_accounts(access_token)
    today = date.today().isoformat()

    with get_conn() as conn:
        for acct in accounts:
            conn.execute(
                """
                INSERT INTO accounts (account_id, item_id, name, mask, kind, subtype, iso_currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    name=excluded.name, mask=excluded.mask,
                    kind=excluded.kind, subtype=excluded.subtype,
                    iso_currency=excluded.iso_currency,
                    item_id=excluded.item_id
                """,
                (
                    acct.account_id, item_id, acct.name, acct.mask,
                    acct.kind, acct.subtype, acct.iso_currency,
                ),
            )
            conn.execute(
                """
                INSERT INTO balances (account_id, snapshot_date, current, available)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, snapshot_date) DO UPDATE SET
                    current=excluded.current, available=excluded.available
                """,
                (acct.account_id, today, acct.current_balance, acct.available_balance),
            )

    page = plaid.get_transactions(access_token, cursor=cursor)
    with get_conn() as conn:
        for tx in page.rows:
            conn.execute(
                """
                INSERT INTO transactions
                  (transaction_id, account_id, date, name, merchant, amount,
                   iso_currency, category_primary, category_detailed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    name=excluded.name, merchant=excluded.merchant, amount=excluded.amount,
                    category_primary=excluded.category_primary,
                    category_detailed=excluded.category_detailed
                """,
                (
                    tx.transaction_id, tx.account_id, tx.date.isoformat(),
                    tx.name, tx.merchant, tx.amount, tx.iso_currency,
                    tx.category_primary, tx.category_detailed,
                ),
            )

        # Apply Plaid's removed-transaction deltas. On the very first sync
        # (cursor was NULL) Plaid reports any stale pending-transaction rows
        # it now considers deleted; on incremental syncs these are the
        # deltas since the last persisted cursor. Either way, drop them.
        if page.removed_ids:
            # Batch deletes in chunks to stay within SQLite's bound-parameter
            # limit (999 by default).
            for i in range(0, len(page.removed_ids), 500):
                chunk = page.removed_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    f"DELETE FROM transactions WHERE transaction_id IN ({placeholders})",
                    chunk,
                )

        # Persist the cursor so the next sync is incremental.
        conn.execute(
            "UPDATE items SET cursor = ? WHERE item_id = ?",
            (page.next_cursor, item_id),
        )

    # Investment holdings, only for logins that include an investment account.
    has_investments = any(a.kind == "investment" for a in accounts)
    holdings_count = sync_holdings(item_id, access_token, today) if has_investments else 0

    return {
        "accounts": len(accounts),
        "transactions": len(page.rows),
        "holdings": holdings_count,
    }


def list_items() -> list[dict]:
    """Linked items with account counts and last sync date. Never includes tokens."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.item_id, i.institution, i.created_at,
                   COUNT(DISTINCT a.account_id) AS accounts,
                   MAX(b.snapshot_date) AS last_synced
            FROM items i
            LEFT JOIN accounts a ON a.item_id = i.item_id
            LEFT JOIN balances b ON b.account_id = a.account_id
            GROUP BY i.item_id
            ORDER BY i.institution
            """
        ).fetchall()
    return [dict(r) for r in rows]


def unlink_item(item_id: str) -> None:
    """Remove a linked item and all its dependent data.

    Deletes in dependency order because the ``transactions`` FK lacks
    ON DELETE CASCADE (older schema). Accounts/balances/holdings do cascade
    from accounts, but we delete explicitly to be safe.
    """
    with get_conn() as conn:
        acct_ids = [
            r["account_id"]
            for r in conn.execute(
                "SELECT account_id FROM accounts WHERE item_id = ?", (item_id,)
            ).fetchall()
        ]
        if acct_ids:
            placeholders = ",".join("?" * len(acct_ids))
            conn.execute(
                f"DELETE FROM transactions WHERE account_id IN ({placeholders})",
                acct_ids,
            )
            conn.execute(
                f"DELETE FROM holdings WHERE account_id IN ({placeholders})",
                acct_ids,
            )
            conn.execute(
                f"DELETE FROM balances WHERE account_id IN ({placeholders})",
                acct_ids,
            )
            conn.execute(
                f"DELETE FROM accounts WHERE account_id IN ({placeholders})",
                acct_ids,
            )
        conn.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
    keystore.delete(keystore.item_key(item_id))


# Plaid errors meaning "this login has no investment data for us", not a failure.
NO_HOLDINGS_CODES = frozenset({
    "ADDITIONAL_CONSENT_REQUIRED",
    "PRODUCTS_NOT_SUPPORTED",
    "PRODUCT_NOT_ENABLED",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_INVESTMENT_AUTH_ACCOUNTS",
    "NO_ACCOUNTS",
})


def sync_holdings(item_id: str, access_token: str, snapshot_date: str) -> int:
    """Fetch & store investment holdings + securities for an item.

    Returns the count of holdings stored. Items without investments consent
    silently contribute 0 (we catch the Plaid error and skip). The
    ``override_sector`` column persists across syncs: for a new snapshot_date
    the INSERT copies it forward from the prior snapshot, and the ON CONFLICT
    branch (same-date re-sync) leaves it untouched.
    """
    try:
        holdings, securities = plaid.get_holdings(access_token)
    except Exception as exc:
        if plaid.error_code(exc) in NO_HOLDINGS_CODES or "PRODUCT_INVESTMENTS" in str(exc):
            return 0
        raise

    with get_conn() as conn:
        for s in securities:
            conn.execute(
                """
                INSERT INTO securities
                  (security_id, ticker, name, type, sector, industry, class, is_cash_equivalent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id) DO UPDATE SET
                    ticker=excluded.ticker, name=excluded.name, type=excluded.type,
                    sector=excluded.sector, industry=excluded.industry,
                    class=excluded.class, is_cash_equivalent=excluded.is_cash_equivalent
                WHERE securities.manual_override = 0
                """,
                (
                    s.security_id, s.ticker, s.name, s.type, s.sector,
                    s.industry, s.cls, int(s.is_cash_equivalent),
                ),
            )

        for h in holdings:
            security_id = h.security_id or CASH_SECURITY_ID
            # Carry the user's manual sector override forward from the most
            # recent prior snapshot for this position, so reclassifications
            # survive each new snapshot. We match two ways:
            #   1. Same security_id (Plaid keeps stable IDs for resolved
            #      securities).
            #   2. Same ticker (Plaid rotates security_ids for securities it
            #      can't fully resolve, e.g. some OTC tickers; the ticker is
            #      the stable user-visible key the override was set against).
            # The ON CONFLICT branch (same-date re-sync) leaves override_sector
            # untouched (it's omitted from the SET list).
            conn.execute(
                """
                INSERT INTO holdings
                  (account_id, security_id, quantity, institution_price,
                   institution_value, cost_basis, snapshot_date, override_sector)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                    COALESCE(
                        (SELECT h0.override_sector FROM holdings h0
                         WHERE h0.account_id = ? AND h0.security_id = ?
                           AND h0.snapshot_date < ?
                         ORDER BY h0.snapshot_date DESC LIMIT 1),
                        (SELECT h0.override_sector FROM holdings h0
                         JOIN securities s0 ON s0.security_id = h0.security_id
                         WHERE h0.account_id = ?
                           AND s0.ticker IS NOT NULL
                           AND s0.ticker = (SELECT ticker FROM securities
                                            WHERE security_id = ?)
                           AND h0.snapshot_date < ?
                         ORDER BY h0.snapshot_date DESC LIMIT 1)
                    ))
                ON CONFLICT(account_id, security_id, snapshot_date) DO UPDATE SET
                    quantity=excluded.quantity,
                    institution_price=excluded.institution_price,
                    institution_value=excluded.institution_value,
                    cost_basis=excluded.cost_basis
                """,
                (
                    h.account_id, security_id, h.quantity,
                    h.institution_price, h.institution_value, h.cost_basis,
                    snapshot_date,
                    # security_id match:
                    h.account_id, security_id, snapshot_date,
                    # ticker fallback:
                    h.account_id, security_id, snapshot_date,
                ),
            )

    return len(holdings)
