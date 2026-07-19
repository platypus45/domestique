"""3.4.1 M3 — adjustment-aware day modal: card and click agree.

Owner incident: the home card showed the EFFECTIVE session (HRV/glyco-
adjusted: "Z2, 60 TSS") per the 3.3.1 B1 decision, but clicking the card
opened the day modal (openDayWorkout via openTodayRich/calOpenDay) rendering
the ORIGINAL stored session ("Sweet Spot Steady 80min", 95% FTP ramps) with
zero trace of the adjustment.

Contract (locked):
  1. The modal consumes the SAME effective-session decision the card uses —
     ONE shared function (_effectiveTodaySession) feeds _todayPreviewSource
     (card) and _dayModalModel (modal).
  2. Pending adjustment on the opened day → the modal LEADS with the
     adjusted view (type/duration/TSS + the synthetic approximate shape via
     the existing fileless render branch) behind a banner.
  3. The original matched file demotes to a labeled collapsed block
     ("Original plan: …").
  4. Banner actions: accept → EXISTING /api/today-session/persist +
     /api/plan/re-draw; keep-original → EXISTING /api/readiness/revert-cap
     (C6), which now suppresses the whole live adjustment server-side.
  5. Downloads while pending serve the ADJUSTED targets: FIT synthesizes
     from type+duration (zwo_file=null), ZWO generates from the adjusted
     zone targets (downloadGeneratedZwo); calendar-push hidden (fileless).
  6. Unadjusted / non-today days render EXACTLY as before (pinned).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import os
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

requires_node = pytest.mark.skipif(shutil.which("node") is None,
                                   reason="node not installed")


def _extract_js_function(src: str, name: str) -> str:
    """Brace-count slice of `[async ]function <name>(...) {...}`."""
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


def _fns(src: str, *names: str) -> str:
    return "\n".join(_extract_js_function(src, n) for n in names)


# Shared stubs for harnesses that exercise the HTML builders / modal body.
_STUBS = """
global.esc = s => String(s == null ? '' : s);
global.escJs = s => String(s == null ? '' : s).replace(/'/g, "\\\\'");
global.fixZeroMin = s => s;
global.CAL_SESSION_LABEL = {rest:'REST', recovery:'RECOVERY', z2:'Z2',
  long_z2:'LONG Z2', tempo:'TEMPO', sweetspot:'SWEET SPOT',
  threshold:'THRESHOLD', vo2max:'VO2MAX', overunder:'OVER/UNDER',
  sprint:'SPRINTS', ftp_test:'FTP TEST'};
global.CAL_CONTENT_LABEL = {};
"""

# The owner's incident, as an /api/today-session fixture: sweet-spot stored
# plan (matched file) live-adjusted to Z2.
_TODAY_FIXTURE = """
const TODAY_D = {
  was_modified: true,
  planned: {session_type: 'sweetspot', duration_min: 80, tss_estimate: 95,
            description: 'Sweet Spot Steady 80min',
            zwo_file: 'sweet_spot_steady_80.zwo'},
  adjusted: {session_type: 'z2', duration_min: 60, tss_estimate: 42,
             description: 'z2 (was sweetspot)'},
  adjustment_reason: "Yesterday had 12 minutes at sprint intensity — easing today to Z2",
  reason: "Yesterday had 12 minutes at sprint intensity — easing today to Z2",
};
const SESSION = {
  day: new Date().toLocaleDateString('en-CA'), day_name: 'Monday',
  session_type: 'sweetspot', content_class: '', duration_min: 80,
  tss_estimate: 95, zwo_file: 'sweet_spot_steady_80.zwo',
  zwo_name: 'Sweet Spot Steady 80min', display_name: '',
  zwo_duration_min: 80, description: 'Sweet Spot Steady 80min',
  zone_dist: null, status: 'pending',
};
"""


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the modal model chooses adjusted when was_modified (shared decision fn)
# ═══════════════════════════════════════════════════════════════════════════

@requires_node
def test_modal_model_chooses_adjusted_when_was_modified():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _fns(src, "_effectiveTodaySession", "_dayModalModel") + _TODAY_FIXTURE + """
const todayIso = new Date().toLocaleDateString('en-CA');
let m = _dayModalModel(SESSION, TODAY_D, todayIso);
if (m.lead !== 'adjusted') throw new Error('pending adjustment must lead adjusted, got ' + m.lead);
if (m.rest) throw new Error('z2 adjustment is not rest');
if (m.effSession.session_type !== 'z2') throw new Error('eff type must be ADJUSTED: ' + m.effSession.session_type);
if (m.effSession.duration_min !== 60) throw new Error('eff duration must be ADJUSTED: ' + m.effSession.duration_min);
if (m.effSession.tss_estimate !== 42) throw new Error('eff tss must be ADJUSTED');
if (m.effSession.zwo_file !== '') throw new Error('adjusted session must be FILELESS (live, never matched)');
if (m.original !== SESSION) throw new Error('original session must be preserved');
if (!/easing today to Z2/.test(m.reason)) throw new Error('reason must carry the engine sentence');

// Unadjusted day → original, untouched.
m = _dayModalModel(SESSION, {...TODAY_D, was_modified: false}, todayIso);
if (m.lead !== 'original') throw new Error('unadjusted day must lead original');

// Adjustment pending but a DIFFERENT day is opened → original (adjustments
// are live + today-only).
m = _dayModalModel({...SESSION, day: '2000-01-01'}, TODAY_D, todayIso);
if (m.lead !== 'original') throw new Error('non-today day must lead original');

// Adjusted to rest → rest flag for the compact render.
m = _dayModalModel(SESSION, {...TODAY_D,
  adjusted: {session_type: 'rest', duration_min: 0, tss_estimate: 0,
             description: 'Forced rest'}}, todayIso);
if (m.lead !== 'adjusted' || m.rest !== true) throw new Error('rest adjustment must set rest flag');

// No payload at all → original (graceful).
m = _dayModalModel(SESSION, null, todayIso);
if (m.lead !== 'original') throw new Error('missing payload must lead original');
console.log('OK');
"""
    _run_node(harness)


def test_one_shared_decision_fn_feeds_both_surfaces():
    """Contract §1: card preview AND modal model both consume
    _effectiveTodaySession — no second mod?adjusted:planned fork."""
    src = DASHBOARD.read_text(encoding="utf-8")
    card = _extract_js_function(src, "_todayPreviewSource")
    modal = _extract_js_function(src, "_dayModalModel")
    assert "_effectiveTodaySession(d)" in card
    assert "_effectiveTodaySession(todayData)" in modal
    # The decision expression lives ONLY in the shared fn.
    shared = _extract_js_function(src, "_effectiveTodaySession")
    assert "was_modified" in shared
    assert "was_modified" not in card
    assert "was_modified" not in modal


# ═══════════════════════════════════════════════════════════════════════════
# 2/3 — banner text + original-plan secondary block
# ═══════════════════════════════════════════════════════════════════════════

@requires_node
def test_banner_text_and_actions():
    """3.4.2 M5 §1 — the banner names BOTH workouts and the actions are
    verb+named-workout (no bare "original"/"Keep original" pronouns)."""
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _STUBS + _fns(
        src, "_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
        "_dayModalModel", "_adjBannerHtml"
    ) + _TODAY_FIXTURE + """
const m = _dayModalModel(SESSION, TODAY_D, new Date().toLocaleDateString('en-CA'));
const html = _adjBannerHtml(m);
if (!html.includes('Planned: <b>SWEET SPOT — Sweet Spot Steady, 80min</b>'))
  throw new Error('banner must name the PLANNED workout: ' + html);
if (!html.includes('Now: <b>Z2, 60min · 42 TSS</b>'))
  throw new Error('banner must name the ADJUSTED workout: ' + html);
if (!html.includes('Below is the adjusted session')) throw new Error('banner body missing');
if (!html.includes('easing today to Z2')) throw new Error('banner must carry the engine reason');
if (!html.includes('✓ Ride the easier Z2 (60min)')) throw new Error('accept action label missing');
if (!html.includes('Ride the original SWEET SPOT anyway')) throw new Error('keep-original action missing');
if (html.includes('>Keep original<')) throw new Error('nameless keep-original must be gone');
if (!html.includes('acceptTodayAdjustment()')) throw new Error('accept not wired');
if (!html.includes('keepOriginalToday()')) throw new Error('keep-original not wired');

// Rest adjustment gets a truthful accept label; keep-original stays named.
const mr = _dayModalModel(SESSION, {...TODAY_D,
  adjusted: {session_type: 'rest', duration_min: 0, tss_estimate: 0, description: ''}},
  new Date().toLocaleDateString('en-CA'));
if (!_adjBannerHtml(mr).includes('✓ Take the rest day'))
  throw new Error('rest accept label missing');
if (!_adjBannerHtml(mr).includes('Ride the original SWEET SPOT anyway'))
  throw new Error('rest keep-original must name the workout');
console.log('OK');
"""
    _run_node(harness)


@requires_node
def test_original_plan_secondary_block():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _STUBS + _fns(src, "_sessTypeLabel", "_adjOriginalBlockHtml") + """
global.window = {_targetMode: 'power'};
let html = _adjOriginalBlockHtml({
  session_type: 'sweetspot', content_class: '', duration_min: 80,
  tss_estimate: 95, zwo_file: 'sweet_spot_steady_80.zwo',
  zwo_name: 'Sweet Spot Steady 80min', zwo_duration_min: 80,
});
if (!html.startsWith('<details')) throw new Error('original block must be collapsed (<details>)');
if (!html.includes('Original plan:')) throw new Error('label missing');
if (!html.includes('Sweet Spot Steady 80min')) throw new Error('matched file name missing');
if (!html.includes('80min')) throw new Error('original duration missing');
if (!html.includes('95 TSS')) throw new Error('original TSS missing');
if (!html.includes('Original workout (ZWO)')) throw new Error('labeled original download missing');
if (!html.includes("downloadZwoFile('sweet_spot_steady_80.zwo')")) throw new Error('original ZWO not wired');

// hr mode: ZWO is power-only — no original-ZWO button (mirrors the modal).
global.window = {_targetMode: 'hr'};
html = _adjOriginalBlockHtml({session_type: 'sweetspot', duration_min: 80,
  tss_estimate: 95, zwo_file: 'x.zwo', zwo_name: 'X'});
if (html.includes('downloadZwoFile')) throw new Error('hr mode must not offer the power-only ZWO');
console.log('OK');
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# 4 — accept calls the EXISTING persist + re-draw pair; keep-original calls
#     the EXISTING revert-cap. Both refresh card + calendar + grid.
# ═══════════════════════════════════════════════════════════════════════════

_ACTION_STUBS = """
let calls = [];
let closed = 0, refreshed = [];
global.showToast = () => {};
global.closeModal = () => { closed++; };
global.loadTodaySession = async () => { refreshed.push('today'); };
global.loadCalendar = async () => { refreshed.push('cal'); };
global.loadPlan = async () => { refreshed.push('plan'); };
global.loadWeeklyCalendar = () => { refreshed.push('week'); };
"""


@requires_node
def test_accept_calls_existing_persist_then_redraw():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _STUBS + _ACTION_STUBS + _fns(
        src, "_effectiveTodaySession", "acceptTodayAdjustment",
        "_refreshAfterAdjustAction"
    ) + _TODAY_FIXTURE + """
(async () => {
  global.window = {_todaySessionData: TODAY_D};
  global.fetch = async (url, opts) => {
    calls.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null});
    if (url === '/api/today-session/persist')
      return {ok: true, json: async () => ({ok: true, day: SESSION.day})};
    if (url === '/api/plan/re-draw')
      return {ok: true, json: async () => ({ok: true, action: 'redrawn'})};
    throw new Error('unexpected fetch ' + url);
  };
  await acceptTodayAdjustment();
  if (calls.length !== 2) throw new Error('expected persist+re-draw, got ' + JSON.stringify(calls.map(c=>c.url)));
  if (calls[0].url !== '/api/today-session/persist') throw new Error('first call must be the EXISTING persist endpoint');
  if (calls[0].body.session_type !== 'z2') throw new Error('persist must carry the ADJUSTED type');
  if (calls[0].body.duration_min !== 60) throw new Error('persist must carry the ADJUSTED duration');
  if (calls[0].body.tss_estimate !== 42) throw new Error('persist must carry the ADJUSTED tss');
  if (!/easing today to Z2/.test(calls[0].body.reason)) throw new Error('persist must carry the reason');
  if (calls[1].url !== '/api/plan/re-draw') throw new Error('second call must rematch a real file');
  if (calls[1].body.date !== SESSION.day) throw new Error('re-draw must target today');
  if (closed !== 1) throw new Error('modal must close after accept');
  if (!(refreshed.includes('today') && refreshed.includes('cal') && refreshed.includes('plan') && refreshed.includes('week')))
    throw new Error('card+calendar+grid must all refresh, got ' + refreshed);

  // rest acceptance: persist only — nothing to rematch.
  calls = []; closed = 0; refreshed = [];
  global.window = {_todaySessionData: {...TODAY_D,
    adjusted: {session_type: 'rest', duration_min: 0, tss_estimate: 0, description: ''}}};
  await acceptTodayAdjustment();
  if (calls.length !== 1 || calls[0].url !== '/api/today-session/persist')
    throw new Error('rest accept must persist without re-draw');

  // persist failure: keep the modal open, no re-draw, no refresh.
  calls = []; closed = 0; refreshed = [];
  global.window = {_todaySessionData: TODAY_D};
  global.fetch = async (url) => {
    calls.push({url});
    return {ok: false, status: 500, json: async () => ({error: 'boom'})};
  };
  await acceptTodayAdjustment();
  if (closed !== 0) throw new Error('failed persist must not close the modal');
  if (calls.length !== 1) throw new Error('failed persist must not re-draw');
  if (refreshed.length !== 0) throw new Error('failed persist must not refresh');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@requires_node
def test_keep_original_calls_existing_revert_cap():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _STUBS + _ACTION_STUBS + _fns(
        src, "_sessTypeLabel", "_effectiveTodaySession", "keepOriginalToday",
        "_refreshAfterAdjustAction") + """
(async () => {
  global.window = {};
  global.fetch = async (url, opts) => {
    calls.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null});
    return {ok: true, json: async () => ({ok: true, reverted: true})};
  };
  await keepOriginalToday();
  if (calls.length !== 1 || calls[0].url !== '/api/readiness/revert-cap')
    throw new Error('keep-original must call the EXISTING C6 revert endpoint, got ' + JSON.stringify(calls.map(c=>c.url)));
  if (closed !== 1) throw new Error('modal must close after keep-original');
  if (!(refreshed.includes('today') && refreshed.includes('cal') && refreshed.includes('plan') && refreshed.includes('week')))
    throw new Error('card+calendar+grid must all refresh');

  // network failure: modal stays open, no refresh.
  calls = []; closed = 0; refreshed = [];
  global.fetch = async () => ({ok: false, status: 500, json: async () => ({})});
  await keepOriginalToday();
  if (closed !== 0 || refreshed.length !== 0)
    throw new Error('failed revert must not close/refresh');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# 5/6 — the full modal render: adjusted day leads adjusted with effective-
#       target downloads; unadjusted day renders exactly as before.
# ═══════════════════════════════════════════════════════════════════════════

_MODAL_STUBS = _STUBS + """
global.buildPowerBlocks = (type, dur) => [{name: String(type), min: dur || 0,
  pctLow: 70, pctHigh: 70, kind: 'steady'}];
global.renderPowerBlocksSVG = blocks => `<svg data-blocks="${blocks.map(b=>b.name).join(',')}"></svg>`;
global.workoutProfileSVG = () => '<svg data-real="1"></svg>';
global.ftpTestChooserHtml = () => '';
let modalHtml = null;
global.openModal = h => { modalHtml = h; };
"""


@requires_node
def test_modal_renders_adjusted_lead_and_effective_downloads():
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _MODAL_STUBS + _fns(
        src, "_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
        "_dayModalModel", "_todayDayModalModel", "_calTodayStr",
        "_dayModalTitle", "_adjBannerHtml", "_adjOriginalBlockHtml",
        "openDayWorkout"
    ) + _TODAY_FIXTURE + """
(async () => {
  global.window = {_weekPlanSessions: [SESSION], _todaySessionData: TODAY_D,
                   _targetMode: 'power'};
  global.fetch = async (url) => {
    if (url === '/api/settings') return {ok: true, json: async () => ({ftp: 250, lthr: 170})};
    throw new Error('unexpected fetch ' + url);
  };
  await openDayWorkout(0);
  if (!modalHtml) throw new Error('modal did not open');
  // Leads with the banner (both workouts NAMED, 3.4.2 M5 §1) + ADJUSTED hero.
  if (!modalHtml.includes('Planned: <b>SWEET SPOT — Sweet Spot Steady, 80min</b>'))
    throw new Error('banner missing / planned workout unnamed');
  if (!modalHtml.includes('Now: <b>Z2, 60min · 42 TSS</b>'))
    throw new Error('banner must name the adjusted workout');
  if (modalHtml.indexOf('Planned: <b>') > modalHtml.indexOf('<h2>'))
    throw new Error('banner must LEAD the modal');
  if (!modalHtml.includes('Today — Z2 (60min)')) throw new Error('hero must be the ADJUSTED session: ' + modalHtml.match(/<h2>[^<]*<\\/h2>/));
  // The approximate adjusted shape (synthetic — fileless by design).
  if (!modalHtml.includes('data-blocks="z2"')) throw new Error('chart must be the adjusted synthetic shape');
  if (!modalHtml.includes('Approximate preview')) throw new Error('synthetic shape must be labeled approximate');
  // Downloads serve the ADJUSTED targets: generated ZWO + synthesized FIT.
  if (!modalHtml.includes("downloadGeneratedZwo('z2', 60")) throw new Error('ZWO must generate from adjusted zone targets');
  if (!modalHtml.includes("downloadFIT('z2', 60")) throw new Error('FIT must synthesize from the adjusted type+duration');
  if (!/downloadFIT\\('z2', 60, '[^']*', null\\)/.test(modalHtml)) throw new Error('FIT must NOT transcode the original file');
  if (modalHtml.includes("downloadZwoFile('sweet_spot_steady_80.zwo')") &&
      modalHtml.indexOf("downloadZwoFile('sweet_spot_steady_80.zwo')") < modalHtml.indexOf('Original plan:'))
    throw new Error('original ZWO must only appear in the original block');
  if (modalHtml.includes('calPushPlanner')) throw new Error('calendar-push must hide while an adjustment is pending');
  // Original demoted to the labeled secondary block.
  if (!modalHtml.includes('Original plan:')) throw new Error('original secondary block missing');
  if (!modalHtml.includes('Sweet Spot Steady 80min')) throw new Error('original name missing');
  if (!modalHtml.includes('Original workout (ZWO)')) throw new Error('original download missing');
  // The Change-this-workout cluster (rematch/swap/easier) + skip stay out
  // until the adjustment is resolved (3.4.2 M6 §6 verbs).
  if (modalHtml.includes('Swap workout')) throw new Error('rematch must hide while pending');
  if (modalHtml.includes('Change training type')) throw new Error('swap must hide while pending');
  if (modalHtml.includes('Make it easier today')) throw new Error('tier-down must hide while pending');
  if (modalHtml.includes('Skip today')) throw new Error('skip must hide while pending');

  // Adjusted to REST → compact banner + original, no chart/downloads.
  modalHtml = null;
  global.window = {_weekPlanSessions: [SESSION], _targetMode: 'power',
    _todaySessionData: {...TODAY_D,
      adjusted: {session_type: 'rest', duration_min: 0, tss_estimate: 0,
                 description: 'Forced rest — HRV below SWC for 3+ days (Plews protocol).'}}};
  await openDayWorkout(0);
  if (!modalHtml.includes('✓ Take the rest day')) throw new Error('rest accept missing');
  if (!modalHtml.includes('Rest day')) throw new Error('rest hero missing');
  if (modalHtml.includes('downloadFIT') || modalHtml.includes('downloadGeneratedZwo'))
    throw new Error('rest day has nothing to download');
  if (!modalHtml.includes('Original plan:')) throw new Error('rest render must keep the original block');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@requires_node
def test_unadjusted_day_renders_exactly_as_before():
    """Pin: was_modified=false (same day, same session) → the unadjusted
    modal: no banner, original hero + matched-file line, original-file
    downloads, calendar-push, the full Change-this-workout cluster + skip
    (3.4.2 M6 §6 rider verbs) all present."""
    src = DASHBOARD.read_text(encoding="utf-8")
    harness = _MODAL_STUBS + _fns(
        src, "_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
        "_dayModalModel", "_todayDayModalModel", "_calTodayStr",
        "_dayModalTitle", "_adjBannerHtml", "_adjOriginalBlockHtml",
        "openDayWorkout"
    ) + _TODAY_FIXTURE + """
(async () => {
  global.window = {_weekPlanSessions: [SESSION], _targetMode: 'power',
                   _todaySessionData: {...TODAY_D, was_modified: false}};
  global.fetch = async (url) => {
    if (url === '/api/settings') return {ok: true, json: async () => ({ftp: 250, lthr: 170})};
    if (String(url).startsWith('/api/workout/all/'))
      return {ok: true, json: async () => ({segments: [{type: 'SteadyState', duration: 4800, power: 225}],
                                            ftp: 250, total_seconds: 4800})};
    throw new Error('unexpected fetch ' + url);
  };
  await openDayWorkout(0);
  if (!modalHtml) throw new Error('modal did not open');
  if (modalHtml.includes('Planned: <b>')) throw new Error('unadjusted day must not banner');
  if (modalHtml.includes('Original plan:')) throw new Error('no secondary block when unadjusted');
  if (!modalHtml.includes('Today — SWEET SPOT (80min)')) throw new Error('hero must be the stored session: ' + modalHtml.match(/<h2>[^<]*<\\/h2>/));
  if (!modalHtml.includes('Matched library file:')) throw new Error('matched-file line missing');
  if (!modalHtml.includes('data-real="1"')) throw new Error('real segment chart missing');
  if (!modalHtml.includes("downloadZwoFile('sweet_spot_steady_80.zwo')")) throw new Error('original ZWO download missing');
  if (!/downloadFIT\\('sweetspot', 80, '[^']*', 'sweet_spot_steady_80\\.zwo'\\)/.test(modalHtml))
    throw new Error('FIT must transcode the matched file when unadjusted');
  if (!modalHtml.includes('calPushPlanner')) throw new Error('calendar-push missing when unadjusted');
  if (!modalHtml.includes('Change this workout')) throw new Error('action cluster missing when unadjusted');
  if (!modalHtml.includes('Swap workout')) throw new Error('rematch missing when unadjusted');
  if (!modalHtml.includes('Change training type')) throw new Error('swap missing when unadjusted');
  if (!modalHtml.includes('Make it easier today')) throw new Error('tier-down missing on today+sweetspot');
  if (!modalHtml.includes('Skip today')) throw new Error('skip missing when unadjusted');

  // A DIFFERENT (non-today) day never consults today-session at all: same
  // unadjusted render even while today IS adjusted — and the today-only
  // verbs speak honestly ("Skip this day", no "easier today").
  modalHtml = null;
  const other = {...SESSION, day: '2000-01-01'};
  global.window = {_weekPlanSessions: [other], _targetMode: 'power',
                   _todaySessionData: TODAY_D};
  await openDayWorkout(0);
  if (modalHtml.includes('Planned: <b>')) throw new Error('other days must not banner');
  if (!modalHtml.includes('Swap workout')) throw new Error('other days keep the full action row');
  if (modalHtml.includes('Make it easier today')) throw new Error('tier-down is today-only');
  if (!modalHtml.includes('Skip this day')) throw new Error('non-today skip label');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


def test_modal_fork_is_wired_in_dashboard():
    """Source pins: the fork actually feeds openDayWorkout (banner lead,
    original demotion, action-row gate) — guards against a refactor that
    keeps the helpers but drops the wiring."""
    src = DASHBOARD.read_text(encoding="utf-8")
    body = _extract_js_function(src, "openDayWorkout")
    assert "_todayDayModalModel(session)" in body
    assert "let html = (_adjLead ? _adjBannerHtml(_adjModel) : '');" in body
    assert "_adjOriginalBlockHtml(_origSession)" in body
    # The stored-session action row is gated off while pending.
    assert body.index("if (_adjLead) {") < body.index("rematchDaySession")


# ═══════════════════════════════════════════════════════════════════════════
# server — the C6 revert flag suppresses the WHOLE live adjustment (without
# this, [Keep original] is a visible no-op for HRV/score/DFA causes)
# ═══════════════════════════════════════════════════════════════════════════

def _mk_today_plan(today: date) -> dict:
    monday = today - timedelta(days=today.weekday())
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        if d == today:
            sessions.append({"day": d.isoformat(), "day_name": d.strftime("%a"),
                             "session_type": "tempo", "duration_min": 75,
                             "tss_estimate": 60, "description": "3x15min @ 88% FTP",
                             "zwo_file": "tempo_test.zwo", "zwo_name": "tempo test",
                             "status": "pending"})
        else:
            sessions.append({"day": d.isoformat(), "day_name": d.strftime("%a"),
                             "session_type": "rest", "duration_min": 0,
                             "tss_estimate": 0, "description": "Rest day",
                             "zwo_file": "", "zwo_name": "", "status": "pending"})
    return {"goal": {"type": "general", "hours_per_week": 8.0},
            "phases": [],
            "weeks": [{"week_num": 1, "start": monday.isoformat(),
                       "end": (monday + timedelta(days=6)).isoformat(),
                       "phase": "base", "tss_target": 60, "is_stepback": False,
                       "sessions": sessions}],
            "generated": "2026-07-01T00:00:00"}


class TestRevertFlagSuppressesLiveAdjustment(unittest.TestCase):
    """3.4.1 M3 app.py gate: reverted flag → adjusted == planned,
    was_modified False; unreverted → the HRV adjustment stands."""

    def setUp(self):
        app_module.clear_cache()
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        (tmp / "current_plan.json").write_text(json.dumps(_mk_today_plan(date.today())))
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = tmp
        self._patches = [
            patch.object(app_module, "_maybe_lazy_icu_sync", return_value=None),
            patch.object(app_module, "_kick_lazy_icu_sync", return_value=None),
            patch.object(app_module.db, "query_activities", return_value=[]),
            # HRV streak day 1 + tempo planned → Z2 downgrade (the control).
            patch.object(app_module, "get_sleep_metrics",
                         return_value={"red_hrv_streak": 1, "ln_rmssd_7d": None,
                                       "swc_lower": None, "swc_upper": None,
                                       "sleep_h": 7.5, "rhr_delta": 0}),
            patch.object(app_module, "get_today_metrics",
                         return_value={"ctl": 50, "atl": 45, "tsb": 5}),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()
        app_module.clear_cache()

    def test_unreverted_adjustment_stands(self):
        with patch.object(app_module, "_is_readiness_cap_reverted_today",
                          return_value=False):
            data = self.client.get("/api/today-session").json()
        self.assertTrue(data.get("was_modified"))
        self.assertEqual(data["adjusted"]["session_type"], "z2")

    def test_reverted_flag_restores_planned(self):
        with patch.object(app_module, "_is_readiness_cap_reverted_today",
                          return_value=True):
            data = self.client.get("/api/today-session").json()
        self.assertFalse(data.get("was_modified"),
                         f"revert flag must suppress the adjustment: {data.get('reason')!r}")
        self.assertEqual(data["adjusted"]["session_type"], "tempo")
        self.assertEqual(data["adjusted"]["duration_min"], 75)
        self.assertEqual(data.get("adjustment_reason", ""), "")
        # Targets follow the restored planned session too.
        self.assertEqual(data["planned"]["session_type"], "tempo")

    def test_accept_flow_converges(self):
        """The accept button's server side: persisting the adjusted block
        makes the NEXT /api/today-session read unadjusted (planned == the
        accepted z2; the HRV gate has nothing hard left to demote) — card,
        modal and grid then all tell the same story."""
        with patch.object(app_module, "_is_readiness_cap_reverted_today",
                          return_value=False):
            before = self.client.get("/api/today-session").json()
            self.assertTrue(before.get("was_modified"))
            adj = before["adjusted"]
            r = self.client.post("/api/today-session/persist", json={
                "reason": before.get("adjustment_reason") or "adapted",
                "session_type": adj["session_type"],
                "duration_min": adj["duration_min"],
                "tss_estimate": adj["tss_estimate"],
                "description": adj.get("description") or "",
            })
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("ok"))
            after = self.client.get("/api/today-session").json()
        self.assertEqual(after["planned"]["session_type"], "z2")
        self.assertFalse(after.get("was_modified"),
                         f"accepted day must not re-adjust: {after.get('reason')!r}")
        # The stale sweet-spot ZWO was cleared for the re-match pass.
        plan = json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text())
        sess = next(s for w in plan["weeks"] for s in w["sessions"]
                    if s["day"] == date.today().isoformat())
        self.assertEqual(sess["zwo_file"], "")
        self.assertTrue(sess.get("adapted"))


if __name__ == "__main__":
    unittest.main()
