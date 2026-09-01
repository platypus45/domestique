"""v1.3.1 BLOCKER hot-fix regression — Chart.js must be vendored and loaded
before any inline `new Chart(...)` invocation in dashboard.html.

v1.3.0 shipped the Power Curve, Fatigue Resistance scatter, and 6
phase-summary charts but forgot to add a `<script src="...chart...">` tag,
so all of them threw "Chart is not defined" at runtime. This test locks
the fix in.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_FILE = REPO_ROOT / "src/static/vendor/chart.umd.min.js"
DASHBOARD_FILE = REPO_ROOT / "src/templates/dashboard.html"


def test_chart_js_vendored_and_non_empty():
    assert VENDOR_FILE.exists(), f"missing vendored Chart.js bundle at {VENDOR_FILE}"
    size = VENDOR_FILE.stat().st_size
    # Chart.js 4.x UMD min bundle is ~200 KB; sanity-check it's not a stub.
    assert size > 50_000, f"Chart.js bundle suspiciously small: {size} bytes"


def test_dashboard_has_exactly_one_chart_script_tag():
    html = DASHBOARD_FILE.read_text()
    # Count the script tag occurrences for chart.umd.
    matches = [
        line for line in html.splitlines()
        if "<script" in line and "chart.umd" in line
    ]
    assert len(matches) == 1, (
        f"expected exactly 1 Chart.js script tag, got {len(matches)}: {matches!r}"
    )


def test_chart_script_tag_appears_before_first_new_chart_call():
    """Script must be loaded before any inline `new Chart(...)` invocation,
    otherwise the global is undefined when the inline JS runs."""
    lines = DASHBOARD_FILE.read_text().splitlines()
    script_line_no = None
    new_chart_line_nos = []
    for i, line in enumerate(lines, start=1):
        if "<script" in line and "chart.umd" in line:
            script_line_no = i
        if "new Chart(" in line:
            new_chart_line_nos.append(i)
    assert script_line_no is not None, "no Chart.js <script> tag found"
    assert new_chart_line_nos, "no `new Chart(` invocations found in dashboard.html"
    assert script_line_no < min(new_chart_line_nos), (
        f"Chart.js <script> at line {script_line_no} but earliest "
        f"`new Chart(` at line {min(new_chart_line_nos)} — script must come first"
    )
