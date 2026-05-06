"""v1.3.2 BUG-FIX regression — energy-system breakdown chart on the home
dashboard renders empty.

v1.0.6 introduced the secondary "Energy-system breakdown — beta" <details>
panel containing #energy-system-chart. The render call (energySystemChart())
was wired ONLY into loadFitnessChart(days), which fires on date-range button
clicks. On initial home load, loadHome() paints the primary fitnessChart()
but never invokes the secondary chart, so opening the <details> panel shows
the legend and caption but an empty canvas area.

Fix: call energySystemChart() inline with fitnessChart() inside loadHome()
so the secondary chart is painted on every home view, not just after the
user clicks a date-range button.

This test locks in:
1. The canvas element #energy-system-chart exists in the rendered template.
2. energySystemChart() is invoked from loadHome() AND from loadFitnessChart()
   (so the chart paints both on initial load and on date-range switches).
3. /api/wellness returns the three Banister fitness fields
   (cp_fitness, w_prime_fitness, pmax_fitness) in every record, which the
   chart uses as its data source — even when None (3D model not yet run).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = REPO_ROOT / "templates/dashboard.html"


def test_energy_system_canvas_in_template():
    """The host div for the secondary chart must be present and ID-targetable."""
    html = DASHBOARD_FILE.read_text()
    assert 'id="energy-system-chart"' in html, (
        "missing #energy-system-chart host div — render target removed"
    )


def test_energy_system_chart_called_from_load_home():
    """v1.3.2 fix: the secondary chart must be painted on initial home load,
    not just on date-range button click. This test asserts the call appears
    inside the loadHome() function body, which fires on DOMContentLoaded."""
    html = DASHBOARD_FILE.read_text()
    # Slice from `async function loadHome()` to the next top-level `async function`
    # / `function` boundary, then assert the call is inside.
    start = html.index("async function loadHome()")
    # Use the next async-function declaration as a coarse-but-safe upper bound.
    rest = html[start + len("async function loadHome()"):]
    next_async = rest.find("\nasync function ")
    next_func = rest.find("\nfunction ")
    candidates = [c for c in (next_async, next_func) if c >= 0]
    end_offset = min(candidates) if candidates else len(rest)
    body = rest[:end_offset]
    assert "energySystemChart(" in body, (
        "energySystemChart() not called from loadHome() — chart will be empty "
        "until user clicks a date-range button"
    )


def test_energy_system_chart_still_called_from_load_fitness_chart():
    """Don't regress the original v1.0.6 wiring: the chart should also paint
    when the user switches date ranges via loadFitnessChart()."""
    html = DASHBOARD_FILE.read_text()
    start = html.index("async function loadFitnessChart")
    rest = html[start:]
    # Find function close (next top-level function declaration).
    next_async = rest[1:].find("\nasync function ")
    next_func = rest[1:].find("\nfunction ")
    candidates = [c for c in (next_async, next_func) if c >= 0]
    end_offset = min(candidates) if candidates else len(rest)
    body = rest[:end_offset]
    assert "energySystemChart(" in body, (
        "loadFitnessChart() no longer calls energySystemChart() — date-range "
        "buttons would stop refreshing the secondary chart"
    )


def test_wellness_endpoint_returns_three_fitness_arrays():
    """Data shape contract: /api/wellness must surface cp_fitness,
    w_prime_fitness, pmax_fitness on every record (None is allowed when the
    3D model has not yet computed a value, but the keys must be present so
    energySystemChart() can call .map(d => d.cp_fitness ?? null) safely)."""
    client = TestClient(app)
    res = client.get("/api/wellness?days=90")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    if not data:
        # If a fresh test environment has no wellness yet, skip the per-record
        # contract check — the other tests still lock the JS wiring.
        return
    required_keys = {"cp_fitness", "w_prime_fitness", "pmax_fitness"}
    for rec in data:
        missing = required_keys - set(rec.keys())
        assert not missing, (
            f"record {rec.get('date')} missing fitness keys: {missing} — "
            f"energySystemChart() will read undefined and render empty"
        )
