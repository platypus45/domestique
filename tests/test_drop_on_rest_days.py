"""Drag-and-drop onto rest days (forum report, 3.12.0).

A rider could not move a workout onto a rest day when life moved their
training, nor drag a mistakenly moved workout back: the day it left becomes a
rest stub, and both plan views painted rest days as invalid drop targets (the
plan grid's rest cells had no drop handler at all). Rest days now take a drop
in both views; days the rider marked unavailable still do not.

Runs the real calDragStart / pgDragStart from dashboard.html under node with a
minimal fake DOM.
"""
import json
import subprocess
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
       ).read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = SRC.index(f"function {name}")
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    raise AssertionError(name)


_DOM = """
function cell(attrs) {
  const cls = new Set();
  return { attrs, getAttribute: k => (k in attrs ? attrs[k] : null),
           classList: { add: c => cls.add(c), remove: c => cls.delete(c), has: c => cls.has(c) },
           cls };
}
global.HTMLElement = Object;
const ev = { dataTransfer: { setData() {}, effectAllowed: '' }, currentTarget: null };
"""


def _run(js: str) -> dict:
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_calendar_rest_day_is_a_drop_target_and_unavailable_is_not():
    js = _DOM + _fn("_calIsoMonday") + "\n" + _fn("calDragStart") + """
const cells = {
  rest: cell({'data-date': '2026-09-24', 'data-cs': 'rest'}),
  unavailable: cell({'data-date': '2026-09-25', 'data-cs': 'future_unavailable'}),
  planned: cell({'data-date': '2026-09-26', 'data-cs': 'planned', 'data-cal-rest': '0',
                 'data-cal-completed': '0', 'data-cal-missing': '0'}),
  ridden_rest: cell({'data-date': '2026-09-27', 'data-cs': 'rest', 'data-cal-rest': '1'}),
  next_week: cell({'data-date': '2026-09-28', 'data-cs': 'rest'}),
};
global.document = { querySelectorAll: () => Object.values(cells) };
global.window = {};
calDragStart(ev, '2026-09-23', 0);
const out = {};
for (const [k, c] of Object.entries(cells)) out[k] = c.cls.has('cal-dropping-valid');
console.log(JSON.stringify(out));
"""
    out = _run(js)
    assert out == {"rest": True, "unavailable": False, "planned": True,
                   "ridden_rest": False, "next_week": False}, out


def test_plan_grid_rest_day_is_a_drop_target_and_unavailable_is_not():
    js = _DOM + _fn("_pgIsoMonday") + "\n" + _fn("pgDragStart") + """
const cells = {
  rest: cell({'data-pg-day': '2026-09-24', 'data-pg-rest': '1', 'data-pg-completed': '0'}),
  unavailable: cell({'data-pg-day': '2026-09-25', 'data-pg-rest': '1', 'data-pg-completed': '0'}),
  ridden_rest: cell({'data-pg-day': '2026-09-26', 'data-pg-rest': '1', 'data-pg-completed': '1'}),
  planned: cell({'data-pg-day': '2026-09-27', 'data-pg-rest': '0', 'data-pg-completed': '0'}),
};
global.document = { querySelectorAll: () => Object.values(cells) };
global.window = { _planData: { availability: { '2026-09-25': { hours: 0, type: 'unavailable' } } } };
pgDragStart(ev, '2026-09-23', 0);
const out = {};
for (const [k, c] of Object.entries(cells)) out[k] = c.cls.has('pg-dropping-valid');
console.log(JSON.stringify(out));
"""
    out = _run(js)
    assert out == {"rest": True, "unavailable": False, "ridden_rest": False,
                   "planned": True}, out


def test_rest_cells_carry_drop_handlers_in_both_views():
    assert "ondrop=\"calDrop(event,'${esc(d.date)}',${wi})\">" in SRC
    assert "ondrop=\"pgDrop(event,'${dayStr}',${wi})\"`" in SRC
