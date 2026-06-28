"""v1.0.3 IMPL-UI — minimal smoke tests for the dashboard template
edits made by IMPL-UI-V103.

The dashboard is a static template (rendered on `/`), so the easiest
tests just read templates/dashboard.html and assert on substring
presence. This avoids spinning up the full app fixture for what is
fundamentally a markup-presence check.

Three smoke checks:
  1. Rendered HTML contains all four `planpop-*` IDs (the popover
     blocks for Reforecast / Regenerate / Availability / Rematch).
  2. Rendered HTML contains the `<details class="plan-help-panel">`
     "How your plan updates" panel.
  3. The new `loadMissedSuggestions` JS function name appears in
     the rendered HTML.
"""
from __future__ import annotations

import unittest
from pathlib import Path


HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
DASHBOARD_HTML = REPO_ROOT / "templates" / "dashboard.html"


def _read_dashboard() -> str:
    with open(DASHBOARD_HTML, encoding="utf-8") as f:
        return f.read()


class TestUIv103Smoke(unittest.TestCase):
    """Substring-presence smoke checks for the v1.0.3 Plan-tab help UI."""

    def test_all_four_planpop_ids_present(self):
        """Each of the four locked popover IDs must render in the HTML."""
        html = _read_dashboard()
        for pid in (
            "planpop-reforecast",
            "planpop-regenerate",
            "planpop-availability",
            "planpop-rematch",
        ):
            self.assertIn(
                pid, html,
                f"Expected popover id '{pid}' not found in dashboard.html",
            )

    def test_plan_help_panel_present(self):
        """The collapsed `<details class="plan-help-panel">` panel must render."""
        html = _read_dashboard()
        self.assertIn('<details class="plan-help-panel"', html)
        # Lead phrase from §6 locked copy verifies we wired the right block.
        self.assertIn("How your plan updates", html)

    def test_missed_sessions_auto_rescheduled_no_banner(self):
        """v2.3.0 — the manual "Reschedule missed sessions?" banner + its
        loadMissedSuggestions() loader were removed; missed sessions are now
        rescheduled automatically server-side. Lock the removal so the manual
        prompt can't silently come back."""
        html = _read_dashboard()
        self.assertNotIn("async function loadMissedSuggestions", html)
        self.assertNotIn('id="plan-missed-banner"', html)


if __name__ == "__main__":
    unittest.main()
