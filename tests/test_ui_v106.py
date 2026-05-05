"""v1.0.6 IMPL-3D-DASHBOARD — string-presence smoke tests for the dashboard
template edits made by IMPL-3D-DASHBOARD-V106.

The dashboard is a static template (rendered on `/`), so we follow the same
pattern as test_ui_v103 / test_ui_v104_title_source: read templates/dashboard.html
once and assert on substring presence. This avoids the overhead of spinning up
the FastAPI fixture for what is a markup-presence verification.

Four smoke checks (per MASTER_DECISIONS_v106 §5 locked acceptance):
  1. Rendered HTML contains the new collapsed `<details class="energy-system-breakdown">`
     panel below the primary CTL/ATL/TSB chart.
  2. Rendered HTML contains the "Belastingscore" label inside the ride-detail
     modal (secondary card BELOW the primary TSS hero grid).
  3. Rendered HTML contains all four locked verbatim tooltip strings
     (CP fitness / W' fitness / Pmax fitness / Belastingscore).
  4. Regression — TSS still primary. CTL / ATL / TSB / TSS labels remain in
     the rendered HTML and have NOT been removed or hidden by the v1.0.6
     additions. This is the non-negotiable invariant from §0.
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


# Locked tooltip copy — verbatim per MASTER_DECISIONS_v106 §5. Any drift here
# fails the test; the strings are part of the locked Wave-2 contract.
TOOLTIP_CP = (
    "Your aerobic engine. Builds and recovers slowly over a 52-day window; "
    "long Z2 rides move it."
)
TOOLTIP_WP = (
    "Glycolytic capacity above threshold. Recovers in days; built by VO2max "
    "and over-under intervals (5-day window)."
)
TOOLTIP_PMAX = (
    "Sprint and PCr power. Recovers fastest (10-day window); driven by short "
    "all-out efforts under 20 s."
)
TOOLTIP_BELASTING = (
    "Total stress this ride imposed across all three energy systems. Sum of "
    "Aerobe + Glycolytisch + PCr."
)


class TestUIv106Smoke(unittest.TestCase):
    """v1.0.6 IMPL-3D-DASHBOARD substring-presence smoke checks."""

    def test_energy_system_breakdown_details_panel_present(self):
        """The new collapsed `<details class="energy-system-breakdown">` panel
        must render below the primary CTL/ATL/TSB fitness chart.
        """
        html = _read_dashboard()
        self.assertIn(
            '<details class="energy-system-breakdown"', html,
            "Expected `<details class=\"energy-system-breakdown\">` panel "
            "not found in dashboard.html — the secondary 3D fitness panel "
            "is missing.",
        )
        # Default closed: there must NOT be an `open` attribute on this
        # particular details. We do a narrow check (just our class) so an
        # unrelated `<details open>` elsewhere in the template doesn't trip
        # the assertion.
        self.assertNotIn(
            '<details class="energy-system-breakdown" open', html,
            "Energy-system breakdown panel must default closed (no `open` "
            "attribute) so the primary CTL/ATL/TSB chart leads the page.",
        )

    def test_belastingscore_present_in_ride_detail_modal(self):
        """The Belastingscore secondary card label must render inside the
        ride-detail modal (placed BELOW the primary TSS hero grid).
        """
        html = _read_dashboard()
        self.assertIn(
            "Belastingscore", html,
            "Expected 'Belastingscore' label not found in dashboard.html — "
            "the ride-detail secondary card is missing.",
        )
        # Confirm it shows up in the openRideDetail() function body (sanity
        # check that we placed it in the right modal, not just a stray comment).
        ord_start = html.find("async function openRideDetail")
        self.assertGreater(
            ord_start, -1, "openRideDetail() function not found",
        )
        # Search a generous window after the function start (modal body is
        # several thousand chars); we only need to confirm 'Belastingscore'
        # falls within it.
        ord_window = html[ord_start:ord_start + 20000]
        self.assertIn(
            "Belastingscore", ord_window,
            "'Belastingscore' must appear within the openRideDetail() body, "
            "not only in comments outside the modal.",
        )

    def test_all_four_locked_tooltip_strings_present(self):
        """All four §5-locked tooltip strings (CP / W' / Pmax / Belastingscore)
        must appear verbatim in the rendered HTML.
        """
        html = _read_dashboard()
        for label, copy in (
            ("CP fitness", TOOLTIP_CP),
            ("W' fitness", TOOLTIP_WP),
            ("Pmax fitness", TOOLTIP_PMAX),
            ("Belastingscore", TOOLTIP_BELASTING),
        ):
            self.assertIn(
                copy, html,
                f"Locked tooltip copy for '{label}' not found verbatim in "
                f"dashboard.html. The v1.0.6 contract requires the exact "
                f"§5 string. Expected: {copy!r}",
            )

    def test_tss_primary_regression_ctl_atl_tsb_still_prominent(self):
        """REGRESSION GUARD — v1.0.6 ships TSS PRIMARY, 3D ADDITIVE.

        The CTL / ATL / TSB / TSS labels must remain prominently rendered
        in the dashboard HTML. This invariant is non-negotiable per
        MASTER_DECISIONS_v106 §0. If this test fails, the 3D additions
        have demoted or removed the TSS-driven primary view.
        """
        html = _read_dashboard()
        for label in ("CTL", "ATL", "TSB", "TSS"):
            self.assertIn(
                label, html,
                f"REGRESSION: '{label}' label was removed from "
                f"dashboard.html. v1.0.6 must NOT remove or hide TSS-driven "
                f"primary indicators. The 3D model is ADDITIVE only.",
            )
        # Confirm CTL/ATL/TSB still appear in the *primary* fitness card
        # legend (not just buried in a tooltip elsewhere). The legend lives
        # right above the #fitness-chart container.
        legend_anchor = html.find('id="fitness-chart"')
        self.assertGreater(
            legend_anchor, -1, "#fitness-chart container not found",
        )
        # Look in a window of 4 KB BEFORE the chart for the legend dots.
        legend_window = html[max(0, legend_anchor - 4000):legend_anchor]
        for label in ("CTL (Fitness)", "ATL (Fatigue)", "TSB (Form)"):
            self.assertIn(
                label, legend_window,
                f"REGRESSION: '{label}' missing from primary fitness-chart "
                f"legend area. The CTL/ATL/TSB legend MUST remain above "
                f"the chart container.",
            )
        # Confirm TSS still appears in the ride-detail hero grid (one of
        # the four primary tiles).
        modal_anchor = html.find("async function openRideDetail")
        self.assertGreater(modal_anchor, -1)
        # Look at the window containing the hero grid.
        modal_window = html[modal_anchor:modal_anchor + 20000]
        # The TSS tile in the hero grid uses the inline label "TSS".
        self.assertIn(
            ">TSS<", modal_window,
            "REGRESSION: 'TSS' tile missing from the openRideDetail() hero "
            "grid. v1.0.6 must KEEP TSS as a primary tile.",
        )


if __name__ == "__main__":
    unittest.main()
