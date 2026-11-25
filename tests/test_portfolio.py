from __future__ import annotations

from hoard.analytics import portfolio_by_sector


def test_portfolio_includes_older_synced_account(make_account, make_balance):
    """A depository account last synced a week ago still appears in
    portfolio_by_sector totals."""
    make_account("chk1", kind="depository", institution="Bank A")
    make_account("chk2", kind="depository", institution="Bank B")
    make_balance("chk1", "2026-08-20", 500.0)
    make_balance("chk2", "2026-08-29", 300.0)

    sectors = portfolio_by_sector()
    cash = [s for s in sectors if s.sector == "Cash"]
    assert len(cash) == 1
    # Both accounts should contribute: 500 + 300 = 800
    assert cash[0].total == 800.0

def test_sector_totals_match_asset_details(make_account, make_balance):
    """portfolio_by_sector and assets_in_sector come from one builder, so they agree."""
    import pytest

    from hoard import db
    from hoard.analytics import assets_in_sector

    make_account("chk", kind="depository")
    make_balance("chk", "2026-08-29", 300.0)
    make_account("brk", kind="investment", subtype="brokerage")
    make_balance("brk", "2026-08-29", 1000.0)
    make_account("k401", kind="investment", subtype="401k", name="ACME 401(K)")
    make_balance("k401", "2026-08-29", 2000.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO securities (security_id, ticker, name, type, sector) "
            "VALUES ('s1', 'AAPL', 'Apple', 'equity', 'Electronic Technology')"
        )
        conn.execute(
            "INSERT INTO holdings (account_id, security_id, quantity, institution_value, "
            "snapshot_date) VALUES ('brk', 's1', 2, 400.0, '2026-08-29')"
        )
        conn.execute(
            "INSERT INTO plan_allocations (account_id, label, ticker, allocation_pct, sector) "
            "VALUES ('k401', 'Bond Fund', 'BND', 25, 'Bonds'), "
            "('k401', 'Stock Fund', 'VTI', 75, 'US Broad Market')"
        )

    sectors = portfolio_by_sector()
    assert sectors
    for s in sectors:
        detail = assets_in_sector(s.sector)
        assert sum(a.value for a in detail) == pytest.approx(s.total)
        assert len(detail) == s.asset_count
    by = {s.sector: s.total for s in sectors}
    assert by["Bonds"] == pytest.approx(500.0)
    assert by["US Broad Market"] == pytest.approx(1500.0)
    assert by["Cash"] == pytest.approx(300.0)
