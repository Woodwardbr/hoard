from __future__ import annotations

from fastapi.testclient import TestClient

from hoard.app import app


def _dashboard(client: TestClient) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def test_dashboard_shows_amounts_without_privacy(make_account, make_balance):
    make_account("chk", name="Checking")
    make_balance("chk", "2026-11-01", 1234.5)

    html = _dashboard(TestClient(app))

    assert "$1,234.50" in html
    assert "0000" in html  # last 4 from the make_account fixture
    assert 'class="privacy"' not in html


def test_dashboard_masks_amounts_with_privacy_cookie(make_account, make_balance):
    make_account("chk", name="Checking")
    make_balance("chk", "2026-11-01", 1234.5)

    client = TestClient(app, cookies={"hoard_privacy": "1"})
    html = _dashboard(client)

    assert "1,234.50" not in html
    assert "$•••••" in html
    assert "<td>0000</td>" not in html
    assert '<html lang="en" class="privacy">' in html


def test_settings_page_renders(make_account):
    make_account("chk", name="Checking")
    resp = TestClient(app).get("/settings")
    assert resp.status_code == 200
    assert "Test Bank" in resp.text
    assert "Data folder" in resp.text


def test_items_api_never_returns_tokens(make_account):
    make_account("chk")
    items = TestClient(app).get("/api/items").json()
    assert items and "access_token" not in items[0] and "cursor" not in items[0]
    assert items[0]["accounts"] == 1
