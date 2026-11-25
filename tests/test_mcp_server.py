from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hoard import mcp_server

WRITE_TOOLS = {
    "record_payment", "update_loans", "create_split_group", "add_split_members",
    "assign_split_expenses", "unassign_split_expense", "record_split_repayment",
}


def _tool_names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def _call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    return result.structured_content


def test_write_tools_hidden_unless_allowed():
    read_only = _tool_names(mcp_server.build_server(allow_writes=False))
    assert "get_accounts" in read_only and "refresh_from_banks" in read_only
    assert not read_only & WRITE_TOOLS

    assert WRITE_TOOLS <= _tool_names(mcp_server.build_server(allow_writes=True))


def test_read_tools_are_marked_read_only():
    tools = {t.name: t for t in asyncio.run(mcp_server.build_server(False).list_tools())}
    assert tools["get_accounts"].annotations.read_only_hint is True
    assert tools["refresh_from_banks"].annotations.read_only_hint is False


def test_get_accounts_returns_balances(make_account, make_balance):
    make_account("chk", kind="depository", name="Checking")
    make_balance("chk", "2026-08-29", 1500.0)
    out = _call(mcp_server.build_server(False), "get_accounts")
    rows = out["result"] if isinstance(out, dict) and "result" in out else out
    assert rows[0]["name"] == "Checking" and rows[0]["balance"] == 1500.0


def test_bad_input_becomes_readable_tool_error():
    server = mcp_server.build_server(False)
    with pytest.raises(ToolError, match="period"):
        _call(server, "get_budgets", {"period": "nope"})


def test_record_payment_validation_error_is_readable(make_account):
    server = mcp_server.build_server(True)
    with pytest.raises(ToolError, match="account_id is required"):
        _call(server, "record_payment", {"amount": 10})


def test_mcp_writes_need_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("AGENT_ALLOW_WRITES", raising=False)
    monkeypatch.setenv("MUSE_API_TOKEN", "legacy")  # legacy default doesn't apply to MCP
    assert not _tool_names(mcp_server.build_server()) & WRITE_TOOLS
    monkeypatch.setenv("AGENT_ALLOW_WRITES", "1")
    assert WRITE_TOOLS <= _tool_names(mcp_server.build_server())
