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


# ═══════════════════════════════════════════════════════════════════════════
# Setup-wizard polish pack: pywebview-native folder browse, inline validation
# (client mirror of SETUP_LIMITS + lthr<max_hr), intervals.icu prefill tags,
# step-3 copy.
# ═══════════════════════════════════════════════════════════════════════════
import re
import types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _setup_html():
    with open(os.path.join(REPO, "src", "templates", "setup.html"), encoding="utf-8") as f:
        return f.read()


def _fake_webview(dialog_result, windows=True):
    """A stand-in webview module: records create_file_dialog calls."""
    m = types.ModuleType("webview")
    m.FOLDER_DIALOG = 20
    m.calls = []

    class _Win:
        def create_file_dialog(self, dialog_type, **kw):
            m.calls.append(dialog_type)
            return dialog_result

    m.windows = [_Win()] if windows else []
    return m


def _fake_tkinter(picked, hits):
    """Stand-in tkinter + tkinter.filedialog; records askdirectory calls."""
    tk = types.ModuleType("tkinter")
    fd = types.ModuleType("tkinter.filedialog")

    class _Tk:
        def withdraw(self): pass
        def attributes(self, *a): pass
        def destroy(self): pass

    tk.Tk = _Tk

    def askdirectory(**kw):
        hits.append(kw)
        return picked

    fd.askdirectory = askdirectory
    tk.filedialog = fd
    return tk, fd


def test_pick_folder_prefers_webview_dialog(monkeypatch):
    wv = _fake_webview(("/Users/t/Rides",))
    hits = []
    tk, fd = _fake_tkinter("/WRONG", hits)
    monkeypatch.setitem(sys.modules, "webview", wv)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fd)
    r = client.get("/api/setup/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"path": "/Users/t/Rides"}   # tuple result normalised
    assert wv.calls == [wv.FOLDER_DIALOG]
    assert hits == []                               # tkinter never touched


def test_pick_folder_webview_cancel_is_empty_not_tkinter(monkeypatch):
    wv = _fake_webview(None)                        # user cancelled the dialog
    hits = []
    tk, fd = _fake_tkinter("/WRONG", hits)
    monkeypatch.setitem(sys.modules, "webview", wv)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fd)
    r = client.get("/api/setup/pick-folder")
    assert r.json() == {"path": ""}
    assert wv.calls == [wv.FOLDER_DIALOG]
    assert hits == []                               # cancel must NOT re-prompt


def test_pick_folder_falls_back_to_tkinter_without_window(monkeypatch):
    wv = _fake_webview(("/ignored",), windows=False)  # dev/browser run
    hits = []
    tk, fd = _fake_tkinter("/Users/t/GPX", hits)
    monkeypatch.setitem(sys.modules, "webview", wv)
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fd)
    r = client.get("/api/setup/pick-folder")
    assert r.json() == {"path": "/Users/t/GPX"}
    assert wv.calls == []
    assert len(hits) == 1


def test_setup_page_render_smoke():
    r = client.get("/setup")
    assert r.status_code == 200
    for anchor in ('id="step-1"', 'id="step-3"', 'id="save-btn"'):
        assert anchor in r.text


# ── Item 1: inline validation mirrors the server rules ──────────────────────

def test_wizard_static_bounds_mirror_server_table():
    """Parse both sides, assert equal (the static attrs are the offline
    fallback; applyServerLimits overwrites them from the same table)."""
    html = _setup_html()
    ids = {"weight": "s-weight", "ftp": "s-ftp", "lthr": "s-lthr",
           "max_hr": "s-maxhr", "hours_per_week": "s-hours",
           "age": "s-age", "cp": "s-cp", "wprime_j": "s-wprime"}
    for field, iid in ids.items():
        m = re.search(r'<input[^>]*id="%s"[^>]*>' % re.escape(iid), html)
        assert m, f"input #{iid} missing"
        lo = re.search(r'min="([\d.]+)"', m.group(0))
        hi = re.search(r'max="([\d.]+)"', m.group(0))
        assert lo and hi, f"input #{iid} lacks min/max"
        assert [float(lo.group(1)), float(hi.group(1))] == \
            [float(x) for x in app_module.SETUP_LIMITS[field]], \
            f"#{iid} bounds drifted from SETUP_LIMITS[{field}]"


def test_wizard_inline_validation_wiring():
    html = _setup_html()
    # validator + per-field error slots
    assert "function fieldError" in html
    assert "function refreshValidation" in html
    for iid in ("s-weight", "s-ftp", "s-lthr", "s-maxhr",
                "s-age", "s-cp", "s-wprime", "s-hours"):
        assert f'id="err-{iid}"' in html, f"missing error div for #{iid}"
    # validates on blur AND input
    assert "addEventListener('input', refreshValidation)" in html
    assert "addEventListener('blur', refreshValidation)" in html
    # red-at-the-field styling uses the existing vars
    assert "color: var(--red)" in html
    # cross-field mirror of the AC5b server check (same phrasing as the 400)
    assert "must be below max HR" in html
    # Continue (step 2) + Finish gated while invalid
    assert 'id="step2-next"' in html
    assert "n2.disabled = !allOk" in html
    assert "sv.disabled = !allOk" in html
    # required-vs-optional visually distinct
    assert html.count('class="skip-link">(optional)</span>') >= 3  # age, sex, folders


def test_wizard_lthr_bound_and_cross_rule_match_server():
    """The client cross-check exists AND the server still enforces it — the
    client is a mirror, not a replacement (server rules unchanged)."""
    html = _setup_html()
    assert "l >= m" in html                       # client: lthr >= max_hr → error
    import inspect
    src = inspect.getsource(app_module.setup_save)
    assert "must be below max HR" in src          # server: AC5b 400 detail


# ── Item 4: intervals.icu prefill tags ───────────────────────────────────────

def test_wizard_icu_prefill_tags_present():
    html = _setup_html()
    for iid in ("s-ftp", "s-weight", "s-lthr", "s-maxhr"):
        assert f'id="icu-tag-{iid}"' in html, f"missing prefill tag for #{iid}"
    assert html.count("from intervals.icu</span>") == 4
    # tags refresh from the AC5f origin set, and editing clears them because
    # _markTouched(id, false) drops the origin before refreshing
    assert "_icuOrigin.has(id)" in html
    assert html.count("refreshIcuTags()") >= 2    # _markTouched + restore path


# ── Item 3: step-3 copy ──────────────────────────────────────────────────────

def test_step3_copy_plain_english_and_after_finish():
    html = _setup_html()
    step3 = html[html.index('id="step-3"'):html.index('id="step-4"')]
    assert "Virtual Trainer" not in step3         # old jargon gone
    assert "workout library built in" in step3    # folders are optional
    assert "When you finish" in step3             # explains what Finish does
    assert "training plan" in step3
    assert "intervals.icu" in step3
