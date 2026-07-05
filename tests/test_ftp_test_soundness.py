"""v3.2.0 — FTP-test detection + calc soundness (ramp best-minute from the
ramp, not a stray sprint; a non-ramp isn't scored as a ramp). These functions
had ZERO coverage before; these are the load-bearing invariants."""
from __future__ import annotations

import fitness_estimation as fe


def _ramp(warmup_sprint=False, cooldown=True):
    """A 100→400W ramp in 1-min steps. Optionally a warmup spike-and-coast and
    a cooked ~30W cooldown free-ride."""
    s = [120] * 60
    if warmup_sprint:
        s += [800] * 30 + [100] * 30      # spike-and-coast minute
    for step in range(100, 420, 20):
        s += [step] * 60                  # flat 1-min steps to 400
    if cooldown:
        s += [30] * 120                   # cooked: free-ride, or stop
    return s


def test_ramp_scores_off_the_ramp_not_a_warmup_sprint():
    r = fe.ramp_test_ftp(_ramp(warmup_sprint=True))
    # best sustained minute is the 400W ramp top → 0.75×400 = 300.
    # The 800W/30s sprint minute (mean ~450) is rejected by the flatness guard.
    assert r["best_60s"] == 400
    assert r["value"] == 300


def test_ramp_cooldown_freeride_never_wins():
    # No cooldown guard needed — 30W can't be the best minute (you're cooked).
    assert fe.ramp_test_ftp(_ramp(cooldown=True))["value"] == 300


def test_clean_freeform_ramp_detects():
    assert fe.detect_ftp_test_shape(_ramp(warmup_sprint=False)) == "ramp"


def test_4x8_threshold_ride_is_not_a_ramp():
    t = [100] * 60 + ([260] * 480 + [120] * 180) * 4
    assert fe.detect_ftp_test_shape(t) is None


def test_filename_tag_is_authoritative():
    assert fe.detect_ftp_test_shape([100] * 300, "ftp_test_ramp_20w.zwo") == "ramp"
    assert fe.detect_ftp_test_shape([100] * 300, "ftp_test_coggan_20min.zwo") == "coggan_20min"


def test_coggan_mean_not_np_and_finish_sprint_capped():
    # 20-min @280W arithmetic mean + a 700W/15s finish kick: FTP off the
    # sustained plateau (~0.95×280 = 266), not the sprint-inflated window.
    series = [280] * 1185 + [700] * 15
    out = fe.coggan_20min_ftp(series)
    assert out is not None
    assert 262 <= out["value"] <= 268   # ~266, kick trimmed; not ~271
