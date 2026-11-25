from __future__ import annotations

from unittest.mock import patch

from hoard import plaid, sync
from hoard.db import get_conn


def test_link_item_twice_yields_one_row():
    """Linking the same item twice yields one items row."""
    with patch.object(sync, "sync_item", return_value={"accounts": 0, "transactions": 0}):
        with patch.object(plaid, "exchange_public_token") as mock_exchange:
            mock_exchange.return_value = plaid.LinkedItem(
                item_id="real-plaid-item-1",
                access_token="access-sandbox-abc",
            )
            with patch.object(plaid, "get_accounts", return_value=[]):
                with patch.object(plaid, "get_transactions", return_value=([], [], "")):
                    with patch.object(plaid, "get_holdings", return_value=([], [])):
                        sync.link_item("public-token-1", institution="Test Bank")
                        sync.link_item("public-token-1", institution="Test Bank")

    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE item_id = ?",
            ("real-plaid-item-1",),
        ).fetchone()["n"]
    assert count == 1


def test_linked_item_dataclass():
    """LinkedItem holds both item_id and access_token."""
    item = plaid.LinkedItem(item_id="abc", access_token="tok123")
    assert item.item_id == "abc"
    assert item.access_token == "tok123"

def _login_required_error() -> Exception:
    import json

    import plaid as plaid_sdk

    exc = plaid_sdk.ApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({
        "error_code": "ITEM_LOGIN_REQUIRED",
        "display_message": "The user's OAuth connection to this institution has been invalidated.",
    })
    return exc


def test_sync_all_continues_past_failed_item():
    """One item needing re-login doesn't stop the other items from syncing."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
            ("broken-item", "tok-broken", "Broken Bank"),
        )
        conn.execute(
            "INSERT INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
            ("good-item", "tok-good", "Good Bank"),
        )

    def fake_sync_item(item_id, access_token=None):
        if item_id == "broken-item":
            raise _login_required_error()
        return {"accounts": 2, "transactions": 5, "holdings": 0}

    with patch.object(sync, "sync_item", side_effect=fake_sync_item) as mock_sync:
        result = sync.sync_all()

    assert mock_sync.call_count == 2
    assert result["accounts"] == 2
    assert result["transactions"] == 5
    assert result["errors"] == [{
        "item_id": "broken-item",
        "institution": "Broken Bank",
        "error_code": "ITEM_LOGIN_REQUIRED",
        "message": "The user's OAuth connection to this institution has been invalidated.",
    }]


def test_update_mode_link_token_omits_products():
    """Plaid rejects products in update mode; the access token must be passed instead."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.link_token_create.return_value = {"link_token": "link-update-1"}
    with patch.object(plaid, "_client", return_value=client):
        assert plaid.create_link_token(access_token="tok-existing") == "link-update-1"
    request = client.link_token_create.call_args.args[0]
    assert request["access_token"] == "tok-existing"
    assert "products" not in request


def test_link_token_endpoint_unknown_item_404():
    from fastapi.testclient import TestClient

    from hoard.app import app

    resp = TestClient(app).get("/api/link-token", params={"item_id": "nope"})
    # 400 if Plaid creds aren't configured in the test env; 404 otherwise.
    assert resp.status_code in (400, 404)
