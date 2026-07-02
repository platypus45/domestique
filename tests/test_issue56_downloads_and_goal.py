"""Issues #5 (empty ZWO/FIT downloads) + #6 (goal reverts to Event).

#5 — On macOS the pywebview bridge fetch() can resolve with an EMPTY body
(WKWebView download interception), so the native save wrote 0-byte files. The
launcher runs the FastAPI app in-process, so JsApi now produces the bytes
SERVER-SIDE when the JS body is empty, and refuses to write 0 bytes. Plus the
FIT builder fails loudly (422) on a ZWO that yields no steps instead of emitting
a useless header-only file.

#6 — populatePlanFormFromGoal restored ~12 fields but never the goal selector,
so it snapped back to the hardcoded <option value="event"> after restart.
"""
import glob
import os
import re
import unittest
from pathlib import Path

import app
import launcher

REPO = Path(__file__).resolve().parent.parent
DASH = REPO / "templates" / "dashboard.html"


def _a_library_zwo() -> str:
    return os.path.basename(sorted(glob.glob(str(REPO / "workouts" / "*.zwo")))[0])


class TestFitBytesGuards(unittest.TestCase):
    def test_generic_fit_has_bytes(self):
        self.assertGreater(len(app.build_fit_workout_bytes("vo2max", 60, "t")), 100)

    def test_from_zwo_has_bytes(self):
        self.assertGreater(
            len(app.build_fit_workout_bytes("z2", 60, "t", _a_library_zwo())), 100)

    def test_missing_zwo_raises(self):
        with self.assertRaises(FileNotFoundError):
            app.build_fit_workout_bytes("z2", 60, "t", "does_not_exist_xyz.zwo")

    def test_zero_step_zwo_raises_not_empty_file(self):
        tmp = Path(app.WORKOUT_DIR) / "_t_issue5_empty.zwo"
        tmp.write_text("<workout_file><workout></workout></workout_file>")
        try:
            with self.assertRaises(ValueError):
                app.build_fit_workout_bytes("z2", 60, "t", tmp.name)
        finally:
            tmp.unlink(missing_ok=True)


class TestJsApiServerSideFallback(unittest.TestCase):
    """The core #5 fix: empty JS body → real bytes produced in-process."""

    def setUp(self):
        self.api = launcher.JsApi()

    def test_zwo_read_serverside_when_js_empty(self):
        data = self.api._read_zwo_serverside(_a_library_zwo())
        self.assertGreater(len(data), 10)

    def test_fit_build_serverside_when_js_empty(self):
        data = self.api._build_fit_serverside("vo2max", 60, "t", None)
        self.assertGreater(len(data), 100)

    def test_save_refuses_empty_payload(self):
        # Must NOT pop a dialog / write a 0-byte file.
        res = self.api._save("x.zwo", b"", ())
        self.assertFalse(res["ok"])

    def test_save_fit_malformed_b64_without_params_still_errors(self):
        res = self.api.save_fit("w.fit", "###not-b64###")
        self.assertFalse(res["ok"])
        self.assertIn("base64", res["error"].lower())


class TestGoalSelectorRestore(unittest.TestCase):
    """#6 — populatePlanFormFromGoal must restore the goal selector itself,
    BEFORE plan-edate/plan-weeks (togglePlanFields would otherwise clobber the
    restored weeks for event goals)."""

    def test_restores_plan_goal_before_edate(self):
        src = DASH.read_text(encoding="utf-8")
        i = src.find("function populatePlanFormFromGoal")
        self.assertGreater(i, 0)
        # Bound the search to the FUNCTION BODY (up to the next top-level
        # function) — a fixed char slice went stale as the function grew
        # (template/intensity-model restore landed between goal and edate).
        j = src.find("\nfunction ", i + 1)
        body = src[i:j if j > i else i + 8000]
        g = body.find("setVal('plan-goal'")
        e = body.find("setVal('plan-edate'")
        self.assertGreater(g, 0, "goal selector never restored (issue #6 regressed)")
        self.assertGreater(e, 0)
        self.assertLess(g, e, "plan-goal must be restored before plan-edate")

    def test_goal_option_values_match_planner_enum(self):
        """Setting the selector only works if the <option> values equal the
        stored goal_type strings."""
        src = DASH.read_text(encoding="utf-8")
        sel = src[src.find('id="plan-goal"'):]
        sel = sel[:sel.find("</select>")]
        opts = set(re.findall(r'<option value="([^"]+)"', sel))
        for gt in ("general", "event", "ftp", "vo2max", "ftp_vo2max", "ctl"):
            self.assertIn(gt, opts, f"missing goal option {gt}")


if __name__ == "__main__":
    unittest.main()
