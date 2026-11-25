from __future__ import annotations

from fastapi.testclient import TestClient

from hoard import sync
from hoard.app import app


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_same_origin_post_allowed(monkeypatch):
    monkeypatch.setattr(sync, "sync_all", lambda: {"ok": True})
    resp = _client().post("/api/sync", headers={"Origin": "http://127.0.0.1:8000"})
    assert resp.status_code == 200


def test_cross_site_post_blocked(monkeypatch):
    called = []
    monkeypatch.setattr(sync, "sync_all", lambda: called.append(1))
    resp = _client().post("/api/sync", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert not called


def test_null_origin_blocked():
    assert _client().post("/api/sync", headers={"Origin": "null"}).status_code == 403


def test_cross_site_fetch_metadata_blocked_without_origin():
    resp = _client().delete("/api/items/x", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_script_without_origin_allowed(monkeypatch):
    monkeypatch.setattr(sync, "sync_all", lambda: {"ok": True})
    assert _client().post("/api/sync").status_code == 200


def test_dns_rebinding_host_blocked():
    resp = TestClient(app, base_url="http://evil.example:8000").get("/api/net-worth")
    assert resp.status_code == 403


def test_localhost_get_allowed():
    assert TestClient(app, base_url="http://localhost:8000").get("/api/health").status_code == 200
