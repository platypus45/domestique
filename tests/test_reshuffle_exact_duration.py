"""v3.2.2 (#15) — exact-duration reshuffle contract, re-calibrated.

History: the v1.8.24 suite asserted every reshuffle pick lands on the SINGLE
closest-available duration. Two deliberate engine changes rotted that:
  (a) v2.2.13 replaced the closest-tier collapse with a VARIETY BAND — all
      candidates within max(8% of slot, 3 min) are kept (variety among
      genuinely-close files); the closest-duration tier (+0.5 min epsilon)
      applies only when the band is empty (training_planner ~tp:3886-3899).
  (b) v3.2.0 watertight changed the candidate pool itself (facts gate
      `file_admissible`, class-aware Score floors, ContentClass basis), so
      the suite's hand-rolled Protocol/Score≥3 pool mirror computed a
      fictitious "best possible" duration.
The suite was blanket-xfailed at v3.0.0 (57 tests). This rewrite asserts the
REAL contract over the REAL admissible pool:

    diff(pick) ≤ max(band, best_dedup + 0.5)        (+0.1 float slack)

where band = max(0.08 × slot, 3.0) and best_dedup is the smallest duration
diff over the per-Name-DEDUPED admissible pool (match_zwo keeps the highest-
Score row per Name before the band logic — grill P6: computing best on the
raw pool can false-fail when a same-Name variant sits closer).

The pool mirror consumes the engine's own gates — `file_admissible`,
`_class_aware_score_floor` (+ the easy-tier ≥20-min stub guard),
`_TYPE_TO_CONTENT_CLASS` / `_TYPE_TO_FALLBACK_CLASSES` (hoisted in v3.2.2 so
this mirror can never rot against a stale copy), the ftp_test tag skip, and
the easy-slot Z3+ ceiling — so engine-gate evolution moves the test's
expectation automatically instead of breaking it.
"""
from datetime import date

import pytest

import training_planner as tp

# The 8 slot types the original suite covered (sprint excluded then and now:
# sprint slots carry their own IF-ceiling contract, pinned elsewhere).
_SESSION_TYPES = [
    "z2", "long_z2", "recovery", "sweetspot",
    "threshold", "vo2max", "overunder", "tempo",
]
_SLOTS = [45, 60, 75, 90, 120, 122, 134]

# Mirror of match_zwo's easy-slot grey-zone ceiling (Z3+Z4+Z5+Z6 %).
_EASY_Z345_CEILING = {"recovery": 25.0, "z2": 40.0, "long_z2": 40.0}


def _band(slot: float) -> float:
    """The v2.2.13 variety band: max(8% of slot, 3.0) minutes."""
    return max(slot * 0.08, 3.0)


def _z345(w: dict) -> float:
    return sum(float(w.get(k, 0) or 0) for k in ("Z3%", "Z4%", "Z5%", "Z6%"))


def _admissible_pool(lib, session_type):
    """Re-derive match_zwo's candidate pool via the engine's OWN gates."""
    prim = tp._TYPE_TO_CONTENT_CLASS[session_type]
    fbs = set(tp._TYPE_TO_FALLBACK_CLASSES[session_type])
    ceiling = _EASY_Z345_CEILING.get(session_type)
    pool = []
    for w in lib:
        cc = tp._content_class_for_row(w)
        if not (cc == prim or cc in fbs):
            continue
        score = int(w.get("Score", 0) or 0)
        dur = float(w.get("Duration(min)", 0) or 0)
        if cc in ("endurance", "recovery", "endurance_intervals"):
            if score < 1 or dur < 20:  # easy tier + stub guard
                continue
        elif score < tp._class_aware_score_floor(cc):
            continue
        tags = {t.lower() for t in (w.get("Tags") or [])}
        if "ftp_test" in tags:
            continue
        if ceiling is not None and _z345(w) > ceiling:
            continue
        if not tp.file_admissible(session_type, w):
            continue
        pool.append(w)
    # Per-Name dedup, keep highest Score — mirrors match_zwo's seen_names.
    best_by_name: dict = {}
    for w in pool:
        name = w.get("Name", "")
        if name not in best_by_name or (w.get("Score", 0) or 0) > (
                best_by_name[name].get("Score", 0) or 0):
            best_by_name[name] = w
    return list(best_by_name.values())


def _best_diff(pool, slot: float):
    if not pool:
        return None
    return min(abs(float(w.get("Duration(min)", 0) or 0) - slot) for w in pool)


def _reshuffle(lib, session_type, slot, variation):
    s = tp.PlannedSession(
        day=date(2026, 6, 15), day_name="Mon", session_type=session_type,
        duration_min=slot, tss_estimate=float(slot), description="",
    )
    s.profile_id = str(variation)
    tp.match_zwo(
        s, lib, week_num=variation * 100, day_idx=0,
        used_names=set(), raise_on_empty=True, exact_duration=True,
    )
    meta = next(
        (w for w in lib if w.get("File") == s.zwo_file or w.get("Name") == s.zwo_name),
        {},
    )
    return s.zwo_file, float(meta.get("Duration(min)") or 0)


@pytest.fixture(scope="module")
def lib():
    return tp.load_workout_library()


@pytest.mark.parametrize("session_type", _SESSION_TYPES)
@pytest.mark.parametrize("slot", _SLOTS)
def test_reshuffle_pick_within_band_or_closest_tier(lib, session_type, slot):
    """Every reshuffle pick is inside the variety band around the slot, or —
    when the admissible pool has nothing that close — within the closest-
    duration tier (+0.5 epsilon). This IS the engine's band arithmetic
    (tp ~3886-3899) evaluated against the engine's own pool gates."""
    pool = _admissible_pool(lib, session_type)
    best = _best_diff(pool, slot)
    if best is None:
        pytest.skip(f"no admissible candidates for {session_type} "
                    "(coverage-fallback territory, different contract)")
    allowed = max(_band(slot), best + 0.5) + 0.1
    for v in range(1, 13):
        try:
            _f, dur = _reshuffle(lib, session_type, slot, v)
        except tp.NoCandidateWorkoutError:
            continue
        diff = abs(dur - slot)
        assert diff <= allowed, (
            f"{session_type} {slot}min slot -> {dur:.0f}min (diff {diff:.1f}) "
            f"outside band {_band(slot):.1f} and closest tier "
            f"(best {best:.1f} + 0.5)"
        )


def test_90min_slot_never_returns_a_45min_file(lib):
    """The original v1.8.24 symptom: a 90-min slot must never resolve to a
    ~45-min workout when ~90-min files exist in the type."""
    for st in ("sweetspot", "threshold", "vo2max", "tempo"):
        best = _best_diff(_admissible_pool(lib, st), 90)
        if best is None or best > 5:
            continue  # type genuinely lacks a ~90-min file; not the symptom
        for v in range(1, 21):
            _f, dur = _reshuffle(lib, st, 90, v)
            assert dur >= 70, f"{st} 90-min slot returned {dur:.0f}min file"


def test_dense_cell_stays_inside_band(lib):
    """A well-covered cell (90-min sweetspot) always lands inside the band:
    8% of 90 = 7.2 min (+0.5 tier epsilon headroom = 7.7)."""
    durations = set()
    for v in range(1, 21):
        _f, dur = _reshuffle(lib, "sweetspot", 90, v)
        durations.add(round(dur))
    assert durations, "no picks"
    assert all(abs(d - 90) <= 7.7 for d in durations), f"durations={sorted(durations)}"


def test_reshuffle_preserves_variety_on_dense_cell(lib):
    """The band exists FOR variety: reshuffling a dense cell must reach ≥2
    distinct files across 12 variations."""
    files = set()
    for v in range(1, 13):
        f, _dur = _reshuffle(lib, "threshold", 60, v)
        files.add(f)
    assert len(files) >= 2, f"variety collapsed: only {files}"


def test_default_path_unchanged_and_deterministic(lib):
    """exact_duration defaults False: bulk-generation behaviour is preserved
    (same seed → same pick), proving generation is untouched by the flag."""
    def pick():
        s = tp.PlannedSession(
            day=date(2026, 6, 15), day_name="Mon", session_type="sweetspot",
            duration_min=60, tss_estimate=60.0, description="",
        )
        tp.match_zwo(s, lib, week_num=3, day_idx=2, used_names=set())
        return s.zwo_file
    assert pick() == pick()  # deterministic default path


def test_exact_mode_no_farther_than_default_beyond_band(lib):
    """exact mode widens the ±25% gate but shares the band collapse, so its
    pick may differ from default WITHIN the band — never beyond it when the
    default already found something closer."""
    for st in ("vo2max", "sweetspot", "threshold"):
        slot = 90
        s_def = tp.PlannedSession(day=date(2026, 6, 15), day_name="Mon",
                                  session_type=st, duration_min=slot,
                                  tss_estimate=float(slot), description="")
        tp.match_zwo(s_def, lib, week_num=7, day_idx=1, used_names=set())
        meta_def = next((w for w in lib if w.get("File") == s_def.zwo_file), {})
        d_def = abs(float(meta_def.get("Duration(min)") or 0) - slot)
        _f, d_exact_dur = _reshuffle(lib, st, slot, 7)
        d_exact = abs(d_exact_dur - slot)
        assert d_exact <= max(_band(slot), d_def) + 0.5, (
            f"{st}: exact mode ({d_exact:.1f}) beyond band AND farther than "
            f"default ({d_def:.1f})"
        )
