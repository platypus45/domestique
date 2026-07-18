"""3.4.4 — Fitness & Form date-range filtering (ICU-style).

Three layers under test:

1. ``GET /api/wellness`` grew optional inclusive ``from``/``to`` ISO params
   (TestClient): records are sliced to the window server-side, ``days`` is
   widened to reach back to ``from``, and the legacy days-only contract
   (home boot ?days=90, default 28) is byte-identical untouched.
2. The pure range helpers in dashboard.html (node harness): year-chip
   derivation, range-label formatting, the click-start-then-end calendar
   state machine (end<start swaps), month navigation, Monday-first grid,
   clamping (future 'to' → clamp + note; fully-outside → null), quick-link
   ranges, and the per-profile localStorage round-trip.
3. fitnessChart listener discipline (node harness): each render replaces the
   svg node via container.innerHTML, so repeated filter clicks must yield
   exactly ONE mousemove listener on the current node — no stacking on any
   persistent node (hover regression guard for efa76cd5 lives in
   test_344_ff_hover.py; this file pins the re-render side).
"""
from __future__ import annotations

import datetime as _dt
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "templates" / "dashboard.html").read_text(encoding="utf-8")

_PURE_START = "// ═══ FF DATE-RANGE (3.4.4) — pure helpers"
_PURE_END = "// ═══ FF DATE-RANGE — UI wiring"


def _pure_helpers_src() -> str:
    i = _SRC.index(_PURE_START)
    j = _SRC.index(_PURE_END)
    return _SRC[i:j]


def _fitness_chart_src() -> str:
    i = _SRC.index("function fitnessChart(")
    j = _SRC.index("function metricColor(")
    return _SRC[i:j]


_LS_STUB = """
const _store = {};
globalThis.localStorage = {
  getItem: k => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
globalThis.__PROFILE = 'p1';
globalThis._profileLsKey = base => base + ':' + globalThis.__PROFILE;
"""


def _run_node(body: str, tmp_path: Path, name: str = "harness.js") -> str:
    js = _LS_STUB + "\n" + _pure_helpers_src() + "\n" + body
    f = tmp_path / name
    f.write_text(js, encoding="utf-8")
    res = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}"
    return res.stdout


# ═══════════════════════════════════════════════════════════════════════════
# 1. /api/wellness from/to (TestClient)
# ═══════════════════════════════════════════════════════════════════════════

class TestWellnessFromTo(unittest.TestCase):
    """Server-side window slicing + legacy days contract."""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        from fastapi.testclient import TestClient
        cls.app_module = app_module
        cls.client = TestClient(app_module.app)

    def setUp(self):
        # 10 contiguous days spanning a year boundary: 2025-12-28..2026-01-06.
        base = _dt.date(2025, 12, 28)
        self._dates = [(base + _dt.timedelta(days=i)).isoformat() for i in range(10)]
        recs = [{"id": d, "ctl": 50.0 + i, "atl": 40.0 + i}
                for i, d in enumerate(self._dates)]
        self.fetch_days_calls: list[int] = []

        def fake_fetch(days):
            self.fetch_days_calls.append(days)
            return [dict(r) for r in recs]

        # Bypass the TTL cache entirely so every request re-runs fake_fetch.
        self._p_cached = patch.object(self.app_module, "cached",
                                      lambda key, fn, ttl=300: fn())
        self._p_fetch = patch.object(self.app_module, "fetch_wellness", fake_fetch)
        self._p_cached.start()
        self._p_fetch.start()

    def tearDown(self):
        self._p_fetch.stop()
        self._p_cached.stop()

    def _dates_of(self, resp):
        self.assertEqual(resp.status_code, 200, resp.text)
        return [r["date"] for r in resp.json()]

    def test_subset_inclusive_bounds(self):
        r = self.client.get("/api/wellness?from=2025-12-30&to=2026-01-02")
        self.assertEqual(self._dates_of(r),
                         ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"])

    def test_single_day_window(self):
        r = self.client.get("/api/wellness?from=2026-01-03&to=2026-01-03")
        self.assertEqual(self._dates_of(r), ["2026-01-03"])

    def test_from_only(self):
        r = self.client.get("/api/wellness?from=2026-01-04")
        self.assertEqual(self._dates_of(r),
                         ["2026-01-04", "2026-01-05", "2026-01-06"])

    def test_to_only(self):
        r = self.client.get("/api/wellness?to=2025-12-29")
        self.assertEqual(self._dates_of(r), ["2025-12-28", "2025-12-29"])

    def test_window_outside_data_is_empty(self):
        r = self.client.get("/api/wellness?from=2024-01-01&to=2024-02-01")
        self.assertEqual(self._dates_of(r), [])

    def test_legacy_days_param_unchanged(self):
        # No from/to → NO slicing: the full upstream payload comes back and
        # fetch_wellness receives exactly the requested days.
        r = self.client.get("/api/wellness?days=5")
        self.assertEqual(self._dates_of(r), self._dates)
        self.assertEqual(self.fetch_days_calls[-1], 5)
        # Values untouched: ctl passthrough + computed tsb.
        body = r.json()
        self.assertEqual(body[0]["ctl"], 50.0)
        self.assertEqual(body[0]["tsb"], 10.0)

    def test_legacy_default_days_28(self):
        r = self.client.get("/api/wellness")
        self.assertEqual(self._dates_of(r), self._dates)
        self.assertEqual(self.fetch_days_calls[-1], 28)

    def test_from_widens_upstream_fetch(self):
        far_back = (_dt.date.today() - _dt.timedelta(days=40)).isoformat()
        self.client.get(f"/api/wellness?from={far_back}")
        self.assertGreaterEqual(self.fetch_days_calls[-1], 41)

    def test_explicit_days_wider_than_from_wins(self):
        near = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
        self.client.get(f"/api/wellness?days=200&from={near}")
        self.assertEqual(self.fetch_days_calls[-1], 200)

    def test_from_after_to_is_422(self):
        r = self.client.get("/api/wellness?from=2026-01-05&to=2026-01-01")
        self.assertEqual(r.status_code, 422)

    def test_malformed_dates_are_422(self):
        for frm in ("2026-13-01", "garbage", "2026/01/01"):
            r = self.client.get(f"/api/wellness?from={frm}&to=2026-01-05")
            self.assertEqual(r.status_code, 422, frm)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Markup + wiring pins (source-level)
# ═══════════════════════════════════════════════════════════════════════════

class TestMarkupPins(unittest.TestCase):
    def test_preset_chips_present(self):
        import re
        for chip_id, label in (("dr-30", "1m"), ("dr-42", "42d"), ("dr-90", "3m"),
                               ("dr-180", "6m"), ("dr-365", "1y"), ("dr-0", "All")):
            pat = rf'id="{chip_id}"[^>]*>{label}</button>'
            self.assertIsNotNone(re.search(pat, _SRC), f"{chip_id} chip with label {label}")

    def test_default_active_chip_is_90d(self):
        self.assertIn('id="dr-90" class="active"', _SRC)

    def test_range_label_and_popover_containers(self):
        for el_id in ("ff-range-label", "ff-cal-pop", "ff-year-chips", "ff-clamp-note"):
            self.assertIn(f'id="{el_id}"', _SRC, el_id)

    def test_popover_document_listeners_are_paired(self):
        """Open binds Esc + outside-click on document; close removes BOTH —
        repeated open/close cycles must not stack document listeners."""
        i = _SRC.index("function ffOpenCal(")
        j = _SRC.index("function ffRenderCal(")
        open_close = _SRC[i:j]
        self.assertIn("document.addEventListener('keydown', _ffCalDocKey)", open_close)
        self.assertIn("document.addEventListener('mousedown', _ffCalDocClose)", open_close)
        self.assertIn("document.removeEventListener('keydown', _ffCalDocKey)", open_close)
        self.assertIn("document.removeEventListener('mousedown', _ffCalDocClose)", open_close)

    def test_home_boot_legacy_fetch_untouched(self):
        self.assertIn("fetch('/api/wellness?days=90')", _SRC)

    def test_analysis_tab_uses_persisted_selection(self):
        i = _SRC.index("function loadAnalysisTab(")
        j = _SRC.index("\n}", i)
        body = _SRC[i:j]
        self.assertIn("loadFitnessChart()", body)
        self.assertNotIn(".date-range-btns button.active", body)

    def test_hover_still_uses_screen_ctm(self):
        # Belt+braces alongside test_344_ff_hover: the re-render work must not
        # regress the efa76cd5 hover fix.
        self.assertIn("getScreenCTM", _fitness_chart_src())


# ═══════════════════════════════════════════════════════════════════════════
# 3. Pure helpers (node harness)
# ═══════════════════════════════════════════════════════════════════════════

class TestRangeHelpersNode(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_year_chips_and_year_ranges(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
eq(ffYearChips('2024-03-15', '2026-07-18'), [2026, 2025, 2024], 'multi-year, newest first');
eq(ffYearChips('2025-06-01', '2025-08-01'), [2025], 'single year');
eq(ffYearChips('bogus', '2026-01-01'), [], 'bogus first date');
// Year ranges clamp to available data at BOTH ends.
eq(ffYearRange(2024, '2024-03-15', '2026-07-18'), {from: '2024-03-15', to: '2024-12-31'}, 'first year clamps from');
eq(ffYearRange(2026, '2024-03-15', '2026-07-18'), {from: '2026-01-01', to: '2026-07-18'}, 'current year clamps to');
eq(ffYearRange(2025, '2024-03-15', '2026-07-18'), {from: '2025-01-01', to: '2025-12-31'}, 'middle year unclamped');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_range_label_formatting(self):
        out = _run_node("""
if (ffRangeLabel('2023-01-03', '2026-07-18') !== '03 JAN 2023 – 18 JUL 2026')
  throw new Error('label: ' + ffRangeLabel('2023-01-03', '2026-07-18'));
if (ffFmtDMY('2025-09-05') !== '05 SEP 2025') throw new Error('SEP must be 3 letters (no locale Sept)');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_calendar_pick_state_machine(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
// Fresh pick → start only, incomplete.
let s = ffCalPick({start: null, end: null}, '2026-03-10');
eq(s, {start: '2026-03-10', end: null, complete: false}, 'first pick');
// Second pick AFTER start → completes in order.
s = ffCalPick(s, '2026-03-20');
eq(s, {start: '2026-03-10', end: '2026-03-20', complete: true}, 'ordered completion');
// Second pick BEFORE start → swaps.
let sw = ffCalPick({start: '2026-03-10', end: null}, '2026-03-01');
eq(sw, {start: '2026-03-01', end: '2026-03-10', complete: true}, 'end<start swaps');
// Same day twice → single-day range.
let sd = ffCalPick({start: '2026-03-10', end: null}, '2026-03-10');
eq(sd, {start: '2026-03-10', end: '2026-03-10', complete: true}, 'single-day range');
// Picking after a complete pair RESTARTS.
let rs = ffCalPick(s, '2026-04-01');
eq(rs, {start: '2026-04-01', end: null, complete: false}, 'restart after complete');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_month_navigation(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
eq(ffCalNav(2026, 1, -1), {y: 2025, m: 12}, 'Jan -1 → Dec prev year');
eq(ffCalNav(2025, 12, 1), {y: 2026, m: 1}, 'Dec +1 → Jan next year');
eq(ffCalNav(2026, 7, -14), {y: 2025, m: 5}, 'multi-month back');
eq(ffCalNav(2026, 7, 14), {y: 2027, m: 9}, 'multi-month forward');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_calendar_grid_monday_first(self):
        out = _run_node("""
// Property checks — no hand-picked weekday facts needed.
for (const [y, m] of [[2026, 7], [2026, 2], [2024, 2], [2025, 12]]) {
  const cells = ffCalGrid(y, m);
  if (cells.length !== 42) throw new Error('42 cells');
  // First cell is a Monday (UTC) and the grid is contiguous days.
  const first = new Date(cells[0].iso + 'T00:00:00Z');
  if (first.getUTCDay() !== 1) throw new Error('grid must start on Monday: ' + cells[0].iso);
  for (let i = 1; i < 42; i++) {
    const prev = Date.parse(cells[i-1].iso + 'T00:00:00Z');
    if (Date.parse(cells[i].iso + 'T00:00:00Z') - prev !== 86400000) throw new Error('non-contiguous at ' + i);
  }
  // inMonth cells are exactly the month's days, in order 1..N.
  const inM = cells.filter(c => c.inMonth);
  const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
  if (inM.length !== daysInMonth) throw new Error('inMonth count for ' + y + '-' + m);
  inM.forEach((c, i) => { if (c.day !== i + 1) throw new Error('day order'); });
  if (inM[0].iso !== `${y}-${String(m).padStart(2, '0')}-01`) throw new Error('day 1 iso');
}
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_clamping_and_clamp_note_flag(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
const FIRST = '2024-03-15', LAST = '2026-07-18';
// Future 'to' → clamped to last data day, clampedTo drives the note.
eq(ffClampRange('2026-06-01', '2027-01-01', FIRST, LAST),
   {from: '2026-06-01', to: LAST, clampedFrom: false, clampedTo: true}, 'future to clamps');
// Before-first 'from' → clamped silently (clampedFrom, no note case).
eq(ffClampRange('2020-01-01', '2024-06-01', FIRST, LAST),
   {from: FIRST, to: '2024-06-01', clampedFrom: true, clampedTo: false}, 'past from clamps');
// Inside the span → untouched.
eq(ffClampRange('2025-01-01', '2025-06-01', FIRST, LAST),
   {from: '2025-01-01', to: '2025-06-01', clampedFrom: false, clampedTo: false}, 'inside untouched');
// Entirely outside the data → null (nothing to show).
if (ffClampRange('2027-01-01', '2027-06-01', FIRST, LAST) !== null) throw new Error('after-data must be null');
if (ffClampRange('2020-01-01', '2020-06-01', FIRST, LAST) !== null) throw new Error('before-data must be null');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_quick_link_ranges(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
eq(ffPrevMonthRange('2026-07-18'), {from: '2026-06-01', to: '2026-06-30'}, 'previous month');
eq(ffPrevMonthRange('2026-01-05'), {from: '2025-12-01', to: '2025-12-31'}, 'previous month across year');
eq(ffPrevMonthRange('2026-03-10'), {from: '2026-02-01', to: '2026-02-28'}, 'february length');
eq(ffThisYearRange('2026-07-18'), {from: '2026-01-01', to: '2026-07-18'}, 'this year');
// Season = 1 Nov → 31 Oct; last season = the one before today's.
eq(ffLastSeasonRange('2026-07-18'), {from: '2024-11-01', to: '2025-10-31'}, 'last season (mid-season)');
eq(ffLastSeasonRange('2026-11-05'), {from: '2025-11-01', to: '2026-10-31'}, 'last season (after Nov 1)');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_localstorage_round_trip_per_profile(self):
        out = _run_node("""
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + ': ' + JSON.stringify(a)); };
// Default (nothing stored) = pre-3.4.4 90-day window.
eq(ffLoadRange(), {type: 'days', days: 90}, 'default 90d');
// Days selection round-trips.
ffSaveRange({type: 'days', days: 42});
eq(ffLoadRange(), {type: 'days', days: 42}, 'days round-trip');
// Range selection round-trips.
ffSaveRange({type: 'range', from: '2025-01-01', to: '2025-12-31', label: 'y2025'});
eq(ffLoadRange(), {type: 'range', from: '2025-01-01', to: '2025-12-31', label: 'y2025'}, 'range round-trip');
// Per-profile isolation: switching profile must NOT see p1's selection.
globalThis.__PROFILE = 'p2';
eq(ffLoadRange(), {type: 'days', days: 90}, 'other profile gets default');
globalThis.__PROFILE = 'p1';
// Corrupted payloads fall back to the default.
localStorage.setItem(_profileLsKey('ffRange'), 'not-json{{');
eq(ffLoadRange(), {type: 'days', days: 90}, 'corrupt json → default');
localStorage.setItem(_profileLsKey('ffRange'), JSON.stringify({type: 'range', from: '2026-05-01', to: '2026-01-01'}));
eq(ffLoadRange(), {type: 'days', days: 90}, 'inverted range → default');
localStorage.setItem(_profileLsKey('ffRange'), JSON.stringify({type: 'days', days: -5}));
eq(ffLoadRange(), {type: 'days', days: 90}, 'negative days → default');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)

    def test_range_query_shapes(self):
        out = _run_node("""
if (ffRangeQuery({type: 'days', days: 42}) !== 'days=42') throw new Error('days query');
if (ffRangeQuery({type: 'days', days: 0}) !== 'days=1825') throw new Error('All → 5y fetch (pre-3.4.4 contract)');
if (ffRangeQuery({type: 'range', from: '2025-01-01', to: '2025-12-31'}) !== 'from=2025-01-01&to=2025-12-31')
  throw new Error('range query');
console.log('OK');
""", self.tmp_path)
        self.assertIn("OK", out)


# ═══════════════════════════════════════════════════════════════════════════
# 4. fitnessChart re-render listener discipline (node harness, fake DOM)
# ═══════════════════════════════════════════════════════════════════════════

class TestFitnessChartListenerStacking(unittest.TestCase):
    def test_two_renders_one_listener_on_replaced_node(self):
        harness = """
// Minimal DOM: innerHTML assignment REPLACES children (real-DOM semantics) —
// ids in the new markup resolve to fresh nodes, old nodes are orphaned.
const nodes = {};
function mkNode(id) {
  return {
    id, listeners: {}, style: {}, attrs: {},
    _innerHTML: '',
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    removeEventListener(t, f) { const a = this.listeners[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); },
    setAttribute(k, v) { this.attrs[k] = v; },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(h) {
      this._innerHTML = h;
      for (const m of String(h).matchAll(/id="([^"]+)"/g)) nodes[m[1]] = mkNode(m[1]);
    },
  };
}
const container = mkNode('fitness-chart');
nodes['fitness-chart'] = container;
const $ = id => nodes[id] || null;

%FITNESS_CHART%

const data = [
  {date: '2026-01-01', ctl: 50, atl: 40, tsb: 10},
  {date: '2026-01-02', ctl: 51, atl: 42, tsb: 9},
  {date: '2026-01-03', ctl: 52, atl: 41, tsb: 11},
];
fitnessChart(data, 'fitness-chart');
const svg1 = nodes['fitness-svg'];
if (!svg1) throw new Error('no svg after render 1');
if ((svg1.listeners.mousemove || []).length !== 1) throw new Error('render 1: expected exactly 1 mousemove');
// Second render = a filter click. The svg must be a NEW node carrying exactly
// one listener; nothing may accumulate on the persistent container.
fitnessChart(data, 'fitness-chart');
const svg2 = nodes['fitness-svg'];
if (svg2 === svg1) throw new Error('svg node must be replaced per render');
if ((svg2.listeners.mousemove || []).length !== 1) throw new Error('render 2: expected exactly 1 mousemove');
if ((svg2.listeners.mouseleave || []).length !== 1) throw new Error('render 2: expected exactly 1 mouseleave');
if ((container.listeners.mousemove || []).length) throw new Error('listener stacked on persistent container');
if ((svg1.listeners.mousemove || []).length !== 1) throw new Error('old node must keep (orphaned) listener, not migrate');
console.log('OK');
"""
        harness = harness.replace("%FITNESS_CHART%", _fitness_chart_src())
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "stacking.js"
            f.write_text(harness, encoding="utf-8")
            res = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert "OK" in res.stdout
