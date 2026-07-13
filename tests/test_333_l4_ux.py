"""3.3.3 (L4-UX) — smooth "I've been training already → scan → generate" flow.

Wave-B surface (app.py + dashboard.html) built against the Wave-A engine
contract (recognize_entry weeks_remaining field + generate_plan ValueError
on a backdated non-event plan with <1 schedulable week):

1  Scanning state — the scan button is the live progress surface (disabled +
   ticking label), restored afterwards. Never a dead control.
2  Scan-result card — headline "Matched your last N weeks — you're at week X
   of Y", expandable why? (rows + weekly-TSS trend), consequence line in
   plan language, length chips (non-event), phase-strip preview from the
   recognized position (entry shading + "you are here ▼" + end date +
   per-phase counts). /api/plan/entry-scan extended with plan_weeks /
   entry_week / weeks_remaining (defensive) / plan_end_date — date math
   only, no new engine work.
3  One-click generate from the card (no second confirm); engine ValueError
   renders INTO the card as a friendly state, never a raw error/toast.
4  Landing — after a recognized-entry generate the grid scrolls to TODAY's
   week, flashes it, notes "picked up at week X of Y". Never week 1.
5  Change-safety — goal/length/date drift greys the card with a
   "settings changed — re-scan" chip and clears the armed backdate; the
   card-less re-arm (backdate restored from the saved plan) is cleared too.
6  Green ring — verified as legitimate goal-date styling (server is_today
   drives the red today ring); the green ring carries the PLAN END / EVENT
   badge + a "not today" tooltip and never doubles as the today marker.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "templates" / "dashboard.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _extract_js_function(src: str, name: str) -> str:
    """Slice `[async ]function <name>(...) {...}` out of dashboard.html by
    brace count (same extractor as test_331_surfaces / test_333_owner_fixes)."""
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


def _fns(src: str, *names: str) -> str:
    return "\n".join(_extract_js_function(src, n) for n in names)


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "OK" in res.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Extended /api/plan/entry-scan response (TestClient, hermetic)
# ═══════════════════════════════════════════════════════════════════════════

def _canned_scan(proposal: int, start: date | None, *, extra: dict | None = None,
                 n_rows: int | None = None) -> dict:
    rows = []
    n = proposal if n_rows is None else n_rows
    for k in range(1, n + 1):
        rows.append({
            "index": k,
            "window_start": (date.today() - timedelta(days=7 * (n - k + 1))).isoformat(),
            "actual_tss": 300.0 + 5 * k,
            "target_tss": 320.0,
            "qualifies": True,
            "shape_note": "78% easy riding",
        })
    out = {
        "proposal_weeks": proposal,
        "equivalent_start_date": start.isoformat() if start else None,
        "capped": False,
        "weeks": rows,
    }
    if extra:
        out.update(extra)
    return out


class EntryScanBase(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(app_module, "_load_all_rides_safe", return_value=[]),
            patch.object(app_module, "cached",
                         side_effect=lambda key, fn, ttl=300: {"ctl": 55.0}),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()


class TestEntryScanCardFields(EntryScanBase):
    """The endpoint wraps recognize_entry with the card's fields — pure date
    math on what the recognizer computed (plan_weeks / entry_week /
    weeks_remaining / plan_end_date), engine payload passed through intact."""

    def test_non_event_fields_and_weeks_remaining_fallback(self):
        start = date.today() - timedelta(weeks=8)
        canned = _canned_scan(8, start)  # NO weeks_remaining → fallback path
        with patch.object(tp, "recognize_entry", return_value=canned):
            r = self.client.get("/api/plan/entry-scan",
                                params={"goal": "ftp", "plan_weeks": 12})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["plan_weeks"], 12)
        self.assertEqual(d["goal"], "ftp")
        self.assertEqual(d["entry_week"], 9)
        self.assertEqual(d["weeks_remaining"], 4)  # 12 − 8, defensive .get
        self.assertEqual(
            d["plan_end_date"],
            (start + timedelta(days=12 * 7 - 1)).isoformat(),
        )
        # Engine payload passes through untouched.
        self.assertEqual(d["proposal_weeks"], 8)
        self.assertEqual(d["equivalent_start_date"], start.isoformat())
        self.assertEqual(len(d["weeks"]), 8)
        self.assertIn("shape_note", d["weeks"][0])

    def test_wave_a_weeks_remaining_passes_through(self):
        start = date.today() - timedelta(weeks=8)
        canned = _canned_scan(8, start, extra={"weeks_remaining": 3})
        with patch.object(tp, "recognize_entry", return_value=canned):
            r = self.client.get("/api/plan/entry-scan",
                                params={"goal": "ftp", "plan_weeks": 12})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["weeks_remaining"], 3)  # Wave A's value wins

    def test_no_proposal_shape(self):
        canned = _canned_scan(0, None, n_rows=3)
        with patch.object(tp, "recognize_entry", return_value=canned):
            r = self.client.get("/api/plan/entry-scan",
                                params={"goal": "general", "plan_weeks": 12})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIsNone(d["entry_week"])
        self.assertEqual(d["weeks_remaining"], 12)  # fresh start = full runway
        self.assertNotIn("plan_end_date", d)
        self.assertEqual(len(d["weeks"]), 3)  # evidence rows still shown

    def test_event_goal_end_is_target_and_weeks_are_anchor_spanned(self):
        target = date.today() + timedelta(days=119)  # 17 today-anchored weeks
        start = date.today() - timedelta(weeks=10)
        canned = _canned_scan(10, start)
        with patch.object(tp, "recognize_entry", return_value=canned):
            r = self.client.get(
                "/api/plan/entry-scan",
                params={"goal": "event_preparation",
                        "event_date": target.isoformat()})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["plan_end_date"], target.isoformat())
        # H1 parity: Y is the anchor→target span, not the today-anchored one.
        self.assertEqual(d["plan_weeks"], 27)  # ceil((119 + 70) / 7)
        self.assertEqual(d["entry_week"], 11)
        self.assertEqual(d["weeks_remaining"], 17)


# ═══════════════════════════════════════════════════════════════════════════
# Generate with a card-selected length + the ValueError surface shape
# ═══════════════════════════════════════════════════════════════════════════

class GenerateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tp.load_workout_library()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self._patches = [
            patch.object(app_module, "_plan_dir", return_value=self._tmp),
            patch.object(app_module, "cached",
                         side_effect=lambda key, fn, ttl=300: {"ctl": 55.0}),
            patch.object(app_module, "_longest_ride_h_90d", return_value=3.0),
        ]
        for p in self._patches:
            p.start()
        import ride_storage as _rs
        self._p_rides = patch.object(_rs, "list_rides", return_value=[])
        self._p_rides.start()
        self._p_tss = patch.object(_rs, "recent_mean_weekly_tss",
                                   return_value=330.0)
        self._p_tss.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._p_tss.stop()
        self._p_rides.stop()
        for p in self._patches:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()


class TestGenerateWithCardLength(GenerateBase):
    def test_selected_length_feeds_plan_weeks(self):
        """The card's length chip lands in the generate body as plan_weeks;
        the engine honors it on a recognized backdated start and the plan
        keeps schedulable weeks after today."""
        start = date.today() - timedelta(weeks=2)
        r = self.client.post("/api/plan/generate", json={
            "goal": "ftp",
            "hours_per_week": 8.0,
            "max_weekday": 1.5,
            "max_weekend": 3.0,
            "plan_weeks": 16,                       # card-selected chip
            "rest_days": [0],
            "daily_availability": {},
            "start_date": start.isoformat(),
            "entry_mode": "recognized",
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        goal = d["plan_json"]["goal"]
        self.assertEqual(goal["plan_weeks"], 16)
        self.assertEqual(goal["start_date"], start.isoformat())
        self.assertEqual(goal["entry_mode"], "recognized")
        weeks = d["plan_json"]["weeks"]
        self.assertEqual(len(weeks), 16)
        today_iso = date.today().isoformat()
        future_weeks = [w for w in weeks if w["end"] >= today_iso]
        self.assertGreaterEqual(len(future_weeks), 13)
        future_sessions = [
            s for w in weeks for s in w["sessions"]
            if s["session_type"] != "rest" and s["day"] >= today_iso
        ]
        self.assertTrue(future_sessions,
                        "a 14-week remainder must schedule real sessions")


class TestValueErrorSurfaceShape(GenerateBase):
    def test_valueerror_becomes_400_detail_no_write(self):
        """Engine input rejections surface as 400 {"detail": <message>} —
        the card's friendly-state contract — and never persist a plan."""
        with patch.object(tp, "generate_plan",
                          side_effect=ValueError("no trainable weeks left")):
            r = self.client.post("/api/plan/generate", json={
                "goal": "ftp", "plan_weeks": 12,
                "hours_per_week": 8.0, "rest_days": [0],
                "daily_availability": {},
                "start_date": (date.today() - timedelta(weeks=15)).isoformat(),
                "entry_mode": "recognized",
            })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"detail": "no trainable weeks left"})
        self.assertFalse((self._tmp / "current_plan.json").exists(),
                         "a rejected generate must not persist a plan")

    def test_wave_a_guard_fires_end_to_end(self):
        """Integration with Wave A's G2 gate: a backdated non-event start
        with <1 schedulable week is refused as a 400 with a user-facing
        detail (message text owned by Wave A — only the shape is pinned).
        If THIS test alone is red, attribute to training_planner (Wave A)."""
        r = self.client.post("/api/plan/generate", json={
            "goal": "ftp", "plan_weeks": 12,
            "hours_per_week": 8.0, "rest_days": [0],
            "daily_availability": {},
            "start_date": (date.today() - timedelta(weeks=15)).isoformat(),
            "entry_mode": "recognized",
        })
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json().get("detail", "")
        self.assertIsInstance(detail, str)
        self.assertTrue(detail, "400 must carry a user-facing detail")
        self.assertFalse((self._tmp / "current_plan.json").exists())


# ═══════════════════════════════════════════════════════════════════════════
# Node harness — card states, strip, landing, scanning button, today-ring
# ═══════════════════════════════════════════════════════════════════════════

_CARD_STUBS = """
const esc = s => String(s == null ? '' : s);
const PHASE_COLORS = { base:'#3b82f6', build1:'#10b981', build2:'#f59e0b',
                       peak:'#ef4444', taper:'#a855f7', recovery:'#64748b' };
function _isEventGoal(goal) { return goal === 'event' || goal === 'event_preparation'; }
const els = {
  'entry-scan-results': { innerHTML: '' },
  'entry-scan-btn': { disabled: false, textContent: 'Scan my rides',
                      dataset: {}, setAttribute(){}, removeAttribute(){} },
  'entry-recognized-note': { style: {} },
  'plan-goal': { value: 'ftp' },
  'plan-weeks': { value: '12' },
  'plan-edate': { value: '' },
  'plan-sdate': { value: '' },
};
const $ = id => els[id] || null;
"""

_SCAN_FIXTURE = """
function mkScanData() {
  const rows = [];
  for (let k = 1; k <= 8; k++) rows.push({
    index: k, window_start: '2026-05-' + String(10 + k).padStart(2, '0'),
    actual_tss: k <= 4 ? 300 : 340, target_tss: 320, qualifies: true,
    shape_note: '78% easy riding',
  });
  return { proposal_weeks: 8, equivalent_start_date: '2026-05-18',
           capped: false, weeks: rows, weeks_remaining: 4, plan_weeks: 12,
           entry_week: 9, goal: 'ftp', plan_end_date: '2026-08-09' };
}
function mkEs() {
  return { data: mkScanData(), selectedWeeks: 12, whyOpen: false,
           stale: false, done: false, error: '', doneNote: '',
           fingerprint: _entryScanFingerprint(), stripCache: {}, _stripReq: 0 };
}
const PHASES_12 = [
  { name:'base',   weeks:5 }, { name:'build1', weeks:3 },
  { name:'build2', weeks:2 }, { name:'peak',   weeks:1 },
  { name:'taper',  weeks:1 },
];
"""


@needs_node
def test_1_scan_button_live_progress_never_dead():
    """While the scan runs the button is disabled with a live 'Scanning'
    label; afterwards it is restored — success AND failure paths."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryScanFingerprint", "_setEntryRecognized",
               "scanEntryRides", "_entryFmtDate", "_entryEndDateIso",
               "_entryTssTrend", "_entryWhyRowsHtml", "_renderEntryScan",
               "_entryStripHtml")
    harness = _CARD_STUBS + """
const window = {};
function _entryFetchStrip() { return Promise.resolve(null); }
""" + fns + _SCAN_FIXTURE + """
(async () => {
  const btn = els['entry-scan-btn'];
  let resolveFetch;
  let fetch = () => new Promise(r => { resolveFetch = r; });
  globalThis.fetch = (...a) => fetch(...a);
  const p = scanEntryRides();
  if (btn.disabled !== true) throw new Error('button must be disabled while scanning');
  if (!/^Scanning your rides\\./.test(btn.textContent))
    throw new Error('button must show live progress text, got: ' + btn.textContent);
  resolveFetch({ ok: true, json: async () => mkScanData() });
  await p;
  if (btn.disabled) throw new Error('button must be restored after the scan');
  if (btn.textContent !== 'Scan my rides')
    throw new Error('idle label must be restored, got: ' + btn.textContent);
  if (els['entry-scan-results'].innerHTML.indexOf('Matched your last 8 weeks') < 0)
    throw new Error('card must render after the scan');
  // Failure path: control restored too (never dead).
  fetch = () => Promise.reject(new Error('boom'));
  await scanEntryRides();
  if (btn.disabled || btn.textContent !== 'Scan my rides')
    throw new Error('button must be restored after a failed scan');
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_2_card_fresh_state_headline_chips_consequence():
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryScanFingerprint", "_setEntryRecognized",
               "_entryFmtDate", "_entryEndDateIso", "_entryTssTrend",
               "_entryWhyRowsHtml", "_renderEntryScan", "_entryStripHtml",
               "entryCardToggleWhy", "entryCardSelectWeeks")
    harness = _CARD_STUBS + """
const window = {};
function _entryFetchStrip() { return Promise.resolve(null); }
""" + fns + _SCAN_FIXTURE + """
window._entryScan = mkEs();
window._entryScan.stripCache[12] = PHASES_12;
_renderEntryScan();
const html = els['entry-scan-results'].innerHTML;
if (html.indexOf('Matched your last 8 weeks') < 0)
  throw new Error('headline N missing');
if (html.indexOf('week 9 of 12') < 0)
  throw new Error('headline X of Y missing');
// Consequence line in plan language with the end date.
if (html.indexOf('Generating now leaves 4 trainable weeks') < 0)
  throw new Error('consequence line missing');
if (html.indexOf('plan ends 9 Aug') < 0)
  throw new Error('plan end date missing from consequence');
// Length chips: current + presets + custom stepper.
for (const chip of ['data-entry-chip="12"', 'data-entry-chip="16"',
                    'data-entry-chip="20"', 'data-entry-chip="26"'])
  if (html.indexOf(chip) < 0) throw new Error('missing chip ' + chip);
if (html.indexOf('12w (current)') < 0) throw new Error('current chip unlabeled');
if (html.indexOf('One week longer') < 0 || html.indexOf('One week shorter') < 0)
  throw new Error('custom stepper missing');
// One-click generate + why toggle + no old two-step Confirm button.
if (html.indexOf('entry-card-generate') < 0) throw new Error('generate button missing');
if (html.indexOf('start at week 9') < 0) throw new Error('generate label must name the entry week');
if (html.indexOf('entry-why-toggle') < 0) throw new Error('why? toggle missing');
if (/>\\s*Confirm\\s*</.test(html)) throw new Error('two-step Confirm must be gone');
// why? closed by default, opens with rows + TSS trend.
if (html.indexOf('entry-why-body') >= 0) throw new Error('why? must start closed');
entryCardToggleWhy();
const html2 = els['entry-scan-results'].innerHTML;
if (html2.indexOf('entry-why-body') < 0) throw new Error('why? must open');
if (html2.indexOf('Weekly TSS 300 → 340 (building)') < 0)
  throw new Error('TSS trend missing, got: ' + html2.slice(html2.indexOf('why-body'), html2.indexOf('why-body') + 300));
if (html2.indexOf('78% easy riding') < 0) throw new Error('week rows missing from why?');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_2_card_chip_select_rerenders_for_length():
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryScanFingerprint", "_setEntryRecognized",
               "_entryFmtDate", "_entryEndDateIso", "_entryTssTrend",
               "_entryWhyRowsHtml", "_renderEntryScan", "_entryStripHtml",
               "entryCardSelectWeeks")
    harness = _CARD_STUBS + """
const window = {};
const stripCalls = [];
function _entryFetchStrip(w) { stripCalls.push(w); return Promise.resolve(null); }
""" + fns + _SCAN_FIXTURE + """
window._entryScan = mkEs();
window._entryScan.stripCache[12] = PHASES_12;
_renderEntryScan();
entryCardSelectWeeks(20);
const es = window._entryScan;
if (es.selectedWeeks !== 20) throw new Error('chip select must update length');
const html = els['entry-scan-results'].innerHTML;
if (html.indexOf('week 9 of 20') < 0) throw new Error('headline must follow the chip');
if (html.indexOf('Generating now leaves 12 trainable weeks') < 0)
  throw new Error('consequence must follow the chip');
if (html.indexOf('plan ends 4 Oct') < 0)
  throw new Error('end date must follow the chip');
if (stripCalls.indexOf(20) < 0)
  throw new Error('strip preview must be fetched for the selected length');
// Clamps: floor = proposal+1 (never zero schedulable), ceiling = 52.
entryCardSelectWeeks(3);
if (window._entryScan.selectedWeeks !== 9) throw new Error('floor clamp must be N+1');
entryCardSelectWeeks(60);
if (window._entryScan.selectedWeeks !== 52) throw new Error('ceiling clamp must be 52');
// K = 1 remainder is named as the consolidation week (plan language).
entryCardSelectWeeks(9);
if (els['entry-scan-results'].innerHTML.indexOf('leaves 1 easy consolidation week') < 0)
  throw new Error('K=1 must speak in consolidation-week language');
// A new length is a new attempt — a prior engine refusal clears.
window._entryScan.error = 'old refusal';
entryCardSelectWeeks(16);
if (window._entryScan.error !== '') throw new Error('chip select must clear the refusal');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_2_strip_render_shading_marker_end_counts():
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryFmtDate", "_entryStripHtml")
    harness = """
const esc = s => String(s == null ? '' : s);
const PHASE_COLORS = { base:'#3b82f6', build1:'#10b981', build2:'#f59e0b',
                       peak:'#ef4444', taper:'#a855f7' };
""" + fns + """
const phases = [
  { name:'base',   weeks:5 }, { name:'build1', weeks:3 },
  { name:'build2', weeks:2 }, { name:'peak',   weeks:1 },
  { name:'taper',  weeks:1 },
];
const html = _entryStripHtml(phases, 8, '2026-08-09');
if (html.indexOf('you are here') < 0) throw new Error('you-are-here marker missing');
if (html.indexOf('data-strip-here') < 0) throw new Error('marker element missing');
if (html.indexOf('left:66.66') < 0) throw new Error('marker must sit at 8/12 of the strip');
if (html.indexOf('data-strip-elapsed') < 0) throw new Error('entry-week shading missing');
if (html.indexOf('width:66.66') < 0) throw new Error('shading must cover the credited weeks');
if (html.indexOf('8w credited') < 0) throw new Error('credited count missing');
if (html.indexOf('ends 9 Aug') < 0) throw new Error('end date missing');
for (const seg of ['base 5w', 'build1 3w', 'build2 2w', 'peak 1w', 'taper 1w'])
  if (html.indexOf(seg) < 0) throw new Error('per-phase week count missing: ' + seg);
// Segment colors come from the shared phase palette.
if (html.indexOf('#3b82f6') < 0) throw new Error('phase colors must be reused');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_5_stale_card_greys_chips_and_clears_backdate():
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryScanFingerprint", "_setEntryRecognized",
               "_entryScanCheckStale", "_entryFmtDate", "_entryEndDateIso",
               "_entryTssTrend", "_entryWhyRowsHtml", "_renderEntryScan",
               "_entryStripHtml", "entryCardGenerate")
    harness = _CARD_STUBS + """
const window = { _entryRecognized: false };
function _entryFetchStrip() { return Promise.resolve(null); }
let generateCalls = 0;
async function generatePlan() { generateCalls++; }
""" + fns + _SCAN_FIXTURE + """
(async () => {
  window._entryScan = mkEs();
  window._entryScan.stripCache[12] = PHASES_12;
  els['plan-sdate'].value = '2026-05-18';
  window._entryRecognized = true;
  _renderEntryScan();
  if (els['entry-scan-results'].innerHTML.indexOf('entry-card-stale') >= 0)
    throw new Error('fresh card must not be stale');
  // No drift → no-op.
  _entryScanCheckStale();
  if (window._entryScan.stale) throw new Error('unchanged settings must not stale');
  // The user changes plan length in the FORM → stale + cleared backdate.
  els['plan-weeks'].value = '8';
  _entryScanCheckStale();
  const es = window._entryScan;
  if (!es.stale) throw new Error('length change must stale the card');
  if (els['plan-sdate'].value !== '') throw new Error('stored backdate must be cleared');
  if (window._entryRecognized) throw new Error('recognized flag must be cleared');
  const html = els['entry-scan-results'].innerHTML;
  if (html.indexOf('entry-card-stale') < 0) throw new Error('card must grey out');
  if (html.indexOf('settings changed &mdash; re-scan') < 0)
    throw new Error('re-scan chip missing');
  if (html.indexOf('scanEntryRides()') < 0) throw new Error('chip must re-scan');
  // One-click generate is inert on a stale card.
  await entryCardGenerate();
  if (generateCalls !== 0) throw new Error('stale card must never generate');
  // Card-less re-arm (backdate restored from the saved plan): drift clears it.
  window._entryScan = null;
  els['plan-sdate'].value = '2026-04-27';
  window._entryRecognized = true;
  window._entryArm = { fp: _entryScanFingerprint(), src: 'plan' };
  els['plan-goal'].value = 'general';   // goal change = a different plan
  _entryScanCheckStale();
  if (window._entryArm !== null) throw new Error('re-arm must clear on drift');
  if (els['plan-sdate'].value !== '') throw new Error('re-armed backdate must clear on drift');
  if (window._entryRecognized) throw new Error('recognized flag must clear on drift');
  // DONE card (post-generate): the arm must still clear on drift — a done
  // card is historical, not a live proposal (live-repro'd hole).
  els['plan-goal'].value = 'ftp';
  window._entryScan = mkEs();
  window._entryScan.done = true;
  els['plan-sdate'].value = '2026-06-01';
  window._entryRecognized = true;
  window._entryArm = { fp: _entryScanFingerprint(), src: 'plan' };
  els['plan-goal'].value = 'vo2max';
  _entryScanCheckStale();
  if (window._entryArm !== null) throw new Error('done-card arm must clear on drift');
  if (els['plan-sdate'].value !== '') throw new Error('done-card backdate must clear on drift');
  if (window._entryRecognized) throw new Error('done-card recognized flag must clear on drift');
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_3_one_click_generate_and_friendly_engine_refusal():
    """entryCardGenerate arms date+length+provenance and calls generatePlan
    with NO confirm dialog; a 400 {detail} renders INTO the card as the
    friendly no-trainable-weeks state; success lands + marks the card done."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_entryScanFingerprint", "_setEntryRecognized",
               "_entryFmtDate", "_entryEndDateIso", "_entryTssTrend",
               "_entryWhyRowsHtml", "_renderEntryScan", "_entryStripHtml",
               "entryCardGenerate", "_pgEntryCurrentIdx", "_pgEntryNoteText",
               "generatePlan")
    harness = _CARD_STUBS + """
els['plan-status'] = { textContent: '', style: {} };
els['plan-distribution'] = { value: 'polarized' };
els['plan-block-periodization'] = { checked: false };
els['plan-mode'] = { value: 'auto' };
els['plan-tdate'] = { value: '' };
els['plan-backdate-on'] = { checked: true };
const window = { _entryRecognized: false, _planData: { weeks: [{}, {}] } };
const document = { querySelectorAll: () => [] };
const confirm = () => { throw new Error('one-click: confirm() must NOT fire from the card'); };
const readBcRaces = () => [];
const _phaseSplitBlocked = () => false;
const _phaseSplitPayload = () => undefined;
const _phaseSplitErrorText = () => '';
const _syncPhaseSplitError = () => {};
function _entryFetchStrip() { return Promise.resolve(null); }
let rendered = null;
async function renderPlanJSON(p) { rendered = p; }
let landed = null;
function _pgEntryLanding(p) { landed = p; }
let fetchBody = null;
let fetchResp = null;
globalThis.fetch = async (url, init) => { fetchBody = JSON.parse(init.body); return fetchResp; };
""" + fns + _SCAN_FIXTURE + """
(async () => {
  // ── engine refusal → friendly card state, never a raw toast ──
  window._entryScan = mkEs();
  window._entryScan.stripCache[12] = PHASES_12;
  fetchResp = { ok: false, status: 400, json: async () => ({
    detail: 'Start date 2026-03-30 is 15 weeks back, but the plan is only 12 weeks long.' }) };
  await entryCardGenerate();
  if (fetchBody.plan_weeks !== 12) throw new Error('selected length must feed plan_weeks');
  if (fetchBody.start_date !== '2026-05-18') throw new Error('recognized date must feed start_date');
  if (fetchBody.entry_mode !== 'recognized') throw new Error('provenance must be recognized');
  const es = window._entryScan;
  if (!es.error) throw new Error('400 detail must land in the card state');
  const html = els['entry-scan-results'].innerHTML;
  if (html.indexOf('This start leaves no trainable weeks &mdash; extend the plan or pick a later date.') < 0)
    throw new Error('friendly refusal line missing');
  if (html.indexOf('15 weeks back') < 0) throw new Error('server detail must be shown (small)');
  if (els['plan-status'].textContent !== '')
    throw new Error('refusal must not leak into the status toast, got: ' + els['plan-status'].textContent);
  // ── success → landing + done card ──
  window._entryScan = mkEs();
  window._entryScan.stripCache[12] = PHASES_12;
  const fmt = d => d.toLocaleDateString('en-CA');
  const t = new Date(); t.setHours(12, 0, 0, 0);
  const mon = new Date(t); mon.setDate(t.getDate() - ((t.getDay() + 6) % 7) - 14);
  const weeks = [];
  for (let i = 0; i < 4; i++) {
    const s = new Date(mon); s.setDate(mon.getDate() + i * 7);
    const e = new Date(s); e.setDate(s.getDate() + 6);
    weeks.push({ week_num: i + 1, start: fmt(s), end: fmt(e), sessions: [] });
  }
  const planJson = { goal: {}, weeks };
  fetchResp = { ok: true, status: 200, json: async () => ({ ok: true, plan_json: planJson }) };
  await entryCardGenerate();
  if (rendered !== planJson) throw new Error('success must render the plan');
  if (landed !== planJson) throw new Error('recognized generate must land on today');
  if (!window._entryScan.done) throw new Error('card must flip to done');
  if (window._entryScan.doneNote.indexOf('Picked up at week 3 of 4') < 0)
    throw new Error('done note must carry the pickup line, got: ' + window._entryScan.doneNote);
  if (els['entry-scan-results'].innerHTML.indexOf('Picked up at week 3 of 4') < 0)
    throw new Error('done card must show the pickup line');
  console.log('OK');
})().catch(e => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_4_landing_scrolls_highlights_and_notes_current_week():
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _fns(src, "_pgEntryCurrentIdx", "_pgEntryNoteText", "_pgEntryLanding")
    harness = """
const esc = s => String(s == null ? '' : s);
const row = { classes: [], scrolled: false,
              classList: { add(c) { row.classes.push(c); } },
              scrollIntoView(o) { row.scrolled = true; } };
let querySel = null;
const host = { prepended: '',
               insertAdjacentHTML(pos, html) { host.prepended = html; },
               querySelector(sel) { querySel = sel; return row; } };
const document = { getElementById: id => (id === 'plan-calendar' ? host : null) };
""" + fns + """
const fmt = d => d.toLocaleDateString('en-CA');
const t = new Date(); t.setHours(12, 0, 0, 0);
const mon = new Date(t); mon.setDate(t.getDate() - ((t.getDay() + 6) % 7) - 14);
const weeks = [];
for (let i = 0; i < 12; i++) {
  const s = new Date(mon); s.setDate(mon.getDate() + i * 7);
  const e = new Date(s); e.setDate(s.getDate() + 6);
  weeks.push({ start: fmt(s), end: fmt(e) });
}
const plan = { weeks };
if (_pgEntryCurrentIdx(plan) !== 2) throw new Error('current week must be index 2');
if (_pgEntryNoteText(plan) !== 'Picked up at week 3 of 12')
  throw new Error('note text wrong: ' + _pgEntryNoteText(plan));
_pgEntryLanding(plan);
if (querySel !== '[data-pg-week-idx="2"]')
  throw new Error('must target TODAY\\'s row (never week 1), got: ' + querySel);
if (row.classes.indexOf('pg-entry-week-flash') < 0) throw new Error('highlight class missing');
if (!row.scrolled) throw new Error('must scroll the current week into view');
if (host.prepended.indexOf('Picked up at week 3 of 12') < 0)
  throw new Error('one-line note missing: ' + host.prepended);
// All weeks in the past (shouldn't happen post-Wave-A, but never week 1):
const oldMon = new Date(t); oldMon.setDate(t.getDate() - 200);
const oldWeeks = [];
for (let i = 0; i < 4; i++) {
  const s = new Date(oldMon); s.setDate(oldMon.getDate() + i * 7);
  const e = new Date(s); e.setDate(s.getDate() + 6);
  oldWeeks.push({ start: fmt(s), end: fmt(e) });
}
if (_pgEntryCurrentIdx({ weeks: oldWeeks }) !== 3)
  throw new Error('fallback must be the LAST week, never week 1');
console.log('OK');
"""
    _run_node(harness)


_CAL_STUBS = """
const esc = s => String(s == null ? '' : s);
const calCardTitleWithStructure = p => (p && p.session_type) || '';
const calContentCss = () => '';
const calCellTooltip = () => '';
const calActualClass = () => '';
const _calMatchBadge = () => '';
const _classifColor = () => null;
"""


@needs_node
def test_6_today_ring_is_today_only_and_goal_ring_labeled():
    """The red today ring follows the server's is_today ONLY; the green
    plan-end ring never doubles as a today marker and carries the PLAN END
    badge + a 'not today' tooltip (owner misread: green ring on 19 Jul while
    today was the 13th)."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "renderCalDay")
    harness = _CAL_STUBS + """
const window = { _calData: { goal: { type: 'ftp', end_date: '2026-07-19' } } };
""" + fn + """
const today = '2026-07-13';
// TODAY's cell (server is_today) → red ring, no goal badge.
let html = renderCalDay({date:'2026-07-13', card_state:'planned', is_today:true,
                         planned:{session_type:'z2', duration_min:60, tss:45}},
                        0, 0, 'base', today);
if (html.indexOf('cal-today') < 0) throw new Error('today cell must carry the red ring');
if (html.indexOf('cal-event-day') >= 0 || html.indexOf('cal-goal-badge') >= 0)
  throw new Error('today cell must not carry the goal ring/badge');
// Plan-end cell (the owner's 19 Jul) → green ring + badge + tooltip, NEVER cal-today.
html = renderCalDay({date:'2026-07-19', card_state:'rest', is_today:false},
                    0, 6, 'base', today);
if (html.indexOf('cal-today') >= 0)
  throw new Error('plan-end cell must never carry the today ring');
if (html.indexOf('cal-event-day') < 0) throw new Error('plan-end green ring missing');
if (html.indexOf('PLAN END') < 0) throw new Error('PLAN END badge missing');
if (html.indexOf('Plan ends here') < 0 || html.indexOf('not today') < 0)
  throw new Error('plan-end tooltip must say it is not today');
// Event goal variant: EVENT badge + event tooltip.
window._calData.goal = { type: 'event_preparation', event_date: '2026-07-19' };
html = renderCalDay({date:'2026-07-19', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 6, 'taper', today);
if (html.indexOf('EVENT') < 0) throw new Error('EVENT badge missing');
if (html.indexOf('Event day') < 0 || html.indexOf('not today') < 0)
  throw new Error('event tooltip must say it is not today');
if (html.indexOf('cal-today') >= 0) throw new Error('event cell is not today');
// Coincidence: goal date IS today → the red ring wins, badge suppressed.
html = renderCalDay({date:'2026-07-19', card_state:'planned', is_today:true,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 6, 'taper', '2026-07-19');
if (html.indexOf('cal-today') < 0) throw new Error('today ring must win on coincidence');
if (html.indexOf('cal-goal-badge') >= 0) throw new Error('badge suppressed when today');
console.log('OK');
"""
    _run_node(harness)


if __name__ == "__main__":
    unittest.main()
