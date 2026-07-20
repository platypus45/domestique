"""3.4.2 M5+M6 — named adjustment actions, form truth for continuous,
FR explainer, mode-card wizard, rider-verb workout actions.

Node-harness tests over templates/dashboard.html, one section per item:

M5 §1  name-the-workout — the adjustment chip (today card) and the day-modal
       banner name BOTH workouts ("Planned: TEMPO — <file>, 80min → Now: Z2,
       60min · 42 TSS"); banner buttons are verb+named-workout ("✓ Ride the
       easier Z2 (60min)" / "Ride the original TEMPO anyway"). No bare
       "original"/"the other one" pronouns anywhere.
M5 §2  continuous hides the PLAN WEEKS label+stepper entirely behind
       "Rolling 4-week horizon — extends itself weekly"; finite restores.
M5 §3  continuous greys/disables the whole "I've been training already"
       entry block (scan + backdate) with the teaching note; finite restores.
M5 §4  Fatigue Resistance "?" info-icon on the h3 → .info-popover (standard
       [data-popover] pattern) with the what-is-this copy.
M6 §5  PLAN CONFIGURATION leads with the goal-vs-continuous MODE cards;
       "continuous" left the Goal dropdown; selection filters the form;
       populatePlanFormFromGoal round-trips the card; generatePlan payload
       is byte-identical to the dropdown era.
M6 §6  day-modal "Change this workout" cluster with rider verbs — same
       handlers/endpoints, renamed surfaces; "Make it easier today" only
       where the tier-down endpoint can act; "Skip today" + consequence
       line; plan-grid ⟳ tooltip/toast carry the ONE name.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "templates" / "dashboard.html"

requires_node = pytest.mark.skipif(shutil.which("node") is None,
                                   reason="node not installed")


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Brace-count slice of `[async ]function <name>(...) {...}`."""
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


def _fns(src: str, *names: str) -> str:
    return "\n".join(_extract_js_function(src, n) for n in names)


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"
    assert "OK" in res.stdout


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

# The M3 owner-incident fixture: sweet-spot stored plan (matched file)
# live-adjusted to Z2.
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
# M5 §1 — the shared Planned→Now builder names BOTH workouts
# ═══════════════════════════════════════════════════════════════════════════

@requires_node
def test_1_planned_now_builder_names_both_workouts():
    src = _src()
    harness = _STUBS + _fns(src, "_sessTypeLabel", "_adjPlannedNowHtml") + """
// Matched-file case: name stripped of its embedded duration, ONE ", Nmin".
let line = _adjPlannedNowHtml(
  {session_type: 'sweetspot', duration_min: 80, zwo_duration_min: 80},
  {session_type: 'z2', duration_min: 60, tss_estimate: 42},
  'Sweet Spot Steady 80min');
if (!line.includes('Planned: <b>SWEET SPOT — Sweet Spot Steady, 80min</b>'))
  throw new Error('planned side must name label + file + duration: ' + line);
if (!line.includes('Now: <b>Z2, 60min · 42 TSS</b>'))
  throw new Error('now side must name label + duration + TSS: ' + line);
if (!line.includes('→')) throw new Error('arrow joiner missing');

// No matched name → label + duration still name the workout.
line = _adjPlannedNowHtml({session_type: 'tempo', duration_min: 75},
                          {session_type: 'z2', duration_min: 75, tss_estimate: 50}, '');
if (!line.includes('Planned: <b>TEMPO, 75min</b>'))
  throw new Error('nameless planned side: ' + line);

// Rest adjustment reads "Rest day", never "REST, 0min".
line = _adjPlannedNowHtml({session_type: 'tempo', duration_min: 75},
                          {session_type: 'rest', duration_min: 0, tss_estimate: 0}, '');
if (!line.includes('Now: <b>Rest day</b>')) throw new Error('rest now-side: ' + line);
console.log('OK');
"""
    _run_node(harness)


@requires_node
def test_1_banner_names_both_and_buttons_are_verb_plus_name():
    src = _src()
    harness = _STUBS + _fns(
        src, "_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
        "_dayModalModel", "_adjBannerHtml",
    ) + _TODAY_FIXTURE + """
const m = _dayModalModel(SESSION, TODAY_D, new Date().toLocaleDateString('en-CA'));
const html = _adjBannerHtml(m);
// Banner names BOTH workouts.
if (!html.includes('Planned: <b>SWEET SPOT — Sweet Spot Steady, 80min</b>'))
  throw new Error('banner must name the planned workout: ' + html);
if (!html.includes('Now: <b>Z2, 60min · 42 TSS</b>'))
  throw new Error('banner must name the adjusted workout: ' + html);
if (!html.includes('easing today to Z2')) throw new Error('banner must carry the engine reason');
if (!html.includes('Below is the adjusted session')) throw new Error('banner body missing');
// Buttons: verb + NAMED workout.
if (!html.includes('✓ Ride the easier Z2 (60min)'))
  throw new Error('accept must name the easier workout: ' + html);
if (!html.includes('Ride the original SWEET SPOT anyway'))
  throw new Error('keep-original must name the original workout: ' + html);
// No bare pronouns: the old nameless labels are gone; every user-visible
// "original" carries the workout name right after it.
if (html.includes('>Keep original<')) throw new Error('bare "Keep original" leaked');
if (/>[^<]*\\boriginal\\b(?! SWEET SPOT)[^<]*</.test(html))
  throw new Error('bare "original" pronoun leaked: ' + html);
// Handlers unchanged.
if (!html.includes('acceptTodayAdjustment()')) throw new Error('accept not wired');
if (!html.includes('keepOriginalToday()')) throw new Error('keep-original not wired');

// Rest adjustment: truthful verb, keep-original still named.
const mr = _dayModalModel(SESSION, {...TODAY_D,
  adjusted: {session_type: 'rest', duration_min: 0, tss_estimate: 0, description: ''}},
  new Date().toLocaleDateString('en-CA'));
const rhtml = _adjBannerHtml(mr);
if (!rhtml.includes('✓ Take the rest day')) throw new Error('rest accept label missing');
if (!rhtml.includes('Ride the original SWEET SPOT anyway'))
  throw new Error('rest keep-original must still name the workout');
if (!rhtml.includes('Now: <b>Rest day</b>')) throw new Error('rest now-side missing');
console.log('OK');
"""
    _run_node(harness)


@requires_node
def test_1_today_card_chip_names_both_workouts():
    src = _src()
    fns = _fns(src, "loadTodaySession", "_effectiveTodaySession",
               "_todayPreviewSource", "_sessTypeLabel", "_adjPlannedNowHtml",
               "_todayPlannedZwoName")
    harness = """
const esc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const CAL_SESSION_LABEL = {rest:'REST', recovery:'RECOVERY', z2:'Z2',
  long_z2:'LONG Z2', tempo:'TEMPO', sweetspot:'SWEET SPOT',
  threshold:'THRESHOLD', vo2max:'VO2MAX', overunder:'OVER/UNDER'};
const REASON = 'Yesterday had 12 minutes at sprint intensity — easing today to Z2';
const payload = {
  planned:  { session_type: 'sweetspot', duration_min: 80, tss_estimate: 95,
              description: 'Sweet Spot Steady 80min',
              zwo_file: 'sweet_spot_steady_80.zwo' },
  adjusted: { session_type: 'z2', duration_min: 60, tss_estimate: 42,
              description: 'z2 (was sweetspot)' },
  was_modified: true, reason: REASON, adjustment_reason: REASON,
  readiness: 55, hr_target: {}, power_target: {}, target_mode: 'power',
  retest_nudge: null,
};
const el = { innerHTML: '', style: {}, classList: { remove(){}, add(){} } };
const holder = { innerHTML: '' };
const els = { 'home-recommendation': el, 'today-blocks-preview': holder };
const $ = id => els[id] || null;
const document = { getElementById: id => els[id] || null };
// The week payload is already cached — the chip reads the matched name off it.
const window = { _planData: { weeks: [{}] }, _athleteFtp: 250,
  _weekPlanSessions: [{ day: new Date().toLocaleDateString('en-CA'),
                        zwo_name: 'Sweet Spot Steady 80min' }] };
const fetch = async () => ({ ok: true, json: async () => payload });
const fixZeroMin = s => s;
const _continuousSuggestionHtml = () => '';
const _deloadAdvanceChipHtml = () => '';
const buildPowerBlocks = () => [{ name: 'Main', min: 60, pctLow: 65, pctHigh: 65 }];
const renderPowerBlocksSVG = () => '<svg data-stub="synthetic"></svg>';
const workoutProfileSVG = () => '<svg data-stub="file"></svg>';
const renderRetestNudge = () => {};
const openTodayRich = () => {};
""" + fns + """
(async () => {
  await loadTodaySession();
  const html = el.innerHTML;
  if (!html) throw new Error('card never rendered');
  // The chip names BOTH workouts, with the matched-file name resolved from
  // the cached week payload (embedded duration stripped, ONE ", Nmin").
  if (!html.includes('Planned: <b>SWEET SPOT — Sweet Spot Steady, 80min</b>'))
    throw new Error('chip must name the planned workout: ' + html);
  if (!html.includes('Now: <b>Z2, 60min · 42 TSS</b>'))
    throw new Error('chip must name the adjusted workout: ' + html);
  // Reason renders EXACTLY once, at --text2.
  if (html.split(REASON).length - 1 !== 1) throw new Error('reason must render once');
  // The old nameless chip lead is gone.
  if (html.includes('Adjusted to <b>')) throw new Error('nameless chip lead must be gone');

  // Name unavailable (cold caches) → label + duration still name it.
  el.innerHTML = ''; window._weekPlanSessions = []; window._planData = null;
  await loadTodaySession();
  if (!el.innerHTML.includes('Planned: <b>SWEET SPOT, 80min</b>'))
    throw new Error('nameless fallback must keep label+duration: ' + el.innerHTML);
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# M5 §2 + §3 (+ M6 §5 filter) — togglePlanFields: continuous vs finite
# ═══════════════════════════════════════════════════════════════════════════

_FORM_STUBS = """
const els = {};
const mk = () => ({ style: {}, value: '', disabled: false, checked: false,
                    title: '', innerHTML: '',
                    dataset: {}, setAttribute(){}, });
for (const id of ['plan-event-fields','plan-target-fields','plan-focus-group',
                  'plan-bc-races','plan-weeks','plan-weeks-group',
                  'plan-horizon-note','plan-goal-group','plan-goal',
                  'plan-edate','plan-entry-block','plan-entry-note',
                  'plan-backdate-on','plan-backdate-wrap','plan-sdate',
                  'entry-scan-results','entry-recognized-note',
                  'plan-mode-card-goal','plan-mode-card-continuous'])
  els[id] = mk();
els['plan-mode-card-goal'].dataset = { selected: '1', mode: 'goal' };
els['plan-mode-card-continuous'].dataset = { selected: '0', mode: 'continuous' };
const $ = id => els[id] || null;
const document = { getElementById: id => els[id] || null };
const window = { _entryScan: null, _entryArm: null, _entryRecognized: false };
const _isEventGoal = g => g === 'event' || g === 'event_preparation';
const _computePlanWeeksFromEventDate = () => null;
let previews = 0;
const refreshPlanPreview = () => { previews++; };
"""


@requires_node
def test_23_toggle_plan_fields_continuous_hides_weeks_and_greys_entry():
    src = _src()
    fns = _fns(src, "_planTrainingMode", "_planGoalValue", "selectPlanMode",
               "togglePlanFields", "onBackdateToggle", "_setEntryRecognized")
    harness = _FORM_STUBS + fns + """
// A finite plan with the backdate armed…
els['plan-goal'].value = 'ftp';
els['plan-backdate-on'].checked = true;
els['plan-sdate'].value = '2026-06-01';

// …switches to CONTINUOUS via the mode card.
selectPlanMode('continuous');
// §2 — PLAN WEEKS label+stepper hidden ENTIRELY, horizon note shown.
if (els['plan-weeks-group'].style.display !== 'none')
  throw new Error('weeks group must HIDE for continuous (disabled-but-styled looked live)');
if (els['plan-horizon-note'].style.display !== '')
  throw new Error('rolling-horizon note must show for continuous');
if (els['plan-weeks'].disabled !== true)
  throw new Error('hidden weeks input stays inert');
// §3 — entry block greyed/disabled with the note; the armed backdate cleared.
if (els['plan-entry-block'].style.opacity !== '0.45')
  throw new Error('entry block must grey out');
if (els['plan-entry-block'].style.pointerEvents !== 'none')
  throw new Error('entry block must be inert');
if (els['plan-entry-note'].style.display !== '')
  throw new Error('entry teaching note must show');
if (els['plan-backdate-on'].disabled !== true)
  throw new Error('backdate checkbox must disable');
if (els['plan-backdate-on'].checked !== false)
  throw new Error('a checked backdate must clear on continuous entry');
if (els['plan-sdate'].value !== '')
  throw new Error('stale start date must clear (payload safety)');
// §5 filter — goal list hidden, focus shown, B/C + event/target fields out.
if (els['plan-goal-group'].style.display !== 'none')
  throw new Error('goal dropdown must hide in continuous mode');
if (els['plan-focus-group'].style.display !== '')
  throw new Error('focus select must show in continuous mode');
if (els['plan-bc-races'].style.display !== 'none')
  throw new Error('B/C races must hide for continuous');
if (els['plan-event-fields'].style.display !== 'none')
  throw new Error('event fields must hide for continuous');
if (els['plan-target-fields'].style.display !== 'none')
  throw new Error('target fields must hide for continuous');
if (_planGoalValue() !== 'continuous')
  throw new Error('mode-aware goal read must say continuous');

// Back to GOAL mode: everything restores.
selectPlanMode('goal');
if (els['plan-weeks-group'].style.display !== '')
  throw new Error('weeks group must restore for finite');
if (els['plan-horizon-note'].style.display !== 'none')
  throw new Error('horizon note must hide for finite');
if (els['plan-weeks'].disabled !== false)
  throw new Error('weeks input editable again');
if (els['plan-entry-block'].style.opacity !== '' ||
    els['plan-entry-block'].style.pointerEvents !== '')
  throw new Error('entry block must restore for finite');
if (els['plan-entry-note'].style.display !== 'none')
  throw new Error('entry note must hide for finite');
if (els['plan-backdate-on'].disabled !== false)
  throw new Error('backdate checkbox must re-enable');
if (els['plan-goal-group'].style.display !== '')
  throw new Error('goal dropdown must return for finite');
if (els['plan-focus-group'].style.display !== 'none')
  throw new Error('focus select must hide for finite');
if (els['plan-target-fields'].style.display !== 'block')
  throw new Error('ftp goal must show target fields again');
if (_planGoalValue() !== 'ftp')
  throw new Error('mode-aware goal read must return the dropdown value');
if (previews < 2) throw new Error('preview must refresh per mode change');
console.log('OK');
"""
    _run_node(harness)


def test_2_horizon_note_and_ids_in_markup():
    src = _src()
    assert 'id="plan-weeks-group"' in src
    assert 'id="plan-horizon-note"' in src
    assert "Rolling 4-week horizon — extends itself weekly" in src
    # The note starts hidden (finite default).
    note = src[src.index('id="plan-horizon-note"'):]
    note = note[:note.index(">") + 1]
    assert "display:none" in note


def test_3_entry_note_and_ids_in_markup():
    src = _src()
    assert 'id="plan-entry-block"' in src
    assert 'id="plan-entry-note"' in src
    assert ("Continuous plans place themselves — your recent rides and "
            "readiness are read automatically, every day.") in src
    # The note is a SIBLING of the greyed block (inside it the opacity dim
    # would grey the explanation too).
    block = _slice_div(src, 'id="plan-entry-block"')
    assert 'id="plan-entry-note"' not in block


def _slice_div(src: str, marker: str) -> str:
    """Slice a <div …marker…>…</div> by tag-depth count."""
    start = src.rindex("<div", 0, src.index(marker) + 1)
    depth = 0
    i = start
    while i < len(src):
        nxt_open = src.find("<div", i)
        nxt_close = src.find("</div>", i)
        if nxt_close == -1:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 6
            if depth == 0:
                return src[start:i]
    raise AssertionError(f"unbalanced div slicing {marker}")


# ═══════════════════════════════════════════════════════════════════════════
# M5 §4 — Fatigue Resistance explainer popover
# ═══════════════════════════════════════════════════════════════════════════

def test_4_fr_info_popover_present_and_wired():
    src = _src()
    # The ? icon rides the standard delegated [data-popover] pattern, on the h3.
    h3_i = src.index("Fatigue Resistance <span")
    icon_i = src.index('data-popover="frpop-what"')
    assert 0 < icon_i - h3_i < 400, "the ? icon must sit on the FR h3"
    icon = src[src.rindex("<span", 0, icon_i):src.index(">", icon_i) + 2]
    assert 'class="info-icon"' in icon
    assert 'tabindex="0"' in icon and 'role="button"' in icon
    # The popover div: standard .info-popover (opaque since the --card fix),
    # carrying the locked copy.
    pop_i = src.index('id="frpop-what"')
    pop = src[src.rindex("<div", 0, pop_i):src.index("</div>", pop_i)]
    assert 'class="info-popover"' in pop
    for phrase in (
        "how much punch you keep after hard work",
        "best 5s–5min power drops after 1,000/1,500/2,000 kJ",
        "the number that decides finales in long races",
        "Computed from your own rides",
        "rides without power data are excluded and noted",
        "Higher = you fade less",
    ):
        assert phrase in pop, f"FR popover copy missing: {phrase}"
    # The pre-existing threshold (i) popover keeps its own id (regression pin).
    assert 'id="fatigue-resistance-info-popover"' in src
    assert 'id="fatigue-resistance-info-icon"' in src


# ═══════════════════════════════════════════════════════════════════════════
# M6 §5 — mode cards: markup, filtering, round-trip, payload pin
# ═══════════════════════════════════════════════════════════════════════════

def test_5_mode_cards_markup_and_dropdown_lost_continuous():
    src = _src()
    # Two selectable cards lead PLAN CONFIGURATION, with the locked copy.
    cfg = src.index("<h3>Plan Configuration</h3>")
    cards = src.index('id="plan-mode-cards"')
    goal_row = src.index('id="plan-goal-group"')
    assert cfg < cards < goal_row, "mode cards must LEAD the form"
    assert 'id="plan-mode-card-goal"' in src
    assert 'id="plan-mode-card-continuous"' in src
    assert "🎯 Train toward a goal" in src
    assert "base→build→peak" in src
    assert "♾ Train continuously" in src
    assert "reads your rides &amp; readiness daily, extends itself weekly" in src
    # Default on fresh load = goal mode.
    goal_card = src[src.index('id="plan-mode-card-goal"') - 200:
                    src.index('id="plan-mode-card-goal"') + 400]
    assert 'data-selected="1"' in goal_card
    # Continuous LEFT the goal dropdown; the finite goals stayed.
    sel = src[src.index('id="plan-goal"'):]
    sel = sel[:sel.index("</select>")]
    assert 'value="continuous"' not in sel
    for gt in ("general", "event", "ftp", "vo2max", "ftp_vo2max", "ctl"):
        assert f'value="{gt}"' in sel
    # Keyboard access on both cards.
    for cid in ("plan-mode-card-goal", "plan-mode-card-continuous"):
        chunk = src[src.index(f'id="{cid}"'):src.index(f'id="{cid}"') + 600]
        assert 'role="button"' in chunk and 'tabindex="0"' in chunk


@requires_node
def test_5_select_plan_mode_updates_cards_and_mode_read():
    src = _src()
    fns = _fns(src, "_planTrainingMode", "selectPlanMode")
    harness = """
const els = {};
const mkCard = sel => ({ style: {}, dataset: { selected: sel },
  attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } });
els['plan-mode-card-goal'] = mkCard('1');
els['plan-mode-card-continuous'] = mkCard('0');
const document = { getElementById: id => els[id] || null };
let toggles = 0;
const togglePlanFields = () => { toggles++; };
""" + fns + """
if (_planTrainingMode() !== 'goal') throw new Error('default mode must be goal');
selectPlanMode('continuous');
if (_planTrainingMode() !== 'continuous') throw new Error('selection must persist on the DOM');
if (els['plan-mode-card-continuous'].dataset.selected !== '1') throw new Error('card state');
if (els['plan-mode-card-goal'].dataset.selected !== '0') throw new Error('exclusive selection');
if (els['plan-mode-card-continuous'].attrs['aria-pressed'] !== 'true')
  throw new Error('aria-pressed must track selection');
if (els['plan-mode-card-continuous'].style.border.indexOf('--accent') < 0)
  throw new Error('selected card wears the accent border');
if (els['plan-mode-card-goal'].style.border.indexOf('--border') < 0)
  throw new Error('deselected card drops the accent');
if (toggles !== 1) throw new Error('selection must re-filter the form');
selectPlanMode('goal');
if (_planTrainingMode() !== 'goal') throw new Error('round-trip back to goal');
// Cards absent (legacy markup / harness) → goal mode, never a crash.
delete els['plan-mode-card-continuous'];
if (_planTrainingMode() !== 'goal') throw new Error('missing cards must default to goal');
console.log('OK');
"""
    _run_node(harness)


@requires_node
def test_5_populate_form_round_trips_the_mode_card():
    src = _src()
    fns = _fns(src, "_planTrainingMode", "selectPlanMode",
               "populatePlanFormFromGoal")
    harness = """
const els = {};
const mk = () => ({ style: {}, value: '', checked: false, innerHTML: '',
                    textContent: '', dataset: {}, setAttribute(){}, });
for (const id of ['plan-goal','plan-focus','plan-distribution','plan-mode',
                  'plan-template','plan-backdate-on','plan-backdate-wrap',
                  'plan-sdate','plan-edate','plan-ename','plan-ekm',
                  'plan-eclimb','plan-etype','plan-weeks','plan-bc-rows',
                  'plan-active-config'])
  els[id] = mk();
els['plan-block-periodization'] = { checked: false };
const mkCard = sel => ({ style: {}, dataset: { selected: sel }, setAttribute(){} });
els['plan-mode-card-goal'] = mkCard('1');
els['plan-mode-card-continuous'] = mkCard('0');
els['plan-goal'].value = 'event';   // the hardcoded markup default
const $ = id => els[id] || null;
const document = { getElementById: id => els[id] || null,
                   querySelectorAll: () => [] };
const window = {};
let toggles = 0;
const togglePlanFields = () => { toggles++; };
const onPlanModeChange = () => {};
const onBackdateToggle = () => {};
const _setEntryRecognized = () => {};
const _entryScanFingerprint = () => 'fp';
const addBcRaceRow = () => {};
const refreshPlanPreview = () => {};
let _phaseSplit = { state: 'rec', values: {}, runwayChanged: false };
""" + fns + """
// Stored CONTINUOUS goal → continuous card selected; the dropdown is left
// alone (there is no continuous option to select).
populatePlanFormFromGoal({ type: 'continuous', focus: 'vo2',
                           hours_per_week: 8 });
if (_planTrainingMode() !== 'continuous')
  throw new Error('stored continuous goal must select the continuous card');
if (els['plan-goal'].value !== 'event')
  throw new Error('dropdown must not be forced to a nonexistent option');
if (els['plan-focus'].value !== 'vo2') throw new Error('focus must round-trip');
if (toggles < 1) throw new Error('mode restore must re-filter the form');

// Stored FINITE goal → goal card + dropdown restored.
populatePlanFormFromGoal({ type: 'ftp', plan_weeks: 10 });
if (_planTrainingMode() !== 'goal')
  throw new Error('finite goal must select the goal card');
if (els['plan-goal'].value !== 'ftp') throw new Error('dropdown must restore');
if (els['plan-weeks'].value !== 10) throw new Error('weeks must restore');

// A rogue continuous goal carrying start_date must NOT re-arm the disabled
// entry block (payload safety under the greyed block).
populatePlanFormFromGoal({ type: 'continuous', start_date: '2026-06-01' });
if (els['plan-backdate-on'].checked !== false)
  throw new Error('continuous restore must not re-check the backdate');
if (window._entryArm != null)
  throw new Error('continuous restore must not arm the entry fingerprint');
console.log('OK');
"""
    _run_node(harness)


@requires_node
def test_5_generate_payload_identical_continuous_and_finite():
    src = _src()
    fns = _fns(src, "_planTrainingMode", "_planGoalValue", "generatePlan")
    harness = """
const els = {};
const mk = v => ({ style: {}, value: v == null ? '' : v, checked: false });
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
const mkCard = sel => ({ style: {}, dataset: { selected: sel }, setAttribute(){} });
els['plan-mode-card-goal'] = mkCard('0');
els['plan-mode-card-continuous'] = mkCard('1');   // continuous card ACTIVE
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
  throw new Error('stop-after-capture');
};
""" + fns + """
(async () => {
  // CONTINUOUS card active → goal_type continuous + focus; payload otherwise
  // identical to the dropdown era (plan_weeks still rides along, ignored by
  // the engine; no event/end date; no events[]).
  await generatePlan();
  if (!captured) throw new Error('generate body never sent');
  if (captured.goal !== 'continuous')
    throw new Error('continuous card must send goal=continuous, got ' + captured.goal);
  if (captured.focus !== 'vo2')
    throw new Error('continuous must send the chosen focus, got ' + captured.focus);
  if ('event_date' in captured) throw new Error('continuous must not send an event/end date');
  if ('events' in captured) throw new Error('continuous must not attach B/C events');
  if (captured.plan_weeks !== 12) throw new Error('plan_weeks stays in the payload');
  if (captured.start_date !== null) throw new Error('no backdate for continuous');
  if (captured.hours_per_week !== 7) throw new Error('availability hours unchanged');

  // GOAL card active → the dropdown drives; byte-compatible finite body.
  captured = null;
  els['plan-mode-card-continuous'].dataset.selected = '0';
  els['plan-mode-card-goal'].dataset.selected = '1';
  await generatePlan();
  if (captured.goal !== 'ftp') throw new Error('goal card must send the dropdown goal');
  if ('focus' in captured) throw new Error('finite goals must NOT grow a focus key');
  if (captured.event_date !== '2026-09-01') throw new Error('ftp end date must ride plan-tdate');
  if (captured.plan_weeks !== 12) throw new Error('plan_weeks must persist');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# M6 §6 — rider-verb action cluster in the day modal
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

_MODAL_FNS = ("_sessTypeLabel", "_adjPlannedNowHtml", "_effectiveTodaySession",
              "_dayModalModel", "_todayDayModalModel", "_calTodayStr",
              "_dayModalTitle", "_adjBannerHtml",
              "_adjOriginalBlockHtml", "dayModalTssStat", "openDayWorkout")


@requires_node
def test_6_change_workout_cluster_rider_verbs_and_wiring():
    src = _src()
    harness = _MODAL_STUBS + _fns(src, *_MODAL_FNS) + _TODAY_FIXTURE + """
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
  // ONE cluster with the rider verbs, wired to the SAME handlers.
  if (!modalHtml.includes('Change this workout')) throw new Error('cluster header missing');
  if (!modalHtml.includes('Swap workout — same type, different session')) throw new Error('rematch verb missing');
  if (!modalHtml.includes(`rematchDaySession('${SESSION.day}')`)) throw new Error('rematch handler unchanged');
  if (!modalHtml.includes('Change training type (VO2, tempo, &hellip;)')) throw new Error('swap verb missing');
  if (!modalHtml.includes(`swapTypeOpen('${SESSION.day}')`)) throw new Error('swap handler unchanged');
  // Today + sweetspot (on the ladder) → the tier-down verb shows, wired to
  // the EXISTING apply.
  if (!modalHtml.includes('Make it easier today')) throw new Error('easier verb missing for today');
  if (!modalHtml.includes('applyReadinessTierDown()')) throw new Error('tier-down handler unchanged');
  // Skip today + the consequence line; dismiss handler unchanged.
  if (!modalHtml.includes('Skip today')) throw new Error('skip verb missing');
  if (!modalHtml.includes('the week re-fits around it — nothing piles up'))
    throw new Error('consequence line missing');
  if (!modalHtml.includes(`dismissSession('${SESSION.day}')`)) throw new Error('dismiss handler unchanged');
  // Old labels are gone (M6 originals + the 3.4.3 pre-relabel verbs).
  for (const old of ['Rematch workout', 'Swap type', 'Dismiss this session',
                     'Give me a different workout', 'Change the type&hellip;'])
    if (modalHtml.includes(old)) throw new Error('old label leaked: ' + old);
  // The rematch info icon keeps the planpop-rematch popover.
  if (!modalHtml.includes('data-popover="planpop-rematch"')) throw new Error('rematch popover unwired');

  // A NON-TODAY day: "Skip this day" (honest), and no "easier today".
  modalHtml = null;
  const other = {...SESSION, day: '2000-01-01'};
  global.window = {_weekPlanSessions: [other], _targetMode: 'power',
                   _todaySessionData: {...TODAY_D, was_modified: false}};
  await openDayWorkout(0);
  if (modalHtml.includes('Make it easier today'))
    throw new Error('tier-down only acts on today — must hide on other days');
  if (!modalHtml.includes('Skip this day')) throw new Error('non-today skip label');
  if (modalHtml.includes('Skip today')) throw new Error('"Skip today" on a non-today day is a lie');

  // A type the ladder cannot drop (z2 → already_at_bottom): no easier verb.
  modalHtml = null;
  const easy = {...SESSION, session_type: 'z2', zwo_file: '', zwo_name: ''};
  global.window = {_weekPlanSessions: [easy], _targetMode: 'power',
                   _todaySessionData: {...TODAY_D, was_modified: false}};
  await openDayWorkout(0);
  if (modalHtml.includes('Make it easier today'))
    throw new Error('z2 is already the bottom — no easier verb');
  if (!modalHtml.includes('Swap workout — same type, different session'))
    throw new Error('rematch verb must stay for z2');

  // Dismissed day: un-dismiss branch, no skip, no easier.
  modalHtml = null;
  const dis = {...SESSION, status: 'dismissed'};
  global.window = {_weekPlanSessions: [dis], _targetMode: 'power',
                   _todaySessionData: {...TODAY_D, was_modified: false}};
  await openDayWorkout(0);
  if (!modalHtml.includes('Un-dismiss (restore to pending)')) throw new Error('un-dismiss branch');
  if (modalHtml.includes('Skip today')) throw new Error('no skip on a dismissed day');
  if (modalHtml.includes('Make it easier today')) throw new Error('no easier on a dismissed day');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


def test_6_one_name_everywhere_grid_tooltip_toast_popover():
    src = _src()
    # Plan-grid + calendar ⟳ tooltips carry the ONE name (3.4.3 relabel).
    assert src.count('title="Swap workout — same type, different session"') == 2
    assert 'title="Re-draw this workout"' not in src
    # Success toasts speak the same language (no "Re-drew").
    assert "Different workout for ${dayLabel}: ${newName}" in src
    assert "Different workout for ${dayStr}: ${newName}" in src
    assert "Re-drew" not in src
    # The rematch popover explains under the new name.
    pop = src[src.index('id="planpop-rematch"'):]
    pop = pop[:pop.index("</div>")]
    assert "<strong>Swap workout</strong>" in pop
    assert "Rematch workout" not in src, "old feature name must be fully retired"
    assert "Give me a different workout" not in src, \
        "pre-3.4.3 verb must be fully retired (buttons, tooltips, help prose)"
    # Modal cluster + skip consequence (source-level pins).
    assert "Change this workout" in src
    assert "Skip today" in src and "Skip this day" in src
    assert "the week re-fits around it — nothing piles up" in src
    # Endpoints unchanged: same fetch targets as before the rename.
    for ep in ("/api/plan/preview-redraw", "/api/plan/accept-redraw",
               "/api/plan/re-draw", "/api/plan/swap-type",
               "/api/readiness/apply-tier-down", "/api/plan/dismiss-session"):
        assert ep in src, f"endpoint {ep} must survive the rename"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
