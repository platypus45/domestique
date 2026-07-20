"""3.3.3 (owner) — three owner-reported UX fixes.

A   Phase-split editor: a custom split summing to FEWER (or more) weeks than
    the plan length now shows a PROMINENT red error at the BOTTOM of the
    editor + a mirror next to the (disabled) Generate button, live-updated on
    every stepper click via _syncPhaseSplitError() — no server round-trip
    needed. One message generator (_phaseSplitErrorText) feeds all surfaces.

B   Wizard per-day hours seed the Availability Calendar on generate. The old
    plan's dense "available" rows (the PREVIOUS wizard's defaults) are no
    longer fed to generate_plan as availability_overrides — only explicit
    blocks (holiday/injury/illness/unavailable) survive, exactly the rows the
    v1.8.21 calendar rebuild preserves. Symptom killed: wizard said 1h/day
    but the engine kept building 2h sessions off the stale rows.

C   The green goal ring on the calendar (event day / end-of-plan day) was
    misread as the today marker — it now carries a small green pill badge
    (EVENT on event goals, PLAN END otherwise). The red ring stays TODAY.
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
    brace count (same extractor as test_331_surfaces)."""
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


def _phase_split_fns(src: str, *names: str) -> str:
    return "\n".join(_extract_js_function(src, n) for n in names)


# ═══════════════════════════════════════════════════════════════════════════
# A — phase-split under/over-distribution error
# ═══════════════════════════════════════════════════════════════════════════

@needs_node
def test_a_error_text_and_blocked_predicate():
    """Pure validation: under → 'Only N of M … assign K more', over →
    'remove K', exact → allowed; rec/disabled states never block."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _phase_split_fns(src, "_phaseSplitSum", "_phaseSplitBlocked",
                           "_phaseSplitErrorText")
    harness = fns + """
let _phaseSplit = { state: 'custom', values: {base:4,build1:3,build2:2,peak:1,taper:1},
                    rec: {}, M: 12, lockedWeeks: 0, disabled: false,
                    disabledReason: '', runwayChanged: false };
// 11 of 12 — the owner's exact case.
if (!_phaseSplitBlocked()) throw new Error('11/12 must block Generate');
let msg = _phaseSplitErrorText();
if (msg !== 'Only 11 of 12 weeks distributed — assign 1 more week before generating.')
  throw new Error('under message wrong: ' + msg);
// 9 of 12 — pluralization.
_phaseSplit.values = {base:2,build1:3,build2:2,peak:1,taper:1};
msg = _phaseSplitErrorText();
if (msg.indexOf('assign 3 more weeks') < 0)
  throw new Error('plural under message wrong: ' + msg);
// 14 of 12 — over-distribution, unified style.
_phaseSplit.values = {base:7,build1:3,build2:2,peak:1,taper:1};
if (!_phaseSplitBlocked()) throw new Error('14/12 must block Generate');
msg = _phaseSplitErrorText();
if (msg !== '14 of 12 weeks distributed — remove 2 weeks before generating.')
  throw new Error('over message wrong: ' + msg);
// Locked weeks (e.g. consolidation) count toward the sum.
_phaseSplit.values = {base:4,build1:3,build2:2,peak:1,taper:1};
_phaseSplit.lockedWeeks = 1;
if (_phaseSplitBlocked()) throw new Error('11 + 1 locked === 12 must be allowed');
if (_phaseSplitErrorText() !== '') throw new Error('exact sum must yield no error');
_phaseSplit.lockedWeeks = 0;
// Runway-changed variant keeps the count and adds the reset hint.
_phaseSplit.runwayChanged = true;
msg = _phaseSplitErrorText();
if (msg.indexOf('Only 11 of 12 weeks distributed') !== 0 ||
    msg.indexOf('Plan length changed') < 0)
  throw new Error('runway-changed message wrong: ' + msg);
_phaseSplit.runwayChanged = false;
// state 'rec' reseeds from the server — never blocked.
_phaseSplit.state = 'rec';
if (_phaseSplitBlocked() || _phaseSplitErrorText() !== '')
  throw new Error('rec state must never block');
// disabled editor (race-week plan) — never blocked.
_phaseSplit.state = 'custom';
_phaseSplit.disabled = true;
if (_phaseSplitBlocked() || _phaseSplitErrorText() !== '')
  throw new Error('disabled editor must never block');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_a_generate_button_disabled_and_mirror_message():
    """_syncGenerateBlocked disables the Generate button AND paints the same
    message next to it; both clear when the sum is fixed."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _phase_split_fns(src, "_phaseSplitSum", "_phaseSplitBlocked",
                           "_phaseSplitErrorText", "_syncGenerateBlocked",
                           "_syncPhaseSplitError")
    harness = fns + """
const els = {
  'btn-generate-plan': { disabled: false },
  'plan-generate-error': { textContent: '', style: {} },
  'phase-split-error': { textContent: '', style: {} },
  'phase-split-sum': { textContent: '', style: {} },
};
const $ = id => els[id];
let _phaseSplit = { state: 'custom', values: {base:4,build1:3,build2:2,peak:1,taper:1},
                    rec: {}, M: 12, lockedWeeks: 0, disabled: false,
                    disabledReason: '', runwayChanged: false };
_syncPhaseSplitError();
if (els['btn-generate-plan'].disabled !== true)
  throw new Error('Generate must be disabled at 11/12');
if (els['plan-generate-error'].textContent.indexOf('Only 11 of 12 weeks distributed') < 0)
  throw new Error('mirror next to Generate must carry the message, got: ' +
                  els['plan-generate-error'].textContent);
if (els['plan-generate-error'].style.display === 'none')
  throw new Error('mirror must be visible while blocked');
if (els['phase-split-error'].textContent.indexOf('assign 1 more week') < 0)
  throw new Error('editor bottom line must carry the message');
if (els['phase-split-sum'].textContent !== '11 of 12 weeks distributed')
  throw new Error('counter wrong: ' + els['phase-split-sum'].textContent);
// Fix the split — everything clears without any render round-trip.
_phaseSplit.values.base = 5;
_syncPhaseSplitError();
if (els['btn-generate-plan'].disabled !== false)
  throw new Error('Generate must re-enable at 12/12');
if (els['plan-generate-error'].style.display !== 'none')
  throw new Error('mirror must hide when fixed');
if (els['phase-split-error'].style.display !== 'none')
  throw new Error('editor bottom line must hide when fixed');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_a_error_renders_at_editor_bottom():
    """The rendered editor carries #phase-split-error BELOW the last phase
    row (prominent, red), and hides it when the sum is exact."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fns = _phase_split_fns(src, "_phaseSplitSum", "_phaseSplitBlocked",
                           "_phaseSplitErrorText", "_phaseSplitBadgeHtml",
                           "_pwFloor", "_pwCeil", "_renderPlanPreviewPhases")
    harness = """
const window = {};
const esc = s => String(s == null ? '' : s);
const els = { 'plan-phases': { innerHTML: '' }, 'plan-summary': { innerHTML: '' } };
const $ = id => els[id];
const _syncGenerateBlocked = () => {};
""" + fns + """
let _phaseSplit = { state: 'custom', values: {base:4,build1:3,build2:2,peak:1,taper:1},
                    rec: {base:5,build1:3,build2:2,peak:1,taper:1}, M: 12,
                    lockedWeeks: 0, disabled: false, disabledReason: '',
                    runwayChanged: false };
const phases = [
  {name:'base',   weeks:5, start:'2026-01-05', end:'2026-02-08', focus:'', weekly_tss:300},
  {name:'build1', weeks:3, start:'2026-02-09', end:'2026-03-01', focus:'', weekly_tss:340},
  {name:'build2', weeks:2, start:'2026-03-02', end:'2026-03-15', focus:'', weekly_tss:360},
  {name:'peak',   weeks:1, start:'2026-03-16', end:'2026-03-22', focus:'', weekly_tss:380},
  {name:'taper',  weeks:1, start:'2026-03-23', end:'2026-03-29', focus:'', weekly_tss:200},
];
_renderPlanPreviewPhases(phases, 12, 'general', '', {plan_weeks: 12});
let html = els['plan-phases'].innerHTML;
const errAt = html.indexOf('id="phase-split-error"');
if (errAt < 0) throw new Error('bottom error div missing from editor render');
const lastRowAt = html.lastIndexOf('border-left:4px solid');
if (errAt < lastRowAt)
  throw new Error('error line must render BELOW the last phase row');
if (html.indexOf('Only 11 of 12 weeks distributed — assign 1 more week before generating.') < 0)
  throw new Error('prominent message missing from render');
if (html.slice(errAt, errAt + 400).indexOf('var(--red)') < 0)
  throw new Error('error line must be red');
if (html.slice(errAt - 200, errAt + 100).indexOf('display:none') >= 0)
  throw new Error('error must be VISIBLE while mis-summing');
// Exact sum → the div still exists (for live sync) but hidden + empty.
_phaseSplit.values.base = 5;
_renderPlanPreviewPhases(phases, 12, 'general', '', {plan_weeks: 12});
html = els['plan-phases'].innerHTML;
const at2 = html.indexOf('id="phase-split-error"');
if (at2 < 0) throw new Error('hidden error div must still render for live sync');
if (html.slice(at2 - 200, at2 + 100).indexOf('display:none') < 0)
  throw new Error('error must be hidden when the sum is exact');
console.log('OK');
"""
    _run_node(harness)


# ═══════════════════════════════════════════════════════════════════════════
# B — wizard per-day hours seed the availability calendar on generate
# ═══════════════════════════════════════════════════════════════════════════

class TestWizardHoursSeedAvailability(unittest.TestCase):
    """TestClient round-trip: old plan carries dense 2h 'available' rows (the
    previous wizard's defaults) + two explicit blocks; the new wizard says
    1h/day. The engine must see ONLY the explicit blocks as overrides, the
    persisted calendar must read 1h everywhere else, and no session may be
    built to the stale 2h."""

    @classmethod
    def setUpClass(cls):
        tp.load_workout_library()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self._patch_plan_dir = patch.object(
            app_module, "_plan_dir", return_value=self._tmp)
        self._patch_plan_dir.start()
        self._fit_dir = self._tmp / "fit"
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._patch_fit = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir)
        self._patch_fit.start()
        import ride_storage as _rs
        self._patch_rides = patch.object(_rs, "list_rides", return_value=[])
        self._patch_rides.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_fit.stop()
        self._patch_plan_dir.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _write_old_plan_with_2h_calendar(self, holiday: str, injury: str):
        monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
        weeks = []
        for w_idx in range(2):
            wstart = monday + timedelta(weeks=w_idx)
            sessions = []
            for off in range(7):
                d = wstart + timedelta(days=off)
                sessions.append({
                    "day": d.isoformat(), "day_name": d.strftime("%a"),
                    "session_type": "z2" if off in (1, 3) else "rest",
                    "duration_min": 60 if off in (1, 3) else 0,
                    "tss_estimate": 45 if off in (1, 3) else 0,
                    "description": "",
                    "zwo_file": "z2_test.zwo" if off in (1, 3) else "",
                    "zwo_name": "Z2" if off in (1, 3) else "",
                    "status": "pending",
                })
            weeks.append({
                "week_num": w_idx + 1, "start": wstart.isoformat(),
                "end": (wstart + timedelta(days=6)).isoformat(),
                "phase": "base", "tss_target": 200, "is_stepback": False,
                "sessions": sessions, "hit_per_week": 0,
            })
        # Dense old calendar: 2h "available" everywhere (the OLD wizard's
        # defaults materialized by the v1.8.21 rebuild) + explicit blocks.
        avail = {}
        for i in range(60):
            avail[(date.today() + timedelta(days=i)).isoformat()] = {
                "hours": 2.0, "type": "available"}
        avail[holiday] = {"hours": 0, "type": "holiday"}
        avail[injury] = {"hours": 0, "type": "injury"}
        plan = {
            "goal": {"type": "general", "hours_per_week": 14.0,
                     "rest_days": [], "max_weekday_hours": 2.0,
                     "max_weekend_hours": 2.0},
            "phases": [], "weeks": weeks,
            "generated": "2026-04-19T00:00:00", "availability": avail,
        }
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))

    def test_wizard_1h_seeds_calendar_and_engine(self):
        holiday = (date.today() + timedelta(days=10)).isoformat()
        injury = (date.today() + timedelta(days=12)).isoformat()
        self._write_old_plan_with_2h_calendar(holiday, injury)

        captured: dict = {}
        real_generate = tp.generate_plan

        def spy(goal, **kw):
            captured.update(kw)
            return real_generate(goal, **kw)

        with patch.object(tp, "generate_plan", side_effect=spy):
            r = self.client.post("/api/plan/generate", json={
                "goal": "general",
                "hours_per_week": 7.0,
                "max_weekday": 1.0,
                "max_weekend": 1.0,
                "plan_weeks": 8,
                "rest_days": [],
                "daily_availability": {str(i): 1.0 for i in range(7)},
            })
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertNotIn("error", d)

        # 1. Engine overrides: ONLY the explicit blocks — the stale 2h
        #    "available" rows must NOT reach generate_plan.
        passed = captured.get("availability_overrides") or {}
        self.assertEqual(
            set(passed), {holiday, injury},
            f"engine must see only explicit blocks, got {sorted(passed)[:5]}…",
        )
        self.assertEqual(passed[holiday], 0.0)

        # 2. Persisted calendar: wizard's 1h/day everywhere in the plan span,
        #    explicit blocks preserved verbatim.
        saved = json.loads((self._tmp / "current_plan.json").read_text())
        avail = saved.get("availability") or {}
        self.assertTrue(avail, "generate must write a dense availability dict")
        self.assertEqual(avail.get(holiday), {"hours": 0, "type": "holiday"})
        self.assertEqual(avail.get(injury), {"hours": 0, "type": "injury"})
        others = {k: v for k, v in avail.items() if k not in (holiday, injury)}
        self.assertTrue(others)
        for day_iso, entry in others.items():
            self.assertEqual(
                (entry.get("hours"), entry.get("type")), (1.0, "available"),
                f"{day_iso} must carry the wizard's 1h, got {entry}",
            )

        # 3. The owner-visible symptom: no session built to the stale 2h.
        #    Every cap the wizard sent says 60 min.
        over = [
            (s["day"], s["session_type"], s["duration_min"])
            for w in d["plan_json"]["weeks"] for s in w["sessions"]
            if s["session_type"] != "rest" and s["duration_min"] > 60
        ]
        self.assertEqual(over, [], f"sessions exceed the wizard's 1h/day cap: {over}")


# ═══════════════════════════════════════════════════════════════════════════
# C — green goal ring labeled (EVENT / PLAN END), not mistakable for today
# ═══════════════════════════════════════════════════════════════════════════

_CAL_STUBS = """
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
def test_c_plan_end_badge_only_in_marked_cell():
    """Weeks/FTP/general goal: the end-of-plan cell carries the PLAN END
    badge inside the green ring; ordinary cells carry neither."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "renderCalDay")
    harness = _CAL_STUBS + """
const window = { _calData: { goal: { type: 'general', end_date: '2026-09-27' } } };
""" + fn + """
const today = '2026-07-13';
// Rest cell ON the end date → green ring + PLAN END badge.
let html = renderCalDay({date:'2026-09-27', card_state:'rest', is_today:false},
                        0, 0, 'taper', today);
if (html.indexOf('cal-event-day') < 0) throw new Error('green ring missing on plan-end cell');
if (html.indexOf('cal-goal-badge') < 0) throw new Error('badge missing on plan-end cell');
if (html.indexOf('PLAN END') < 0) throw new Error('weeks-goal badge must read PLAN END');
if (html.indexOf('EVENT<') >= 0) throw new Error('weeks goal must not read EVENT');
// Planned (non-rest) cell ON the end date → badge too (main branch).
html = renderCalDay({date:'2026-09-27', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'taper', today);
if (html.indexOf('cal-goal-badge') < 0 || html.indexOf('PLAN END') < 0)
  throw new Error('planned plan-end cell must carry the badge');
// Any other cell → no ring, no badge.
html = renderCalDay({date:'2026-07-19', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'base', today);
if (html.indexOf('cal-goal-badge') >= 0 || html.indexOf('cal-event-day') >= 0)
  throw new Error('unmarked cell must not carry ring or badge');
html = renderCalDay({date:'2026-07-19', card_state:'rest', is_today:false},
                    0, 0, 'base', today);
if (html.indexOf('cal-goal-badge') >= 0)
  throw new Error('unmarked rest cell must not carry the badge');
// Goal end falling ON today → red TODAY ring wins, no green badge.
html = renderCalDay({date:'2026-09-27', card_state:'planned', is_today:true,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'taper', today);
if (html.indexOf('cal-goal-badge') >= 0)
  throw new Error('today must keep its red ring — no goal badge');
if (html.indexOf('cal-today') < 0) throw new Error('today class must win');
console.log('OK');
"""
    _run_node(harness)


@needs_node
def test_c_event_goal_badge_reads_event():
    """Event goal: the event-date cell reads EVENT (rest + planned branches)."""
    src = DASHBOARD.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "renderCalDay")
    harness = _CAL_STUBS + """
const window = { _calData: { goal: {
  type: 'event_preparation', event_date: '2026-10-04' } } };
""" + fn + """
const today = '2026-07-13';
let html = renderCalDay({date:'2026-10-04', card_state:'rest', is_today:false},
                        0, 0, 'taper', today);
if (html.indexOf('cal-goal-badge') < 0 || html.indexOf('EVENT') < 0)
  throw new Error('event-day rest cell must carry the EVENT badge');
if (html.indexOf('PLAN END') >= 0)
  throw new Error('event goal must not read PLAN END');
html = renderCalDay({date:'2026-10-04', card_state:'planned', is_today:false,
                     planned:{session_type:'z2', duration_min:60, tss:45}},
                    0, 0, 'taper', today);
if (html.indexOf('cal-goal-badge') < 0 || html.indexOf('EVENT') < 0)
  throw new Error('event-day planned cell must carry the EVENT badge');
console.log('OK');
"""
    _run_node(harness)


if __name__ == "__main__":
    unittest.main()
