"""Every calendar cell the server sends opens in the dashboard.

The calendar's day click (calOpenDay) refuses a cell whose content_class its
own table does not accept for the cell's session_type, and the Today card goes
through the same click. When the cell began carrying the library row's
ContentClass (2026-09-14, step 2 of the week-view programme), 624 of the
library's 4,307 files stopped opening: tempo_intervals, endurance_intervals,
threshold_ladder, vo2_ladder and sweet_spot_ladder are not in that table. The
adversarial review found it by clicking; no test served the finer classes.

Driven end to end: one stored session per content class in the library, each
under the planner's own label and one under a mismatched label (the owner's
sweetspot slot on a threshold file), through /api/calendar, then the
dashboard's own calContentMatches and calContentCss under node."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import cache
import clock
import training_planner as tp

TODAY = date(2026, 9, 10)
DASHBOARD = Path(app_module.__file__).parent / "templates" / "dashboard.html"


def _extract_js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def _extract_const(src: str, name: str) -> str:
    m = re.search(rf"const {name} = \{{.*?\n\}};", src, flags=re.S)
    assert m, name
    return m.group(0)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class CalendarCellsOpen(unittest.TestCase):
    def setUp(self):
        clock.freeze(TODAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="cal_cells_"))
        self._patches = [patch.object(app_module, "_plan_dir", return_value=self.tmp)]
        for p in self._patches:
            p.start()
        cache.clear_cache()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        clock.unfreeze()
        cache.clear_cache()

    def _plan(self):
        by_class = {}
        for row in tp.load_workout_library():
            cc = (row.get("ContentClass") or "").strip().lower()
            if cc and cc not in by_class and row.get("File"):
                by_class[cc] = row
        rows = [by_class[c] for c in sorted(by_class)]
        self.assertGreater(len(rows), 10)
        endurance = by_class["endurance"]
        sessions = []
        day = TODAY
        for label, row in ([(tp._session_type_from_row(r), r) for r in rows]
                           + [("sweetspot", by_class["threshold"]), ("long_z2", endurance)]):
            sessions.append({
                "day": day.isoformat(), "day_name": day.strftime("%a"),
                "session_type": label, "duration_min": 60, "tss_estimate": 50,
                "description": "", "zwo_file": row["File"], "zwo_name": row["File"],
                "status": "pending"})
            day += timedelta(days=1)
        monday = TODAY - timedelta(days=TODAY.weekday())
        weeks = []
        while monday <= day:
            end = monday + timedelta(days=6)
            weeks.append({"week_num": len(weeks) + 1, "start": monday.isoformat(),
                          "end": end.isoformat(), "phase": "base", "tss_target": 300.0,
                          "is_stepback": False,
                          "sessions": [s for s in sessions
                                       if monday.isoformat() <= s["day"] <= end.isoformat()]})
            monday = end + timedelta(days=1)
        return {"goal": {"type": "general"}, "phases": [], "weeks": weeks,
                "generated": f"{TODAY.isoformat()}T09:00:00"}, len(sessions)

    def test_every_served_cell_opens(self):
        plan, n = self._plan()
        (self.tmp / "current_plan.json").write_text(json.dumps(plan))
        cal = TestClient(app_module.app).get("/api/calendar").json()
        cells = [d["planned"] for w in cal["weeks"] for d in w["days"]
                 if d.get("planned") and d["planned"].get("zwo_file")]
        self.assertEqual(len(cells), n)
        src = DASHBOARD.read_text(encoding="utf-8")
        harness = "\n".join([
            _extract_const(src, "CAL_SESSION_CSS"),
            _extract_js_function(src, "calContentMatches"),
            _extract_js_function(src, "calContentCss"),
            f"const cells = {json.dumps(cells)};",
            """
const refused = cells.filter(c => !calContentMatches(c)).map(c => c.zwo_file + ' as ' + c.session_type + '/' + c.content_class);
if (refused.length) { console.error('refused: ' + refused.join(', ')); process.exit(1); }
const long = cells.filter(c => c.session_type === 'long_z2');
if (long.length !== 1 || calContentCss(long[0].content_class, long[0].session_type) !== 'wc-long') {
  console.error('the long ride lost its colour: ' + JSON.stringify(long)); process.exit(1);
}
console.log('OK ' + cells.length);
"""])
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
