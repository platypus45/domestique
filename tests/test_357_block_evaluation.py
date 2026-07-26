"""v3.5.6 — lap-based block evaluation: "which blocks did I actually do?"

The rider marks a Garmin lap per interval, so the laps ARE the block
boundaries. score_blocks() grades the prescription against them and answers
what a load number cannot: did I finish, did I stop early, and where.

Also pins the two real defects found while building it:
  * laps are destroyed by any re-sync unless carried forward (measured: a
    61-lap ride went to 0 laps on one forced sync);
  * rep detection must come from the prescription's STRUCTURE, not an
    intensity floor — a 30/15 float has 89 % OFF legs and a 65 % warmup
    drill, both of which were being counted as work reps.
"""
from __future__ import annotations

import json

import pytest

import structure_fidelity as sf

FTP = 248.0

# 3 sets x 3 reps of 30 s @125 % with 15 s floats at 89 %, a 65 % warmup
# drill (an IntervalsT too — must NOT be graded), and 5 min between sets.
_ZWO = """<workout_file><workout>
  <Warmup Duration="300" PowerLow="0.45" PowerHigh="0.75"/>
  <IntervalsT Repeat="3" OnDuration="30" OffDuration="30" OnPower="0.65" OffPower="0.45"/>
  <SteadyState Duration="120" Power="0.50"/>
  <IntervalsT Repeat="3" OnDuration="30" OffDuration="15" OnPower="1.25" OffPower="0.89"/>
  <SteadyState Duration="300" Power="0.50"/>
  <IntervalsT Repeat="3" OnDuration="30" OffDuration="15" OnPower="1.25" OffPower="0.89"/>
  <SteadyState Duration="300" Power="0.50"/>
  <IntervalsT Repeat="3" OnDuration="30" OffDuration="15" OnPower="1.25" OffPower="0.89"/>
  <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
</workout></workout_file>"""


def _segs():
    return sf.parse_zwo_text(_ZWO)


def _lap(dur, pct, kind="WORK"):
    return {"duration_s": dur, "ftp_pct": pct, "type": kind,
            "avg_power_w": round(FTP * pct / 100.0)}


def _laps(n_work, dur=30, pct=123.0):
    """n_work WORK laps with a RECOVERY lap between each (as ICU returns)."""
    out = [_lap(600, 60.0, "RECOVERY")]
    for i in range(n_work):
        out.append(_lap(dur, pct))
        out.append(_lap(15, 50.0, "RECOVERY"))
    return out


def test_reps_come_from_structure_not_an_intensity_floor():
    """The 65 % warmup drill and the 89 % float legs must not be reps."""
    reps = sf._prescribed_reps(_segs(), FTP)
    assert len(reps) == 9, [r["target_frac"] for r in reps]
    assert all(abs(r["target_frac"] - 1.25) < 1e-9 for r in reps)
    assert all(r["dur_s"] == 30 for r in reps)


def test_sets_are_split_on_the_long_between_set_recovery():
    reps = sf._prescribed_reps(_segs(), FTP)
    assert [r["set"] for r in reps] == [0, 0, 0, 1, 1, 1, 2, 2, 2]


def test_full_session_is_completed():
    r = sf.score_blocks(_segs(), _laps(9), FTP)
    assert r["outcome"] == "completed"
    assert (r["reps_prescribed"], r["reps_done"], r["reps_missed"]) == (9, 9, 0)
    assert r["work_fraction"] == 1.0
    assert r["stopped_after"] == 9
    assert [(s["set"], s["done"]) for s in r["sets"]] == [(1, 3), (2, 3), (3, 3)]
    assert r["reps"][0]["on_target"] is True          # 123 % vs 125 % target
    assert r["basis"] == "laps"


def test_stopping_early_is_cut_short_and_names_the_block():
    r = sf.score_blocks(_segs(), _laps(5), FTP)
    assert r["outcome"] == "cut_short"
    assert r["reps_done"] == 5 and r["reps_missed"] == 4
    assert r["stopped_after"] == 5
    # Set 2 half done, set 3 untouched — this is the answer a TSS number
    # cannot give.
    assert [(s["set"], s["done"], s["missed"]) for s in r["sets"]] == [
        (1, 3, 0), (2, 2, 1), (3, 0, 3)]


def test_a_short_rep_is_partial_not_done():
    laps = _laps(9)
    # third WORK lap only 12 s of the prescribed 30 s
    work_idx = [i for i, l in enumerate(laps) if l["type"] == "WORK"]
    laps[work_idx[2]]["duration_s"] = 12
    r = sf.score_blocks(_segs(), laps, FTP)
    assert r["reps_partial"] == 1
    assert r["reps"][2]["status"] == "partial"
    # v3.6.0: "off_plan" was the wrong verdict here and the UI copy proved it —
    # a rider who rode all nine blocks with one cut short was shown "blocks
    # missing / 0 of 9". Nothing is missing when nothing was skipped.
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["work_fraction"] < 1.0


def test_all_blocks_ridden_short_is_not_reported_as_missing():
    # The case that exposed it: every prescribed block attempted, all of them
    # under length. Anything that reads as "you skipped blocks" is a lie.
    laps = _laps(9)
    for lap in laps:
        if lap["type"] == "WORK":
            lap["duration_s"] = int(lap["duration_s"] * 0.6)
    r = sf.score_blocks(_segs(), laps, FTP)
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["reps_partial"] == r["reps_prescribed"]
    assert r["reps_done"] == 0            # none at FULL length...
    assert r["stopped_after"] == r["reps_prescribed"]   # ...but they finished


def test_ui_counts_ridden_blocks_not_only_full_length_ones():
    from pathlib import Path
    import app as app_module
    src = (Path(app_module.__file__).parent / "templates" / "dashboard.html"
           ).read_text(encoding="utf-8")
    assert "blocks ridden" in src
    assert "at full length" in src
    assert "all blocks, cut short" in src


def test_a_missing_middle_lap_is_attributed_to_the_end():
    """DOCUMENTED LIMIT of positional matching, asserted rather than hidden.

    If the rider skips one rep in the middle, no lap exists for it, and the
    remaining laps are byte-identical to having stopped one rep early. So the
    COUNT is right (8 of 9) but the hole is reported at the end. Detecting a
    true mid-session skip would need cumulative lap timing vs the prescribed
    schedule; not built, because the reported use case is stopping early.
    """
    laps = _laps(9)
    work_idx = [i for i, l in enumerate(laps) if l["type"] == "WORK"]
    del laps[work_idx[4]]                  # drop one mid-session rep
    r = sf.score_blocks(_segs(), laps, FTP)
    assert (r["reps_done"], r["reps_missed"]) == (8, 1)   # count is correct
    assert r["outcome"] == "cut_short"                    # attribution is not
    assert r["stopped_after"] == 8


def test_no_work_laps_is_not_attempted():
    r = sf.score_blocks(_segs(), [_lap(1800, 55.0, "RECOVERY")], FTP)
    assert r is None or r["outcome"] == "not_attempted"


def test_untyped_laps_fall_back_to_intensity():
    """Not every source labels laps; grade those on ftp_pct."""
    laps = []
    for _ in range(9):
        laps.append({"duration_s": 30, "ftp_pct": 123.0})   # no "type"
        laps.append({"duration_s": 15, "ftp_pct": 50.0})
    r = sf.score_blocks(_segs(), laps, FTP)
    assert r["reps_done"] == 9 and r["outcome"] == "completed"


def test_grades_without_an_ftp_value():
    """ICU already gives per-lap ftp_pct, so FTP is not required."""
    r = sf.score_blocks(_segs(), _laps(9), None)
    assert r["outcome"] == "completed"
    assert r["reps"][0]["delivered_pct"] == 123.0


def test_returns_none_when_it_cannot_honestly_grade():
    assert sf.score_blocks([], _laps(9), FTP) is None
    assert sf.score_blocks(_segs(), [], FTP) is None
    assert sf.score_blocks(_segs(), None, FTP) is None


def test_steady_block_session_without_declared_intervals():
    """A threshold session lapped per block: no IntervalsT, so steady blocks
    above the floor are the reps."""
    zwo = """<workout_file><workout>
      <Warmup Duration="300" PowerLow="0.45" PowerHigh="0.75"/>
      <SteadyState Duration="300" Power="1.00"/><SteadyState Duration="180" Power="0.50"/>
      <SteadyState Duration="300" Power="1.00"/><SteadyState Duration="180" Power="0.50"/>
      <SteadyState Duration="300" Power="1.00"/>
      <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
    </workout></workout_file>"""
    segs = sf.parse_zwo_text(zwo)
    assert len(sf._prescribed_reps(segs, FTP)) == 3
    laps = [_lap(300, 99.0), _lap(180, 50.0, "RECOVERY"),
            _lap(300, 98.0), _lap(180, 50.0, "RECOVERY")]
    r = sf.score_blocks(segs, laps, FTP)
    assert r["reps_prescribed"] == 3 and r["reps_done"] == 2
    assert r["outcome"] == "cut_short" and r["stopped_after"] == 2


def test_laps_survive_a_resync(tmp_path, monkeypatch):
    """Regression: laps arrive only on a DETAIL fetch; the hourly sync uses the
    activity LIST payload (no intervals) and used to overwrite them away."""
    import ride_storage as rs
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: tmp_path)
    detail = {"id": "i1", "type": "VirtualRide", "name": "x",
              "start_date_local": "2026-07-13T20:17:16", "elapsed_time": 2700,
              "icu_intervals": [{"duration_s": 30, "type": "WORK",
                                 "ftp_pct": 123.0, "avg_power_w": 305}]}
    p = rs.persist_icu_activity(detail)
    assert len(json.loads(p.read_text())["intervals"]) == 1
    # Re-persist from a list-shaped payload that carries no intervals.
    rs.persist_icu_activity({k: v for k, v in detail.items()
                             if k != "icu_intervals"})
    assert len(json.loads(p.read_text())["intervals"]) == 1, "re-sync wiped laps"


def test_locked_lap_constants():
    assert sf.LAP_SHORT_FRAC == 0.80
    assert sf.LAP_POWER_TOL_FRAC == 0.10
