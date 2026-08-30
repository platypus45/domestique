"""v1.0.6 — `_detect_interval_signature` SteadyState-pair branch reliability.

The steady-pair fallback used to key candidate cycles on the full 4-tuple
(on_power, off_power, on_s, off_s) and return the single most-common pair. Two
failure modes:
  * off-power / off-duration drifting between cycles fragmented one real set
    into several keys -> undercount;
  * a set ending on its final hard effort (no trailing recovery) dropped that
    last rep.
Fix: group candidate on-blocks by the ON SHAPE only -- (on_s, round(on_power,2))
-- sum reps of the dominant shape, and count a matching dangling final work
block. Mirrors the IntervalsT branch.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from classify_library_content import parse_zwo_full, _detect_interval_signature  # noqa: E402

WORKOUTS = Path(__file__).resolve().parent.parent / "workouts"


def _sig_for(name):
    *_, segs = parse_zwo_full(WORKOUTS / name)
    return _detect_interval_signature(segs)


def test_steady_pair_real_file_counts_all_five():
    """(a) anaerobic_5x3min_55min: 5×180s@120% SteadyState; the 5th interval has
    no trailing recovery. Must report 5, not 4."""
    sig = _sig_for("anaerobic_5x3min-5min_120pct_57min.zwo")
    assert sig is not None, "expected an interval signature"
    reps, on_s, _off_s, on_p = sig
    assert reps == 5, f"expected 5 reps, got {reps}"
    assert on_s == 180 and round(on_p, 2) == 1.20


def test_steady_pair_varying_off_groups_to_one_shape():
    """(b) 5 identical ON (180s@1.20) with OFF power+duration drifting every
    cycle must group to a single 5-rep shape, not fragment."""
    segs = [{"kind": "steady", "duration_s": 300, "power": 0.50}]
    for off_p, off_d in [(0.50, 300), (0.45, 280), (0.55, 300), (0.48, 320), (0.50, 300)]:
        segs.append({"kind": "steady", "duration_s": 180, "power": 1.20})
        segs.append({"kind": "steady", "duration_s": off_d, "power": off_p})
    sig = _detect_interval_signature(segs)
    assert sig is not None
    reps, on_s, _off_s, on_p = sig
    assert reps == 5, f"expected 5 grouped reps, got {reps}"
    assert on_s == 180 and round(on_p, 2) == 1.20


def test_steady_pair_single_sustained_block_is_not_reps():
    """A single sustained block must not be read as an interval rep count."""
    segs = [{"kind": "steady", "duration_s": 300, "power": 0.50},
            {"kind": "steady", "duration_s": 1200, "power": 0.90}]
    assert _detect_interval_signature(segs) is None


def test_steady_pair_over_under_dominant_alternation():
    """over_under_1min_10x_64min: dominant alternation must be captured (the
    old off<0.75 gate skipped it and latched a minor sub-section, reps=3).

    Re-pinned after the W′-feasibility wave (1de456dd): the overs were
    scaled 1.05 → 1.02 to bring the file's tank demand feasible; the
    signature contract (dominant alternation, ~10 reps) is unchanged — only
    the pinned on-power moved with the amendment."""
    sig = _sig_for("over_under_2x5x1min_102pct_64min.zwo")
    assert sig is not None
    reps, _on_s, _off_s, on_p = sig
    assert reps >= 9, f"expected ~10 OU reps, got {reps}"
    assert round(on_p, 2) == 1.02


def test_steady_pair_sweet_spot_wobble_is_not_over_under():
    """A sweet-spot block with a small dip (0.90 on / 0.80 off) is NOT an
    over-under (over-leg < 0.95) and must not be counted as interval reps."""
    segs = [{"kind": "steady", "duration_s": 300, "power": 0.50}]
    for _ in range(4):
        segs += [{"kind": "steady", "duration_s": 600, "power": 0.90},
                 {"kind": "steady", "duration_s": 120, "power": 0.80}]
    assert _detect_interval_signature(segs) is None
