from __future__ import annotations

import dataclasses
import json

import plaid as plaid_sdk
from fastapi.testclient import TestClient

from hoard import plaid, sync
from hoard.app import app


def _no_credentials(monkeypatch):
    monkeypatch.setattr(
        plaid, "settings", dataclasses.replace(plaid.settings, plaid_client_id="", plaid_secret="")
    )


def test_link_token_without_credentials_is_friendly_400(monkeypatch):
    _no_credentials(monkeypatch)
    resp = TestClient(app).get("/api/link-token")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "CREDENTIALS_MISSING"
    assert "PLAID_CLIENT_ID" in body["detail"]


def test_plaid_api_error_returns_display_message(monkeypatch):
    def boom(item_id):
        exc = plaid_sdk.ApiException(status=400, reason="Bad Request")
        exc.body = json.dumps({
            "error_code": "ITEM_LOGIN_REQUIRED",
            "error_message": "raw",
            "display_message": "Please log in to your bank again.",
        })
        raise exc

    monkeypatch.setattr(sync, "sync_item", boom)
    resp = TestClient(app, raise_server_exceptions=False).post("/api/items/x/sync")
    assert resp.status_code == 502
    assert resp.json() == {
        "detail": "Please log in to your bank again.",
        "error_code": "ITEM_LOGIN_REQUIRED",
    }


def test_missing_token_is_409(make_account, monkeypatch):
    def missing(item_id):
        raise sync.MissingTokenError(item_id)

    monkeypatch.setattr(sync, "sync_item", missing)
    resp = TestClient(app, raise_server_exceptions=False).post("/api/items/x/sync")
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "ACCESS_TOKEN_MISSING"
