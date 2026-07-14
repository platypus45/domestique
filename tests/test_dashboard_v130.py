"""v1.3.0 IMPL-DASHBOARD — Power Curve + Fatigue Resistance + PR badges
string-presence smoke tests.

Locks the DOM + JS contracts that the IMPL-DASHBOARD agent ships in
templates/dashboard.html. Endpoint behaviour is locked separately in
tests/test_power_curve.py (POWER-CURVE-CORE), test_fatigue_resistance.py
(FATIGUE-RESISTANCE), and test_pr_detection.py (PR-DETECTION).

Coverage (per /tmp/MASTER_DECISIONS_v130_PATCH.md):
  1. Power Curve panel present — chart container + 3-way y-axis toggle +
     window selector (no filter checkboxes).
  2. Fatigue Resistance panel present — 2-button kJ threshold toggle
     (1500 default + 2000 strict Pinot 2014) + (i) info icon literal.
  3. PR badges section present in openRideDetail() — top-3 cap + tier
     classes (first/major/minor) per PATCH G6 + G7.
  4. window.FEATURES feature-flag object with all 3 keys per PATCH G14.
  5. (i) tooltip popover contains all four reference hyperlinks
     (Coyle 1986 / Pinot 2014 / Mateo-March 2022 / Maturana 2025) per
     PATCH G5.
  6. <script> tag balance + new function names present (per v1.2.1
     PATCH G13 lessons).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _html() -> str:
    return (REPO_ROOT / "templates/dashboard.html").read_text()


def test_power_curve_panel_present():
    html = _html()
    # Card + canvas + meta line
    assert 'id="power-curve-card"' in html
    assert 'id="power-curve-chart"' in html
    assert 'id="power-curve-meta"' in html
    # Three-way y-axis toggle (Watts / W/kg / % FTP)
    assert 'id="pc-yaxis-watts"' in html
    assert 'id="pc-yaxis-wkg"' in html
    assert 'id="pc-yaxis-pct-ftp"' in html
    assert "setPowerCurveYAxis('watts')" in html
    assert "setPowerCurveYAxis('wkg')" in html
    assert "setPowerCurveYAxis('pct_ftp')" in html
    # Five-button window selector 30/90/180/365/All (default 90)
    for w in ("30", "90", "180", "365"):
        assert f'id="pc-win-{w}"' in html
    assert 'id="pc-win-all"' in html
    # No filter checkboxes (user direction "an effort = an effort")
    assert 'id="pc-filter-' not in html
    assert 'pc-filter-checkbox' not in html


def test_fatigue_resistance_panel_present():
    html = _html()
    # 3.4.1 M2 — own always-open card (the <details>/<summary> fold and its
    # ontoggle lazy loader are retired; loadAnalysisTab loads the card).
    assert '<div class="card fatigue-resistance"' in html
    assert '<details class="fatigue-resistance"' not in html
    assert 'id="fatigue-resistance-panel"' in html
    # Two-button kJ threshold toggle (default 1500, strict 2000)
    assert 'id="fr-kj-1500"' in html
    assert 'id="fr-kj-2000"' in html
    assert 'setFatigueResistanceThreshold(1500)' in html
    assert 'setFatigueResistanceThreshold(2000)' in html
    # The literal copy in the active toggle button labels
    assert '&gt; 1500 kJ' in html
    assert '&gt; 2000 kJ' in html
    # (i) info icon
    assert 'id="fatigue-resistance-info-icon"' in html
    assert 'toggleFatigueResistanceInfo' in html
    assert 'id="fatigue-resistance-info-popover"' in html


def test_pr_badges_section_present():
    html = _html()
    # PR badges card sits between Belastingscore card and extended-stats grid
    assert 'class="pr-badges-card"' in html
    assert 'id="pr-badges-card"' in html
    assert 'id="pr-badges-list"' in html
    # PATCH G7 top-3 cap literal
    assert 'TOP_PR_LIMIT = 3' in html
    # PATCH G6 tier classes (first = gold, major = green, minor = blue)
    assert "'first'" in html and "'major'" in html and "'minor'" in html
    # Footer "and N more — click to expand"
    assert 'click to expand' in html
    # Ordering inside openRideDetail: belastingscore-card BEFORE pr-badges-card
    # BEFORE the extended-stats grid comment.
    assert html.index('belastingscore-card') < html.index('pr-badges-card')
    assert html.index('pr-badges-card') < html.index('Extended stats grid')


def test_feature_flags_present():
    html = _html()
    # PATCH G14 — window.FEATURES gate with three keys
    assert 'window.FEATURES' in html
    assert 'power_curve' in html
    assert 'fatigue_resistance' in html
    assert 'pr_detection' in html
    # Each panel checks via typeof loadX === 'function' probe
    assert "typeof loadPowerCurve === 'function'" in html
    assert "typeof loadFatigueResistance === 'function'" in html
    assert "typeof loadPRBadges === 'function'" in html


def test_info_tooltip_has_all_four_references():
    html = _html()
    # PATCH G5 — four literature anchors in the (i) popover
    assert 'pubmed.ncbi.nlm.nih.gov/3536834' in html      # Coyle 1986
    assert 'fredericgrappe.com' in html                    # Pinot & Grappe 2014
    assert 'journals.humankinetics.com' in html            # Mateo-March 2022
    assert 'pmc.ncbi.nlm.nih.gov/articles/PMC12174182' in html  # Maturana 2025
    # All four labelled as expected
    assert 'Coyle 1986' in html
    assert 'Pinot' in html and 'Grappe 2014' in html
    assert 'Mateo-March' in html
    assert 'Maturana' in html
    # Both threshold sections in the popover
    assert 'THRESHOLD: &gt; 1500 kJ' in html
    assert 'THRESHOLD: &gt; 2000 kJ' in html


def test_no_unbalanced_script_tags():
    html = _html()
    assert html.count("<script") == html.count("</script>"), "unbalanced <script> tags"
    # All v1.3.0 functions present
    assert 'function loadPowerCurve' in html
    assert 'function setPowerCurveWindow' in html
    assert 'function setPowerCurveYAxis' in html
    assert 'function renderPowerCurve' in html
    assert 'function loadFatigueResistance' in html
    assert 'function setFatigueResistanceThreshold' in html
    assert 'function toggleFatigueResistanceInfo' in html
    assert 'function renderFatigueResistance' in html
    assert 'function loadPRBadges' in html
