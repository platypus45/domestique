"""v1.3.2 BUG fix: session-detail modal showed two different durations.

User screenshot: title read "Wednesday — Tempo (45min) (81min)" because
`display_name`/`zwo_name` already embed the workout-file duration (e.g.
"Tempo 45min — 4×8min @ 91%" or planner-emitted "Tempo (45min)") and
`openDayWorkout` then appended `(${actualDur}min)` on top.

Three regression tests, same static-template style as
`test_ui_v104_title_source.py` (no app fixture):

1. Title interpolation now uses the cleaned `titleClass` plus exactly
   ONE session-duration suffix — no double "(Nmin)" pattern.
2. The mismatch label fires when |zwo_duration_min - session.duration_min|
   exceeds 10% of session.duration_min.
3. The v1.0.4 cascade contract (`display_name → zwo_name → session_type`)
   is still intact — we didn't regress the title source.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
DASHBOARD_HTML = REPO_ROOT / "templates" / "dashboard.html"


def _read_dashboard() -> str:
    with open(DASHBOARD_HTML, encoding="utf-8") as f:
        return f.read()


def _open_day_workout_body(html: str) -> str:
    """Slice from `function openDayWorkout(` to the next top-level
    `function ` so the assertions only see the modal builder."""
    start = html.find("async function openDayWorkout")
    assert start >= 0, "openDayWorkout function not found"
    end = html.find("\nasync function ", start + 1)
    if end < 0:
        end = html.find("\nfunction ", start + 1)
    return html[start: end if end > 0 else len(html)]


class TestV132SessionDurationDisplay(unittest.TestCase):
    """v1.3.2 — single duration in session-detail modal title."""

    def test_title_interpolates_one_duration_only(self):
        """Still ONE duration in the title. v2.2.12 makes it the matched FILE's
        real length (dispDur = fileDur || sessionDur) so the title agrees with the
        Duration stat + the power chart + the downloaded file. heroTitle =
        `${slotLabel} (${dispDur}min)`: exactly one `(Nmin)` suffix, no second
        duration appended (the v1.3.2 double-duration bug stays fixed).
        """
        body = _open_day_workout_body(_read_dashboard())
        m = re.search(r"const\s+heroTitle\s*=\s*`([^`]+)`", body)
        self.assertIsNotNone(m, "heroTitle template literal not found")
        tmpl = m.group(1)
        suffixes = re.findall(r"\(\s*\$\{[^}]+\}\s*min\s*\)", tmpl)
        self.assertEqual(
            len(suffixes), 1,
            f"heroTitle must contain exactly ONE `(${{...}}min)` suffix, "
            f"got {len(suffixes)} in {tmpl!r}",
        )
        # The single suffix is the displayed (file) duration; label is slot-centric.
        self.assertIn("dispDur", tmpl)
        self.assertIn("slotLabel", tmpl)

    def test_duration_mismatch_label_appears_when_gap_over_10pct(self):
        """v2.2.12 — the Duration stat + hero now show the matched FILE's real
        length (dispDur), matching the power chart + the downloaded file (a 45min
        slot matched to a 57min file used to show 45 next to a 57min chart). When
        the file differs from the slot by >10%, the planner's slot target is
        surfaced as a calm 'plan target Nmin' note on the secondary
        'Matched library file' line. gap calc + 10% threshold unchanged.
        """
        body = _open_day_workout_body(_read_dashboard())
        self.assertRegex(body, r"const\s+gapPct\s*=",
                         "openDayWorkout must compute a duration-mismatch gap percentage")
        self.assertRegex(body, r"gapPct\s*>\s*0\.10",
                         "Mismatch note must fire when gap > 10% of slot duration")
        self.assertIn("Matched library file:", body)
        # Duration now reflects the file (dispDur = fileDur || sessionDur).
        self.assertRegex(body, r"const\s+dispDur\s*=\s*fileDur\s*>\s*0\s*\?\s*fileDur\s*:\s*sessionDur",
                         "Duration must show the matched file's real length")
        self.assertRegex(body, r"plan target\s*\$\{sessionDur\}min",
                         "the >10% note must surface the planner's slot target")

    def test_title_is_slot_centric_not_file_cascade(self):
        """SUPERSEDES the v1.0.4 cascade in the MODAL: B1 sources the title from
        the SLOT (CAL_CONTENT_LABEL → CAL_SESSION_LABEL), not display_name. The
        matched file's zwo_name moves to the secondary line; zwo_duration_min is
        still consulted for the gap note. (The calendar-CELL cascade in
        calCardTitle is unchanged — see test_ui_v104_title_source.)
        """
        body = _open_day_workout_body(_read_dashboard())
        self.assertIn("const slotLabel", body)
        self.assertIn("CAL_CONTENT_LABEL[session.content_class]", body)
        self.assertIn("CAL_SESSION_LABEL[session.session_type]", body)
        # The modal must NOT title from the old display_name||zwo_name||type cascade.
        cascade_re = re.compile(
            r"session\.display_name[^|]*\|\|[^|]*session\.zwo_name[^|]*\|\|[^|]*session\.session_type",
            re.DOTALL,
        )
        self.assertNotRegex(
            body, cascade_re,
            "modal must no longer title from the display_name cascade (B1 slot-centric)")
        # zwo_duration_min is still consumed (feeds the gap note).
        self.assertIn("session.zwo_duration_min", body)


if __name__ == "__main__":
    unittest.main()
