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


# ── shared fixture: an unambiguous 10x1min session ──────────────────────────
# Real intervals.icu laps tile the ride (2717 s of laps on a 2720 s ride), and
# the grader derives each lap's position on the ride clock by summing the ones
# before it — so any synthetic lap list must tile too.

_Z_10x1_EARLY = """<workout_file><workout>
  <Warmup Duration="600" PowerLow="0.50" PowerHigh="0.70"/>
  <IntervalsT Repeat="10" OnDuration="60" OffDuration="120" OnPower="1.25" OffPower="0.50"/>
  <Cooldown Duration="300" PowerLow="0.65" PowerHigh="0.45"/>
</workout></workout_file>"""


def _segs10():
    return sf.parse_zwo_text(_Z_10x1_EARLY)


def _w(dur, pct=125.0):
    return {"type": "WORK", "duration_s": dur, "ftp_pct": pct,
            "avg_power_w": int(FTP * pct / 100)}


def _r(dur, pct=50.0):
    return {"type": "RECOVERY", "duration_s": dur, "ftp_pct": pct}


def _tiling10(works):
    """Laps for the 10x1min session: warm-up, then each work lap followed by its
    recovery, then the cooldown — tiling the ride so the clock is right. A block
    the rider never started still spends its time, as recovery."""
    out = [_r(600, 60.0)]
    for w in works:
        out.append(w if w is not None else _r(60))
        out.append(_r(120))
    out.append(_r(300, 55.0))
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
    laps = _tiling10([_w(60)] * 2 + [_w(24)] + [_w(60)] * 7)
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is not None
    assert r["reps_partial"] == 1
    assert r["reps"][2]["status"] == "partial"
    # v3.6.0: "off_plan" was the wrong verdict here and the UI copy proved it —
    # a rider who rode all the blocks with one cut short was shown "blocks
    # missing / 0 of 9". Nothing is missing when nothing was skipped.
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["work_fraction"] < 1.0


def test_all_blocks_ridden_short_is_not_reported_as_missing():
    # Every prescribed block attempted, all of them under length. Anything that
    # reads as "you skipped blocks" is a lie.
    r = sf.score_blocks(_segs10(), _tiling10([_w(40)] * 10), FTP)
    assert r is not None
    assert r["outcome"] == "short_blocks"
    assert r["reps_missed"] == 0
    assert r["reps_partial"] == r["reps_prescribed"]
    assert r["reps_done"] == 0            # none at FULL length...
    assert r["stopped_after"] == r["reps_prescribed"]   # ...but they finished


def test_the_grader_is_not_surfaced_in_this_release():
    """Deliberate: the grader is kept and measured, not shown. Four attempts to
    infer which blocks a rider did from an unlabelled lap list each shipped a
    different class of confident wrong verdict. If a future change wires it back
    in, this test should fail and force the decision to be made explicitly."""
    from pathlib import Path
    import app as app_module
    app_src = Path(app_module.__file__).read_text(encoding="utf-8")
    dash = (Path(app_module.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    # the comment explaining the decision mentions the name; no CALL may exist
    assert "_sf.score_blocks(" not in app_src
    assert "score_blocks(segs" not in app_src
    assert '"blocks"] = ' not in app_src
    assert "_blockEvalHtml(" not in dash


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


# ── what laps can and cannot determine ──────────────────────────────────────

def test_a_perfect_ride_grades_as_completed():
    r = sf.score_blocks(_segs10(), _tiling10([_w(60)] * 10), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert (r["reps_done"], r["reps_missed"]) == (10, 0)


def test_a_warmup_lap_typed_WORK_is_not_mistaken_for_a_block():
    """A head unit types a lap WORK on intensity alone, so a warm-up ramp step
    arrives looking like work. Counting laps off in order then graded a 60 s rep
    against a 200 s lead-in and cascaded down the session — 350 of 1925 library
    workouts misgraded a PERFECT ride that way, reported with a green tick."""
    laps = [_r(200, 60.0), _w(200, 78.0), _r(200, 60.0)]
    laps += _tiling10([_w(60)] * 10)[1:]
    r = sf.score_blocks(_segs10(), laps, FTP)
    assert r is not None and r["outcome"] == "completed", r and r["reps"]


def test_alignment_preserves_order():
    """A rider cannot ride block 5 before block 4, so the assignment must be
    monotone — otherwise it could 'explain' any session by reshuffling."""
    reps = sf._prescribed_reps(_segs10(), FTP)
    wl = sf._work_laps(_tiling10([_w(60)] * 8 + [None, None]))
    idx = [j for j in sf._align_laps(reps, wl) if j is not None]
    assert idx == sorted(idx)
    assert len(set(idx)) == len(idx), "a lap must not grade two reps"


def test_token_lap_taps_are_not_blocks_you_rode():
    """One-second laps on every block: 'all blocks done' with a green tick on
    1.7% of the prescribed work was the old verdict."""
    r = sf.score_blocks(_segs10(), _tiling10([_w(1)] * 10), FTP)
    assert r is not None and r["outcome"] == "not_attempted"
    assert r["reps_done"] == 0 and r["reps_missed"] == 10
    assert r["work_fraction"] < 0.05


def test_stray_taps_after_stopping_still_read_as_stopping():
    """Stopped after 8 of 10, then double-tapped the lap button twice. Counting
    those as blocks reported all ten attempted."""
    r = sf.score_blocks(_segs10(), _tiling10([_w(60)] * 8 + [_w(5), _w(5)]), FTP)
    assert r is not None and r["outcome"] == "cut_short"
    assert r["stopped_after"] == 8
    assert r["reps_done"] == 8 and r["reps_missed"] == 2


def test_a_genuine_early_stop_is_unchanged():
    r = sf.score_blocks(_segs10(), _tiling10([_w(60)] * 8 + [None, None]), FTP)
    assert r is not None and r["outcome"] == "cut_short"
    assert r["stopped_after"] == 8 and r["reps_missed"] == 2


def test_a_mid_session_skip_is_named_one():
    """`off_plan` was unreachable: the tail check used an empty slice whenever
    the last block WAS ridden, so all() on nothing was True and a hole at block
    2 reported "stopped early — stopped after block 10"."""
    works = [_w(60)] * 10
    works[3] = None
    r = sf.score_blocks(_segs10(), _tiling10(works), FTP)
    assert r is not None and r["outcome"] == "off_plan"
    assert r["reps_missed"] == 1 and r["reps"][3]["status"] == "missed"


def test_a_block_ridden_longer_than_prescribed_is_not_missed():
    """Over-delivery is doing the block. Scoring |delivered − prescribed|
    symmetrically graded a block ridden at 2x as 'missed'."""
    r = sf.score_blocks(_segs10(), _tiling10([_w(85)] * 10), FTP)
    assert r is not None and r["outcome"] == "completed"
    assert r["reps_missed"] == 0


def test_a_lap_at_the_wrong_intensity_cannot_steal_a_block():
    """As a 0.25-weight term, intensity could cost a mismatch at most 0.09, so a
    60 s lap at 60% FTP outscored the correct 45 s lap at 120% and took the rep
    — reported "done" while its own row said on_target=False."""
    assert sf._lap_match_score({"dur_s": 60, "target_frac": 1.25, "start_s": 0},
                               {"duration_s": 60, "ftp_pct": 60.0, "_t0": 0}) == 0.0


def test_per_set_counts_include_blocks_ridden_short():
    """The set line printed `done` only, so a set whose blocks all ran a little
    short read "0/1" on the same line that called them ridden."""
    r = sf.score_blocks(_segs10(), _tiling10([_w(40)] * 10), FTP)
    assert r is not None
    for st in r["sets"]:
        assert st["ridden"] == st["done"] + st["partial"]
        assert st["ridden"] + st["missed"] == st["prescribed"]


def test_an_ambiguous_session_is_not_graded_at_all():
    """The honest limit. When the file itself contains a non-block above the work
    floor that shares a block's shape and sits near it, no inference can tell
    "that lap was the block" from "the block was skipped and that lap was the
    warm-up". Three attempts to infer past this each shipped confident wrong
    answers; the fourth answer is to say nothing."""
    amb = sf.parse_zwo_text("""<workout_file><workout>
      <SteadyState Duration="60" Power="1.20"/>
      <SteadyState Duration="120" Power="0.50"/>
      <IntervalsT Repeat="3" OnDuration="60" OffDuration="120" OnPower="1.25" OffPower="0.50"/>
    </workout></workout_file>""")
    reps = sf._prescribed_reps(amb, FTP)
    assert sf._session_is_ambiguous(amb, reps, FTP) is True
    laps = [_w(60, 120.0), _r(120)] + [_w(60), _r(120)] * 3
    assert sf.score_blocks(amb, laps, FTP) is None


def test_the_library_grades_three_known_rides_or_says_nothing():
    """The regression guard. Every interval workout in the library, ridden three
    ways whose truth is known by construction: exactly as prescribed, stopping
    one block short, and skipping one block mid-session. A verdict that
    disagrees with the truth is a misgrade; no verdict at all is allowed (the
    session's own structure may make it undecidable) — a wrong verdict is not.
    """
    import glob

    def _laps_for(segs, reps, omit_start=None):
        out = []
        for seg in segs:
            dur = seg.get("dur_s") or 0
            if not dur:
                continue
            mid = sf._seg_frac_at(seg, dur // 2)
            if omit_start is not None and seg.get("start_s") == omit_start:
                out.append({"type": "RECOVERY", "duration_s": dur,
                            "ftp_pct": 50.0})
                continue
            is_work = mid is not None and mid >= sf.WORK_FLOOR_FRAC
            out.append({"type": "WORK" if is_work else "RECOVERY",
                        "duration_s": dur,
                        "ftp_pct": round((mid or 0.5) * 100, 1)})
        return out

    wrong, graded, silent = [], 0, 0
    for path in sorted(glob.glob("workouts/*.zwo")):
        try:
            segs = sf.parse_zwo_file(path)
        except Exception:
            continue
        if not segs or not any(x.get("kind") == "interval_on" for x in segs):
            continue
        reps = sf._prescribed_reps(segs, FTP)
        n = len(reps)
        if n < 3:
            continue
        name = path.split("/")[-1]
        for label, omit, want in (
            ("perfect", None, ("completed", n, 0)),
            ("stopped one short", n - 1, ("cut_short", n - 1, 1)),
            ("skipped one mid", max(1, n // 2), (None, n - 1, 1)),
        ):
            r = sf.score_blocks(
                segs, _laps_for(segs, reps,
                                None if omit is None else reps[omit]["start_s"]),
                FTP)
            if r is None:
                silent += 1
                continue
            graded += 1
            oc, done, missed = want
            if (oc is not None and r["outcome"] != oc) or \
                    r["reps_done"] != done or r["reps_missed"] != missed:
                wrong.append((name, label, r["outcome"], r["reps_done"],
                              r["reps_missed"], want))
    assert graded > 3000, f"the gate suppressed too much to be useful: {graded}"
    assert not wrong, f"{len(wrong)} misgrades, e.g. {wrong[:4]}"
