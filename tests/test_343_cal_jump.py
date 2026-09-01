"""3.4.3 — opening the Training Plan tab auto-jumps the calendar to today.

Owner: the calendar spans months of past weeks; opening the tab landed at
the top and demanded a long manual scroll. The plan-tab open sequence now
calls calJumpToToday('auto') once rows exist; the header button keeps its
smooth scroll.

3.4.3 B3 hardening (today-identity incident):
- The jump scrolls the CONTAINER (#cal-body.scrollTo) — cell.scrollIntoView
  was a no-op on the nested CSS scroll-behavior:smooth scroller in the
  embedded webview (it dragged the whole page instead), so the committed
  auto-jump never landed. Bare scrollTop= is equally swallowed by the CSS.
- "Today" comes from _calTodayStr(): the SERVER-provided /api/calendar
  `today` (the same value that paints the is_today markers), falling back
  to the browser's LOCAL date. Never new Date().toISOString(): that's UTC,
  which shifts the date for the hours around midnight in TZ+ zones and
  landed the jump on YESTERDAY's cell while the today ring sat on the
  server-marked day.
"""
import re
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}\n{res.stdout}"


_JUMP_FNS = (_extract_js_function(SRC, "_calTodayStr") + "\n"
             + _extract_js_function(SRC, "calJumpToToday"))

# v3.5.6 — calJumpToToday now defers the tab-open jump until #cal-body is
# actually on screen (it was scrolling the container while it sat below the
# fold, which left WebKit's hit-test regions stale — clicks landed a couple of
# rows high). This file tests the SCROLL MATH; the visibility gate has its own
# tests in tests/test_356_planfold_and_caljump.py. Stub it to "visible" so the
# unit under test here is isolated, exactly as before the gate existed.
_JUMP_FNS = "function _calJumpWhenVisible() { return true; }\n" + _JUMP_FNS

# Fake #cal-rows with yesterday / today / tomorrow cells + a #cal-body
# scroller. The clock is pinned to a TZ+ zone just after local midnight:
# toISOString() reads YESTERDAY (2026-07-15) while the local date — and the
# server-marked today — is 2026-07-16. A UTC-keyed jump would target the
# wrong cell; the fixed jump must land on the real today.
_JUMP_HARNESS_ENV = """
function mkCell(top) {
  return { getBoundingClientRect: () => ({ top: top }),
           clientHeight: 60, offsetTop: top };
}
const cells = { '2026-07-15': mkCell(940),
                '2026-07-16': mkCell(1000),
                '2026-07-17': mkCell(1060) };
const queried = [];
const scroller = {
  scrollTop: 200, clientHeight: 246,
  getBoundingClientRect: () => ({ top: 53 }),
  calls: [],
  scrollTo(o) { this.calls.push(o); this.scrollTop = o.top; },
};
const document = {
  getElementById: id => (id === 'cal-body' ? scroller : null),
  querySelector: sel => {
    const m = /data-date="([^"]+)"/.exec(sel);
    if (m) { queried.push(m[1]); return cells[m[1]] || null; }
    const w = /data-cal-week-idx="([^"]+)"/.exec(sel);
    if (w) { queried.push('week-' + w[1]); return mkCell(500); }
    return null;
  },
};
// TZ+ after-midnight clock: UTC string is YESTERDAY, local date is today.
const Date = function() {
  return { toLocaleDateString: () => '2026-07-16',
           toISOString: () => '2026-07-15T22:30:00.000Z' };
};
"""


def test_jump_behavior_param_auto_vs_smooth():
    # Tab-open jump must be INSTANT (explicit 'instant' — behavior:'auto'
    # defers to the container's CSS scroll-behavior:smooth, which swallowed
    # the scroll in the embedded webview); the header button stays smooth.
    harness = _JUMP_HARNESS_ENV + """
const window = { _calData: { today: '2026-07-16' } };
""" + _JUMP_FNS + """
calJumpToToday('auto');
calJumpToToday();
if (scroller.calls.length !== 2) throw new Error('expected 2 scrolls, got ' + scroller.calls.length);
if (scroller.calls[0].behavior !== 'instant') throw new Error('tab-open jump must be instant, got ' + scroller.calls[0].behavior);
if (scroller.calls[1].behavior !== 'smooth') throw new Error('button jump must stay smooth, got ' + scroller.calls[1].behavior);
console.log('OK');
"""
    _run_node(harness)


def test_jump_scrolls_container_to_real_today_cell():
    # The scroll target is TODAY's cell (server-provided date), centered in
    # #cal-body: top = scrollTop + cellTop - scrollerTop - (246-60)/2.
    harness = _JUMP_HARNESS_ENV + """
const window = { _calData: { today: '2026-07-16' } };
""" + _JUMP_FNS + """
calJumpToToday('auto');
if (queried.indexOf('2026-07-16') < 0) throw new Error('must query the real today cell, queried: ' + queried);
if (queried.indexOf('2026-07-15') >= 0) throw new Error('UTC-string bug: queried yesterday: ' + queried);
const expected = 200 + 1000 - 53 - (246 - 60) / 2;
if (scroller.calls[0].top !== expected) throw new Error('scroll top ' + scroller.calls[0].top + ' != expected ' + expected);
console.log('OK');
"""
    _run_node(harness)


def test_jump_local_fallback_never_uses_utc_date():
    # No server today available (window._calData unset) → the LOCAL date is
    # used, never toISOString(). With the pinned TZ+ after-midnight clock a
    # UTC-keyed selector would hit yesterday's cell.
    harness = _JUMP_HARNESS_ENV + """
const window = {};
""" + _JUMP_FNS + """
calJumpToToday('auto');
if (queried.indexOf('2026-07-16') < 0) throw new Error('must query the LOCAL today, queried: ' + queried);
if (queried.indexOf('2026-07-15') >= 0) throw new Error('UTC-string bug: queried yesterday: ' + queried);
if (scroller.calls.length !== 1) throw new Error('must scroll');
console.log('OK');
"""
    _run_node(harness)


def test_jump_smooth_swallowed_falls_back_to_instant():
    # Embedded webviews swallow SMOOTH programmatic scrolls on the calendar
    # container entirely (verified live: scrollTop never moves). The header
    # button's smooth jump must verify movement and land instant instead.
    harness = _JUMP_HARNESS_ENV + """
const window = { _calData: { today: '2026-07-16' } };
// This scroller IGNORES smooth scrolls (the embedded-webview failure mode).
scroller.scrollTo = function(o) {
  this.calls.push(o);
  if (o.behavior === 'instant') this.scrollTop = o.top;
};
""" + _JUMP_FNS + """
calJumpToToday();  // header button: smooth
setTimeout(() => {
  const expected = 200 + 1000 - 53 - (246 - 60) / 2;
  if (scroller.calls.length !== 2) throw new Error('expected smooth + instant fallback, got ' + JSON.stringify(scroller.calls));
  if (scroller.calls[1].behavior !== 'instant') throw new Error('fallback must be instant');
  if (scroller.scrollTop !== expected) throw new Error('fallback must land on today, at ' + scroller.scrollTop);
  console.log('OK');
}, 400);
"""
    _run_node(harness)


def test_jump_falls_back_to_current_week_row():
    # Today's cell missing from the DOM (e.g. plan gap) → scroll to the
    # is_current week row instead.
    harness = _JUMP_HARNESS_ENV + """
delete cells['2026-07-16'];
const window = { _calData: { today: '2026-07-16',
                             weeks: [{ is_current: false }, { is_current: true }] } };
""" + _JUMP_FNS + """
calJumpToToday('auto');
if (queried.indexOf('week-1') < 0) throw new Error('must fall back to the current week row, queried: ' + queried);
if (scroller.calls.length !== 1) throw new Error('fallback must still scroll the container');
console.log('OK');
"""
    _run_node(harness)


def test_today_string_prefers_server_over_client_clock():
    # Server truth wins even when the client clock disagrees entirely —
    # _calTodayStr is the ONE definition of today for jump + day modal.
    harness = """
const window = { _calData: { today: '2026-07-16' } };
const Date = function() {
  return { toLocaleDateString: () => '2099-01-01',
           toISOString: () => '2099-01-01T00:00:00.000Z' };
};
""" + _extract_js_function(SRC, "_calTodayStr") + """
if (_calTodayStr() !== '2026-07-16') throw new Error('server today must win, got ' + _calTodayStr());
window._calData = null;
if (_calTodayStr() !== '2099-01-01') throw new Error('local fallback must be toLocaleDateString, got ' + _calTodayStr());
console.log('OK');
"""
    _run_node(harness)


def test_no_utc_or_scrollintoview_in_jump_paths():
    # Source pins: the jump helpers and the first-render auto-scroll block
    # must not regress to toISOString (UTC date shift) or scrollIntoView
    # (no-op on the nested CSS-smooth scroller in the embedded webview).
    def _code_only(js: str) -> str:
        return "\n".join(line.split("//")[0] for line in js.splitlines())

    for name in ("_calTodayStr", "calJumpToToday"):
        fn = _code_only(_extract_js_function(SRC, name))
        assert "toISOString" not in fn, f"{name} must not derive today from UTC"
        assert "scrollIntoView" not in fn, f"{name} must scroll the container, not scrollIntoView"
    i = SRC.index("if (!window._calDidAutoScroll")
    block = SRC[i:i + 700]
    assert "_calTodayStr()" in block, "first-render auto-scroll must use the unified today string"
    assert "toISOString" not in _code_only(block), "first-render auto-scroll must not use the UTC date"


def test_plan_tab_open_sequence_jumps_after_load():
    # The plan loader must call the jump AFTER runPlanOpenSequence resolves
    # (rows exist) and BEFORE the rest of the open-tab work.
    m = re.search(
        r"await runPlanOpenSequence\(\);.*?calJumpToToday\('auto'\);.*?loadPlanMetrics\(\);",
        SRC, re.S)
    assert m, "plan-tab open sequence must auto-jump to today after loading"
