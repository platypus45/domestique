"""3.4.4 — Fitness & Form hover alignment (tester report).

The FF svg letterboxes (preserveAspectRatio="meet" + width:100% + fixed px
height), so screen→viewBox mapping must go through getScreenCTM — the old
linear clientX/rect.width×W map skewed the crosshair off the plot at both
ends. Pins: the handler uses the CTM inverse, keeps the linear fallback,
and the letterboxed arithmetic itself.
"""
import re
import subprocess
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "src" / "templates" / "dashboard.html"
        ).read_text(encoding="utf-8")


def _fitness_chart_src() -> str:
    i = _SRC.index("function fitnessChart(")
    j = _SRC.index("function metricColor(")
    return _SRC[i:j]


def test_ff_hover_maps_through_screen_ctm():
    fn = _fitness_chart_src()
    assert "getScreenCTM" in fn, "hover must use the CTM screen→viewBox transform"
    assert "(e.clientX - ctm.e) / ctm.a" in fn, "CTM inverse arithmetic missing"
    # Linear fallback stays for detached-svg edge (and ONLY as fallback).
    assert "rect.width) * W" in fn


def test_ctm_inverse_arithmetic_letterboxed():
    """Node-proof: with a letterboxed CTM (scale a, offset e), the inverse
    recovers exact viewBox X — cursor on the plot's left padding edge maps
    to PAD_L, center to center, right edge to W-PAD_R."""
    harness = """
const W = 900, PAD_L = 44, PAD_R = 60;
// Letterboxed render: element 1200px wide, 320px tall → scale = 320/320? no:
// meet → scale = min(1200/900, 320/320) = 0.355... use rendered height 320:
// scale a = 320/320 = 1? Take a realistic case: element 700x320 →
// a = min(700/900, 320/320) = 0.7778, content width = 700? no: 900*a = 700 →
// no gutters horizontally... letterbox happens when width/height ratio
// EXCEEDS 900/320: element 1400x320 → a = 1.0, content 900px, gutter
// (1400-900)/2 = 250 → ctm.e = elementLeft + 250.
const a = 1.0, elementLeft = 10, gutter = 250;
const e_off = elementLeft + gutter;
function inv(clientX) { return (clientX - e_off) / a; }
// Cursor exactly at the plot's visual left edge (gutter + PAD_L px from element left):
if (Math.abs(inv(elementLeft + gutter + PAD_L * a) - PAD_L) > 1e-9) throw new Error('left edge');
const mid = PAD_L + (W - PAD_L - PAD_R) / 2;
if (Math.abs(inv(elementLeft + gutter + mid * a) - mid) > 1e-9) throw new Error('center');
if (Math.abs(inv(elementLeft + gutter + (W - PAD_R) * a) - (W - PAD_R)) > 1e-9) throw new Error('right edge');
// The OLD linear map on the same geometry misses by the gutter — pin the
// failure mode so nobody "simplifies" back to it: element width 1400.
const oldMap = (clientX) => ((clientX - elementLeft) / 1400) * W;
const oldAtPlotLeft = oldMap(elementLeft + gutter + PAD_L * a);
if (Math.abs(oldAtPlotLeft - PAD_L) < 50) throw new Error('old map should be badly off (was the bug)');
console.log('OK');
"""
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout
