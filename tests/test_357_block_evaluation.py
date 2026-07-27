"""Block evaluation: "which prescribed blocks did I actually do?"

The rider marks a lap per interval and intervals.icu returns, for every lap,
the offset on the ride clock where it started. Those offsets are the feature:
four earlier versions tried to infer which lap was which block from the shape
of the lap alone, and each shipped a different class of confident wrong verdict
— a session the rider abandoned certified as complete, a block ridden harder
than asked reported as not ridden, a completed block called missing because the
recoveries ran long. Every one of those readings is admissible from shape. None
of them survives knowing WHEN the lap happened.

The last section is the regression that matters: every interval workout in the
library, ridden a dozen ways whose truth is known by construction. A verdict
that disagrees with the truth is a misgrade and fails. No verdict at all is
allowed — the laps do not always settle the question, and saying nothing is the
correct answer when they do not.
"""
from __future__ import annotations

import glob
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

_Z_10x1 = """<workout_file><workout>
  <Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>
  <IntervalsT Repeat="10" OnDuration="60" OffDuration="120" OnPower="1.25" OffPower="0.50"/>
  <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
</workout></workout_file>"""


def _segs():
    return sf.parse_zwo_text(_ZWO)


def _segs10():
    return sf.parse_zwo_text(_Z_10x1)


# ── lap construction ────────────────────────────────────────────────────────
# Real intervals.icu laps tile the ride and each carries start_s, the offset on
# the moving clock where it began. Synthetic lap lists must do both.

def _laps(items):
    """[(duration, ftp_pct or None)] → tiled laps stamped with start_s.

    A ``None`` percentage spends its time without emitting a lap at all — a
    stop with the recorder off, or a stretch intervals.icu did not split out.
    """
    out, t = [], 0
    for dur, pct in items:
        if pct is not None and dur > 0:
            out.append({"start_s": t, "duration_s": int(dur),
                        "ftp_pct": pct, "avg_power_w": int(FTP * pct / 100),
                        "type": "WORK" if pct >= 75 else "RECOVERY"})
        t += int(dur)
    return out


def _tiling10(works, warmup=600, rec=120):
    """Laps for the 10x1min session: warm-up, then each block followed by its
    recovery, then the cooldown. A block the rider never started still spends
    its time, as recovery."""
    items = [(warmup, 60.0)]
    for w in works:
        items.append(w if w is not None else (60, 50.0))
        items.append((rec, 50.0))
    items.append((300, 55.0))
    return _laps(items)


def _sets_laps(n_work, dur=30, pct=123.0):
    """Laps for the 3x3 session above, tiling the full prescribed timeline."""
    items = [(300, 55.0)]                       # warm-up
    items += [(30, 65.0), (30, 45.0)] * 3       # the warm-up drill
    items.append((120, 50.0))
    done = 0
    for s in range(3):
        for _ in range(3):
            done += 1
            items.append((dur, pct) if done <= n_work else (30, 50.0))
            items.append((15, 50.0))
        items.append((300, 50.0))
    items.append((300, 55.0))
    return _laps(items)


# ── what the prescription says a block is ───────────────────────────────────

def test_reps_come_from_structure_not_an_intensity_floor():
    """The 65 % warmup drill and the 89 % float legs must not be blocks."""
    reps = sf._prescribed_reps(_segs(), FTP)
    assert len(reps) == 9, [r["target_frac"] for r in reps]
    assert all(abs(r["target_frac"] - 1.25) < 1e-9 for r in reps)
    assert all(r["dur_s"] == 30 for r in reps)


def test_sets_are_split_on_the_long_between_set_recovery():
    reps = sf._prescribed_reps(_segs(), FTP)
    assert [r["set"] for r in reps] == [0, 0, 0, 1, 1, 1, 2, 2, 2]


# ── the clock is required, not optional ─────────────────────────────────────

def test_laps_without_a_clock_are_not_graded_at_all():
    """Summing the durations of the laps before this one is NOT a clock: it is
    the same number only while the laps tile with no gaps and the rider never
    stops. Four versions grading off that number is what this feature cost."""
    laps = _tiling10([(60, 125.0)] * 10)
    assert sf.score_blocks(_segs10(), laps, FTP)["outcome"] == "completed"
    for lap in laps:
        lap.pop("start_s")
    assert sf.score_blocks(_segs10(), laps, FTP) is None


def test_laps_with_no_intensity_are_not_graded_either():
    """Every admissibility test is an intensity test. Without one the grader is
    matching on duration alone, which is how a soft-pedal came to stand in for
    a VO2 rep."""
    laps = _tiling10([(60, 125.0)] * 10)
    for lap in laps:
        lap["ftp_pct"] = None
        lap["avg_power_w"] = None
    assert sf.score_blocks(_segs10(), laps, None) is None


def test_returns_none_when_there_is_nothing_to_grade():
    assert sf.score_blocks([], _sets_laps(9), FTP) is None
    assert sf.score_blocks(_segs(), [], FTP) is None
    assert sf.score_blocks(_segs(), None, FTP) is None


# ── the verdicts ────────────────────────────────────────────────────────────

def test_a_perfect_ride_grades_as_completed():
    r = sf.score_blocks(_segs10(), _tiling10([(60, 125.0)] * 10), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert (r["reps_done"], r["reps_missed"]) == (10, 0)
    assert r["work_fraction"] == 1.0
    assert r["basis"] == "laps"


def test_full_session_is_completed_with_per_set_counts():
    r = sf.score_blocks(_segs(), _sets_laps(9), FTP)
    assert r["outcome"] == "completed"
    assert (r["reps_prescribed"], r["reps_done"], r["reps_missed"]) == (9, 9, 0)
    assert r["stopped_after"] == 9
    assert [(s["set"], s["done"]) for s in r["sets"]] == [(1, 3), (2, 3), (3, 3)]
    assert r["reps"][0]["on_target"] is True          # 123 % vs 125 % target


def test_stopping_early_is_cut_short_and_names_the_block():
    r = sf.score_blocks(_segs(), _sets_laps(5), FTP)
    assert r["outcome"] == "cut_short"
    assert r["reps_done"] == 5 and r["reps_missed"] == 4
    assert r["stopped_after"] == 5
    # Set 2 half done, set 3 untouched — the answer a TSS number cannot give.
    assert [(s["set"], s["done"], s["missed"]) for s in r["sets"]] == [
        (1, 3, 0), (2, 2, 1), (3, 0, 3)]


def test_a_mid_session_skip_is_named_one_not_reported_as_stopping():
    """The reading that shape alone cannot reach. With no clock, a block
    skipped in the middle and a block dropped off the end leave byte-identical
    lap lists; the count comes out right and the attribution does not."""
    works = [(60, 125.0)] * 10
    works[3] = None
    r = sf.score_blocks(_segs10(), _tiling10(works), FTP)
    assert r is not None and r["outcome"] == "off_plan"
    assert r["reps_missed"] == 1 and r["reps"][3]["status"] == "missed"
    assert r["reps"][9]["status"] == "done"


def test_a_genuine_early_stop_is_still_cut_short():
    r = sf.score_blocks(_segs10(), _tiling10([(60, 125.0)] * 8 + [None, None]),
                        FTP)
    assert r is not None and r["outcome"] == "cut_short"
    assert r["stopped_after"] == 8 and r["reps_missed"] == 2


def test_a_short_block_is_partial_not_missing():
    laps = _tiling10([(60, 125.0)] * 2 + [(30, 125.0)] + [(60, 125.0)] * 7)
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is not None
    assert r["reps"][2]["status"] == "partial"
    # A rider who rode all the blocks with one cut short was shown "blocks
    # missing / 0 of 9". Nothing is missing when nothing was skipped.
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["work_fraction"] < 1.0


def test_all_blocks_ridden_short_is_not_reported_as_missing():
    r = sf.score_blocks(_segs10(), _tiling10([(36, 125.0)] * 10), FTP)
    assert r is not None
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["reps_partial"] == r["reps_prescribed"]
    assert r["reps_done"] == 0            # none at FULL length...
    assert r["stopped_after"] == r["reps_prescribed"]   # ...but they finished


def test_per_set_counts_include_blocks_ridden_short():
    """The set line printed `done` only, so a set whose blocks all ran a little
    short read "0/1" on the same line that called them ridden."""
    r = sf.score_blocks(_segs10(), _tiling10([(36, 125.0)] * 10), FTP)
    assert r is not None
    for st in r["sets"]:
        assert st["ridden"] == st["done"] + st["partial"]
        assert st["ridden"] + st["missed"] == st["prescribed"]


# ── the readings that were wrong before ─────────────────────────────────────

def test_a_block_ridden_longer_than_prescribed_is_not_missed():
    """Over-delivery is doing the block. Scoring |delivered − prescribed|
    symmetrically graded a block ridden at 2x as 'missed'."""
    r = sf.score_blocks(_segs10(), _tiling10([(85, 125.0)] * 10), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert r["reps_missed"] == 0


def test_a_block_ridden_harder_than_prescribed_is_not_missed():
    """Twenty points of FTP over target on every block. Reported as "not
    ridden" on 61 % of sessions by the version that vetoed intensity
    symmetrically."""
    r = sf.score_blocks(_segs10(), _tiling10([(60, 133.0)] * 10), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert r["reps_missed"] == 0


def test_recoveries_that_run_long_do_not_read_as_skipped_blocks():
    """Three minutes over on every recovery — 39 % of sessions reported a
    completed block "missing" when the clock was anchored absolutely."""
    r = sf.score_blocks(_segs10(), _tiling10([(60, 125.0)] * 10, rec=300), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert r["reps_missed"] == 0


def test_a_warmup_effort_typed_WORK_is_not_mistaken_for_a_block():
    """A head unit types a lap WORK on intensity alone, so a warm-up ramp step
    arrives looking like work. Counting laps off in order then graded a 60 s
    block against a 200 s lead-in and cascaded down the session — 350 of 1925
    library workouts misgraded a PERFECT ride that way, with a green tick."""
    laps = _laps([(200, 60.0), (200, 78.0), (200, 60.0)]
                 + [(60, 125.0), (120, 50.0)] * 10 + [(300, 55.0)])
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is not None and r["outcome"] == "completed", r and r["reps"]


def test_quitting_before_the_first_block_is_not_all_blocks_done():
    """A short warm-up, a lead-in effort, and home. The version gated on a
    file-level decidability test still told this rider they had done every
    block and 100 % of the work."""
    laps = _laps([(240, 60.0), (120, 95.0), (120, 55.0)])
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is None or (r["reps_done"] == 0 and r["reps_partial"] == 0), r


def test_token_lap_taps_are_not_blocks_you_rode():
    """One-second laps on every block: 'all blocks done' with a green tick on
    1.7 % of the prescribed work was the old verdict."""
    r = sf.score_blocks(_segs10(), _tiling10([(1, 125.0)] * 10), FTP)
    assert r is None or (r["outcome"] == "not_attempted"
                         and r["reps_done"] == 0)


def test_stray_taps_after_stopping_still_read_as_stopping():
    """Stopped after 8 of 10, then double-tapped the lap button twice."""
    r = sf.score_blocks(
        _segs10(), _tiling10([(60, 125.0)] * 8 + [(5, 125.0), (5, 125.0)]), FTP)
    assert r is not None and r["outcome"] == "cut_short"
    assert r["stopped_after"] == 8
    assert r["reps_done"] == 8 and r["reps_missed"] == 2


def test_a_double_tapped_lap_is_still_one_block():
    """The rider hit lap mid-effort, so one block arrives as two laps. Reading
    them as one delivery of half the block calls a completed block short."""
    items = [(600, 60.0)]
    for i in range(10):
        if i == 4:
            items += [(30, 125.0), (30, 125.0)]
        else:
            items.append((60, 125.0))
        items.append((120, 50.0))
    items.append((300, 55.0))
    r = sf.score_blocks(_segs10(), _laps(items), FTP)
    assert r is not None and r["outcome"] == "completed", r and r["reps"]


def test_a_forgotten_lap_tap_is_not_two_missed_blocks():
    """One lap spanning a block, its recovery and the next block. Which of the
    two was ridden is not in the recording, so nothing may be claimed either
    way — but "you missed both" is a lie, and that is what the alignment says
    on its own."""
    items = [(600, 60.0)]
    for i in range(10):
        if i == 4:
            items.append((240, 87.5))       # block + recovery + block, one lap
            continue
        if i == 5:
            items.append((120, 50.0))       # …its own recovery still follows
            continue
        items += [(60, 125.0), (120, 50.0)]
    items.append((300, 55.0))
    r = sf.score_blocks(_segs10(), _laps(items), FTP)
    assert r is None or r["reps_missed"] == 0, r and r["reps"]


def test_a_pause_does_not_move_the_blocks():
    """Ten minutes stopped after the second block, recorded as nothing at all.
    The clock the earlier versions used — the sum of the laps before this one —
    slides every later lap by the length of the stop."""
    items = [(600, 60.0)]
    for i in range(10):
        items += [(60, 125.0), (120, 50.0)]
        if i == 1:
            items.append((600, None))       # a hole in the lap list
    items.append((300, 55.0))
    r = sf.score_blocks(_segs10(), _laps(items), FTP)
    assert r is None or (r["outcome"] == "completed"
                         and r["reps_missed"] == 0), r and r["reps"]


def test_a_short_warmup_does_not_shift_the_blocks():
    r = sf.score_blocks(_segs10(), _tiling10([(60, 125.0)] * 10, warmup=240),
                        FTP)
    assert r is not None and r["outcome"] == "completed"


def test_a_lap_at_the_wrong_intensity_cannot_be_a_block():
    """As a 0.25-weight term, intensity could cost a mismatch at most 0.09, so
    a 60 s lap at 60 % FTP outscored the correct 45 s lap at 120 % and took the
    block — reported "done" while its own row said on_target=False."""
    assert sf._pairing({"dur_s": 60, "target_frac": 1.25, "start_s": 0},
                       {"t0": 0, "dur": 60, "frac": 0.60, "pct": 60.0}) is None


# ── shapes the prescription can take ────────────────────────────────────────

def test_steady_block_session_without_declared_intervals():
    """A threshold session lapped per block: no IntervalsT, so steady blocks
    above the floor are the blocks."""
    zwo = """<workout_file><workout>
      <Warmup Duration="300" PowerLow="0.45" PowerHigh="0.75"/>
      <SteadyState Duration="300" Power="1.00"/><SteadyState Duration="180" Power="0.50"/>
      <SteadyState Duration="300" Power="1.00"/><SteadyState Duration="180" Power="0.50"/>
      <SteadyState Duration="300" Power="1.00"/>
      <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
    </workout></workout_file>"""
    segs = sf.parse_zwo_text(zwo)
    assert len(sf._prescribed_reps(segs, FTP)) == 3
    laps = _laps([(300, 55.0), (300, 99.0), (180, 50.0), (300, 98.0),
                  (180, 50.0)])
    r = sf.score_blocks(segs, laps, FTP)
    assert r["reps_prescribed"] == 3 and r["reps_done"] == 2
    assert r["outcome"] == "cut_short" and r["stopped_after"] == 2


def test_untyped_laps_fall_back_to_intensity():
    """Not every source labels laps; grade those on ftp_pct."""
    laps = _tiling10([(60, 125.0)] * 10)
    for lap in laps:
        lap.pop("type")
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is not None and r["outcome"] == "completed"


def test_grades_without_an_ftp_value():
    """ICU already gives per-lap ftp_pct, so FTP is not required."""
    r = sf.score_blocks(_segs10(), _tiling10([(60, 125.0)] * 10), None)
    assert r is not None and r["outcome"] == "completed"
    assert r["reps"][0]["delivered_pct"] == 125.0


def test_locked_lap_constants():
    assert sf.LAP_SHORT_FRAC == 0.80
    assert sf.LAP_POWER_TOL_FRAC == 0.10


# ── the clock survives the round trip through storage ───────────────────────

def test_laps_survive_a_resync(tmp_path, monkeypatch):
    """Regression: laps arrive only on a DETAIL fetch; the hourly sync uses the
    activity LIST payload (no intervals) and used to overwrite them away."""
    import ride_storage as rs
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: tmp_path)
    detail = {"id": "i1", "type": "VirtualRide", "name": "x",
              "start_date_local": "2026-07-13T20:17:16", "elapsed_time": 2700,
              "icu_intervals": [{"start_index": 0, "moving_time": 30,
                                 "type": "WORK", "average_watts": 305}]}
    p = rs.persist_icu_activity(detail)
    assert len(json.loads(p.read_text())["intervals"]) == 1
    # Re-persist from a list-shaped payload that carries no intervals.
    rs.persist_icu_activity({k: v for k, v in detail.items()
                             if k != "icu_intervals"})
    assert len(json.loads(p.read_text())["intervals"]) == 1, "re-sync wiped laps"


def test_the_ride_clock_is_persisted_with_the_laps(tmp_path, monkeypatch):
    """start_index is the MOVING clock and start_time the elapsed one; they
    part company the moment the rider stops, and grading wants the moving one
    — a stop at the lights is not five minutes of the plan going by."""
    import ride_storage as rs
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: tmp_path)
    p = rs.persist_icu_activity({
        "id": "i2", "type": "VirtualRide", "start_date_local": "2026-07-13T20:00:00",
        "elapsed_time": 900, "icu_intervals": [
            {"start_index": 0, "start_time": 0, "moving_time": 600,
             "type": "RECOVERY", "average_watts": 150},
            {"start_index": 600, "start_time": 900, "moving_time": 60,
             "type": "WORK", "average_watts": 300},
        ]})
    iv = json.loads(p.read_text())["intervals"]
    assert [x["start_s"] for x in iv] == [0, 600]
    assert [x["elapsed_start_s"] for x in iv] == [0, 900]


# ── the regression that matters ─────────────────────────────────────────────

_BEHAVIOURS = {}


def _behaviour(name, outcome):
    def deco(fn):
        _BEHAVIOURS[name] = (fn, outcome)
        return fn
    return deco


def _plan_of(segs, reps):
    """[(dur, frac, is_block, start_s)] for every prescribed segment."""
    rep_starts = {r["start_s"] for r in reps}
    out = []
    for s in segs:
        d = s.get("dur_s") or 0
        if not d:
            continue
        mid = sf._seg_frac_at(s, d // 2)
        out.append((float(d), mid if mid is not None else 0.5,
                    s.get("start_s") in rep_starts, float(s["start_s"])))
    return out


def _stamp(items):
    out, t = [], 0.0
    for dur, frac in items:
        if frac is not None and dur > 0:
            out.append({"start_s": int(round(t)), "duration_s": int(round(dur)),
                        "ftp_pct": round(frac * 100.0, 1),
                        "type": "WORK" if frac >= sf.WORK_FLOOR_FRAC else "RECOVERY",
                        "avg_power_w": int(frac * FTP)})
        t += dur
    return out


@_behaviour("perfect", lambda n: ("completed", n, 0, 0))
def _b_perfect(plan, reps):
    return [(d, f) for d, f, _r, _s in plan]


@_behaviour("all harder", lambda n: ("completed", n, 0, 0))
def _b_harder(plan, reps):
    return [(d, f + 0.08 if r else f) for d, f, r, _s in plan]


@_behaviour("all longer", lambda n: ("completed", n, 0, 0))
def _b_longer(plan, reps):
    out, carry = [], 0.0
    for d, f, r, _s in plan:
        if r:
            out.append((d * 1.15, f))
            carry = 0.15 * d
        else:
            out.append((max(1.0, d - carry), f))
            carry = 0.0
    return out


@_behaviour("all short", lambda n: ("short_blocks", 0, n, 0))
def _b_short(plan, reps):
    out, carry = [], 0.0
    for d, f, r, _s in plan:
        if r:
            out.append((max(1.0, 0.55 * d), f))
            carry = 0.45 * d
        else:
            out.append((d + carry, f))
            carry = 0.0
    return out


@_behaviour("long recoveries", lambda n: ("completed", n, 0, 0))
def _b_long_rec(plan, reps):
    out, seen = [], False
    for i, (d, f, r, _s) in enumerate(plan):
        if r:
            seen = True
            out.append((d, f))
        else:
            after = any(p[2] for p in plan[i + 1:])
            out.append((d + (180.0 if seen and after else 0.0), f))
    return out


@_behaviour("slow recoveries", lambda n: ("completed", n, 0, 0))
def _b_slow_rec(plan, reps):
    out, seen = [], False
    for i, (d, f, r, _s) in enumerate(plan):
        if r:
            seen = True
            out.append((d, f))
        elif seen and any(p[2] for p in plan[i + 1:]):
            out.append((d * 1.6, f))
        else:
            out.append((d, f))
    return out


@_behaviour("short warm-up", lambda n: ("completed", n, 0, 0))
def _b_short_wu(plan, reps):
    out, seen = [], False
    for d, f, r, _s in plan:
        if r:
            seen = True
        out.append((d if seen else max(30.0, 0.5 * d), f))
    return out


@_behaviour("warm-up ramp laps", lambda n: ("completed", n, 0, 0))
def _b_ramp_laps(plan, reps):
    out, first = [], True
    for d, f, r, _s in plan:
        if first and not r and d >= 180:
            out += [(d / 3.0, max(0.4, f - 0.15)), (d / 3.0, f),
                    (d / 3.0, min(1.05, f + 0.30))]
            first = False
        else:
            first = first and not r
            out.append((d, f))
    return out


@_behaviour("paused", lambda n: ("completed", n, 0, 0))
def _b_paused(plan, reps):
    anchor = reps[min(1, len(reps) - 1)]["start_s"]
    out = []
    for d, f, _r, s in plan:
        out.append((d, f))
        if s == anchor:
            out.append((600.0, None))
    return out


@_behaviour("split lap", lambda n: ("completed", n, 0, 0))
def _b_split(plan, reps):
    k = reps[len(reps) // 2]["start_s"]
    out = []
    for d, f, _r, s in plan:
        if s == k and d >= 20:
            out += [(d / 2.0, f), (d / 2.0, f)]
        else:
            out.append((d, f))
    return out


@_behaviour("forgotten lap", lambda n: ("completed", n, 0, 0))
def _b_forgotten(plan, reps):
    if len(reps) < 2:
        return None
    k = min(len(reps) // 2, len(reps) - 2)
    a, b = reps[k]["start_s"], reps[k + 1]["start_s"]
    out, run = [], []
    for d, f, _r, s in plan:
        if a <= s <= b:
            run.append((d, f))
            if s == b:
                tot = sum(x[0] for x in run)
                out.append((tot, sum(x[0] * x[1] for x in run) / tot))
                run = []
            continue
        out.append((d, f))
    return out


@_behaviour("stopped early", lambda n: ("cut_short", n - 1, 0, 1))
def _b_stopped(plan, reps):
    last = reps[-1]["start_s"]
    return [(d, f) for d, f, _r, s in plan if s < last]


@_behaviour("mid skip (soft)", lambda n: ("off_plan", n - 1, 0, 1))
def _b_skip_soft(plan, reps):
    k = reps[len(reps) // 2]["start_s"]
    return [(d, 0.5 if s == k else f) for d, f, _r, s in plan]


@_behaviour("mid skip (merged)", lambda n: ("off_plan", n - 1, 0, 1))
def _b_skip_merged(plan, reps):
    k = reps[len(reps) // 2]["start_s"]
    out, merge = [], None
    for d, f, _r, s in plan:
        if s == k:
            merge = [(d, 0.5)]
            continue
        if merge is not None:
            if out and out[-1][1] < sf.WORK_FLOOR_FRAC:
                merge.insert(0, out.pop())
            merge.append((d, f))
            tot = sum(x[0] for x in merge)
            out.append((tot, sum(x[0] * x[1] for x in merge) / tot))
            merge = None
            continue
        out.append((d, f))
    return out + (merge or [])


@_behaviour("quit in warm-up", lambda n: ("not_attempted", 0, 0, n))
def _b_quit(plan, reps):
    out = []
    for d, f, r, _s in plan:
        if r:
            break
        out.append((d, f))
    if not out:
        return None
    out[-1] = (out[-1][0], min(1.05, out[-1][1] + 0.30))
    return out


def _library_cases():
    for path in sorted(glob.glob("workouts/*.zwo")):
        segs = sf.parse_zwo_file(path)
        if not segs or not any(x.get("kind") == "interval_on" for x in segs):
            continue
        reps = sf._prescribed_reps(segs, FTP)
        if len(reps) < 3:
            continue
        yield path, segs, reps


_GRADED_FLOOR = {
    # How often each behaviour must produce a verdict at all. Grading nothing
    # passes the correctness half of this test trivially, and the versions of
    # this feature that were cut failed in the other direction — so both halves
    # are pinned. The low floors are honest: a lap list does not always settle
    # which block was which. "perfect" allows for the boundary-free files —
    # blocks bordered by prescribed material at their own intensity, where a
    # block boundary does not exist in the plan or in any recording of it.
    "perfect": 0.80, "all harder": 0.80, "all longer": 0.72,
    "warm-up ramp laps": 0.65, "split lap": 0.80, "all short": 0.80,
    "slow recoveries": 0.80, "paused": 0.80, "short warm-up": 0.72,
    "stopped early": 0.80, "mid skip (soft)": 0.78, "mid skip (merged)": 0.0,
    "long recoveries": 0.75, "quit in warm-up": 0.05, "forgotten lap": 0.0,
}


@pytest.mark.parametrize("label", sorted(_BEHAVIOURS))
def test_every_interval_workout_is_graded_right_or_not_at_all(label):
    """Every interval workout in the library, ridden a dozen ways whose truth
    is known by construction. A verdict that disagrees with the truth is a
    misgrade and fails the build. No verdict at all is allowed: a lap list does
    not always settle which block was which, and silence is the right answer
    when it does not — a missing report is recoverable, a false green tick is
    not.

    Split per behaviour so the suite can run them side by side; the assertion
    is over the whole library either way.
    """
    fn, truth = _BEHAVIOURS[label]
    wrong, graded, total = [], 0, 0
    for path, segs, reps in _library_cases():
        items = fn(_plan_of(segs, reps), reps)
        if items is None:
            continue
        total += 1
        r = sf.score_blocks(segs, _stamp(items), FTP)
        if r is None:
            continue
        graded += 1
        got = (r["outcome"], r["reps_done"], r["reps_partial"],
               r["reps_missed"])
        if got != truth(len(reps)):
            wrong.append((path.split("/")[-1], got, truth(len(reps))))
    assert total > 1000, f"the library scan found almost nothing: {total}"
    assert not wrong, f"{len(wrong)} misgrades of {total}, e.g. {wrong[:5]}"
    assert graded >= _GRADED_FLOOR[label] * total, (
        f"{label}: graded {graded}/{total}, too little to be worth showing")


# ── what an independent adversarial pass found ──────────────────────────────
# Every one of these was produced by a reviewer whose brief was to prove the
# grader wrong, against a suite that was green. They are the failures that
# matter: three of the five hand the rider a green tick for work they did not
# do. They stay here, failing, until the grader earns them — a red gate is the
# only thing that has ever stopped this feature shipping wrong.

def test_an_abandoned_over_under_is_not_certified_complete():
    """Rode 3 of 6 blocks, took the unders long, went home. The under-legs sit
    at 90 % FTP — above the work floor — and were credited as the blocks that
    were never ridden, including one prescribed eight minutes after the rider
    had stopped. Library-wide: 121 to 206 over-credited sessions depending on
    how long the recoveries ran."""
    segs = sf.parse_zwo_file("workouts/over_under_6x2min_33min.zwo")
    laps = _laps([(300, 72.6), (120, 50.0)]
                 + [(120, 105.0), (120, 90.0)] * 3      # 3 blocks, long unders
                 + [(360, 51.0)])                        # …then home
    r = sf.score_blocks(segs, laps, FTP)
    assert r is None or r["reps_done"] <= 3, r["outcome"]


def test_a_block_ridden_in_two_halves_with_a_rest_is_not_done_in_full():
    """_merged_lap's span test lets a rest of up to a quarter of the block slide
    through, and the recovery lap between the halves is invisible because it was
    filtered out before the merge. A rider who could not hold 20 minutes and took
    five off in the middle of every block was told they did all three."""
    zwo = """<workout_file><workout>
      <Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>
      <IntervalsT Repeat="3" OnDuration="1200" OffDuration="300" OnPower="0.95" OffPower="0.50"/>
    </workout></workout_file>"""
    items = [(600, 60.0)]
    for _ in range(3):
        items += [(600, 95.0), (300, 50.0), (600, 95.0), (300, 50.0)]
    r = sf.score_blocks(sf.parse_zwo_text(zwo), _laps(items), FTP)
    assert r is None or r["outcome"] != "completed", r


def test_a_flat_ride_with_auto_lap_is_not_eighteen_vo2_blocks():
    """Continuous tempo at 85 % FTP, never above it, head unit auto-lapping every
    minute. 18 VO2 blocks certified — the under-power veto plus the 80 % length
    rule means 64 % of the prescribed work reads as a completed block."""
    segs = sf.parse_zwo_file("workouts/vo2_short_1min_18x_60min.zwo")
    end = max(s["start_s"] + s["dur_s"] for s in segs)
    items = [(600, 60.0)] + [(60, 85.0)] * ((end - 900) // 60) + [(300, 55.0)]
    r = sf.score_blocks(segs, _laps(items), FTP)
    assert r is None or r["reps_done"] == 0, r["outcome"]


def test_a_block_ridden_twice_as_long_is_not_a_mid_session_skip():
    """Contract: over-delivery is doing the block. LAP_OVERLONG_FRAC vetoes the
    pairing outright at 1.5x and nothing compensates, so the rider who held a
    one-minute block for two is told they SKIPPED it. 354 of 354 graded sessions.

    The gate written to catch exactly this (_unexplained_miss — "a block called
    unridden with a lap that could have been it sitting spare") asks the same
    _pairing that produced the veto, so a vetoed lap is invisible to it."""
    works = [(60, 125.0)] * 10
    works[4] = (120, 125.0)
    r = sf.score_blocks(_segs10(), _tiling10(works), FTP)
    assert r is None or r["reps_missed"] == 0, (r["outcome"], r["reps"][4])


def test_a_long_run_of_skipped_blocks_does_not_shift_the_report():
    """Rode blocks 1, 2, 9 and 10 and soft-pedalled six in the middle. The anchor
    window makes "skip six and resume" inexpressible, so the report slides by a
    slot: block 8 reported done and never ridden, block 10 reported missed and
    ridden, and "stopped after block 9" is false."""
    works = [(60, 125.0)] * 10
    for i in range(2, 8):
        works[i] = (60, 50.0)
    r = sf.score_blocks(_segs10(), _tiling10(works), FTP)
    if r is None:
        return
    assert [x["status"] for x in r["reps"]] == (
        ["done"] * 2 + ["missed"] * 6 + ["done"] * 2), r["outcome"]


def test_it_never_raises_on_an_ftp_it_cannot_use():
    """score_structure guards the identical input; this did not, and app.py's
    blanket except would have turned the raise into a silently absent feature."""
    laps = _tiling10([(60, 125.0)] * 10)
    for ftp in ("250", object(), float("nan"), -1, 0):
        sf.score_blocks(_segs10(), laps, ftp)
