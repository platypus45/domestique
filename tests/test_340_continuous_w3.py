"""3.4.0 W3 (IP_CONTINUOUS_MODE amendment E) — continuous-mode UI surfaces.

Node-harness tests over templates/dashboard.html (the extract-function
pattern from test_331_surfaces), one section per grill-P1 UI consumer:

W   goal wizard — "Continuous — keep improving" option + focus (ftp|vo2|both)
    wired to the existing generate params; plan-weeks greyed (engine pins the
    rolling 4-week horizon); B/C races hidden (not wired for continuous).
T   home today-card — `continuous_suggestion` {family, reason} renders as the
    headline chip ABOVE the matched session (adjustment-chip skin, exposure
    palette); absent field ⇒ '' (finite cards byte-identical).
H   progress header — "Rolling week N · next deload in X days" instead of
    "Week X of Y" / progress %; bar hidden for continuous, restored for
    finite. Finite strings pinned EXACTLY (phase / week-of / milestone / %).
C   weeks-remaining panel — next-deload / FTP-retest countdown for
    continuous; '' for finite plans (the event_readiness branch is theirs).
K   calendar — W{n} sidebar labels keep working for phase "continuous"
    (+ RECOVERY tag on the deload row); finite per-phase counting pinned.
R   plan-end green ring — a continuous plan has no end: no cal-event-day
    ring, no PLAN END badge, even though /api/calendar emits end_date
    (the rolling horizon's last day). Finite PLAN END still renders.
S   phase overview strip — rolling load/deload week strip with deload
    hatching replaces the base/build/peak timeline for continuous.
P   phase-split editor — stays disabled for continuous with the server's
    one-line explainer (no steppers); engine reason contract pinned.

Hermetic: no network, no plan dir; the one training_planner import is pure
(_recommended_phase_weeks) with the library-index snapshot/restore guard.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "templates" / "dashboard.html"
_LIB_INDEX = REPO / "workouts" / ".library_index.json"

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


@pytest.fixture(scope="module", autouse=True)
def _restore_library_index():
    """The single engine import below must not dirty the tracked index."""
    backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None
    yield
    if backup is not None:
        _LIB_INDEX.write_bytes(backup)


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Slice `[async ]function <name>(...) {...}` by brace count (keeps
    ``async`` — the test_331_surfaces extractor, hardened with the open-paren
    anchor so `renderCalendar` can't match `renderCalendarEmpty`)."""
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


# Real-ish esc for XSS pins (mirror of the dashboard's own escaper).
_ESC_STUB = """
const esc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
"""


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# W — goal wizard: continuous option + focus pref
# ═══════════════════════════════════════════════════════════════════════════

def test_w_wizard_markup_has_continuous_option_and_focus_select():
    src = _src()
    # 3.4.2 M6 §5: continuous is a MODE CARD now, not a dropdown option.
    assert '<option value="continuous">' not in src
    assert 'id="plan-mode-card-continuous"' in src
    assert 'id="plan-focus-group"' in src
    # Focus vocabulary matches the engine's (ftp | vo2 | both).
    focus_block = src[src.index('id="plan-focus"'):]
    focus_block = focus_block[:focus_block.index("</select>")]
    for v in ('value="both"', 'value="ftp"', 'value="vo2"'):
        assert v in focus_block, f"focus select must carry {v}"
    # Round-trip: a saved continuous plan restores its focus pref.
    assert "if (goal.focus) setVal('plan-focus', goal.focus);" in src


@needs_node
def test_w_toggle_plan_fields_continuous_vs_finite():
    """3.4.2 M5 §2/§3 + M6 §5: continuous comes from the MODE CARD; the weeks
    stepper hides behind the horizon note; the entry block greys out."""
    src = _src()
    fn = (_extract_js_function(src, "_planTrainingMode")
          + _extract_js_function(src, "_planGoalValue")
          + _extract_js_function(src, "selectPlanMode")
          + _extract_js_function(src, "togglePlanFields"))
    harness = """
const els = {};
const mk = () => ({ style: {}, value: '', disabled: false, checked: false,
                    title: '', dataset: {}, setAttribute(){} });
for (const id of ['plan-event-fields','plan-target-fields','plan-focus-group',
                  'plan-bc-races','plan-weeks','plan-weeks-group',
                  'plan-horizon-note','plan-goal-group','plan-edate',
                  'plan-goal','plan-entry-block','plan-entry-note',
                  'plan-backdate-on'])
  els[id] = mk();
const mkCard = sel => ({ style: {}, dataset: { selected: sel }, setAttribute(){} });
els['plan-mode-card-goal'] = mkCard('1');
els['plan-mode-card-continuous'] = mkCard('0');
const $ = id => els[id];
const document = { getElementById: id => els[id] || null };
const _isEventGoal = g => g === 'event' || g === 'event_preparation';
const _computePlanWeeksFromEventDate = () => null;
const onBackdateToggle = () => {};
let previews = 0;
const refreshPlanPreview = () => { previews++; };
""" + fn + """
// Continuous (via the mode card): focus shown, B/C hidden, weeks stepper
// HIDDEN behind the horizon note, entry block greyed.
els['plan-goal'].value = 'ftp';
selectPlanMode('continuous');
if (els['plan-focus-group'].style.display !== '')
  throw new Error('focus select must show for continuous');
if (els['plan-bc-races'].style.display !== 'none')
  throw new Error('B/C races must hide for continuous (not wired)');
if (els['plan-weeks-group'].style.display !== 'none')
  throw new Error('plan-weeks group must HIDE for continuous');
if (els['plan-horizon-note'].style.display !== '')
  throw new Error('horizon note must show for continuous');
if (els['plan-weeks'].disabled !== true)
  throw new Error('hidden plan-weeks stays inert for continuous');
if (els['plan-goal-group'].style.display !== 'none')
  throw new Error('goal dropdown must hide for continuous');
if (els['plan-entry-block'].style.opacity !== '0.45')
  throw new Error('entry block must grey for continuous');
if (els['plan-event-fields'].style.display !== 'none')
  throw new Error('event fields must hide for continuous');
if (els['plan-target-fields'].style.display !== 'none')
  throw new Error('target fields must hide for continuous');

// Finite regression — ftp: target fields back, focus hidden, weeks editable.
selectPlanMode('goal');
if (els['plan-target-fields'].style.display !== 'block')
  throw new Error('ftp goal must show target fields');
if (els['plan-focus-group'].style.display !== 'none')
  throw new Error('focus select must hide for finite goals');
if (els['plan-bc-races'].style.display !== '')
  throw new Error('B/C races must return for finite goals');
if (els['plan-weeks'].disabled !== false)
  throw new Error('plan-weeks must be editable again for finite goals');
if (els['plan-weeks-group'].style.display !== '')
  throw new Error('plan-weeks group must return for finite goals');
if (els['plan-horizon-note'].style.display !== 'none')
  throw new Error('horizon note must hide for finite goals');
if (els['plan-entry-block'].style.opacity !== '')
  throw new Error('entry block must restore for finite goals');

// Finite regression — event: event fields shown.
els['plan-goal'].value = 'event';
togglePlanFields();
if (els['plan-event-fields'].style.display !== 'block')
  throw new Error('event goal must show event fields');
if (els['plan-focus-group'].style.display !== 'none')
  throw new Error('focus select must stay hidden for event goals');
if (previews !== 3) throw new Error('preview must refresh per toggle');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_w_generate_body_wires_focus_for_continuous_only():
    src = _src()
    fn = (_extract_js_function(src, "_planTrainingMode")
          + _extract_js_function(src, "_planGoalValue")
          + _extract_js_function(src, "generatePlan"))
    harness = """
const els = {};
const mk = v => ({ style: {}, value: v == null ? '' : v, checked: false });
// 3.4.2 M6 §5: continuous is the MODE CARD; the dropdown holds a finite goal.
const mkCard = sel => ({ style: {}, dataset: { selected: sel }, setAttribute(){} });
els['plan-mode-card-goal'] = mkCard('0');
els['plan-mode-card-continuous'] = mkCard('1');
els['plan-goal'] = mk('ftp');
els['plan-weeks'] = mk('12');
els['plan-distribution'] = mk('polarized');
els['plan-block-periodization'] = { checked: false };
els['plan-mode'] = mk('auto');
els['plan-template'] = mk('');
els['plan-backdate-on'] = { checked: false };
els['plan-sdate'] = mk('');
els['plan-status'] = { textContent: '', style: {} };
els['plan-tdate'] = mk('2026-09-01');
els['plan-edate'] = mk('');
els['plan-ename'] = mk('');
els['plan-ekm'] = mk('');
els['plan-eclimb'] = mk('');
els['plan-etype'] = mk('granfondo');
els['plan-focus'] = mk('vo2');
const $ = id => els[id];
const document = {
  getElementById: id => els[id] || null,
  querySelectorAll: sel => {
    if (sel !== '.plan-day-mins') return [];
    return [0,1,2,3,4,5,6].map(d => ({ dataset: { day: String(d) }, value: '60' }));
  },
};
const window = { _planData: null, _entryRecognized: false };
const _phaseSplitBlocked = () => false;
const _phaseSplitErrorText = () => '';
const _syncPhaseSplitError = () => {};
const _phaseSplitPayload = () => undefined;
const readBcRaces = () => [];
const getCustomBands = () => ({});
const confirm = () => { throw new Error('no plan stored — confirm must not fire'); };
let captured = null;
const fetch = async (url, opts) => {
  captured = JSON.parse(opts.body);
  throw new Error('stop-after-capture');   // catch block owns the rest
};
""" + fn + """
(async () => {
  await generatePlan();
  if (!captured) throw new Error('generate body never sent');
  if (captured.goal !== 'continuous') throw new Error('goal must be continuous');
  if (captured.focus !== 'vo2')
    throw new Error('continuous must send the chosen focus, got ' + captured.focus);
  if ('event_date' in captured)
    throw new Error('continuous must not send an event/end date');
  if ('events' in captured)
    throw new Error('continuous must not attach B/C events');

  // Finite regression — ftp body is byte-compatible with pre-W3: no focus
  // key, end date from plan-tdate, same core fields. Switch = the mode card.
  captured = null;
  els['plan-mode-card-continuous'].dataset.selected = '0';
  els['plan-mode-card-goal'].dataset.selected = '1';
  await generatePlan();
  if (captured.goal !== 'ftp') throw new Error('ftp goal body');
  if ('focus' in captured)
    throw new Error('finite goals must NOT grow a focus key');
  if (captured.event_date !== '2026-09-01')
    throw new Error('ftp end date must still ride plan-tdate');
  if (captured.plan_weeks !== 12) throw new Error('plan_weeks must persist');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# T — today card: continuous_suggestion headline chip
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_t_suggestion_chip_renders_family_reason_and_escapes():
    src = _src()
    fn = _extract_js_function(src, "_continuousSuggestionHtml")
    harness = _ESC_STUB + fn + """
// The IP's exact example shape.
let html = _continuousSuggestionHtml({ continuous_suggestion: {
  family: 'high_aerobic', reason: 'HRV in band, 32min Z4 deficit' } });
if (html.indexOf('Today:') < 0) throw new Error('headline must lead with Today:');
if (html.indexOf('high-aerobic') < 0) throw new Error('family label missing');
if (html.indexOf('HRV in band, 32min Z4 deficit') < 0) throw new Error('reason missing');
if (html.indexOf('#f97316') < 0) throw new Error('high-aerobic must wear the exposure orange');
if (html.indexOf('cont-suggestion-chip') < 0) throw new Error('chip class missing');

// All three families tint from the palette the UI already teaches.
if (_continuousSuggestionHtml({continuous_suggestion:{family:'low_aerobic'}}).indexOf('#22c55e') < 0)
  throw new Error('low-aerobic green');
if (_continuousSuggestionHtml({continuous_suggestion:{family:'anaerobic'}}).indexOf('#ef4444') < 0)
  throw new Error('anaerobic red');

// Unknown family degrades to the adjustment-chip yellow, never crashes.
html = _continuousSuggestionHtml({ continuous_suggestion: { family: 'weird_family' } });
if (html.indexOf('weird-family') < 0 || html.indexOf('rgba(234,179,8,0.1)') < 0)
  throw new Error('unknown family must fall back to the adjustment-chip skin');

// Server text is escaped — a hostile reason cannot script the card.
html = _continuousSuggestionHtml({ continuous_suggestion: {
  family: 'anaerobic', reason: '<script>alert(1)</script>' } });
if (html.indexOf('<script>') >= 0) throw new Error('reason must be escaped');
if (html.indexOf('&lt;script&gt;') < 0) throw new Error('escaped reason must render');

// Absent / malformed ⇒ '' — finite cards render byte-identical.
if (_continuousSuggestionHtml({ planned: { session_type: 'z2' } }) !== '')
  throw new Error('finite payload (no field) must render nothing');
if (_continuousSuggestionHtml(null) !== '') throw new Error('null-safe');
if (_continuousSuggestionHtml({ continuous_suggestion: {} }) !== '')
  throw new Error('family-less suggestion must render nothing');
console.log('OK');
"""
    _run_node(harness)


def test_t_chip_sits_above_the_matched_session():
    """Wiring: the chip is injected between the TODAY'S TRAINING kicker and
    the planned-type hero — i.e. the headline ABOVE the matched session."""
    src = _src()
    fn = _extract_js_function(src, "loadTodaySession")
    kicker = fn.index("TODAY'S TRAINING")
    chip = fn.index("html += _continuousSuggestionHtml(d);")
    hero = fn.index("${planType}")
    assert kicker < chip < hero, "chip must render above the matched session"


# ═══════════════════════════════════════════════════════════════════════════
# H — progress header: rolling week + next deload; finite pinned exactly
# ═══════════════════════════════════════════════════════════════════════════

_HEADER_STUBS = """
const els = {};
const mkEl = () => ({ textContent: '', style: {} });
els['pg-progress-header'] = mkEl();
els['pg-progress-phase'] = mkEl();
els['pg-progress-week'] = mkEl();
els['pg-progress-remaining'] = mkEl();
const barWrap = mkEl();
els['pg-progress-bar-fill'] = Object.assign(mkEl(), { parentElement: barWrap });
const document = { getElementById: id => els[id] || null };
"""


def _js_weeks_continuous() -> str:
    """4 rolling weeks around today: current = week_num 6 (idx 1), deload at
    week_num 8 starting D+12. Dates are day-offsets from the real today so
    the countdown math is deterministic in the harness."""
    w = lambda n, s, e, sb: (f"{{week_num:{n},phase:'continuous',start:'{_iso(s)}',"
                             f"end:'{_iso(e)}',is_stepback:{str(sb).lower()}}}")
    return ("[" + ",".join([w(5, -9, -3, False), w(6, -2, 4, False),
                            w(7, 5, 11, False), w(8, 12, 18, True)]) + "]")


@needs_node
def test_h_progress_header_continuous_and_finite_exact():
    src = _src()
    fns = (_extract_js_function(src, "_pgContinuousHeaderModel")
           + _extract_js_function(src, "pgRenderProgressHeader"))
    harness = _HEADER_STUBS + fns + f"""
const contPlan = {{
  goal: {{ type: 'continuous', focus: 'both' }},
  phases: [{{ name: 'continuous', weeks: 4 }}],
  weeks: {_js_weeks_continuous()},
}};
pgRenderProgressHeader(contPlan, 1);
if (els['pg-progress-week'].textContent !== 'Rolling week 6')
  throw new Error('want "Rolling week 6", got ' + els['pg-progress-week'].textContent);
if (els['pg-progress-remaining'].textContent !== 'next deload in 12 days')
  throw new Error('want "next deload in 12 days", got ' + els['pg-progress-remaining'].textContent);
if (els['pg-progress-phase'].textContent !== 'Phase: CONTINUOUS')
  throw new Error('phase chip: ' + els['pg-progress-phase'].textContent);
if (/Week \\d+ of \\d+/.test(els['pg-progress-week'].textContent))
  throw new Error('continuous must never say Week X of Y');
if (barWrap.style.display !== 'none')
  throw new Error('progress bar must hide for continuous (no end to fill toward)');
if (els['pg-progress-bar-fill'].style.width)
  throw new Error('bar fill must not be driven for continuous');
if (els['pg-progress-header'].style.display !== 'flex')
  throw new Error('header itself stays visible');

// Deload week (scheduled or amendment-C advanced) — the header says so.
pgRenderProgressHeader(contPlan, 3);
if (els['pg-progress-week'].textContent !== 'Rolling week 8')
  throw new Error('deload week num');
if (els['pg-progress-remaining'].textContent !== 'Deload week — recover')
  throw new Error('deload text, got ' + els['pg-progress-remaining'].textContent);

// FINITE REGRESSION — pinned EXACTLY (strings + % + bar restored after a
// continuous render hid it).
const finWeeks = [];
for (let i = 0; i < 12; i++) {{
  const ph = i < 8 ? 'base' : (i < 10 ? 'peak' : 'taper');
  finWeeks.push({{ week_num: i + 1, phase: ph, is_stepback: false }});
}}
const finPlan = {{ goal: {{ type: 'event' }}, phases: [{{name:'base'}}], weeks: finWeeks }};
pgRenderProgressHeader(finPlan, 2);
if (els['pg-progress-phase'].textContent !== 'Phase: BASE')
  throw new Error('finite phase: ' + els['pg-progress-phase'].textContent);
if (els['pg-progress-week'].textContent !== 'Week 3 of 12')
  throw new Error('finite week-of: ' + els['pg-progress-week'].textContent);
if (els['pg-progress-remaining'].textContent !== '6 weeks to Peak')
  throw new Error('finite milestone: ' + els['pg-progress-remaining'].textContent);
if (els['pg-progress-bar-fill'].style.width !== '25%')
  throw new Error('finite progress %: ' + els['pg-progress-bar-fill'].style.width);
if (barWrap.style.display !== '')
  throw new Error('finite render must restore the bar');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_h_header_model_cadence_fallback_without_stepback_in_window():
    """Partial payload (no stepback week visible): the 3:1 cadence answers —
    week_num 6 ⇒ 2 weeks ahead ⇒ end-of-week + 1 + 7 days."""
    src = _src()
    fn = _extract_js_function(src, "_pgContinuousHeaderModel")
    harness = fn + f"""
const weeks = [{{ week_num: 6, phase: 'continuous', start: '{_iso(-2)}',
                  end: '{_iso(4)}', is_stepback: false }}];
const m = _pgContinuousHeaderModel(weeks, 0, '{_iso(0)}');
if (m.weekText !== 'Rolling week 6') throw new Error(m.weekText);
if (m.remainingText !== 'next deload in 12 days')
  throw new Error('cadence fallback: ' + m.remainingText);
console.log('OK');
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# C — weeks-remaining panel → next-deload / retest countdown
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_c_countdown_panel_continuous_variants():
    src = _src()
    fn = _extract_js_function(src, "_continuousCountdownHtml")
    harness = _ESC_STUB + fn + f"""
const today = '{_iso(0)}';

// Finite plans: '' — the event_readiness branch stays theirs.
if (_continuousCountdownHtml({{ phase: 'base', week_num: 3 }}, null, today) !== '')
  throw new Error('finite week must render nothing');
if (_continuousCountdownHtml(null, null, today) !== '') throw new Error('null-safe');

// Deload countdown from the stored horizon (authoritative — includes an
// advanced deload).
const planWeeks = [
  {{ week_num: 6, is_stepback: false, start: '{_iso(-2)}', end: '{_iso(4)}',
     sessions: [] }},
  {{ week_num: 7, is_stepback: false, start: '{_iso(5)}', end: '{_iso(11)}',
     sessions: [{{ session_type: 'ftp_test', day: '{_iso(9)}' }}] }},
  {{ week_num: 8, is_stepback: true, start: '{_iso(12)}', end: '{_iso(18)}',
     sessions: [] }},
];
const cur = {{ phase: 'continuous', week_num: 6, is_stepback: false,
               start: '{_iso(-2)}', end: '{_iso(4)}', sessions: [] }};
let html = _continuousCountdownHtml(cur, planWeeks, today);
if (html.indexOf('Continuous training') < 0) throw new Error('panel title');
if (html.indexOf('No end date') < 0) throw new Error('no-end framing');
if (html.indexOf('Next deload in 12 days') < 0)
  throw new Error('deload countdown, got: ' + html);
if (html.indexOf('FTP retest in 9 days') < 0)
  throw new Error('retest countdown, got: ' + html);
if (/weeks? remaining/.test(html)) throw new Error('must not say weeks remaining');

// Current week IS the deload (scheduled or monotony/ACWR-advanced).
html = _continuousCountdownHtml(
  {{ phase: 'continuous', week_num: 8, is_stepback: true, sessions: [] }},
  planWeeks, today);
if (html.indexOf('Deload week — recover') < 0) throw new Error('deload-now text');
if (html.indexOf('var(--yellow)') < 0) throw new Error('deload-now tint');

// Plan JSON not loaded yet → the 3:1 cadence answers (never a blank panel).
html = _continuousCountdownHtml(cur, null, today);
if (html.indexOf('Next deload in 12 days') < 0)
  throw new Error('cadence fallback, got: ' + html);
if (html.indexOf('FTP retest') >= 0)
  throw new Error('no retest line without a scheduled test');
console.log('OK');
"""
    _run_node(harness)


def test_c_finite_event_readiness_branch_untouched():
    """The legacy er-branch condition and render strings are still verbatim
    in loadWeeklyCalendar — finite panels render exactly as before."""
    src = _src()
    fn = _extract_js_function(src, "loadWeeklyCalendar")
    assert ("er && er.weeks_remaining !== null && er.weeks_remaining !== "
            "undefined && ec" in fn)
    assert "${er.weeks_remaining} weeks / ${er.days_remaining} days remaining" in fn
    # The continuous panel is consulted first and gates on ''.
    assert "_continuousCountdownHtml(" in fn


# ═══════════════════════════════════════════════════════════════════════════
# K — calendar W{n} sidebar labels keep working for phase "continuous"
# ═══════════════════════════════════════════════════════════════════════════

_CAL_RENDER_STUBS = _ESC_STUB + """
const rows = { innerHTML: '' };
const document = { getElementById: id => (id === 'cal-rows' ? rows : null),
                   querySelector: () => null };
const window = { _calDidAutoScroll: true };
const requestAnimationFrame = () => {};
const CAL_PHASE_COLORS = { base: '#3b82f6', build1: '#10b981', peak: '#ef4444',
                           taper: '#a855f7', recovery: '#64748b',
                           history: '#475569', continuous: '#0ea5e9' };
const renderCalDay = () => '<div class="cal-day"></div>';
"""


def _js_cal_week(phase: str, wk_start: int, stepback: bool = False,
                 current: bool = False) -> str:
    return (f"{{phase:'{phase}',is_stepback:{str(stepback).lower()},"
            f"is_current:{str(current).lower()},iso_year:2026,iso_week:29,"
            f"start_date:'{_iso(wk_start)}',end_date:'{_iso(wk_start + 6)}',"
            f"planned_tss:300,actual_tss:0,days:[]}}")


@needs_node
def test_k_calendar_week_of_labels_continuous_and_finite():
    src = _src()
    fn = _extract_js_function(src, "renderCalendar")
    weeks_cont = ",".join([
        _js_cal_week("history", -14),
        _js_cal_week("continuous", -2, current=True),
        _js_cal_week("continuous", 5),
        _js_cal_week("continuous", 12, stepback=True),
        _js_cal_week("continuous", 19),
    ])
    weeks_fin = ",".join([
        _js_cal_week("base", -2, current=True),
        _js_cal_week("base", 5),
        _js_cal_week("build1", 12),
    ])
    harness = _CAL_RENDER_STUBS + fn + f"""
renderCalendar({{ today: '{_iso(0)}', weeks: [{weeks_cont}] }});
let html = rows.innerHTML;
// Per-phase W{{n}} counting still works for the continuous block…
for (const lbl of ['W1', 'W2', 'W4']) {{
  if (html.indexOf('>' + lbl + '<') < 0) throw new Error(lbl + ' label missing');
}}
// …the deload row keeps the RECOVERY tag + stepback styling…
if (html.indexOf('W3 · RECOVERY') < 0)
  throw new Error('deload week must read W3 · RECOVERY');
if (html.indexOf('cal-stepback') < 0) throw new Error('stepback row class');
// …and continuous weeks wear their own chip colour, not the history grey.
if (html.indexOf('>continuous<') < 0) throw new Error('phase chip text');
if (html.indexOf('#0ea5e9') < 0)
  throw new Error('continuous chip must not fall back to history grey');

// Finite regression: counting restarts per phase exactly as before.
rows.innerHTML = '';
renderCalendar({{ today: '{_iso(0)}', weeks: [{weeks_fin}] }});
html = rows.innerHTML;
const first = html.indexOf('>W1<');
const second = html.indexOf('>W2<');
const restart = html.indexOf('>W1<', first + 1);
if (first < 0 || second < 0 || restart < 0)
  throw new Error('finite W-of-phase counting changed');
console.log('OK');
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# R — plan-end green ring: never for continuous (finite keeps it)
# ═══════════════════════════════════════════════════════════════════════════

_CAL_DAY_STUBS = """
const esc = s => String(s == null ? '' : s);
const calCardTitleWithStructure = p => (p && p.session_type) || '';
const calContentCss = () => '';
const calCellTooltip = () => '';
const calActualClass = () => '';
const _calMatchBadge = () => '';
const _classifColor = () => null;
// v3.4.4 — renderCalDay sources card content from the shared helper.
const plannedCardParts = (p, cs) => ({ title: calCardTitleWithStructure(p),
  cssCls: calContentCss(), metaText: '', warn: '' });
"""


@needs_node
def test_r_no_plan_end_ring_or_badge_for_continuous():
    src = _src()
    fn = _extract_js_function(src, "renderCalDay")
    harness = _CAL_DAY_STUBS + """
// /api/calendar emits end_date for EVERY non-event goal — for continuous
// it's just the rolling horizon's last generated day, so nothing may render.
const window = { _calData: { goal: { type: 'continuous', end_date: '2026-08-09' } } };
""" + fn + """
const today = '2026-07-14';
// Rest cell on the horizon's last day → NO ring, NO badge.
let html = renderCalDay({date:'2026-08-09', card_state:'rest', is_today:false},
                        0, 0, 'continuous', today);
if (html.indexOf('cal-event-day') >= 0)
  throw new Error('continuous must never wear the green goal ring');
if (html.indexOf('cal-goal-badge') >= 0 || html.indexOf('PLAN END') >= 0)
  throw new Error('continuous must never carry a PLAN END badge');
// Planned (main-branch) cell on that day → same.
html = renderCalDay({date:'2026-08-09', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'continuous', today);
if (html.indexOf('cal-event-day') >= 0 || html.indexOf('cal-goal-badge') >= 0)
  throw new Error('planned continuous cell must not carry ring/badge');

// Finite regression: a weeks/general goal still marks its end day.
window._calData = { goal: { type: 'general', end_date: '2026-08-09' } };
html = renderCalDay({date:'2026-08-09', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'base', today);
if (html.indexOf('cal-event-day') < 0 || html.indexOf('PLAN END') < 0)
  throw new Error('finite PLAN END ring/badge must still render');
console.log('OK');
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# S — phase overview strip: rolling weeks with deload hatching
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_s_continuous_phase_strip_hatches_the_deload():
    src = _src()
    fn = _extract_js_function(src, "_continuousPhaseStripHtml")
    harness = _ESC_STUB + """
const PHASE_COLORS = { continuous: '#0ea5e9' };
""" + fn + """
const plan = {
  goal: { type: 'continuous' },
  phases: [{ name: 'continuous',
             focus: 'Rolling 4-week block — 3 load + 1 deload, FTP + VO2max focus.' }],
  weeks: [
    { week_num: 5, start: '2026-07-13', end: '2026-07-19', is_stepback: false, tss_target: 420 },
    { week_num: 6, start: '2026-07-20', end: '2026-07-26', is_stepback: false, tss_target: 430 },
    { week_num: 7, start: '2026-07-27', end: '2026-08-02', is_stepback: false, tss_target: 440 },
    { week_num: 8, start: '2026-08-03', end: '2026-08-09', is_stepback: true,  tss_target: 260 },
  ],
};
const html = _continuousPhaseStripHtml(plan);
// One segment per rolling week, week numbers carried through.
for (const lbl of ['W5', 'W6', 'W7', 'W8']) {
  if (html.indexOf('>' + lbl + '<') < 0) throw new Error(lbl + ' segment missing');
}
// Deload hatching: exactly the stepback segment.
if ((html.match(/data-cont-deload="1"/g) || []).length !== 1)
  throw new Error('exactly one deload segment');
if ((html.match(/data-cont-deload="0"/g) || []).length !== 3)
  throw new Error('three load segments');
if ((html.match(/repeating-linear-gradient/g) || []).length !== 1)
  throw new Error('hatch must paint the deload segment only');
if ((html.match(/>DELOAD</g) || []).length !== 1 ||
    (html.match(/>LOAD</g) || []).length !== 3)
  throw new Error('segment sublabels');
// Open-ended framing + the engine phase's focus line.
if (html.indexOf('rolling 4-week window') < 0) throw new Error('window headline');
if (html.indexOf('extends weekly') < 0 || html.indexOf('no end date') < 0)
  throw new Error('open-ended framing');
if (html.indexOf('FTP + VO2max focus') < 0) throw new Error('focus line');
// Empty plans render nothing (first-run safety).
if (_continuousPhaseStripHtml({ weeks: [] }) !== '') throw new Error('empty-safe');
if (_continuousPhaseStripHtml(null) !== '') throw new Error('null-safe');
console.log('OK');
"""
    _run_node(harness)


def test_s_render_plan_json_routes_continuous_to_strip():
    """Wiring: renderPlanJSON swaps the finite timeline for the strip and the
    summary line drops 'N phases · M weeks' for the rolling framing."""
    src = _src()
    fn = _extract_js_function(src, "renderPlanJSON")
    assert "_isContinuousPlan ? [] : phases" in fn
    assert "phaseHtml = _continuousPhaseStripHtml(plan);" in fn
    assert "extends weekly" in fn
    # Finite summary string survives verbatim.
    assert "${phases.length} phases &middot; ${totalWeeks} weeks" in fn


# ═══════════════════════════════════════════════════════════════════════════
# P — phase-split editor: disabled for continuous with the one-line explainer
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_p_preview_editor_disabled_shows_explainer_no_steppers():
    src = _src()
    fn = _extract_js_function(src, "_renderPlanPreviewPhases")
    harness = _ESC_STUB + """
const PHASE_COLORS = { continuous: '#0ea5e9' };
let _phaseSplit = { state: 'rec', values: {}, rec: {}, M: 4, lockedWeeks: 0,
                    disabled: true,
                    disabledReason: 'a continuous plan has no phase split — it rolls 3 load + 1 deload weeks indefinitely',
                    runwayChanged: false };
const els = { 'plan-phases': { innerHTML: '' }, 'plan-summary': { innerHTML: '' } };
const $ = id => els[id];
const _syncGenerateBlocked = () => {};
const _phaseSplitSum = () => 0;
const _phaseSplitBadgeHtml = () => '';
const _phaseSplitErrorText = () => '';
const readBcRaces = () => [];
""" + fn + """
_renderPlanPreviewPhases(
  [{ name: 'continuous', weeks: 4, start: '2026-07-13', end: '2026-08-09',
     weekly_tss: 420, focus: 'Rolling 4-week block' }],
  4, 'continuous', '',
  { phase_weeks_rec: null,
    phase_weeks_disabled_reason: _phaseSplit.disabledReason });
const html = els['plan-phases'].innerHTML;
if (html.indexOf('a continuous plan has no phase split') < 0)
  throw new Error('one-line explainer missing');
if (html.indexOf('phaseStepperClick') >= 0)
  throw new Error('steppers must not render when disabled');
if (html.indexOf('Race week') >= 0)
  throw new Error('must show the continuous reason, not the race-week default');
console.log('OK');
"""
    _run_node(harness)


def test_p_engine_reason_contract():
    """The UI renders whatever `phase_weeks_disabled_reason` says — pin the
    engine side of that seam (pure fn, no I/O)."""
    import training_planner as tp
    rec, reason = tp._recommended_phase_weeks(tp.Goal(goal_type="continuous"))
    assert rec is None, "editor must be disabled for continuous"
    assert "no phase split" in reason
    # And generate blocking never engages when the editor is disabled: the
    # client-side guard keys on _phaseSplit.disabled (source pin).
    fn = _extract_js_function(_src(), "_phaseSplitBlocked")
    assert "ps.disabled" in fn


# ═══════════════════════════════════════════════════════════════════════════
# Cross-surface: finite dashboards carry ZERO continuous markup at rest
# ═══════════════════════════════════════════════════════════════════════════

def test_x_continuous_surfaces_are_all_gated():
    """Every continuous render path is behind an explicit continuous gate —
    grep-level belt-and-suspenders that no surface leaks into finite plans."""
    src = _src()
    # Each consumer keys on the goal type / phase name / additive API field.
    assert "_goalType === 'continuous'" in src                 # progress header
    assert "String(week.phase || '') !== 'continuous'" in src  # countdown panel
    assert "_g.type !== 'continuous'" in src                   # ring (rest branch)
    assert "_goal.type !== 'continuous'" in src                # ring (main branch)
    assert "d && d.continuous_suggestion" in src               # today card
    assert re.search(r"_isContinuousPlan\s*=\s*String\(goal\.type", src)  # strip


# ── W2-chip follow-up (task_0bd7be3c): deload-advance chip + revert ─────────

def test_t_deload_advance_chip_renders_reason_revert_and_escapes():
    src = _src()
    fn = _extract_js_function(src, "_deloadAdvanceChipHtml")
    harness = _ESC_STUB + fn + """
let html = _deloadAdvanceChipHtml({ deload_advance: {
  reason: 'Training strain high (monotony 2.3) — deload advanced to this week',
  trigger: 'monotony', week_num: 7, reverted: false } });
if (html.indexOf('Recovery week pulled forward') < 0) throw new Error('headline missing');
if (html.indexOf('monotony 2.3') < 0) throw new Error('reason missing');
if (html.indexOf('revertDeloadAdvance()') < 0) throw new Error('revert affordance missing');
if (html.indexOf('deload-advance-chip') < 0) throw new Error('chip class missing');

// Reverted / absent / reason-less ⇒ '' — finite cards byte-identical.
if (_deloadAdvanceChipHtml({ deload_advance: { reason: 'x', reverted: true } }) !== '')
  throw new Error('reverted record must render nothing');
if (_deloadAdvanceChipHtml({ planned: { session_type: 'z2' } }) !== '')
  throw new Error('absent payload must render nothing');
if (_deloadAdvanceChipHtml(null) !== '') throw new Error('null-safe');

// Server text escaped.
html = _deloadAdvanceChipHtml({ deload_advance: { reason: '<script>x</script>' } });
if (html.indexOf('<script>') >= 0) throw new Error('reason must be escaped');
console.log('OK');
"""
    _run_node(harness)


def test_t_deload_chip_wired_into_today_card_and_revert_posts():
    src = _src()
    load = _extract_js_function(src, "loadTodaySession")
    assert "_deloadAdvanceChipHtml(d)" in load, \
        "loadTodaySession must render the deload chip"
    assert load.index("_continuousSuggestionHtml(d)") < load.index(
        "_deloadAdvanceChipHtml(d)"), "chip sits under the suggestion headline"
    fn = _extract_js_function(src, "revertDeloadAdvance")
    harness = """
let posted = null, toasts = [];
const fetch = (url, opts) => { posted = {url, method: (opts||{}).method};
  return Promise.resolve({ json: () => Promise.resolve({reverted: true}) }); };
const showToast = (m) => toasts.push(m);
const loadTodaySession = () => { toasts.push('__reloaded__'); };
""" + fn + """
revertDeloadAdvance().then(() => {
  if (!posted || posted.url !== '/api/plan/continuous/deload-revert')
    throw new Error('must POST the revert endpoint, got ' + JSON.stringify(posted));
  if (posted.method !== 'POST') throw new Error('method must be POST');
  if (!toasts.some(t => /reverted/i.test(t))) throw new Error('success toast missing');
  if (toasts.indexOf('__reloaded__') < 0) throw new Error('today card must reload');
  console.log('OK');
}).catch(e => { console.error(e.message); process.exit(1); });
"""
    _run_node(harness)
