"""3.3.1 hotfix (Wave 2B) — surface fixes for the v3.3.0 tester incident.

B1  adjusted-card coherence — the today-card blocks preview follows the
    EFFECTIVE session (was_modified ? adjusted : planned): an HRV-capped Z2
    day renders a Z2 silhouette, never the original threshold blocks.
B2  /api/plan/re-draw routes through the modern retry pair
    (_pick_redraw_candidate's 24-attempt widen_band ladder +
    _accept_redraw_apply) instead of the legacy one-shot match; true
    exhaustion maps to an honest "day keeps its zone targets" UI state.
B3a a deliberately-swapped (user_swapped) or engine-adapted FILELESS
    session is NOT broken_ids-protected — its stale old-type ICU event
    sweeps; a genuinely-broken (transient) session stays protected.
B3b non-2xx ICU responses log step + status + body[:300]
    (EVENT=icu_push_http_error) and carry error_detail through the
    reconcile result → the /api/icu/push status surface.
B4  downloadGeneratedZwo hands the XML to the pywebview save bridge in the
    packaged app (WKWebView ignores synthetic anchor clicks); the anchor
    path remains for real browsers.
B5  AUTOMATIC FTP ingestion (F5 eftp auto-apply, the wizard's eFTP
    prefill fallback) is plausibility-gated: candidates <100 W absolute or
    <60% of a real current FTP are rejected + logged; manual entry via
    /api/settings stays ungated.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402
import icu_calendar_push as icp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Reuse the hermetic ICU-push harness (stubbed HOME + the FakeICU transport
# behind the engine's ONE seam). Importing the fixture functions makes them
# fixtures of THIS module too.
from test_icu_push import (  # noqa: E402,F401
    stub, FakeICU, _apikey, _eid, _sess, _write_plan, D1, D2, D3, TODAY,
)

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "templates" / "dashboard.html"


@pytest.fixture()
def fake(monkeypatch):
    f = FakeICU()
    monkeypatch.setattr(icp, "_http", f)
    return f


def _extract_js_function(src: str, name: str) -> str:
    """Slice `[async ]function <name>(...) {...}` out of dashboard.html by
    brace count — keeps the ``async`` keyword (the test_library_search_v2
    extractor drops it, which breaks functions that ``await``)."""
    start = src.find(f"async function {name}")
    if start < 0:
        start = src.index(f"function {name}")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "OK" in res.stdout


# ═══════════════════════════════════════════════════════════════════════════
# B1 — adjusted-card blocks preview follows the EFFECTIVE session
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_b1_preview_source_follows_effective_session():
    src = DASHBOARD.read_text(encoding="utf-8")
    # 3.4.1 M3: the mod?adjusted:planned choice moved into the shared
    # _effectiveTodaySession (one decision fn for card + day modal) —
    # extract it too; every B1 assertion below is unchanged.
    fn = (_extract_js_function(src, "_effectiveTodaySession") + "\n"
          + _extract_js_function(src, "_todayPreviewSource"))
    harness = fn + """
// The tester's exact incident: HRV-capped threshold→z2 day. The stored plan
// still says threshold (and here even has a matched file) — the preview must
// be the ADJUSTED type's synthetic shape, never the threshold blocks.
let out = _todayPreviewSource({
  was_modified: true,
  planned: {session_type: 'threshold', duration_min: 60, tss_estimate: 90,
            zwo_file: 'threshold_2x20.zwo'},
  adjusted: {session_type: 'z2', duration_min: 45, tss_estimate: 35},
});
if (out.kind !== 'synthetic')
  throw new Error('adjusted day must render synthetic, got ' + out.kind);
if (out.session_type !== 'z2')
  throw new Error('preview must use the ADJUSTED type, got ' + out.session_type);
if (out.duration_min !== 45)
  throw new Error('preview must use the ADJUSTED duration, got ' + out.duration_min);

// Unadjusted + matched file → the real segments path (unchanged behavior).
out = _todayPreviewSource({
  was_modified: false,
  planned: {session_type: 'threshold', duration_min: 60, zwo_file: 't.zwo'},
});
if (out.kind !== 'file' || out.zwo_file !== 't.zwo')
  throw new Error('unadjusted matched day must chart the real file');

// Unadjusted fileless (matcher storm) → synthetic of the PLANNED type.
out = _todayPreviewSource({
  was_modified: false,
  planned: {session_type: 'threshold', duration_min: 60, zwo_file: ''},
});
if (out.kind !== 'synthetic' || out.session_type !== 'threshold')
  throw new Error('fileless unadjusted day must render the planned type');

// Adjusted to rest → no preview at all.
out = _todayPreviewSource({
  was_modified: true,
  planned: {session_type: 'threshold', duration_min: 60, zwo_file: 't.zwo'},
  adjusted: {session_type: 'rest', duration_min: 0},
});
if (out.kind !== 'none')
  throw new Error('rest adjustment must suppress the preview');
console.log('OK');
"""
    _run_node(harness)


def test_b1_load_today_session_wired_to_preview_source():
    """The pure helper must actually FEED the card: the fetch branch keys on
    _prevSrc.zwo_file and the synthetic branch builds from _prevSrc.* — and
    the "Approximate shape" footnote semantics stay."""
    src = DASHBOARD.read_text(encoding="utf-8")
    assert "const _prevSrc = _todayPreviewSource(d);" in src
    assert "encodeURIComponent(_prevSrc.zwo_file)" in src
    assert "buildPowerBlocks(_prevSrc.session_type, _prevSrc.duration_min" in src
    assert "Approximate shape — ride to the zone targets above." in src


# ═══════════════════════════════════════════════════════════════════════════
# B2 — re-draw routes through the modern retry/widen ladder
# ═══════════════════════════════════════════════════════════════════════════

def _mk_week_plan(monday: date) -> dict:
    def _s(offset, stype, dur, tss):
        return {"day": (monday + timedelta(days=offset)).isoformat(),
                "day_name": "X", "session_type": stype, "duration_min": dur,
                "tss_estimate": tss, "description": stype, "zwo_file": "",
                "zwo_name": "", "status": "pending"}
    return {
        "goal": {"type": "general", "hours_per_week": 8.0},
        "phases": [],
        "weeks": [{"week_num": 1, "start": monday.isoformat(),
                   "end": (monday + timedelta(days=6)).isoformat(),
                   "phase": "base", "tss_target": 200, "is_stepback": False,
                   "sessions": [_s(0, "rest", 0, 0), _s(1, "z2", 60, 45),
                                _s(2, "threshold", 60, 90)]}],
        "generated": "2026-07-01T00:00:00",
    }


class RedrawRetryBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        (self._tmp / "current_plan.json").write_text(
            json.dumps(_mk_week_plan(self._monday)))
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self._orig_match = tp.match_zwo
        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.match_zwo = self._orig_match
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())


class TestB2RedrawWidenLadder(RedrawRetryBase):
    def test_widened_band_rescues_empty_exact_band(self):
        """Exact-duration attempts (widen_band=False) raise NoCandidate; the
        widened band serves a pick. The legacy one-shot path dead-ended in
        no_candidate here — the routed endpoint must succeed AND persist."""
        calls = []

        def fake_match(session, library, **kw):
            calls.append(bool(kw.get("widen_band")))
            if not kw.get("widen_band"):
                raise tp.NoCandidateWorkoutError("exact band empty")
            session.zwo_file = "widened_pick.zwo"
            session.zwo_name = "widened_pick"
            return session

        tp.match_zwo = fake_match
        tue = (self._monday + timedelta(days=1)).isoformat()
        r = self.client.post("/api/plan/re-draw", json={"date": tue})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"), data)
        self.assertEqual(data.get("action"), "redrawn")
        self.assertEqual(data.get("zwo_file"), "widened_pick.zwo")
        # The ladder actually climbed: non-widened attempts first, then widened.
        self.assertIn(False, calls)
        self.assertIn(True, calls)
        # Persisted under lock.
        sess = next(s for s in self._read_plan()["weeks"][0]["sessions"]
                    if s["day"] == tue)
        self.assertEqual(sess["zwo_file"], "widened_pick.zwo")

    def test_total_exhaustion_maps_to_zone_targets_state(self):
        """Even the widened band is empty → 200 {ok:false, action:
        no_candidate} — the action string the UI maps to the 'day keeps its
        zone targets' toast (NOT a failure)."""
        def fake_match(session, library, **kw):
            raise tp.NoCandidateWorkoutError("nothing admissible")

        tp.match_zwo = fake_match
        tue = (self._monday + timedelta(days=1)).isoformat()
        r = self.client.post("/api/plan/re-draw", json={"date": tue})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("action"), "no_candidate")
        self.assertEqual(data.get("day"), tue)
        # The day was NOT clobbered — still the fileless zone-target session.
        sess = next(s for s in self._read_plan()["weeks"][0]["sessions"]
                    if s["day"] == tue)
        self.assertEqual(sess["zwo_file"], "")
        self.assertEqual(sess["session_type"], "z2")

    def test_rest_day_keeps_legacy_action(self):
        mon = self._monday.isoformat()
        r = self.client.post("/api/plan/re-draw", json={"date": mon})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("action"), "rest_day")


def test_b2_both_redraw_buttons_show_zone_targets_message():
    """pgRedrawSession (plan grid) and calRedrawDay (calendar) both key the
    honest message on action === 'no_candidate' instead of 'Re-draw
    failed'."""
    src = DASHBOARD.read_text(encoding="utf-8")
    for fn_name in ("pgRedrawSession", "calRedrawDay"):
        fn = _extract_js_function(src, fn_name)
        assert "data.action === 'no_candidate'" in fn, fn_name
        assert "keeps its zone targets" in fn, fn_name


# ═══════════════════════════════════════════════════════════════════════════
# B3a — deliberate fileless (swap/adapt) sweeps; transient breakage protects
# ═══════════════════════════════════════════════════════════════════════════

class TestB3aSwapSweep:
    def test_user_swapped_fileless_sweeps_stale_event(self, stub, fake):
        _apikey(stub)
        swapped = _sess(D1, zwo="", stype="z2", user_swapped=True)
        plain = _sess(D2, zwo="", stype="threshold")   # transient breakage
        _write_plan([swapped, plain])
        fake.events = [
            {"id": 1, "external_id": _eid(stub, D1), "category": "WORKOUT",
             "start_date_local": f"{D1}T00:00:00", "name": "Old Threshold",
             "filename": "old_threshold.zwo"},
            {"id": 2, "external_id": _eid(stub, D2), "category": "WORKOUT",
             "start_date_local": f"{D2}T00:00:00", "name": "Old Broken",
             "filename": "old_broken.zwo"},
        ]
        r = icp.reconcile()
        assert not r.get("error"), r
        # The swap's stale old-type event is GONE; the genuinely-broken
        # session's prior event survives (broken_ids protection).
        assert r["deleted"] == 1
        assert fake.ids() == [_eid(stub, D2)]
        # Both are still reported as unmatched skips for the toast.
        assert {"day": D1, "reason": "unmatched"} in r["skipped"]
        assert {"day": D2, "reason": "unmatched"} in r["skipped"]

    def test_broken_ids_classification_swapped_adapted_plain(self, stub):
        swapped = _sess(D1, zwo="", stype="z2", user_swapped=True)
        adapted = _sess(D2, zwo="", stype="z2", adapted=True)
        plain = _sess(D3, zwo="", stype="threshold")
        plan = {"weeks": [{"week_num": 1, "start": D1, "end": D3,
                           "sessions": [swapped, adapted, plain]}]}
        events, skipped, broken = icp._desired_events(
            stub.pm, plan, TODAY, 14, stub.pid)
        assert events == []
        assert len(skipped) == 3
        assert _eid(stub, D1) not in broken     # deliberate user swap
        assert _eid(stub, D2) not in broken     # deliberate engine adapt
        assert _eid(stub, D3) in broken         # transient → protected

    def test_swapped_with_other_breakage_stays_protected(self, stub):
        """A user_swapped session that DID match but whose file is missing is
        a transient build failure — file_missing must stay protected."""
        swapped_missing = _sess(D1, zwo="ghost_file.zwo", stype="z2",
                                user_swapped=True)
        plan = {"weeks": [{"week_num": 1, "start": D1, "end": D1,
                           "sessions": [swapped_missing]}]}
        events, skipped, broken = icp._desired_events(
            stub.pm, plan, TODAY, 14, stub.pid)
        assert events == []
        assert skipped == [{"day": D1, "reason": "file_missing"}]
        assert _eid(stub, D1) in broken


# ═══════════════════════════════════════════════════════════════════════════
# B3b — non-2xx observability: body excerpt in log + error_detail in result
# ═══════════════════════════════════════════════════════════════════════════

class TestB3bHttpObservability:
    def test_bulk_422_logs_step_and_body_and_carries_detail(
            self, stub, fake, caplog):
        _apikey(stub)
        _write_plan([_sess(D1)])
        fake.fail["POST"] = (
            422, b'{"error":"filename must end with .zwo or .fit","row":0}')
        with caplog.at_level(logging.WARNING):
            r = icp.reconcile()
        assert r["error"] == "http_422"
        assert "filename must end with" in r.get("error_detail", "")
        hits = [m for m in caplog.messages
                if "EVENT=icu_push_http_error" in m]
        assert hits, "non-2xx must log EVENT=icu_push_http_error"
        assert "step=bulk_upsert" in hits[0]
        assert "status=422" in hits[0]
        assert "filename must end with" in hits[0]

    def test_window_get_4xx_carries_detail(self, stub, fake, caplog):
        _apikey(stub)
        _write_plan([_sess(D1)])
        fake.fail["GET"] = (422, b"bad window request")
        with caplog.at_level(logging.WARNING):
            r = icp.reconcile()
        assert r["error"] == "http_422"
        assert "bad window request" in r.get("error_detail", "")
        assert any("step=window_get" in m for m in caplog.messages)

    def test_error_detail_reaches_push_status_endpoint(self, stub, fake):
        """POST /api/icu/push (button) stores the result; GET /api/icu/push
        (the sync status the UI polls) surfaces error + error_detail — a
        background 422 is no longer silent."""
        app_module._icu_push_last_result = None
        _apikey(stub)
        _write_plan([_sess(D1)])
        fake.fail["POST"] = (422, b"ICU says: event 0 invalid")
        r = stub.client.post("/api/icu/push", json={})
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == "http_422"
        assert "event 0 invalid" in body["error_detail"]
        s = stub.client.get("/api/icu/push").json()
        assert s["last_result"]["error"] == "http_422"
        assert "event 0 invalid" in s["last_result"]["error_detail"]
        assert s["last_result"]["at"]

    def test_body_excerpt_capped_at_300(self):
        detail = icp._http_error_detail("bulk_upsert", 422, b"x" * 2000)
        assert len(detail) == 300


# ═══════════════════════════════════════════════════════════════════════════
# B4 — downloadGeneratedZwo uses the pywebview bridge; anchor for browsers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_b4_generated_zwo_bridge_then_anchor_fallback():
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "downloadGeneratedZwo")
    assert fn.startswith("async function"), "extractor must keep async"
    harness = fn + """
(async () => {
  let saved = null, clicks = 0;
  global.fetch = async () => ({json: async () => ({ftp: 250})});
  global.showToast = () => {};
  global.esc = s => String(s);
  global.buildPowerBlocks = () => [
    {name: 'Warmup', min: 10, pctLow: 50, pctHigh: 70},
    {name: 'Main', min: 40, pctLow: 70, pctHigh: 70},
  ];
  global.document = {createElement: () => ({click() { clicks++; }})};
  global.URL = {createObjectURL: () => 'blob:fake', revokeObjectURL: () => {}};
  if (typeof Blob === 'undefined') global.Blob = class { constructor() {} };

  // Packaged app: pywebview present → native bridge, NO anchor click.
  global.window = {pywebview: {api: {save_zwo: async (n, x, s) => {
    saved = {n, x, s};
    return {ok: true, path: '/tmp/x.zwo'};
  }}}};
  await downloadGeneratedZwo('z2', 50, 'My Ride!');
  if (!saved) throw new Error('bridge not called when pywebview present');
  if (saved.n !== 'My_Ride_.zwo') throw new Error('bad save name: ' + saved.n);
  if (!saved.x.includes('<workout_file>')) throw new Error('xml not handed over');
  if (!saved.x.includes('<SteadyState')) throw new Error('blocks not serialized');
  if (!saved.x.includes('<Warmup')) throw new Error('ramp block lost');
  if (saved.s !== '') throw new Error('source_file must be the empty string');
  if (clicks !== 0) throw new Error('anchor path must not run under pywebview');

  // Real browser: no pywebview → the original Blob+anchor path.
  saved = null;
  global.window = {};
  await downloadGeneratedZwo('z2', 50, 'My Ride!');
  if (saved !== null) throw new Error('bridge called without pywebview');
  if (clicks !== 1) throw new Error('anchor fallback did not click');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# B5 — automatic-FTP plausibility guard (manual entry stays ungated)
# ═══════════════════════════════════════════════════════════════════════════

def _wellness_with_eftp(eftp):
    return [
        {"id": "2026-07-10", "sportInfo": [{"eftp": float(eftp)}]},
        {"id": "2026-07-11", "sportInfo": [{"eftp": float(eftp)}]},
    ]


class TestB5FtpAutoIngestGuard:
    def test_helper_thresholds(self):
        ok = app_module._ftp_auto_ingest_ok
        assert not ok(122, 258)      # the incident: <60% of current
        assert ok(240, 258)          # ordinary drift-sized change
        assert not ok(90, 0)         # <100 W absolute, even with no current
        assert not ok(None, 258)
        assert ok(160, 258)          # >60% boundary-ish value passes

    def test_auto_ingest_122_onto_258_rejected_and_logged(
            self, monkeypatch, caplog):
        calls = []
        monkeypatch.setattr(tp, "check_and_auto_apply_eftp",
                            lambda series: calls.append(series) or
                            {"applied": True})
        monkeypatch.setattr(config, "ATHLETE_FTP_W", 258, raising=False)
        with caplog.at_level(logging.WARNING):
            out = app_module._guarded_check_and_auto_apply_eftp(
                _wellness_with_eftp(122))
        assert out is None
        assert calls == [], "engine must NOT be invoked on an implausible value"
        assert any("EVENT=ftp_auto_ingest_rejected" in m
                   for m in caplog.messages)

    def test_auto_ingest_240_onto_258_accepted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tp, "check_and_auto_apply_eftp",
                            lambda series: calls.append(series) or
                            {"applied": True, "new_ftp": 240})
        monkeypatch.setattr(config, "ATHLETE_FTP_W", 258, raising=False)
        out = app_module._guarded_check_and_auto_apply_eftp(
            _wellness_with_eftp(240))
        assert out == {"applied": True, "new_ftp": 240}
        assert len(calls) == 1, "plausible value must reach the engine"

    def test_manual_settings_entry_122_allowed(self, stub):
        """The rider may always assert their own number — /api/settings is
        deliberately ungated (do NOT guard manual entry)."""
        r = stub.client.post("/api/settings", json={"ftp": 122})
        assert r.status_code == 200, r.text
        assert stub.pm.ftp == 122

    def test_wizard_eftp_prefill_guarded(self, stub, monkeypatch):
        """/api/icu/athlete-numbers' sport-unfiltered eFTP fallback (the
        prime suspect for the tester's ftp=122) must not suggest an
        implausible value; a plausible one still prefills."""
        _apikey(stub)
        stub.pm.save_athlete({"ftp": 258})
        # 3.4.3 hermetic-fs gate: the guard compares against the GLOBAL
        # config.ATHLETE_FTP_W, which earlier tests mutate via /api/settings.
        # The dev machine's real settings used to make 122 implausible in
        # every ordering; pin it like the sibling tests do so the assertion
        # is order-independent.
        monkeypatch.setattr(config, "ATHLETE_FTP_W", 258, raising=False)
        monkeypatch.setattr(app_module, "fetch_wellness",
                            lambda days: _wellness_with_eftp(122))
        app_module.clear_cache()
        out = stub.client.get("/api/icu/athlete-numbers").json()
        assert out["ftp"] is None

        monkeypatch.setattr(app_module, "fetch_wellness",
                            lambda days: _wellness_with_eftp(240))
        app_module.clear_cache()
        out = stub.client.get("/api/icu/athlete-numbers").json()
        assert out["ftp"] == 240


if __name__ == "__main__":
    unittest.main()
