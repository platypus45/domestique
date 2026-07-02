"""Contract tests for hr_targets.power_target_to_hr (IP_HR_ONLY C1-C6, C14, C16).

The converter is the single source of truth for HR-only prescription: the
workout-detail API, both FIT builders and the dashboard consume its output, so
these boundaries ARE the product behaviour.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hr_targets import (DRIFT_NOTE, HR_DRIFT_S, HR_MIN_SEG_S,
                        power_target_to_hr, zone_of_pct)

LTHR, MAX_HR = 160, 185


def conv(pct, dur=600, pct_end=None, lthr=LTHR, max_hr=MAX_HR):
    return power_target_to_hr(pct, pct if pct_end is None else pct_end,
                              dur, lthr, max_hr)


# ── C1: pure/deterministic ───────────────────────────────────────────────────

def test_deterministic():
    for pct in (40, 65, 88, 100, 110, 130, 160):
        assert conv(pct) == conv(pct)


# ── C2: guidable zones → Coggan-table bpm ranges ────────────────────────────

def test_z2_endurance_range_matches_coggan():
    r = conv(65)
    assert r["kind"] == "hr" and r["zone"] == 2
    assert r["bpm_low"] == round(0.69 * LTHR)   # 110
    assert r["bpm_high"] == round(0.83 * LTHR)  # 133
    assert r["bpm_low"] < r["bpm_high"]


def test_z4_threshold_range_matches_coggan():
    r = conv(100)
    assert r["kind"] == "hr" and r["zone"] == 4
    assert r["bpm_low"] == round(0.95 * LTHR)
    assert r["bpm_high"] == round(1.05 * LTHR)


def test_zone_boundaries():
    # zones.py _POWER_FRACS uppers: 55/75/90/105/120/150
    assert [zone_of_pct(p) for p in (55, 56, 75, 76, 90, 91, 105, 106, 120, 121, 150, 151)] == \
           [1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7]


def test_max_hr_clamp_preserves_order():
    # C16: LTHR 180 / max_hr 182 — Z4 high (189) must clamp to 182, low follows.
    r = conv(100, lthr=180, max_hr=182)
    assert r["bpm_high"] == 182
    assert r["bpm_low"] <= r["bpm_high"]


# ── C3: Z6/Z7 always RPE, any duration ───────────────────────────────────────

def test_z6_z7_always_rpe():
    for pct in (125, 150, 200):
        for dur in (20, 300, 3600):
            r = conv(pct, dur)
            assert r["kind"] == "rpe" and r["reason"] == "supra_threshold"
            assert "bpm_low" not in r


def test_z7_sprint_rpe_is_max():
    r = conv(200, 15)
    assert (r["rpe_low"], r["rpe_high"]) == (10, 10)


# ── C4: short segments → RPE even when aerobic ──────────────────────────────

def test_short_z2_is_rpe():
    r = conv(65, HR_MIN_SEG_S - 1)
    assert r["kind"] == "rpe" and r["reason"] == "short" and r["zone"] == 2


def test_min_seg_boundary_is_hr():
    assert conv(65, HR_MIN_SEG_S)["kind"] == "hr"


# ── C5 (locked D-b): steady Z5 → RPE, never a pseudo-range ───────────────────

def test_steady_z5_is_rpe():
    for dur in (120, 600, 1800):
        r = conv(110, dur)
        assert r["kind"] == "rpe" and r["zone"] == 5


# ── C6: drift note on long steady holds ──────────────────────────────────────

def test_drift_flag():
    long_hold = conv(65, HR_DRIFT_S)
    assert long_hold["drift"] is True and long_hold["note"] == DRIFT_NOTE
    assert conv(65, HR_DRIFT_S - 1)["drift"] is False


# ── C14: ramps ────────────────────────────────────────────────────────────────

def test_warmup_ramp_maps_both_ends():
    # 25% → 75% FTP warm-up: start clamps to the 68% LTHR easy ceiling,
    # end hits the Z2 top anchor (83% LTHR).
    r = conv(25, 600, pct_end=75)
    assert r["kind"] == "hr_ramp" and r["capped"] is False
    assert r["bpm_start"] == round(0.68 * LTHR)
    assert r["bpm_end"] == round(0.83 * LTHR)
    assert r["bpm_start"] < r["bpm_end"]


def test_cooldown_ramp_descends():
    r = conv(75, 600, pct_end=25)
    assert r["kind"] == "hr_ramp"
    assert r["bpm_start"] > r["bpm_end"]  # time order honoured


def test_z5_ramp_end_caps_at_threshold_hr():
    # 90% → 115% ramp: end is Z5 → capped at 105% LTHR, flagged.
    r = conv(90, 600, pct_end=115)
    assert r["kind"] == "hr_ramp" and r["capped"] is True
    assert r["bpm_end"] == min(round(1.05 * LTHR), MAX_HR)


def test_hard_ramp_is_rpe():
    # 110% → 130% (Z6 end) — not mappable, whole ramp is RPE (the 606
    # hard-<Ramp> files from the grill).
    r = conv(110, 120, pct_end=130)
    assert r["kind"] == "rpe" and r["reason"] == "supra_threshold" and r["zone"] == 6


def test_short_ramp_is_rpe():
    r = conv(25, 60, pct_end=75)
    assert r["kind"] == "rpe" and r["reason"] == "short"


def test_ramp_interpolation_midpoint():
    # 82.5% FTP sits midway between the (75,83) and (90,94) anchors →
    # LTHR fraction midway too (88.5%).
    r = conv(55, 600, pct_end=82.5)
    assert r["bpm_end"] == round(0.885 * LTHR)


# ── red-team regressions ─────────────────────────────────────────────────────

def test_float_dust_at_55_boundary_stays_z1():
    """Red-team D1: float("0.55")*100 == 55.000000000000001 misclassified the
    ubiquitous 55%-FTP recovery block as Z2 (wrong bpm floor on 2,557 segments
    across 1,457 library files). zone_of_pct must round away IEEE dust."""
    dirty = float("0.55") * 100
    assert dirty != 55  # the trap is real
    assert zone_of_pct(dirty) == 1
    r = conv(dirty, 600)
    assert r["zone"] == 1


def test_rpe_rows_pinned():
    """Red-team D5: the zone→RPE cues are product behaviour — pin them so a
    silent edit can't shift effort guidance."""
    assert (conv(65, 60)["rpe_low"], conv(65, 60)["rpe_high"]) == (2, 3)      # Z2 short
    assert (conv(100, 60)["rpe_low"], conv(100, 60)["rpe_high"]) == (6, 7)    # Z4 short
    assert (conv(110, 600)["rpe_low"], conv(110, 600)["rpe_high"]) == (8, 9)  # Z5
    assert (conv(130, 600)["rpe_low"], conv(130, 600)["rpe_high"]) == (9, 10) # Z6
    assert (conv(200, 15)["rpe_low"], conv(200, 15)["rpe_high"]) == (10, 10)  # Z7


# ── W1 (v2.5.0): custom prescription rows override ───────────────────────────

def test_custom_rows_override_steady_and_axis_consistency():
    ovr = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]}
    r = conv(65, 600)  # default
    r2 = power_target_to_hr(65, 65, 600, LTHR, MAX_HR, hr_rows_override=ovr)
    assert (r2["bpm_low"], r2["bpm_high"]) == (125, 138)
    assert (r["bpm_low"], r["bpm_high"]) != (125, 138)  # default differs
    z4 = power_target_to_hr(100, 100, 600, LTHR, MAX_HR, hr_rows_override=ovr)
    assert (z4["bpm_low"], z4["bpm_high"]) == (155, 172)


def test_custom_rows_ramp_agrees_with_rows():
    """Ramp endpoints at the Coggan %FTP boundaries must equal the custom
    zone tops — steady and ramp can never disagree (W1 invariant)."""
    ovr = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]}
    wu = power_target_to_hr(25, 75, 600, LTHR, MAX_HR, hr_rows_override=ovr)
    assert wu["kind"] == "hr_ramp"
    assert wu["bpm_start"] == 120 and wu["bpm_end"] == 138
    hard = power_target_to_hr(90, 105, 600, LTHR, MAX_HR, hr_rows_override=ovr)
    assert hard["bpm_end"] == 172


def test_custom_rows_none_is_byte_identical_to_default():
    """C1+: no override (or malformed override) == Coggan defaults exactly."""
    for pct in (40, 65, 88, 100):
        assert power_target_to_hr(pct, pct, 600, LTHR, MAX_HR, hr_rows_override=None) == conv(pct, 600)
        assert power_target_to_hr(pct, pct, 600, LTHR, MAX_HR, hr_rows_override={"bogus": 1}) == conv(pct, 600)


def test_custom_rows_clamped_to_max_hr():
    ovr = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [180, 200]}
    z4 = power_target_to_hr(100, 100, 600, LTHR, max_hr=185, hr_rows_override=ovr)
    assert z4["bpm_high"] == 185 and z4["bpm_low"] <= z4["bpm_high"]


def test_custom_rows_never_affect_rpe_carveouts():
    """Z5+/short stay RPE regardless of custom rows (power-zone rule)."""
    ovr = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]}
    assert power_target_to_hr(110, 110, 600, LTHR, MAX_HR, hr_rows_override=ovr)["kind"] == "rpe"
    assert power_target_to_hr(65, 65, 60, LTHR, MAX_HR, hr_rows_override=ovr)["kind"] == "rpe"
