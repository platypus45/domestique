"""3.4.1 M1 — loading-state cluster (DIAG_M1 items ①-⑤, ⑩ shared bar).

①  power_curve backfill failure paths persist a TERMINAL
   ``no_streams_available`` marker; ``_needs_refetch`` and
   ``count_rides_missing_efforts`` honor it — the cached-% terminates at an
   honest 100 and the per-GET backfill relaunch loop dies.
②  /api/profile/fatigue-resistance counts unfetchable long rides toward
   done (pct 100), exposes ``n_unfetchable``, and stops auto-kicking the
   backfill once terminal.
③  loadFatigueResistance never clobbers a visible bar with a bare
   "Loading…", and renders a success result EVEN at pct<100 (bar becomes a
   thin footer under the score).
④  loadPowerCurve renders its bar + auto-poll into #power-curve-meta (an
   element that EXISTS — the old #power-curve-loading-placeholder target
   was deleted in v2.2.2, so the v3.2.1 bar never showed in any build).
⑤  home DFA card recent===0 → polls GET /api/sync/progress and shows the
   live "Indexing rides…" bar while the auto-kicked ICU sync runs, then
   re-fetches; the DFA tab's update strip no longer bails on the shared
   30-min localStorage throttle.
⑩  (scope add) the same sync bar is one shared helper on both surfaces,
   and the home "Recovery details" section is always visible.

Hermetic: tmp-dir envelopes + monkeypatched module seams + node harnesses
over template-extracted functions. No network, no real HOME writes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import unittest.mock as mock
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "src" / "templates" / "dashboard.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Slice `[async ]function <name>(...) {...}` by brace count (the
    test_331_surfaces extractor with the open-paren anchor)."""
    start = src.find(f"async function {name}(")
    if start < 0:
        start = src.index(f"function {name}(")
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


_ESC_STUB = """
const esc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
"""

# Element stub that records every innerHTML write (clobber forensics) and
# answers querySelector('.backfill-bar') from the CURRENT html.
_EL_STUB = """
function mkEl() {
  let html = '';
  const el = {
    writes: [], textWrites: [], _text: '',
    setAttribute() {}, classList: { add(){}, remove(){}, toggle(){} },
    querySelector(sel) {
      const cls = String(sel).replace(/^\\./, '');
      return html.indexOf(cls) >= 0 ? {} : null;
    },
    // Append WITHOUT re-parsing (the real DOM contract the footer-bar
    // path relies on to keep the live scatter canvas intact).
    insertAdjacentHTML(pos, v) { html += String(v); el.writes.push(html); },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return html; },
    set(v) { html = String(v); el.writes.push(html); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return el._text; },
    set(v) { el._text = String(v); el.textWrites.push(el._text);
             html = ''; el.writes.push(''); },
  });
  return el;
}
"""


# ═══════════════════════════════════════════════════════════════════════════
# ① — terminal no_streams_available marker (power_curve.py)
# ═══════════════════════════════════════════════════════════════════════════

def _mk_envelope(dirpath, name, ext_id, **extra):
    p = dirpath / name
    p.write_text(json.dumps({
        "id": ext_id, "external_id": ext_id,
        "started_at": "2026-07-01T10:00:00", "streams": {},
        **extra,
    }), encoding="utf-8")
    return p


@pytest.fixture
def rides_dir(tmp_path, monkeypatch):
    import power_curve
    d = tmp_path / "icu_rides"
    d.mkdir()
    monkeypatch.setattr(power_curve, "_icu_rides_dir", lambda: d)
    monkeypatch.setattr(power_curve, "_backfill_lock_path",
                        lambda: tmp_path / ".backfill.lock")
    return d


def test_backfill_marks_all_three_failure_paths_terminal(rides_dir,
                                                         monkeypatch):
    """fetch-raises / empty-envelope / no-derivable-efforts each persist the
    marker; a second pass touches NONE of them (already_cached) — the
    infinite relaunch loop dies at the source."""
    import power_curve
    import training

    p_raise = _mk_envelope(rides_dir, "r_raise.json", "i1")
    p_empty = _mk_envelope(rides_dir, "r_empty.json", "i2")
    p_noeff = _mk_envelope(rides_dir, "r_noeff.json", "i3")

    calls = {"n": 0}

    def fake_streams(ext):
        calls["n"] += 1
        if ext == "i1":
            raise RuntimeError("ICU 404")
        if ext == "i2":
            return {}                       # ICU empty envelope
        return {"watts": [], "time": []}    # no watts → no efforts

    monkeypatch.setattr(training, "fetch_activity_streams", fake_streams)

    result = power_curve.backfill_icu_history(max_per_second=1000)
    assert result["failed"] == 3

    for p in (p_raise, p_empty, p_noeff):
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data.get("no_streams_available") is True, p.name
        assert data.get("streams_fetch_failed_at"), p.name
        # ① terminal: never refetched again.
        assert power_curve._needs_refetch(p) is False, p.name

    # Second pass: nothing is re-fetched — every marked ride counts cached.
    calls["n"] = 0
    result2 = power_curve.backfill_icu_history(max_per_second=1000)
    assert calls["n"] == 0, "marked rides must never be re-fetched"
    assert result2["already_cached"] == 3
    assert result2["failed"] == 0


def test_successful_fetch_heals_stale_marker(rides_dir, monkeypatch):
    """A marked envelope that (somehow) gets refetched successfully drops the
    marker — terminal is not a tombstone if ICU later serves the streams."""
    import power_curve
    import training

    p = _mk_envelope(rides_dir, "r_heal.json", "i9",
                     no_streams_available=True,
                     streams_fetch_failed_at="2026-07-01T00:00:00Z")
    # Force the pass to reconsider it (marker normally short-circuits).
    monkeypatch.setattr(power_curve, "_needs_refetch", lambda _p: True)
    monkeypatch.setattr(training, "fetch_activity_streams",
                        lambda ext: {"watts": [200, 500], "time": [0, 1]})
    result = power_curve.backfill_icu_history(max_per_second=1000)
    assert result["backfilled"] == 1
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "no_streams_available" not in data
    assert "streams_fetch_failed_at" not in data
    assert data.get("efforts")


def test_needs_refetch_marker_beats_missing_streams(tmp_path):
    """The marker short-circuits BEFORE the efforts/streams coverage checks
    that would otherwise return True forever."""
    import power_curve
    p = tmp_path / "marked.json"
    p.write_text(json.dumps({
        "external_id": "x", "efforts": [], "streams": {},
        "no_streams_available": True,
    }), encoding="utf-8")
    assert power_curve._needs_refetch(p) is False
    # Control: identical envelope without the marker still needs a refetch.
    q = tmp_path / "unmarked.json"
    q.write_text(json.dumps({
        "external_id": "x", "efforts": [], "streams": {},
    }), encoding="utf-8")
    assert power_curve._needs_refetch(q) is True


def test_count_rides_missing_efforts_counts_marked_as_done(monkeypatch):
    import power_curve
    today = date.today().isoformat()
    rides = [
        # hydrated — done
        {"kj": 900, "started_at": f"{today}T08:00:00",
         "efforts": [{"secs": 5, "watts": 500}]},
        # genuinely missing — still counts missing
        {"kj": 900, "started_at": f"{today}T09:00:00", "efforts": []},
        # terminally unfetchable — counts DONE now (was: missing forever)
        {"kj": 900, "started_at": f"{today}T10:00:00", "efforts": [],
         "no_streams_available": True},
    ]
    monkeypatch.setattr(power_curve, "_load_cached_rides", lambda: rides)
    n, missing = power_curve.count_rides_missing_efforts(90)
    assert (n, missing) == (3, 1)


def test_compute_fatigue_resistance_counts_unfetchable_long_rides(monkeypatch):
    import power_curve
    today = date.today()

    def _ride(i, **extra):
        d = (today - timedelta(days=i + 1)).isoformat()
        return {"ride_id": f"r{i}", "kj": 2100,
                "started_at": f"{d}T08:00:00", **extra}

    rides = [
        _ride(0, streams={"watts": [250] * 120}),
        _ride(1, streams={"watts": [240] * 120}),
        _ride(2, streams={"watts": [230] * 120}),
        _ride(3, no_streams_available=True),
        _ride(4, no_streams_available=True),
        _ride(5),  # plain summary-only (not marked — still just missing)
    ]
    monkeypatch.setattr(power_curve, "_load_cached_rides", lambda: rides)
    out = power_curve.compute_fatigue_resistance(
        None, window_days=90, kj_threshold=1500)
    assert out["n_long_rides"] == 6
    assert out["n_long_rides_with_streams"] == 3
    assert out["n_long_rides_unfetchable"] == 2
    assert out["reason"] == "streams_not_hydrated_run_backfill"


# ═══════════════════════════════════════════════════════════════════════════
# ② — endpoint: terminal pct + n_unfetchable + no auto-kick at 100
# ═══════════════════════════════════════════════════════════════════════════

def _fr_fixture(n_long, n_streams, n_unfetchable, status="success"):
    return {
        "window_days": 365, "n_long_rides": n_long,
        "n_long_rides_with_streams": n_streams,
        "n_long_rides_unfetchable": n_unfetchable,
        "fit_status": status,
        "reason": None if status == "success" else "streams_not_hydrated_run_backfill",
        "kj_threshold": 1500,
        "robustness_score": 87.5 if status == "success" else None,
        "by_duration": [], "scatter": [],
    }


def _hit_fatigue_endpoint(fixture, monkeypatch):
    from fastapi.testclient import TestClient
    import app as appmod
    import power_curve

    appmod._cache.clear()
    appmod._cache_ts.clear()
    monkeypatch.setattr(appmod, "_fatigue_resistance_memoised",
                        lambda *a, **kw: fixture)
    monkeypatch.setattr(power_curve, "latest_ride_id_in_window",
                        lambda *a, **kw: "ride123")
    kick = mock.MagicMock(return_value=(True, {"task_id": "t1"}))
    monkeypatch.setattr(power_curve, "acquire_backfill_lock", kick)
    client = TestClient(appmod.app, raise_server_exceptions=False)
    resp = client.get("/api/profile/fatigue-resistance").json()
    return resp, kick


def test_endpoint_pct_terminal_with_unfetchable(monkeypatch):
    """14 long rides, 13 hydrated + 1 permanently unfetchable → pct 100
    (was stuck at 93 forever) and n_unfetchable surfaced for the UI note."""
    resp, _ = _hit_fatigue_endpoint(_fr_fixture(14, 13, 1), monkeypatch)
    assert resp["power_streams_cached_pct"] == 100
    assert resp["n_unfetchable"] == 1
    assert resp["fit_status"] == "success"
    assert resp["auto_backfill_triggered"] is False


def test_endpoint_all_unfetchable_terminal_no_kick(monkeypatch):
    """Every long ride unfetchable → honest terminal 100 and NO backfill
    auto-kick (n_with_streams==0 used to fire one on every GET forever)."""
    resp, kick = _hit_fatigue_endpoint(
        _fr_fixture(3, 0, 3, status="insufficient_data"), monkeypatch)
    assert resp["power_streams_cached_pct"] == 100
    assert resp["n_unfetchable"] == 3
    assert resp["auto_backfill_triggered"] is False
    kick.assert_not_called()


def test_endpoint_partial_pct_unchanged_semantics(monkeypatch):
    """No unfetchable rides → pct math identical to pre-3.4.1."""
    resp, _ = _hit_fatigue_endpoint(
        _fr_fixture(10, 7, 0, status="insufficient_data"), monkeypatch)
    assert resp["power_streams_cached_pct"] == 70
    assert resp["n_unfetchable"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# ③ — never-clobber + success renders at pct<100 (node)
# ═══════════════════════════════════════════════════════════════════════════

def _fr_js_bundle(src: str) -> str:
    return "\n".join(_extract_js_function(src, n) for n in (
        "backfillBarHtml", "_fatigueClearPoll", "_fatigueSchedulePoll",
        "_fatigueUnfetchableNoteHtml", "_fatigueFooterBarHtml",
        "loadFatigueResistance", "renderFatigueResistance",
    ))


_FR_HARNESS_PRELUDE = _ESC_STUB + _EL_STUB + """
let _fatigueResistanceLoaded = false;
let _fatigueResistanceInflight = false;
let _fatigueResistanceThreshold = 1500;
let _fatigueResistanceWindowDays = 365;
let _fatigueResistanceBackfillStart = 0;
let _fatigueResistanceBackfillTimer = null;
const timers = [];
const setTimeout = (cb, ms) => { timers.push({cb, ms}); return timers.length; };
const clearTimeout = () => {};
const body = mkEl(), content = mkEl(), headline = mkEl();
const document = { getElementById: id => ({
  'fatigue-resistance-body': body,
  'fatigue-resistance-content': content,
  'fatigue-resistance-headline': headline,
}[id] || null) };
"""


@needs_node
def test_success_renders_at_partial_pct_with_footer_bar_and_note():
    """fit_status success at pct 93 → the SCORE renders (was hidden behind
    the bar), the bar shrinks to a footer note, the unfetchable note shows,
    and the 4s poll stays scheduled."""
    src = _src()
    harness = _FR_HARNESS_PRELUDE + """
const payload = {
  fit_status: 'success', robustness_score: 87.5,
  n_long_rides: 14, n_long_rides_with_streams: 13, n_unfetchable: 1,
  power_streams_cached_pct: 93, auto_backfill_triggered: false,
  by_duration: [{duration_s: 60, fr_index_pct: 91.2, n_data_points: 9}],
  scatter: [],
};
const fetch = async () => ({ ok: true, json: async () => payload });
""" + _fr_js_bundle(src) + """
(async () => {
  await loadFatigueResistance();
  const html = body.innerHTML;
  if (html.indexOf('Robustness 87.5%') < 0)
    throw new Error('success result must render even at pct<100');
  if (html.indexOf('backfill-bar') < 0)
    throw new Error('bar must stay as a thin footer while pct<100');
  if (html.indexOf('93%') < 0)
    throw new Error('footer bar must carry the live pct');
  if (html.indexOf('1 ride has no power data on intervals.icu') < 0)
    throw new Error('n_unfetchable note missing');
  if (!timers.length || timers[timers.length - 1].ms !== 4000)
    throw new Error('4s poll must stay scheduled while pct<100');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_poll_never_clobbers_visible_bar_with_bare_loading():
    """While a bar (or a result with its footer bar) is showing, the poll's
    re-entry must NOT write the bare 'Loading…' placeholder — it upgrades
    in place. Fresh empty panel still gets the placeholder."""
    src = _src()
    harness = _FR_HARNESS_PRELUDE + """
let pct = 40;
const fetch = async () => ({ ok: true, json: async () => ({
  fit_status: 'insufficient_data', reason: 'streams_not_hydrated_run_backfill',
  n_long_rides: 10, n_long_rides_with_streams: 4, n_unfetchable: 2,
  power_streams_cached_pct: pct, auto_backfill_triggered: false,
  robustness_score: null, by_duration: [], scatter: [],
}) });
""" + _fr_js_bundle(src) + """
(async () => {
  // Fresh open: empty body → the placeholder IS expected once.
  await loadFatigueResistance();
  if (body.writes.filter(w => w.indexOf('Loading…') >= 0).length !== 1)
    throw new Error('fresh open must show the placeholder exactly once');
  if (body.innerHTML.indexOf('backfill-bar') < 0)
    throw new Error('bar must render at pct 40');
  if (body.innerHTML.indexOf('2 rides have no power data') < 0)
    throw new Error('unfetchable note must sit under the bar');

  // Poll re-entry while the bar is visible: NO bare-Loading write.
  const before = body.writes.length;
  pct = 60;
  await loadFatigueResistance(true);
  const news = body.writes.slice(before);
  if (news.some(w => w.indexOf('Loading…') >= 0))
    throw new Error('poll clobbered the bar with bare Loading…');
  if (body.innerHTML.indexOf('60%') < 0)
    throw new Error('bar must upgrade in place to the new pct');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# ④ — power-curve bar targets an element that EXISTS (node)
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_power_curve_bar_renders_into_meta_and_polls():
    src = _src()
    fn = _extract_js_function(src, "loadPowerCurve")
    assert "getElementById('power-curve-loading-placeholder')" not in fn, \
        "the deleted placeholder must no longer be a render target"
    harness = _EL_STUB + """
let _powerCurveData = null, _powerCurveChart = null;
let _powerCurveYAxis = 'watts', _powerCurveWindow = 90;
let _powerCurveInflight = false;
let _pcPollTimer = null, _pcPollCount = 0;
const timers = [];
const setTimeout = (cb, ms) => { timers.push({cb, ms}); return timers.length; };
let cleared = 0;
const clearTimeout = () => { cleared++; };
const meta = mkEl(), canvas = {};
const unknownIds = [];
const document = { getElementById: id => {
  if (id === 'power-curve-meta') return meta;
  if (id === 'power-curve-chart') return canvas;
  unknownIds.push(id);
  return null;
} };
let rendered = 0;
const renderPowerCurve = () => { rendered++; meta.textContent = 'Window: 90 d · summary'; };
let payload = { n_rides: 0, needs_backfill: true,
                backfill_progress: { running: true, pct: 42 }, rider_curve: [] };
const fetch = async () => ({ ok: true, json: async () => payload });
""" + _extract_js_function(src, "backfillBarHtml") + "\n" + fn + """
(async () => {
  await loadPowerCurve();
  if (unknownIds.length)
    throw new Error('loadPowerCurve touched non-existent ids: ' + unknownIds);
  if (!rendered) throw new Error('chart render must still run');
  if (meta.innerHTML.indexOf('backfill-bar') < 0)
    throw new Error('bar must render into #power-curve-meta');
  if (meta.innerHTML.indexOf('42%') < 0)
    throw new Error('bar must show the live pct');
  if (!timers.length || timers[timers.length - 1].ms !== 4000)
    throw new Error('auto-poll must be scheduled');

  // Poll re-entry: bar visible → no bare 'Loading…' stomp on the meta.
  payload = { n_rides: 0, needs_backfill: true,
              backfill_progress: { running: true, pct: 77 }, rider_curve: [] };
  const beforeTextWrites = meta.textWrites.length;
  await loadPowerCurve(true);
  if (meta.textWrites.slice(beforeTextWrites).some(w => w === 'Loading…'))
    throw new Error('poll clobbered the bar with bare Loading…');
  if (meta.innerHTML.indexOf('77%') < 0)
    throw new Error('bar must upgrade in place');

  // Streams hydrated: bar yields to the meta summary, polling stops.
  payload = { n_rides: 24, needs_backfill: false,
              rider_curve: [{ duration_s: 60, watts: 300 }] };
  await loadPowerCurve(true);
  if (meta.innerHTML.indexOf('backfill-bar') >= 0)
    throw new Error('bar must clear once the curve populates');
  if (meta.textContent.indexOf('Window:') < 0)
    throw new Error('meta summary must show once data lands');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# ⑤/⑩ — home DFA card + DFA tab sync visibility (node)
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_home_dfa_card_shows_sync_bar_then_refetches():
    """recent===0 + /api/sync/progress running → the shared 'Indexing
    rides…' bar renders on the home card, and completion re-fetches
    /api/profile/dfa-alpha1 (the dead 'no rides indexed yet' message only
    shows when no sync is running)."""
    src = _src()
    fns = "\n".join(_extract_js_function(src, n) for n in (
        "_icuSyncBarHtml", "_pollIcuSyncInto", "_renderDfaCard"))
    harness = _ESC_STUB + _EL_STUB + """
const setTimeout = (cb) => { cb(); return 0; };  // immediate — drain the poll
const progress = [
  { state: 'running', done: 12, total: 87 },
  { state: 'running', done: 80, total: 87 },
  { state: 'done',    done: 87, total: 87 },
];
let pi = 0;
let dfaFetches = 0;
const fetch = async (url) => {
  if (String(url).indexOf('/api/sync/progress') === 0)
    return { ok: true, json: async () => progress[Math.min(pi++, progress.length - 1)] };
  if (String(url).indexOf('/api/profile/dfa-alpha1') === 0) {
    dfaFetches++;
    return { ok: true, json: async () => ({
      value: 0.91, n_rides: 3, last_computed_at: '2026-07-13T09:00:00',
      n_recent_total: 3, n_no_rr_data: 0, n_fetch_failed: 0 }) };
  }
  throw new Error('unexpected fetch ' + url);
};
const host = mkEl();
""" + fns + """
(async () => {
  await _renderDfaCard(host, { value: null, n_recent_total: 0,
                               n_no_rr_data: 0, n_fetch_failed: 0 });
  const sawBar = host.writes.some(w =>
    w.indexOf('Indexing rides from intervals.icu') >= 0 &&
    w.indexOf('12 of 87') >= 0);
  if (!sawBar) throw new Error('sync bar with live counts never rendered');
  // Drain the fire-and-forget poll chain.
  for (let i = 0; i < 200 && dfaFetches === 0; i++) await Promise.resolve();
  for (let i = 0; i < 200 && host.innerHTML.indexOf('0.91') < 0; i++)
    await Promise.resolve();
  if (dfaFetches < 1) throw new Error('completion must re-fetch dfa-alpha1');
  if (host.innerHTML.indexOf('Last ride DFA α1') < 0 ||
      host.innerHTML.indexOf('0.91') < 0)
    throw new Error('re-fetched value must replace the bar');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_home_dfa_card_dead_message_only_when_sync_idle():
    src = _src()
    fns = "\n".join(_extract_js_function(src, n) for n in (
        "_icuSyncBarHtml", "_pollIcuSyncInto", "_renderDfaCard"))
    harness = _ESC_STUB + _EL_STUB + """
const setTimeout = (cb) => { cb(); return 0; };
const fetch = async (url) => {
  if (String(url).indexOf('/api/sync/progress') === 0)
    return { ok: true, json: async () => ({ state: 'idle' }) };
  throw new Error('unexpected fetch ' + url);
};
const host = mkEl();
""" + fns + """
(async () => {
  await _renderDfaCard(host, { value: null, n_recent_total: 0,
                               n_no_rr_data: 0, n_fetch_failed: 0 });
  if (host.innerHTML.indexOf('no rides indexed yet') < 0)
    throw new Error('idle sync must fall back to the diagnostic message');
  if (host.innerHTML.indexOf('Indexing rides') >= 0)
    throw new Error('no bar when nothing is syncing');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_dfa_tab_update_strip_bypasses_ls_throttle():
    """A fresh (seconds-old) shared localStorage key must NOT suppress the
    tab strip: the POST always fires and the poller attaches to the
    returned (possibly already_running) task."""
    src = _src()
    fn = _extract_js_function(src, "_dfaTabUpdateProgress")
    assert "_DFA_BACKFILL_THROTTLE_MS) return" not in fn, \
        "the 30-min LS-throttle bail must be gone from the TAB surface"
    harness = """
const _DFA_BACKFILL_LS_KEY = 'k';
const _DFA_BACKFILL_THROTTLE_MS = 30 * 60 * 1000;
const localStorage = {
  getItem: () => String(Date.now()),   // key set seconds ago
  setItem: () => {},
};
const host = { innerHTML: '' };
const document = { getElementById: () => host };
let posted = 0;
const fetch = async (url, opts) => {
  if (String(url).indexOf('/api/profile/dfa-backfill') === 0 &&
      opts && opts.method === 'POST') {
    posted++;
    return { ok: true, json: async () => ({
      status: 'already_running', task_id: 't9' }) };
  }
  throw new Error('unexpected fetch ' + url);
};
let polled = null;
const _dfaPollBackfill = (h, taskId) => { polled = taskId; };
""" + fn + """
(async () => {
  await _dfaTabUpdateProgress();
  if (posted !== 1) throw new Error('tab must always POST (single-flight server-side)');
  if (polled !== 't9') throw new Error('poller must attach to the in-flight task');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


def test_home_recovery_details_always_visible_and_loader_on_home_load():
    """⑩ static pins: the <details> fold around #home-snapshot-dfa is gone
    (header + snapshot always visible) and loadHomeSnapshotDfa still fires
    on the home-load path (never behind a toggle)."""
    import re as _re
    src = _src()
    i = src.index('id="home-snapshot-dfa"')
    window = _re.sub(r"<!--.*?-->", "", src[max(0, i - 900):i], flags=_re.S)
    assert "<details" not in window, \
        "home-snapshot-dfa must not sit inside a <details> fold"
    assert "Recovery details" in window
    assert "<summary" not in window
    load_home = _extract_js_function(src, "loadHome")
    assert "loadHomeSnapshotDfa(activities)" in load_home
    # One shared sync-strip helper feeds both surfaces (home card + DFA tab).
    assert src.count("function _icuSyncBarHtml(") == 1
    for fn_name in ("_renderDfaCard", "loadDfaTab"):
        assert "_icuSyncBarHtml" in _extract_js_function(src, fn_name), \
            f"{fn_name} must render the shared sync strip"
