"""v1.8.25 — match_zwo parity fixes (bring the fallback/reshuffle selector in
line with the v4.5 sampler). Three fixes:

  Fix 1  hard grey-zone gate on easy slots — a z2/recovery slot must not pull a
         file whose Z3+Z4+Z5+Z6 ≥ ceiling (40% z2, 25% recovery), which would
         over-cook the easy day and break polarization.
  Fix 2  bucket on ContentClass (the canonical type), not the Protocol heuristic.
  Fix 3  class-aware Score floor — endurance/recovery admitted at Score≥1 (they
         score low BY CONSTRUCTION) but guarded by Duration≥20min so the tiny
         steady stubs stay excluded; all other classes keep Score≥3.
"""
from datetime import date

import pytest

import training_planner as tp


def _z345(w):
    return sum(float(w.get(k, 0) or 0) for k in ("Z3%", "Z4%", "Z5%", "Z6%"))


@pytest.fixture(scope="module")
def lib():
    return tp.load_workout_library()


def _reshuffle(lib, session_type, slot, variation, exact=True):
    s = tp.PlannedSession(
        day=date(2026, 6, 15), day_name="Mon", session_type=session_type,
        duration_min=slot, tss_estimate=float(slot), description="",
    )
    s.profile_id = str(variation)
    tp.match_zwo(s, lib, week_num=variation * 100, day_idx=0,
                 used_names=set(), raise_on_empty=True, exact_duration=exact)
    # Look up by FILE only — display Names are generic and NOT unique, so a
    # File-or-Name match can return a different (same-Name) row than the actual
    # pick. s.zwo_file is the authoritative selection.
    return next((w for w in lib if w.get("File") == s.zwo_file), {})


# ── Fix 1: easy-slot grey-zone hard gate ────────────────────────────────────

@pytest.mark.parametrize("session_type,slot,ceiling",
                         [("z2", 60, 40), ("z2", 90, 40),
                          ("long_z2", 120, 40), ("recovery", 30, 25)])
def test_easy_slot_never_admits_greyzone(lib, session_type, slot, ceiling):
    for v in range(1, 21):
        try:
            w = _reshuffle(lib, session_type, slot, v)
        except tp.NoCandidateWorkoutError:
            continue
        assert _z345(w) < ceiling, (
            f"{session_type} {slot}min got {w.get('File')} z345={_z345(w):.1f} "
            f"≥ ceiling {ceiling} — over-cooks the easy day")


# ── Fix 3: low-Score endurance unlocked, but stubs stay out ─────────────────

def test_low_score_endurance_now_selectable_for_z2(lib):
    picks = []
    for v in range(1, 41):
        try:
            picks.append(_reshuffle(lib, "z2", 60, v))
        except tp.NoCandidateWorkoutError:
            pass
    assert picks, "no z2 picks"
    # at least one pick is a low-Score (<3) endurance file (the 614 unlocked)
    low = [w for w in picks if (w.get("Score") or 0) < 3]
    assert low, "low-Score endurance still excluded — Fix 3 floor not working"


def test_short_steady_stubs_never_selected(lib):
    """The <20-min Score-1 steady stubs must NOT be selectable for a real slot,
    even though Fix 3 lowers the endurance floor to Score≥1."""
    for st, slot in [("z2", 45), ("z2", 60), ("recovery", 30), ("long_z2", 90)]:
        for v in range(1, 16):
            try:
                w = _reshuffle(lib, st, slot, v)
            except tp.NoCandidateWorkoutError:
                continue
            assert (w.get("Duration(min)") or 0) >= 20, (
                f"{st} {slot}min selected a <20min stub {w.get('File')}")


# ── Fix 2: ContentClass-appropriate matching ────────────────────────────────

@pytest.mark.parametrize("session_type,allowed", [
    ("threshold", {"threshold", "threshold_ladder", "sweet_spot", "over_under"}),
    ("vo2max", {"vo2max", "vo2_short", "vo2_ladder", "anaerobic"}),
    ("sweetspot", {"sweet_spot", "sweet_spot_ladder", "threshold", "tempo"}),
    ("overunder", {"over_under", "threshold"}),
    ("sprint", {"neuromuscular", "anaerobic", "sprint"}),
])
def test_matches_content_class_appropriate(lib, session_type, allowed):
    for v in range(1, 13):
        try:
            w = _reshuffle(lib, session_type, 60, v)
        except tp.NoCandidateWorkoutError:
            continue
        cc = (w.get("ContentClass") or "").strip()
        assert cc in allowed, f"{session_type} got off-class {cc} ({w.get('File')})"


# ── ftp_test path untouched ─────────────────────────────────────────────────

def test_ftp_test_still_returns_tagged_test(lib):
    s = tp.PlannedSession(day=date(2026, 6, 15), day_name="Tue",
                          session_type="ftp_test", duration_min=60,
                          tss_estimate=60.0, description="")
    tp.match_zwo(s, lib, week_num=1, day_idx=0, used_names=set(), raise_on_empty=True)
    w = next((x for x in lib if x.get("File") == s.zwo_file), {})
    tags = [t.lower() for t in (w.get("Tags") or [])]
    assert "ftp_test" in tags, f"ftp_test slot got non-test file {s.zwo_file}"


# ── exact_duration still honoured after the new gates ───────────────────────

def test_exact_duration_still_closest_tier(lib):
    """The v1.8.24 closest-duration collapse must still hold under the new
    gates: a 90-min z2 reshuffle returns ~90-min files, never far ones."""
    for v in range(1, 16):
        w = _reshuffle(lib, "z2", 90, v)
        assert abs((w.get("Duration(min)") or 0) - 90) <= 10, (
            f"z2 90min got {w.get('Duration(min)')}min {w.get('File')}")
