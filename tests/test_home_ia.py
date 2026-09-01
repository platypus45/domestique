"""P3.1 home information-architecture pass — structural contracts.

Plain file-content checks against templates/dashboard.html (same pattern as
test_dashboard_v130.py / test_ui_v104_title_source.py — no app fixture).

Locked scope (IP_ROADMAP_POST250 RE-GRILL G12): six diagnostics panels moved
VERBATIM from the home section into a new "Analysis" tab; element ids
unchanged (document-global). The boot path no longer fetches/renders the
moved panels — the Analysis tab's lazy loader owns them on tab open (the
power-curve Chart.js canvas must not render inside a display:none section).
Home keeps a NEW compact CTL/TSB sparkline fed by loadHome()'s existing
wellness fetch. DFA gets no third location: home's compact snapshot stays on
home, the dedicated sec-dfa tab is untouched.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "src" / "templates" / "dashboard.html"

# The six moved panels (G12 move list). Sub-ids of the power-curve card and
# the fitness card are asserted separately below.
MOVED_PANEL_IDS = [
    'id="fitness-chart"',
    'id="energy-system-chart"',
    'id="tau-fit-results-panel"',
    'id="banister-oos-panel"',
    'id="power-curve-card"',
    'id="fatigue-resistance-panel"',
]

# Power-curve card sub-ids + backfill toast machinery + fitness-card range
# buttons — all travel with their parent cards.
MOVED_SUB_IDS = [
    'id="power-curve-chart"',
    'id="power-curve-chart-container"',
    'id="power-curve-meta"',
    'id="power-curve-help"',
    'id="power-curve-backfill-toast"',
    'id="power-curve-backfill-msg"',
    'id="power-curve-backfill-progress"',
    'id="power-curve-backfill-bar"',
    'id="pc-yaxis-watts"',
    'id="pc-yaxis-wkg"',
    'id="pc-yaxis-pct-ftp"',
    'id="pc-win-30"',
    'id="pc-win-90"',
    'id="pc-win-180"',
    'id="pc-win-365"',
    'id="pc-win-all"',
    'id="fatigue-resistance-content"',
    'id="fatigue-resistance-body"',
    'id="fr-kj-1500"',
    'id="fr-kj-2000"',
    'id="banister-oos-content"',
    'id="tau-fit-results-content"',
    'id="dr-30"',
    'id="dr-90"',
    'id="dr-180"',
    'id="dr-365"',
    'id="dr-0"',
]


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _no_comments(html: str) -> str:
    """Strip HTML comments — historical comments in sec-home mention the
    moved id strings (e.g. the v1.8.8 legacy power-curve-chart note) and
    must not trip element-location assertions."""
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def _slice(html: str, start_marker: str, end_marker: str) -> str:
    i0 = html.index(start_marker)
    i1 = html.index(end_marker)
    assert i0 < i1, f"{start_marker!r} does not precede {end_marker!r}"
    return html[i0:i1]


def _code_only(js: str) -> str:
    """Drop full-line // comments so call-site assertions don't trip on
    explanatory comments that name the relocated functions."""
    return "\n".join(
        ln for ln in js.splitlines() if not ln.lstrip().startswith("//")
    )


def _sec_home(html: str) -> str:
    return _slice(html, 'id="sec-home"', 'id="sec-picker"')


def _sec_analysis(html: str) -> str:
    return _slice(html, 'id="sec-analysis"', 'id="sec-dfa"')


# ── 1. sec-analysis exists and holds all six moved panels (+ sub-ids) ──────

def test_sec_analysis_exists_with_all_moved_panels():
    html = _no_comments(_html())
    assert '<div class="section" id="sec-analysis">' in html
    analysis = _sec_analysis(html)
    for pid in MOVED_PANEL_IDS + MOVED_SUB_IDS:
        assert pid in analysis, f"{pid} not inside sec-analysis"


# ── 2. sec-home no longer contains the moved panels ────────────────────────

def test_sec_home_no_longer_contains_moved_panels():
    html = _no_comments(_html())
    home = _sec_home(html)
    for pid in MOVED_PANEL_IDS + MOVED_SUB_IDS:
        assert pid not in home, f"{pid} still inside sec-home"


# ── 3. tab bar + tab-loader map register the analysis tab ──────────────────

def test_analysis_tab_registered():
    html = _html()
    assert '<div class="tab" data-tab="analysis">Analysis</div>' in html
    # Loader map entry follows the dfa:()=>loadDfaTab() pattern.
    assert (
        "analysis:()=>{ if (typeof loadAnalysisTab === 'function') "
        "loadAnalysisTab(); }"
    ) in html
    assert "function loadAnalysisTab()" in html
    # The tab loader owns both chart loads on tab open.
    body = _slice(html, "function loadAnalysisTab()",
                  "function renderHomeFitnessSparkline")
    assert "loadFitnessChart(" in body
    assert "loadPowerCurve(" in body


# ── 4. DFA disposition: compact snapshot stays on home, no third location ──

def test_home_snapshot_dfa_still_on_home_and_not_in_analysis():
    html = _no_comments(_html())
    assert 'id="home-snapshot-dfa"' in _sec_home(html)
    analysis = _sec_analysis(html)
    for dfa_el in ('id="home-snapshot-dfa"', 'id="dfa-aggregate-host"',
                   'id="dfa-perride-host"', 'id="dfa-tab-status"'):
        assert dfa_el not in analysis, f"{dfa_el} leaked into sec-analysis"
    # Dedicated DFA tab untouched.
    assert '<div class="section" id="sec-dfa">' in html


# ── 5. Boot path no longer fetches/renders the moved panels ────────────────

def test_load_power_curve_not_chained_in_wellness_then_block():
    """The /api/wellness then-block inside loadFitnessChart used to chain
    loadPowerCurve() (one round-trip on Home open). Post-P3.1 the Analysis
    tab loader owns that call."""
    html = _html()
    body = _code_only(_slice(html, "async function loadFitnessChart",
                             "function loadAnalysisTab()"))
    assert "loadPowerCurve(" not in body, (
        "loadPowerCurve is still chained inside loadFitnessChart's wellness "
        "then-block — the Analysis tab loader owns it now"
    )


def test_load_home_boot_path_split():
    html = _html()
    body = _code_only(_slice(html, "async function loadHome()",
                             "async function loadReadinessComposite"))
    assert "loadPowerCurve" not in body, "loadHome still boots the power curve"
    assert "fitnessChart(wellness" not in body, (
        "loadHome still renders the full fitness chart at boot"
    )
    assert "energySystemChart(wellness" not in body, (
        "loadHome still renders the energy-system chart at boot"
    )
    # The wellness payload loadHome fetches now feeds the compact sparkline
    # (and _wellnessCache semantics stay intact for its other consumers).
    assert "renderHomeFitnessSparkline(wellness)" in body
    assert "_wellnessCache = wellness" in body


# ── 6. Home sparkline card present, click routes to the Analysis tab ───────

def test_home_fitness_sparkline_card():
    html = _no_comments(_html())
    home = _sec_home(html)
    assert 'id="home-fitness-sparkline-card"' in home
    assert 'id="home-fitness-sparkline"' in home
    assert 'id="home-fitness-sparkline-stats"' in home
    card = home[home.index('id="home-fitness-sparkline-card"'):]
    assert "data-tab=analysis" in card.split(">", 1)[0].replace('"', ""), (
        "sparkline card click must open the Analysis tab"
    )
    assert "function renderHomeFitnessSparkline" in html


# ── 7. No duplicate element ids introduced by the move ─────────────────────

def test_moved_ids_appear_exactly_once():
    html = _no_comments(_html())
    for pid in MOVED_PANEL_IDS + MOVED_SUB_IDS:
        n = html.count(pid)
        assert n == 1, f"{pid} appears {n}x — the move must not duplicate ids"


def test_static_markup_has_no_duplicate_ids():
    """Whole-document audit: every id= attribute in the static markup
    (scripts excluded — JS builds per-render fragments) is unique."""
    html = _no_comments(_html())
    static = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'id="([^"]+)"', static)
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"duplicate static element ids: {dups}"
