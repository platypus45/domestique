"""Programme summary must never open blank — the three no-data states.

Linux tester, fresh install: "programme summary is blank". Measured: with no
plan the modal wrote one line into #ps-window and returned, leaving four empty
metric grids and six empty chart frames; with a plan but no rides yet every
tile read "no data" over blank axes and the PNG printed "NoneW -> NoneW". Each
state now states what is missing and when it fills in.

Node-harness style follows tests/test_day_modal_file_tss.py: run the REAL
_psRender out of templates/dashboard.html against a stub DOM.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from PIL import ImageDraw

from programme_summary_png import render_programme_summary_png

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _extract_js_line(src: str, needle: str) -> str:
    for line in src.splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"line not found: {needle}")


_DOM_STUB = """
const _els = {};
function _el(id) {
  if (!_els[id]) _els[id] = {
    id, textContent: '', innerHTML: '', style: {display: ''},
    parentElement: {}, replaceWith(){}, getContext(){ return {}; },
  };
  return _els[id];
}
const document = {
  getElementById: _el,
  createElement: () => ({style: {cssText: ''}, textContent: ''}),
};
const window = {};
"""


def _render(summary: dict) -> dict:
    """Run the real _psRender over a stub DOM; return the resulting state."""
    harness = (
        _DOM_STUB
        + _extract_js_line(SRC, "const _PS_BLOCKS") + "\n"
        + _extract_js_function(SRC, "_psDestroyCharts") + "\n"
        + _extract_js_function(SRC, "_psShowEmpty") + "\n"
        + _extract_js_function(SRC, "_psHideEmpty") + "\n"
        + _extract_js_function(SRC, "_psFmtDelta") + "\n"
        + _extract_js_function(SRC, "_psRender") + "\n"
        + f"_psRender({json.dumps(summary)});\n"
        + "console.log(JSON.stringify({"
          "empty: _el('ps-empty').textContent,"
          "emptyDisplay: _el('ps-empty').style.display,"
          "window: _el('ps-window').textContent,"
          "kpiHtml: _el('ps-kpi-row').innerHTML,"
          "kpiDisplay: _el('ps-kpi-row').style.display,"
          "chartsDisplay: _el('ps-charts').style.display,"
          "totalsDisplay: _el('ps-totals').style.display,"
          "pngDisplay: _el('ps-png-link').style.display}));\n"
    )
    res = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}"
    return json.loads(res.stdout)


def _summary(rides: int) -> dict:
    return {
        "plan_id": "current", "start_date": "2026-05-18",
        "end_date": "2026-08-09", "weeks": 12, "rides": rides,
        "ftp_delta": {"start": None, "end": None, "pct": None},
        "eftp_delta": {"start": None, "end": None, "pct": None},
        "vo2max_delta": {"start": None, "end": None, "pct": None},
        "ctl_gain": {"start": None, "end": None, "delta": None},
        "intensity_dist": {"z1z2_min": 0, "z3_min": 0, "z4plus_min": 0},
        "pol_index": {"mean": None, "class": "—"},
        "monotony_max": None, "strain_max": None, "compliance": [],
        "mean_max_curve": {"start": [], "end": []}, "hooper_trend": [],
        "totals": {"km": 0, "hours": 0.0, "kj": 0, "elev_m": 0},
        "decoupling_trend": [], "citations": ["Foster 1998 (MSSE)"],
    }


def test_plan_with_no_rides_explains_itself():
    """Zero rides: a plain sentence, and no empty metric grid behind it."""
    out = _render(_summary(rides=0))
    assert "No rides recorded" in out["empty"]
    assert out["emptyDisplay"] == "block"
    # The plan window is still stated — it is measured, not invented.
    assert "2026-05-18" in out["window"] and "12 weeks" in out["window"]
    # Nothing renders as a zero the rider could mistake for a measurement.
    assert out["kpiHtml"] == ""
    for key in ("kpiDisplay", "chartsDisplay", "totalsDisplay", "pngDisplay"):
        assert out[key] == "none", key


def test_plan_with_no_weeks_explains_itself():
    """The minimal dict (plan file with no weeks) must not render blank."""
    s = _summary(rides=0)
    s.update({"weeks": 0, "start_date": None, "end_date": None})
    out = _render(s)
    assert "no weeks" in out["empty"]
    assert out["chartsDisplay"] == "none"


def test_ridden_plan_still_renders_metrics():
    """Regression: with rides the grids come back — display is restored to
    'grid' explicitly, since clearing it would drop grid-template-columns."""
    out = _render(_summary(rides=4))
    assert out["empty"] == ""
    assert out["emptyDisplay"] == "none"
    assert "FTP" in out["kpiHtml"]
    assert out["kpiDisplay"] == "grid"
    assert out["chartsDisplay"] == "grid"
    assert out["totalsDisplay"] == "grid"


def test_no_plan_and_load_failure_reach_the_empty_state():
    """The 404 and the network-failure branches must both say something."""
    fn = _extract_js_function(SRC, "showProgrammeSummary")
    assert "_psShowEmpty" in fn
    assert "No training plan yet" in fn
    assert "could not be loaded" in fn


def test_modal_is_hoisted_out_of_the_plan_section():
    """#programme-summary-modal ships inside #sec-plan (display:none unless
    the Plan tab is open), so the end-of-plan auto-open laid out a 0x0 box."""
    fn = _extract_js_function(SRC, "showProgrammeSummary")
    assert "document.body.appendChild(overlay)" in fn


def test_png_never_prints_none():
    """An unridden plan printed "NoneW -> NoneW": the delta dicts carry the
    keys with None values, so dict.get(k, '—') never fired."""
    drawn: list[str] = []
    orig = ImageDraw.ImageDraw.text

    def _spy(self, xy, text="", *a, **kw):
        drawn.append(str(text))
        return orig(self, xy, text, *a, **kw)

    with patch.object(ImageDraw.ImageDraw, "text", _spy):
        render_programme_summary_png(_summary(rides=0))
    assert not [t for t in drawn if "None" in t], \
        f"raw None leaked into the PNG: {[t for t in drawn if 'None' in t]}"
