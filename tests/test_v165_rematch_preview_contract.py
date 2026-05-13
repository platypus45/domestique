"""v1.6.5 — REMATCH workout preview contract.

User asked: after clicking "Rematch workout" the result panel should
show the workout's shape (the same chart `openWorkoutDetail` paints in
the library tab), and hovering a block should reveal time + watts for
that block.

The preview is rendered by `workoutProfileSVG(segments, ftp, totalSec)`
which expects each segment to carry the fields it needs to draw the
bars + per-block `<title>` tooltips. This test pins that contract.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


def _pick_workout_filename() -> str:
    wdir: Path = app_module.WORKOUT_DIR
    files = sorted(wdir.glob("*.zwo"))
    assert files, f"no ZWO files in WORKOUT_DIR ({wdir})"
    return files[0].name


def test_workout_detail_returns_fields_workoutProfileSVG_needs():
    """`workoutProfileSVG(segments, ftp, totalSec)` requires:

      - top-level: ``segments`` (list), ``ftp`` (number), ``total_seconds`` (number).
      - per-segment: ``type``, ``start``, ``duration``, plus type-specific power fields
        used to build the hover ``<title>`` (watts + FTP%).

    Without these the rematch preview can't render and the user's
    hover-tooltip request can't be honoured.
    """
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    r = client.get(f"/api/workout/anycat/{filename}")
    assert r.status_code == 200, r.text
    d = r.json()

    assert "segments" in d
    assert isinstance(d["segments"], list)
    assert len(d["segments"]) > 0, "library workout produced 0 segments"
    assert "ftp" in d
    assert isinstance(d["ftp"], (int, float))
    assert "total_seconds" in d
    assert isinstance(d["total_seconds"], (int, float))
    assert d["total_seconds"] > 0


def test_workout_segments_carry_per_block_tooltip_fields():
    """Each segment type must carry the fields the chart's `<title>`
    tooltip interpolates. SteadyState needs ``power`` + ``pct``; ramp
    types need ``power_low/high`` + ``pct_low/high``; IntervalsT needs
    ``on_power/off_power`` + ``on_pct/off_pct``.
    """
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    r = client.get(f"/api/workout/anycat/{filename}")
    assert r.status_code == 200
    d = r.json()

    seen_types: set[str] = set()
    for s in d["segments"]:
        assert "type" in s
        assert "start" in s
        assert "duration" in s
        t = s["type"]
        seen_types.add(t)
        if t == "SteadyState":
            assert "power" in s and "pct" in s, \
                f"SteadyState missing power/pct: {s}"
        elif t in ("Warmup", "Cooldown", "Ramp"):
            assert "power_low" in s and "power_high" in s, \
                f"{t} missing power_low/high: {s}"
            assert "pct_low" in s and "pct_high" in s
        elif t == "IntervalsT":
            assert "on_power" in s and "off_power" in s
            assert "on_pct" in s and "off_pct" in s
            assert "on_duration" in s and "off_duration" in s
            assert "repeats" in s
        # FreeRide etc. are permitted without power fields — workoutProfileSVG
        # renders them as a gray "FREE RIDE" block.

    assert seen_types, "no segments parsed — workout file may be malformed"


def test_workout_detail_two_arg_route_falls_back_to_flat_layout():
    """The rematch preview calls
    ``/api/workout/<category-best-guess>/<filename>`` because the
    re-draw response sometimes carries Category but `workoutProfileSVG`
    must succeed even when the named category doesn't exist as a
    subdirectory — the route falls back to the flat layout. Without
    this fallback the preview would show "workout file not found" on
    every rematch.
    """
    client = TestClient(app_module.app)
    filename = _pick_workout_filename()

    # Use a deliberately-wrong category — the flat-layout fallback must
    # still find the file under WORKOUT_DIR/<filename>.
    r = client.get(f"/api/workout/__no_such_category__/{filename}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert "segments" in d and len(d["segments"]) > 0


def test_redraw_route_is_registered():
    """Smoke-check that the re-draw POST route is wired into FastAPI.
    The endpoint mutates plan state so we don't exercise it here; this
    test only verifies the route exists in the registered router so a
    future refactor can't quietly remove it and break the rematch UI.
    """
    routes = [getattr(r, "path", "") for r in app_module.app.routes]
    assert "/api/plan/re-draw" in routes, \
        "/api/plan/re-draw missing from app.routes"


def test_dashboard_has_chart_tooltip_wiring():
    """v1.6.5 custom JS tooltip — SVG <title> was unreliable in WKWebView,
    so chart segments now also carry ``data-charttip`` and a delegated
    listener paints a positioned ``#chart-tip`` div instantly on hover.
    Pin the wiring so a future templating change doesn't silently drop
    any of the three pieces (CSS class, host div, listener function).
    """
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    # CSS rule for the tooltip container.
    assert ".chart-tip" in dash, "missing .chart-tip CSS class"
    # Host div in the DOM.
    assert 'id="chart-tip"' in dash, "missing #chart-tip host div"
    # Delegated handler.
    assert "_showChartTip" in dash, "missing _showChartTip handler"
    assert "data-charttip" in dash, "missing data-charttip attribute usage"


def test_workout_chart_renderers_emit_data_charttip():
    """Both chart renderers must annotate every segment with
    ``data-charttip``. Without this the v1.6.5 custom tooltip is dark
    on the corresponding chart even though the JS listener is wired."""
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")

    # workoutProfileSVG: covers SteadyState, ramp, IntervalsT (ON+OFF), FreeRide.
    profile_block = dash[dash.index("function workoutProfileSVG"):dash.index("async function openWorkoutDetail")]
    # At least the SteadyState + ramp + intervals-ON branches must emit data-charttip.
    assert profile_block.count("data-charttip") >= 3, \
        f"workoutProfileSVG emits data-charttip too few times: {profile_block.count('data-charttip')}"

    # renderPowerBlocksSVG: covers ramp + steady/interval rectangle branches.
    blocks_block = dash[dash.index("function renderPowerBlocksSVG"):dash.index("function renderPowerBlocksSVG") + 4000]
    assert blocks_block.count("data-charttip") >= 2, \
        f"renderPowerBlocksSVG emits data-charttip too few times: {blocks_block.count('data-charttip')}"
