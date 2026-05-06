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
        """The h2 title template literal must interpolate exactly ONE
        duration suffix — the cleaned titleClass + a single `(Nmin)`.
        Regresses if any future refactor reintroduces the duplicated
        `(${actualDur}min)` after a title that already embeds duration.
        """
        body = _open_day_workout_body(_read_dashboard())
        # Locate the heroTitle assignment.
        m = re.search(r"const\s+heroTitle\s*=\s*`([^`]+)`", body)
        self.assertIsNotNone(m, "heroTitle template literal not found")
        tmpl = m.group(1)
        # Count "(...min)" patterns inside the literal — must be exactly 1.
        suffixes = re.findall(r"\(\s*\$\{[^}]+\}\s*min\s*\)", tmpl)
        self.assertEqual(
            len(suffixes), 1,
            f"heroTitle must contain exactly ONE `(${{...}}min)` suffix, "
            f"got {len(suffixes)} in {tmpl!r}",
        )
        # The titleClass token used must be the cleaned variant — i.e.
        # the code must reference titleClass, and titleClass must be
        # built from a string-replace that strips "(Nmin)" / "Nmin".
        self.assertIn("titleClass", tmpl)
        # Verify the strip exists.
        self.assertRegex(
            body,
            r"\.replace\(\s*/\\s\*\\\(\\s\*\\d\+\\s\*min\\s\*\\\)\\s\*/",
            "titleClass must strip embedded `(Nmin)` from display_name/zwo_name",
        )

    def test_duration_mismatch_label_appears_when_gap_over_10pct(self):
        """When workout-file duration is >10% off session-planned
        duration, surface a "Workout file is N min, session planned
        for M min" label so the user knows to pace or extend.
        """
        body = _open_day_workout_body(_read_dashboard())
        # The gap calc must be present.
        self.assertRegex(
            body,
            r"const\s+gapPct\s*=",
            "openDayWorkout must compute a duration-mismatch gap percentage",
        )
        # The 10% threshold must be the trigger.
        self.assertRegex(
            body,
            r"gapPct\s*>\s*0\.10",
            "Mismatch label must fire when gap > 10% of session duration",
        )
        # The label text must mention both durations and a pacing hint.
        self.assertIn("Workout file is", body)
        self.assertIn("session planned for", body)
        self.assertIn("Pace/extend", body)

    def test_v104_title_cascade_not_regressed(self):
        """Pre-existing v1.0.4 contract: title cascade is still
        `display_name || zwo_name || session_type` and zwo_duration_min
        is still consulted (now for the gap label, not the title suffix).
        """
        html = _read_dashboard()
        body = _open_day_workout_body(html)
        # All three cascade sources must still appear in the modal builder.
        self.assertIn("session.display_name", body)
        self.assertIn("session.zwo_name", body)
        self.assertIn("session.session_type", body)
        # Cascade order: display_name OR zwo_name OR session_type.
        cascade_re = re.compile(
            r"session\.display_name[^|]*\|\|[^|]*session\.zwo_name[^|]*\|\|[^|]*session\.session_type",
            re.DOTALL,
        )
        self.assertRegex(
            body, cascade_re,
            "v1.0.4 title cascade `display_name || zwo_name || session_type` "
            "must still be present",
        )
        # zwo_duration_min must still be consumed (now feeds the gap label).
        self.assertIn("session.zwo_duration_min", body)


if __name__ == "__main__":
    unittest.main()
