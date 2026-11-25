from __future__ import annotations

import dataclasses
import json

import plaid as plaid_sdk
import pytest
from fastapi.testclient import TestClient

from hoard import config, plaid
from hoard.app import app

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr(
        plaid, "settings", dataclasses.replace(plaid.settings, plaid_client_id="", plaid_secret="")
    )


@pytest.fixture
def shared_settings(monkeypatch):
    """apply_plaid_credentials mutates config.settings in place; restore it afterwards."""
    before = dataclasses.asdict(config.settings)
    yield
    for k, v in before.items():
        object.__setattr__(config.settings, k, v)


def test_pages_redirect_to_setup_without_keys(no_keys):
    resp = TestClient(app).get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    page = TestClient(app).get("/setup")
    assert page.status_code == 200 and "Create your free Plaid account" in page.text
    assert 'data-start="welcome"' in page.text


def test_setup_with_keys_starts_at_keys_step():
    page = TestClient(app).get("/setup")
    assert 'data-start="keys"' in page.text


def test_save_keys_validates_then_stores_in_keychain(
    monkeypatch, memory_keyring, shared_settings
):
    seen = {}
    monkeypatch.setattr(
        plaid, "validate_credentials", lambda c, s, e: seen.update(c=c, s=s, e=e)
    )
    resp = TestClient(app).post(
        "/api/setup/keys",
        json={"client_id": " abcdefghijkl ", "secret": "secretsecret1", "environment": "sandbox"},
        headers=ORIGIN,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved_to"] == "keychain"
    assert seen == {"c": "abcdefghijkl", "s": "secretsecret1", "e": "sandbox"}
    store = memory_keyring.store
    assert store[("hoard", "plaid_client_id")] == "abcdefghijkl"
    assert store[("hoard", "plaid_secret_sandbox")] == "secretsecret1"
    assert store[("hoard", "plaid_env")] == "sandbox"
    assert config.settings.plaid_client_id == "abcdefghijkl"  # applied without restart


def test_rejected_keys_give_friendly_error(monkeypatch, memory_keyring):
    def reject(*_):
        exc = plaid_sdk.ApiException(status=400, reason="Bad Request")
        exc.body = json.dumps({"error_code": "INVALID_API_KEYS", "error_message": "x"})
        raise exc

    monkeypatch.setattr(plaid, "validate_credentials", reject)
    resp = TestClient(app).post(
        "/api/setup/keys",
        json={"client_id": "abcdefghijkl", "secret": "secretsecret1"},
        headers=ORIGIN,
    )
    assert resp.status_code == 400
    assert "didn't accept those keys" in resp.json()["detail"]
    assert not memory_keyring.store


def test_keys_fall_back_to_private_env_file_without_keychain(tmp_path, monkeypatch):
    import keyring
    from keyring.backends import fail

    keyring.set_keyring(fail.Keyring())
    monkeypatch.setenv("HOARD_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("APP_PORT=8123\nPLAID_SECRET=old\n")

    assert config.save_plaid_credentials("cid", "sec", "production") == "env"

    env = tmp_path / ".env"
    assert env.read_text().splitlines() == [
        "APP_PORT=8123", "PLAID_CLIENT_ID=cid", "PLAID_SECRET=sec", "PLAID_ENV=production",
    ]
    assert oct(env.stat().st_mode & 0o777) == "0o600"
