"""v3.11.5 — imported rides reach intervals.icu again (ACTIVITY:WRITE).

A Linux rider's log: `icu_fit_upload_auth status=403` after every import. The
sign-in requested ACTIVITY:READ only, so ICU refused the POST, and the
dashboard never showed the failure. Pinned here: the scope set, the granted-
scope parser, the capability check, the import endpoint's skip-with-hint and
403-with-hint, and the diagnostics fields.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import config
import training


def _fake_pm(granted: str):
    from profile_manager import ProfileManager
    ns = types.SimpleNamespace(icu_granted_scopes=granted)
    ns.icu_has_scope = lambda scope, _ns=ns: ProfileManager.icu_has_scope(_ns, scope)
    return ns


@pytest.fixture(autouse=True)
def _clean_creds():
    """Credentials are module attributes set at runtime; clear before AND after."""
    def clear():
        for a in ("ICU_ACCESS_TOKEN", "ICU_API_KEY"):
            try:
                delattr(config, a)
            except AttributeError:
                pass
    clear()
    yield
    clear()


def _creds(monkeypatch, *, token=None, key=None):
    if token:
        config.ICU_ACCESS_TOKEN = token
    if key:
        config.ICU_API_KEY = key


def _stamp(monkeypatch, granted: str):
    """Patch the scope stamp on the REAL (sandboxed) ProfileManager class so the
    rest of the app (ride storage, profile paths) keeps working."""
    from profile_manager import ProfileManager
    monkeypatch.setattr(ProfileManager, "icu_granted_scopes", property(lambda self: granted))


# ── the scope set ────────────────────────────────────────────────────────────

def test_sign_in_asks_for_activity_write_once_per_area():
    scopes = config.ICU_OAUTH_SCOPES.split(",")
    assert "ACTIVITY:WRITE" in scopes
    assert "ACTIVITY:READ" not in scopes          # WRITE implies READ; READ+WRITE = "Duplicate scope"
    areas = [s.split(":")[0] for s in scopes]
    assert len(areas) == len(set(areas))


# ── granted-scope parser ─────────────────────────────────────────────────────

@pytest.mark.parametrize("granted,scope,want", [
    ("ACTIVITY:WRITE,CALENDAR:WRITE", "ACTIVITY:WRITE", True),
    ("activity:write calendar:write", "ACTIVITY:WRITE", True),
    ("ACTIVITY:READ,CALENDAR:WRITE", "ACTIVITY:WRITE", False),
    ("", "ACTIVITY:WRITE", False),                 # legacy connection, no stamp
    ("ACTIVITY:WRITE", "CALENDAR:WRITE", False),
])
def test_icu_has_scope(granted, scope, want):
    assert _fake_pm(granted).icu_has_scope(scope) is want


def test_calendar_write_ok_still_uses_the_stamp(monkeypatch):
    import icu_calendar_push as icp
    _creds(monkeypatch, token="tok")
    assert icp.write_ok(_fake_pm("ACTIVITY:WRITE,CALENDAR:WRITE")) is True
    assert icp.write_ok(_fake_pm("ACTIVITY:WRITE")) is False


# ── capability ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,key,granted,want", [
    (None, "apikey", "", True),                    # API key: full access
    ("tok", None, "ACTIVITY:WRITE,WELLNESS:READ", True),
    ("tok", None, "ACTIVITY:READ,WELLNESS:READ", False),   # every pre-3.11.5 sign-in
    ("tok", None, "", False),                      # legacy, unstamped
    (None, None, "", False),                       # not connected
])
def test_icu_can_upload(monkeypatch, token, key, granted, want):
    import app as app_module
    from profile_manager import ProfileManager
    _creds(monkeypatch, token=token, key=key)
    _stamp(monkeypatch, granted)
    assert app_module._icu_can_upload() is want


# ── the import endpoint ──────────────────────────────────────────────────────

def _fit_bytes(tmp_path: Path) -> bytes:
    from test_hrtss_ingestion import _build_fit
    return _build_fit(tmp_path / "ride.fit", n=120, power=200, hr=140).read_bytes()


def _import(monkeypatch, tmp_path, *, token=None, key=None, granted="", upload_result=None):
    import app as app_module
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager
    _creds(monkeypatch, token=token, key=key)
    _stamp(monkeypatch, granted)
    monkeypatch.setattr(app_module, "_maybe_auto_reforecast", lambda *a, **k: None)
    calls = []

    def fake_upload(path, retry=1):
        calls.append(str(path))
        return upload_result or {"ok": True, "status": 200, "detail": "uploaded"}
    monkeypatch.setattr(training, "upload_fit_to_icu", fake_upload)
    r = TestClient(app_module.app).post(
        "/api/ride/import", files={"fit_file": ("ride.fit", _fit_bytes(tmp_path), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["icu_upload"], calls


def test_import_skips_upload_and_asks_for_reconnect_when_scope_missing(monkeypatch, tmp_path):
    up, calls = _import(monkeypatch, tmp_path, token="tok", granted="ACTIVITY:READ,WELLNESS:READ")
    assert calls == []                              # never hit intervals.icu with a 403 in store
    assert up["needs_reconnect"] is True and up["skipped"] is True and up["ok"] is False


def test_import_uploads_when_scope_granted(monkeypatch, tmp_path):
    up, calls = _import(monkeypatch, tmp_path, token="tok", granted="ACTIVITY:WRITE,WELLNESS:READ")
    assert len(calls) == 1 and up["ok"] is True and not up.get("needs_reconnect")


def test_import_with_api_key_uploads(monkeypatch, tmp_path):
    up, calls = _import(monkeypatch, tmp_path, key="apikey")
    assert len(calls) == 1 and up["ok"] is True


def test_import_403_under_oauth_says_reconnect(monkeypatch, tmp_path):
    up, calls = _import(monkeypatch, tmp_path, token="tok", granted="ACTIVITY:WRITE",
                        upload_result={"ok": False, "status": 403, "detail": "auth_failed"})
    assert len(calls) == 1 and up["needs_reconnect"] is True


def test_import_403_with_api_key_has_no_reconnect_hint(monkeypatch, tmp_path):
    up, calls = _import(monkeypatch, tmp_path, key="apikey",
                        upload_result={"ok": False, "status": 403, "detail": "auth_failed"})
    assert len(calls) == 1 and not up.get("needs_reconnect")


# ── diagnostics ──────────────────────────────────────────────────────────────

def test_diag_reports_capabilities_without_secrets(monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    from profile_manager import ProfileManager
    _creds(monkeypatch, token="tok")
    _stamp(monkeypatch, "ACTIVITY:WRITE,CALENDAR:WRITE")
    app_module._DIAG_HEALTH_CACHE["result"] = None
    app_module._DIAG_HEALTH_CACHE["ts"] = 0.0
    r = TestClient(app_module.app).get("/api/diag/health")
    oa = r.json()["checks"]["icu_oauth"]
    assert oa["granted_scopes"] == ["ACTIVITY:WRITE", "CALENDAR:WRITE"]
    assert oa["can_upload_activities"] is True and oa["can_write_calendar"] is True
    assert "tok" not in r.text
