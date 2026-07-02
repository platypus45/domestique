"""W5 (v2.5.0): hr-mode soft bias in match_zwo.

Contract C8: power mode (hr_bias=False, the default) is bit-identical; hr
mode penalizes RPE-heavy classes only when >=3 guidable alternatives exist;
the bias never mutates library row dicts and never empties a pool.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copy
from datetime import date

import training_planner as tp


def _row(name, cls, score=6, dur=60):
    return {"Name": name, "File": f"{name}.zwo", "Category": "Workout",
            "Duration(min)": dur, "TSS": 60, "IF": 0.8, "Score": score,
            "Protocol": "VO2max", "Z1%": 20, "Z2%": 30, "Z3%": 10, "Z4%": 10,
            "Z5%": 20, "Z6%": 10, "Tags": [], "ContentClass": cls,
            "ContentConfidence": 0.8, "SecondaryFlags": {}}


def _lib():
    # vo2max slot pool: 3 guidable vo2max files + 2 RPE-heavy vo2_short.
    return ([_row(f"vo2_{i}", "vo2max") for i in range(3)]
            + [_row(f"micro_{i}", "vo2_short") for i in range(2)])


def _session():
    return tp.PlannedSession(day="2026-01-05", day_name="Mon",
                             session_type="vo2max", duration_min=60,
                             tss_estimate=70, description="x")


def _match(lib, **kw):
    s = tp.match_zwo(_session(), lib, week_num=1, day_idx=0,
                     plan_start_date=date(2026, 1, 5), **kw)
    return s.zwo_file


def test_power_mode_identical_with_default_kwarg():
    lib = _lib()
    assert _match(copy.deepcopy(lib)) == _match(copy.deepcopy(lib), hr_bias=False)


def test_hr_bias_prefers_guidable_and_never_mutates_rows():
    lib = _lib()
    snapshot = copy.deepcopy(lib)
    picks = {_match(lib, hr_bias=True) for _ in range(3)}  # deterministic seed → same pick
    assert lib == snapshot, "bias mutated shared library rows"
    # With equal base scores and >=3 guidable alternatives, the RPE-heavy
    # micro files are penalized — the pick must be a guidable vo2max file.
    assert all(p.startswith("vo2_") for p in picks), picks


def test_hr_bias_never_empties_pool():
    # ONLY RPE-heavy candidates → no penalty applies (<3 guidable) → slot fills.
    lib = [_row(f"micro_{i}", "vo2_short") for i in range(2)]
    f = _match(lib, hr_bias=True)
    assert f and f.startswith("micro_")
