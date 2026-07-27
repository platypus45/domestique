"""The PRODUCTION path into block grading, and the rating hydration.

Everything else about block grading is tested by calling `score_blocks`
directly. The only path a rider's ride actually travels — matched session →
`ride["intervals"]` → the workout file on disk → `ftp_at_ride` → the graded
result on the execution payload — had no test at all, including its blanket
`except Exception: return None`, which turns any drift in lap shape into a
silently missing feature rather than a visible failure.

Also covered here: the two rating controls reading back what is stored. A value
in the database that the form re-renders blank is indistinguishable, to the
rider, from a value that was never saved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app as app_module
import structure_fidelity as sf

FTP = 250

_ZWO = """<workout_file><workout>
  <Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>
  <IntervalsT Repeat="6" OnDuration="60" OffDuration="120" OnPower="1.25" OffPower="0.50"/>
  <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
</workout></workout_file>"""


def _laps(work_durations):
    """A tiling lap list carrying the ride clock, as intervals.icu returns."""
    rows = [("RECOVERY", 600, 60.0)]
    for d in work_durations:
        rows.append(("RECOVERY", 60, 50.0) if d is None
                    else ("WORK", d, 125.0))
        rows.append(("RECOVERY", 120, 50.0))
    rows.append(("RECOVERY", 300, 55.0))
    out, t = [], 0
    for kind, dur, pct in rows:
        out.append({"type": kind, "duration_s": dur, "ftp_pct": pct,
                    "start_s": t, "avg_power_w": int(FTP * pct / 100)})
        t += dur
    return out


@pytest.fixture()
def workout_on_disk(tmp_path, monkeypatch):
    (tmp_path / "vo2max_6x1min_test.zwo").write_text(_ZWO, encoding="utf-8")
    monkeypatch.setattr(app_module, "WORKOUT_DIR", tmp_path)
    return "vo2max_6x1min_test.zwo"


# ── the production path ──────────────────────────────────────────────────────

def test_it_grades_a_matched_session_end_to_end(workout_on_disk):
    out = app_module._block_eval_for(
        {"zwo_file": workout_on_disk},
        {"intervals": _laps([60] * 6), "ftp_at_ride": FTP})
    assert out is not None
    assert out["outcome"] == "completed"
    assert (out["reps_done"], out["reps_prescribed"]) == (6, 6)
    assert out["basis"] == "laps"


def test_it_reports_stopping_early_end_to_end(workout_on_disk):
    out = app_module._block_eval_for(
        {"zwo_file": workout_on_disk},
        {"intervals": _laps([60] * 4 + [None, None]), "ftp_at_ride": FTP})
    assert out is not None
    assert out["outcome"] == "cut_short"
    assert out["stopped_after"] == 4 and out["reps_missed"] == 2


def test_it_uses_the_ftp_from_the_ride_not_the_current_profile(workout_on_disk,
                                                              monkeypatch):
    """A ride from March must be graded against March's FTP."""
    seen = {}
    real = sf.score_blocks

    def _spy(segs, laps, ftp=None):
        seen["ftp"] = ftp
        return real(segs, laps, ftp)

    monkeypatch.setattr(sf, "score_blocks", _spy)
    app_module._block_eval_for({"zwo_file": workout_on_disk},
                               {"intervals": _laps([60] * 6),
                                "ftp_at_ride": 199})
    assert seen["ftp"] == 199


def test_laps_from_before_the_ride_clock_was_stored_are_silent(workout_on_disk):
    """Records synced before v3.7.0 carry laps with no start_s. They get the
    clock back on the next detail fetch (app._maybe_enrich_icu_record); until
    then there is nothing to grade against and nothing is claimed."""
    laps = _laps([60] * 6)
    for lap in laps:
        lap.pop("start_s")
    assert app_module._block_eval_for(
        {"zwo_file": workout_on_disk},
        {"intervals": laps, "ftp_at_ride": FTP}) is None


@pytest.mark.parametrize("session,ride", [
    ({"zwo_file": ""}, {"intervals": [{"type": "WORK", "duration_s": 60}]}),
    ({"zwo_file": "x.zwo"}, {"intervals": []}),
    ({"zwo_file": "does_not_exist.zwo"}, {"intervals": [{"type": "WORK",
                                                         "duration_s": 60}]}),
    ({}, {}),
])
def test_it_returns_none_rather_than_raising_when_it_cannot_grade(
        session, ride, workout_on_disk):
    assert app_module._block_eval_for(session, ride) is None


def test_a_lap_shape_it_does_not_understand_is_silent_not_fatal(workout_on_disk):
    """The blanket except: prove it swallows rather than propagating, because a
    ride view must still render."""
    out = app_module._block_eval_for(
        {"zwo_file": workout_on_disk},
        {"intervals": [{"type": "WORK", "duration_s": "sixty"}] * 6,
         "ftp_at_ride": FTP})
    assert out is None


def test_the_renderer_is_wired_to_the_payload():
    """Both halves of the wiring stay in place; only the switch is off. Flipping
    BLOCK_EVAL_SURFACED must be all it takes."""
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    assert "function _blockEvalHtml(b)" in dash
    assert "_blockEvalHtml(ex.blocks)" in dash


def test_no_block_report_reaches_the_rider_while_the_gate_is_red():
    """An adversarial pass produced three separate false green ticks against
    this grader. Until tests/test_357_block_evaluation.py is green, a rider is
    shown the execution score, which is true, and no block report — the rule
    being that no verdict beats a wrong one."""
    assert app_module.BLOCK_EVAL_SURFACED is False


# ── the rating reads back ────────────────────────────────────────────────────

def _dash() -> str:
    return (Path(app_module.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")


def test_the_morning_form_preselects_what_is_stored():
    src = _dash()
    assert "function showMorningForm(stored)" in src
    assert "stored.readiness_to_train" in src
    assert "window._todayLog" in src


def test_the_ride_rating_reflects_its_own_save():
    """setRideRpe touched no local state, and the modal caches its payload — so
    the control re-rendered unselected on the next open."""
    src = _dash()
    assert "rpe-row-" in src
    assert "cached.rpe = rpe" in src


# ── the rating's own shape ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (None, None), ("", None), (7, 7), ("7", 7), (7.0, 7),
])
def test_a_rating_is_parsed_as_a_whole_number(raw, expect):
    assert app_module._parse_rtt(raw) == expect


@pytest.mark.parametrize("raw", [5.5, "5.5", "seven", True, [7], {"a": 1}])
def test_a_rating_that_is_not_a_whole_number_is_rejected(raw):
    """`int(raw)` truncated instead of rejecting — 5.5 was stored as 5. An
    upgraded install has no CHECK on this column, so this is the only guard."""
    with pytest.raises(ValueError):
        app_module._parse_rtt(raw)
