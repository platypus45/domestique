"""FIT builder hr target_mode contract (IP_HR_ONLY C8/C9).

C8: in hr mode, HR-guidable steps carry a real FIT HEART_RATE custom target
    (bpm encoded +100 per spec) and non-guidable steps are OPEN with the RPE
    cue in the step name.
C9 (reworded post-grill): power-mode output is unchanged except time_created —
    concretely, no step ever carries a custom_target_heart_rate_* field and
    every step still targets POWER.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fitparse = pytest.importorskip("fitparse")

import app  # noqa: E402
from hr_targets import HR_MIN_SEG_S  # noqa: E402

ZWO = ROOT / "workouts" / "threshold_2x3min-3min_95pct_56min.zwo"
LTHR, MAX_HR = 160, 185


def steps_of(fit_bytes):
    ff = fitparse.FitFile(fit_bytes)
    out = []
    for msg in ff.get_messages("workout_step"):
        out.append({f.name: f.value for f in msg.fields})
    return out


def build(monkeypatch, hr: bool):
    monkeypatch.setattr(app, "_fit_hr_mode", lambda: hr)
    monkeypatch.setattr(app, "_fit_hr_params", lambda: (LTHR, MAX_HR))
    return app._build_fit_workout_from_zwo("test", ZWO, ftp=250)


def test_power_mode_has_no_hr_fields(monkeypatch):
    steps = steps_of(build(monkeypatch, hr=False))
    assert steps, "no steps built"
    for s in steps:
        assert s.get("target_type") == "power"
        assert s.get("custom_target_heart_rate_low") is None
        assert s.get("custom_target_heart_rate_high") is None


def test_hr_mode_steady_blocks(monkeypatch):
    steps = steps_of(build(monkeypatch, hr=True))
    # 180 s @ 95% FTP → Z4 → HEART_RATE 95-105% LTHR, encoded bpm+100.
    z4 = [s for s in steps if s.get("target_type") == "heart_rate"
          and s.get("duration_time") == 180.0]
    assert z4, "no 180s Z4 HR step found"
    assert z4[0]["custom_target_heart_rate_low"] == round(0.95 * LTHR) + 100
    assert z4[0]["custom_target_heart_rate_high"] == round(1.05 * LTHR) + 100
    # 600 s @ 75% FTP → Z2 → 69-83% LTHR.
    z2 = [s for s in steps if s.get("target_type") == "heart_rate"
          and s.get("duration_time") == 600.0
          and s.get("custom_target_heart_rate_high") == round(0.83 * LTHR) + 100]
    assert z2, "no 600s Z2 HR step found"


def test_hr_mode_short_reps_are_open_rpe(monkeypatch):
    steps = steps_of(build(monkeypatch, hr=True))
    shorts = [s for s in steps if (s.get("duration_time") or 0) < HR_MIN_SEG_S
              and s.get("duration_time")]
    assert shorts, "expected sub-min-seg steps in this file"
    for s in shorts:
        assert s.get("target_type") == "open", s
        assert str(s.get("wkt_step_name", "")).startswith("RPE"), s
        assert s.get("custom_target_heart_rate_low") is None


def test_hr_mode_warmup_is_single_hr_ramp_step(monkeypatch):
    steps = steps_of(build(monkeypatch, hr=True))
    # Power mode staircases the 600 s warm-up into 8 sub-steps; hr mode must
    # keep it ONE step spanning 68%→83% LTHR (50→75% FTP endpoints).
    wu = [s for s in steps if s.get("duration_time") == 600.0
          and s.get("target_type") == "heart_rate"
          and s.get("custom_target_heart_rate_low") == round(0.68 * LTHR) + 100]
    assert wu, "warm-up not emitted as a single HR-range step"
    assert wu[0]["custom_target_heart_rate_high"] == round(0.83 * LTHR) + 100


def test_hr_mode_step_count_matches_header(monkeypatch):
    data = build(monkeypatch, hr=True)
    ff = fitparse.FitFile(data)
    n_steps = len(list(ff.get_messages("workout_step")))
    wk = next(ff.get_messages("workout"))
    declared = {f.name: f.value for f in wk.fields}.get("num_valid_steps")
    assert declared == n_steps


# ── red-team regressions ─────────────────────────────────────────────────────

def test_hr_mode_recovery_and_cooldown_have_no_bpm_floor(monkeypatch):
    """Red-team D4: rest/cooldown/Z1 steps must not carry a bpm FLOOR — HR
    decays slowly and idles low; a floored range makes the device beep at a
    rider doing recovery right. Floor is 1 bpm (raw 101), ceiling kept."""
    steps = steps_of(build(monkeypatch, hr=True))
    hr_steps = [s for s in steps if s.get("target_type") == "heart_rate"]
    # The cooldown ramp of this file descends into Z1 → intensity 'cooldown'.
    cooldowns = [s for s in steps if s.get("intensity") == "cooldown"
                 and s.get("target_type") == "heart_rate"]
    assert cooldowns, "no HR cooldown step found"
    for s in cooldowns:
        assert s["custom_target_heart_rate_low"] == 101  # 1 bpm + 100 = no floor
        assert s["custom_target_heart_rate_high"] > 101


def test_blocks_path_vo2_band_is_rpe_not_pinned_hr(monkeypatch):
    """Red-team F1 (HIGH): the synthetic blocks path fed work BANDS (VO2
    106-115) into the converter as ramps, emitting a 1-bpm HEART_RATE target
    pinned at threshold for 4-min VO2 intervals. Bands must convert as steady
    at the band top → steady Z5 → RPE OPEN step."""
    import app
    monkeypatch.setattr(app, "_fit_hr_mode", lambda: True)
    monkeypatch.setattr(app, "_fit_hr_params", lambda: (LTHR, MAX_HR))
    data = app.build_fit_workout_bytes("vo2max", 60, "vo2 test", None)
    steps = steps_of(data)
    vo2 = [s for s in steps if str(s.get("wkt_step_name", "")).startswith("RPE 8-9")]
    assert vo2, f"VO2 blocks did not degrade to RPE: {[s.get('wkt_step_name') for s in steps]}"
    for s in vo2:
        assert s.get("target_type") == "open"
    # No HEART_RATE step may be a pinned 1-bpm band at threshold.
    for s in steps:
        if s.get("target_type") == "heart_rate":
            lo, hi = s["custom_target_heart_rate_low"], s["custom_target_heart_rate_high"]
            assert hi - lo > 2 or lo == 101, f"pinned pseudo-range: {s}"


def test_blocks_path_threshold_band_is_steady_z4(monkeypatch):
    """F1 follow-up: a 20-min 95-100%% threshold BLOCK must convert as steady
    Z4 (full Coggan range), not as a narrow 'ramp' span."""
    import app
    monkeypatch.setattr(app, "_fit_hr_mode", lambda: True)
    monkeypatch.setattr(app, "_fit_hr_params", lambda: (LTHR, MAX_HR))
    data = app.build_fit_workout_bytes("threshold", 60, "thr test", None)
    steps = steps_of(data)
    z4 = [s for s in steps if s.get("target_type") == "heart_rate"
          and s.get("duration_time") == 1200.0]
    assert z4, "no 20-min threshold HR step"
    assert z4[0]["custom_target_heart_rate_low"] == round(0.95 * LTHR) + 100
    assert z4[0]["custom_target_heart_rate_high"] == round(1.05 * LTHR) + 100


def test_fit_uses_custom_prescription_rows(monkeypatch):
    """W1: FIT bpm comes from the athlete's custom rows when set."""
    import app
    OVR = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]}
    monkeypatch.setattr(app, "_fit_hr_mode", lambda: True)
    monkeypatch.setattr(app, "_fit_hr_params", lambda: (LTHR, MAX_HR, OVR))
    steps = steps_of(app._build_fit_workout_from_zwo("t", ZWO, ftp=250))
    z4 = [s for s in steps if s.get("target_type") == "heart_rate"
          and s.get("duration_time") == 180.0]
    assert z4 and z4[0]["custom_target_heart_rate_low"] == 155 + 100
    assert z4[0]["custom_target_heart_rate_high"] == 172 + 100
