"""Structure-fidelity axis — prescribed .zwo timeline vs delivered 1 Hz trace.

Locks:
  * fidelity constants (0.75 work floor, ±5 %/10 W band, 3 s ERG grace,
    ±120 s alignment search, >50 % below-floor ⇒ missing rep);
  * the result-dict field names (the contract for future UI wiring);
  * zwo parsing: IntervalsT expansion, absolute contiguous timeline,
    ramp lo→hi semantics (proved on a REAL library file);
  * behavior: perfect square + 3 s-lag ERG PASS clean, 10 s+ smear
    degrades, a skipped rep drops reps_delivered, an abandoned ride
    reports the missing tail, global trace offset is recovered;
  * score_ride: "fidelity" is ADDITIVE + advisory — None without inputs,
    never moves score/verdict, embedded streams/ftp_at_ride fall back.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import execution_score as es  # noqa: E402
import structure_fidelity as sf  # noqa: E402

FTP = 250.0

ZWO = """<?xml version='1.0' encoding='utf-8'?>
<workout_file>
    <name>Test 8x30/15</name>
    <workout>
        <Warmup Duration="240" PowerLow="0.45" PowerHigh="0.70" />
        <IntervalsT Repeat="8" OnDuration="30" OffDuration="15"
                    OnPower="1.18" OffPower="0.50" />
        <Cooldown Duration="120" PowerLow="0.60" PowerHigh="0.40" />
    </workout>
</workout_file>"""

Z2_ZWO = """<workout_file><workout>
<Warmup Duration="300" PowerLow="0.40" PowerHigh="0.60" />
<SteadyState Duration="1800" Power="0.65" />
<Cooldown Duration="300" PowerLow="0.55" PowerHigh="0.40" />
</workout></workout_file>"""


def _segs():
    return sf.parse_zwo_text(ZWO)


def _target_watts(segs, ftp=FTP):
    """Perfect per-second delivery of the prescribed timeline."""
    n = segs[-1]["start_s"] + segs[-1]["dur_s"]
    out = [0.0] * n
    for s in segs:
        for i in range(s["dur_s"]):
            f = sf._seg_frac_at(s, i)
            out[s["start_s"] + i] = (f or 0.0) * ftp
    return out


def _lagged(tgt, lag_s):
    """First-order-ish trainer response: each target CHANGE is reached via
    a linear ramp lasting ``lag_s`` seconds (deterministic ERG lag model)."""
    if lag_s <= 0:
        return list(tgt)
    out = [tgt[0]]
    ramp_from, ramp_start = tgt[0], 0
    for i in range(1, len(tgt)):
        v = tgt[i]
        if v != tgt[i - 1]:
            ramp_from, ramp_start = out[-1], i
        f = min(1.0, (i - ramp_start + 1) / lag_s)
        out.append(ramp_from + (v - ramp_from) * f)
    return out


def _on_windows(segs):
    return [s for s in segs if s["kind"] == "interval_on"]


# ── locked constants + result contract ───────────────────────────────────────

def test_locked_fidelity_constants():
    assert sf.WORK_FLOOR_FRAC == 0.75
    assert sf.TOL_FRAC == 0.05
    assert sf.TOL_MIN_W == 10.0
    assert sf.TRANSIENT_GRACE_S == 3
    assert sf.ALIGN_MAX_OFFSET_S == 120
    assert sf.MISSING_BELOW_FLOOR_FRAC == 0.5
    assert sf.MISSING_TARGET_FRAC == 0.90


def test_result_field_names_are_the_contract():
    r = sf.score_structure(_segs(), _target_watts(_segs()), FTP)
    assert set(r) == {"reps_prescribed", "reps_delivered", "rep_completion",
                      "mean_on_target_pct", "mean_power_ratio",
                      "alignment_offset_s", "worst_segment", "segments"}
    row_keys = {"index", "start_s", "dur_s", "target_frac", "mean_ratio",
                "on_target_frac", "missing"}
    assert all(set(row) == row_keys for row in r["segments"])
    assert set(r["worst_segment"]) == row_keys


# ── zwo parsing (incl. a REAL library file) ──────────────────────────────────

def test_parse_expands_intervals_and_builds_contiguous_timeline():
    segs = _segs()
    # Warmup + 8×(on+off) + Cooldown = 18 segments, contiguous, 720 s total.
    assert len(segs) == 18
    assert segs[0] == {"kind": "warmup", "start_s": 0, "dur_s": 240,
                       "lo": 0.45, "hi": 0.70}
    t = 0
    for s in segs:
        assert s["start_s"] == t
        t += s["dur_s"]
    assert t == 240 + 8 * 45 + 120 == 720
    ons = _on_windows(segs)
    assert len(ons) == 8
    assert all(s["dur_s"] == 30 and s["lo"] == s["hi"] == 1.18 for s in ons)
    assert segs[-1]["kind"] == "cooldown"


def test_parse_real_library_file_30_15():
    segs = sf.parse_zwo_file(ROOT / "workouts" / "vo2_short_30s15s_13x_59min.zwo")
    assert segs is not None
    ons = _on_windows(segs)
    assert len(ons) == 13                     # IntervalsT Repeat="13"
    assert all(s["dur_s"] == 30 and s["lo"] == 1.25 for s in ons)
    offs = [s for s in segs if s["kind"] == "interval_off"]
    assert len(offs) == 13
    assert all(s["dur_s"] == 15 and s["lo"] == 0.6 for s in offs)
    # Absolute + contiguous, and the total matches the file's structure.
    t = 0
    for s in segs:
        assert s["start_s"] == t
        t += s["dur_s"]
    assert t == 3555
    # End-to-end on the real file: a perfect delivery grades clean.
    r = sf.score_structure(segs, _target_watts(segs), FTP)
    assert r["reps_prescribed"] == 20         # 13 reps + 5 hard steadies
    assert r["reps_delivered"] == 20          # + ramp + sprint
    assert r["rep_completion"] == 1.0
    assert r["mean_on_target_pct"] == 100.0


def test_freeride_advances_clock_without_target():
    segs = sf.parse_zwo_text("""<workout_file><workout>
        <SteadyState Duration="60" Power="1.0" />
        <FreeRide Duration="120" />
        <SteadyState Duration="60" Power="1.0" />
        </workout></workout_file>""")
    assert [s["kind"] for s in segs] == ["steady", "freeride", "steady"]
    assert segs[1]["lo"] is None and segs[1]["hi"] is None
    assert segs[2]["start_s"] == 180


# ── behavior: clean executions PASS ──────────────────────────────────────────

def test_perfect_square_scores_clean():
    r = sf.score_structure(_segs(), _target_watts(_segs()), FTP)
    assert r["reps_prescribed"] == 8
    assert r["reps_delivered"] == 8
    assert r["rep_completion"] == 1.0
    assert r["mean_on_target_pct"] == 100.0
    assert r["mean_power_ratio"] == 1.0
    assert r["alignment_offset_s"] == 0
    assert r["worst_segment"]["missing"] is False


def test_three_second_erg_lag_passes_clean():
    """Normal trainer step response is execution, not infidelity."""
    r = sf.score_structure(_segs(), _lagged(_target_watts(_segs()), 3), FTP)
    assert r["rep_completion"] == 1.0
    assert r["mean_on_target_pct"] >= 95.0
    assert abs(r["alignment_offset_s"]) <= 5


def test_global_trace_offset_is_recovered():
    """20 s of riding before the workout starts must not smear every rep."""
    watts = [50.0] * 20 + _target_watts(_segs())
    r = sf.score_structure(_segs(), watts, FTP)
    assert r["alignment_offset_s"] == 20
    assert r["mean_on_target_pct"] == 100.0
    assert r["rep_completion"] == 1.0


# ── behavior: structural failures DEGRADE ────────────────────────────────────

def test_heavy_smear_degrades_on_target():
    """12 s lag eating 30 s reps: complete but visibly unfaithful."""
    r = sf.score_structure(_segs(), _lagged(_target_watts(_segs()), 12), FTP)
    assert r["rep_completion"] == 1.0          # reps ridden, just smeared
    assert r["mean_on_target_pct"] < 85.0
    assert r["mean_power_ratio"] < 0.97        # lag eats the rep's front


def test_missing_final_rep_is_counted():
    segs = _segs()
    watts = _target_watts(segs)
    last = _on_windows(segs)[-1]
    for i in range(last["dur_s"]):              # soft-pedaled the last rep
        watts[last["start_s"] + i] = 0.50 * FTP
    r = sf.score_structure(segs, watts, FTP)
    assert r["reps_prescribed"] == 8
    assert r["reps_delivered"] == 7
    assert r["rep_completion"] == round(7 / 8, 3)
    assert r["worst_segment"]["missing"] is True
    assert r["worst_segment"]["index"] == 7


def test_rider_gave_up_mid_session():
    segs = _segs()
    watts = _target_watts(segs)[:432]           # stopped at 60 % of the plan
    r = sf.score_structure(segs, watts, FTP)
    assert r["reps_prescribed"] == 8
    assert r["reps_delivered"] == 4             # reps 5-8 never happened
    assert r["rep_completion"] == 0.5
    missing = [row for row in r["segments"] if row["missing"]]
    assert [row["index"] for row in missing] == [4, 5, 6, 7]


# ── None-honesty + determinism ───────────────────────────────────────────────

def test_none_when_nothing_gradeable():
    segs = _segs()
    watts = _target_watts(segs)
    assert sf.score_structure(segs, watts, 0) is None       # unusable FTP
    assert sf.score_structure(segs, watts, None) is None
    assert sf.score_structure(segs, [], FTP) is None        # no trace
    assert sf.score_structure(segs, None, FTP) is None
    assert sf.score_structure([], watts, FTP) is None       # no timeline
    assert sf.score_structure(None, watts, FTP) is None
    z2 = sf.parse_zwo_text(Z2_ZWO)                          # no work segments
    assert sf.score_structure(z2, [0.65 * FTP] * 2400, FTP) is None


def test_deterministic():
    segs = _segs()
    watts = _lagged(_target_watts(segs), 3)
    assert sf.score_structure(segs, watts, FTP) == \
        sf.score_structure(segs, watts, FTP)


# ── score_ride integration: additive + advisory ──────────────────────────────

def _tiz(**kw):
    t = {f"z{i}": 0 for i in range(1, 8)}
    t.update(kw)
    return t


def _planned(stype="vo2_short", dur=12, tss=20):
    return {"session_type": stype, "duration_min": dur, "tss_estimate": tss}


def _ride(**extra):
    r = {"duration_s": 720, "tss": 20,
         "time_in_zone": _tiz(z1=300, z2=120, z5=200, z6=100)}
    r.update(extra)
    return r


def test_score_ride_fidelity_none_without_inputs():
    r = es.score_ride(_planned(), _ride(), "power")
    assert "fidelity" in r
    assert r["fidelity"] is None


def test_score_ride_fidelity_via_kwargs_never_moves_score():
    segs = _segs()
    watts = _target_watts(segs)
    plain = es.score_ride(_planned(), _ride(), "power")
    fid = es.score_ride(_planned(), _ride(), "power",
                        planned_segments=segs, watts=watts, ftp=FTP)
    assert fid["fidelity"]["rep_completion"] == 1.0
    assert fid["fidelity"]["reps_prescribed"] == 8
    # Advisory: score/verdict/components identical with and without it.
    assert fid["score"] == plain["score"]
    assert fid["verdict"] == plain["verdict"]
    assert fid["components"] == plain["components"]


def test_score_ride_fidelity_from_embedded_streams_and_ftp_at_ride():
    segs = _segs()
    ride = _ride(streams={"watts": _target_watts(segs)}, ftp_at_ride=int(FTP))
    r = es.score_ride(_planned(), ride, "power", planned_segments=segs)
    assert r["fidelity"] is not None
    assert r["fidelity"]["reps_delivered"] == 8


def test_score_ride_fidelity_none_when_streams_absent():
    """Summary-only ICU records (no cached samples) stay honest: None."""
    segs = _segs()
    r = es.score_ride(_planned(), _ride(ftp_at_ride=250), "power",
                      planned_segments=segs)
    assert r["fidelity"] is None
