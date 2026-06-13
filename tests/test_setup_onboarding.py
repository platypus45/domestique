"""v1.8.25 — onboarding wizard backend: key-only ICU connect (auto-detect
athlete ID) + the Garmin/activity sync verify.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
import training as training_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app_module.app)


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


def _install_fake_httpx(monkeypatch, router):
    """Patch httpx.get (the endpoints `import httpx` then call httpx.get)."""
    import httpx

    def fake_get(url, *a, **k):
        return router(url)
    monkeypatch.setattr(httpx, "get", fake_get)


def test_test_icu_key_only_autodetects_athlete(monkeypatch):
    # discover_athlete_id resolves the id from the key
    monkeypatch.setattr(training_module, "discover_athlete_id",
                        lambda key: {"id": "i999", "name": "Test Rider"})

    def router(url):
        if "/wellness" in url:
            return _FakeResp(200, [{"sportInfo": [{"eftp": 268}], "weight": 71.2}])
        return _FakeResp(200, {"name": "Test Rider", "weight": 71.2})
    _install_fake_httpx(monkeypatch, router)

    r = client.post("/api/setup/test-icu", json={"api_key": "abc123key"})  # no athlete_id
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["athlete_id"] == "i999"      # auto-detected + returned
    assert d["name"] == "Test Rider"
    assert d["eftp"] == 268


def test_test_icu_requires_key(monkeypatch):
    r = client.post("/api/setup/test-icu", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "API Key" in r.json()["error"]


def test_test_icu_undetectable_athlete(monkeypatch):
    monkeypatch.setattr(training_module, "discover_athlete_id", lambda key: None)
    r = client.post("/api/setup/test-icu", json={"api_key": "badkey"})
    assert r.json()["ok"] is False
    assert "athlete" in r.json()["error"].lower()


def test_check_activities_counts_and_flags_garmin(monkeypatch):
    monkeypatch.setattr(training_module, "discover_athlete_id",
                        lambda key: {"id": "i999", "name": "T"})
    acts = [
        {"source": "GARMIN_CONNECT", "start_date_local": "2026-06-10T07:00:00"},
        {"source": "GARMIN_CONNECT", "start_date_local": "2026-06-08T07:00:00"},
        {"source": "STRAVA", "start_date_local": "2026-06-05T07:00:00"},
        {"device_name": "Garmin Edge 540", "start_date_local": "2026-06-02T07:00:00"},
    ]
    _install_fake_httpx(monkeypatch, lambda url: _FakeResp(200, acts))
    r = client.post("/api/setup/check-activities", json={"api_key": "k", "athlete_id": "i999"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 4
    assert d["garmin_count"] == 3          # 2 source + 1 device_name; Strava NOT counted
    assert d["latest_date"] == "2026-06-10"


def test_check_activities_empty(monkeypatch):
    monkeypatch.setattr(training_module, "discover_athlete_id",
                        lambda key: {"id": "i999", "name": "T"})
    _install_fake_httpx(monkeypatch, lambda url: _FakeResp(200, []))
    r = client.post("/api/setup/check-activities", json={"api_key": "k", "athlete_id": "i999"})
    d = r.json()
    assert d["ok"] is True and d["count"] == 0 and d["garmin_count"] == 0


def test_check_activities_auth_fail(monkeypatch):
    _install_fake_httpx(monkeypatch, lambda url: _FakeResp(401, None))
    r = client.post("/api/setup/check-activities", json={"api_key": "k", "athlete_id": "i999"})
    assert r.json()["ok"] is False
    assert "Authentication" in r.json()["error"]


def test_check_activities_needs_key(monkeypatch):
    r = client.post("/api/setup/check-activities", json={})
    assert r.json()["ok"] is False
