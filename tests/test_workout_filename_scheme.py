"""The library filename scheme, pinned.

Every .zwo is named ``<class>_<structure>[_<pct>pct]_<duration>min[_vN].zwo``
(scripts/rename_workouts_structural.py). The scheme is not cosmetic: the class
token is what both filename-fallback ladders match on when content
classification is unavailable, so a truncated class token silently
reclassifies the file.

That is not hypothetical. Building this scheme truncated ``supra_threshold_*``
to ``supra_*`` twice — once for every multi-word prefix, once for this one —
and 32 threshold sessions fell through both ladders to "mixed". The class
vocabulary test below is the check that catches it, and it is a whole-library
check because the truncation only shows up on the handful of files whose class
name contains an underscore.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKOUTS = ROOT / "src" / "workouts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "scripts"))

_REP = r"\d+x\d+(?:s|min)(?:-\d+(?:s|min))?"        # 4x8min-4min, 13x30s
_SERIES = r"\d+x" + _REP                            # 3x13x30s-15s (Ronnestad)
NAME_RE = re.compile(
    r"^(?P<cls>[a-z0-9]+(?:_[a-z0-9]+)*?)"
    r"_(?P<struct>steady|ladder\d+|" + _SERIES + r"|" + _REP + r")"
    r"(?:_(?P<pct>\d+)pct)?"
    r"_(?P<dur>\d+)min(?:_v\d+)?\.zwo$")

# Every class token the library is allowed to use. Adding one is a deliberate
# act: it must also be recognised by classify_library_content.filename_classify
# and training_planner._classify_protocol, or the file classifies as "mixed".
CLASSES = {
    "anaerobic", "endurance", "ftp_test", "ftp_test_ramp", "intervals",
    "neuromuscular", "over_under", "pyramid", "recovery", "sprints",
    "supra_threshold", "sweet_spot", "sweetspot", "tempo", "tempo_intervals",
    "threshold", "vo2", "vo2_short", "vo2max", "workout", "z2",
}


def _names() -> list[str]:
    return sorted(p.name for p in WORKOUTS.glob("*.zwo"))


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
def test_every_filename_matches_the_scheme():
    bad = [n for n in _names() if not NAME_RE.match(n)]
    assert bad == [], f"{len(bad)} off-scheme names, first: {bad[:5]}"


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
def test_class_tokens_come_from_the_declared_vocabulary():
    """A class token outside the vocabulary is a truncated prefix.

    Longest-match, because "supra_threshold" and "threshold" are both real and
    the greedy read of "supra_threshold_4x8min..." must not stop at "supra".
    """
    seen: dict[str, str] = {}
    for n in _names():
        cls = max((c for c in CLASSES if n.startswith(c + "_")),
                  key=len, default=None)
        if cls is None:
            seen[n] = n.split("_")[0]
    assert seen == {}, (
        f"{len(seen)} files carry a class token that is not in CLASSES "
        f"(a truncated multi-word prefix looks exactly like this): "
        f"{sorted(seen)[:5]}")


# Markers the filename carries as a CONTRACT, with the consumer that reads
# each one and the count the library held when the scheme was pinned. These
# are not descriptions: capacity_cap exempts a ramp test from power capping by
# finding "ftp_test_ramp" in the name, and fitness_estimation returns
# "coggan_20min" authoritatively on "ftp_test_coggan". A rename that drops one
# turns a maximal test into an ordinary workout — capped, and no longer
# recognised as a test — and every test above still passes while it happens.
PROTOCOL_MARKERS = {
    "ftp_test_ramp": 3,        # capacity_cap.is_ramp_test_name
    "ftp_test_coggan": 3,      # fitness_estimation.detect_ftp_test_shape
    "vo2max_short": 10,        # training_planner micro-interval routing
    "supra_threshold": 32,     # both filename-fallback ladders
    "_progression_": 34,       # tests/test_tempo_workout_shape ramp intent
}


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
@pytest.mark.parametrize("marker,floor", sorted(PROTOCOL_MARKERS.items()))
def test_protocol_markers_survive_in_the_library(marker, floor):
    n = sum(1 for name in _names() if marker in name)
    assert n >= floor, (
        f"only {n} files carry the '{marker}' marker, expected >= {floor}. "
        f"A rename dropped it; the consumer that reads this marker now sees "
        f"ordinary workouts.")


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
def test_the_marker_consumers_still_recognise_their_files():
    """Pin the markers through the functions that read them, not just as text."""
    import capacity_cap
    import fitness_estimation as fe

    ramps = [n for n in _names() if capacity_cap.is_ramp_test_name(n)]
    assert len(ramps) >= 3, f"ramp-test exemption matches {len(ramps)} files"
    coggans = [n for n in _names()
               if fe.detect_ftp_test_shape([100] * 300, n) == "coggan_20min"]
    assert len(coggans) >= 3, f"Coggan-20 tag matches {len(coggans)} files"


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
def test_no_class_token_loses_meaning_in_the_fallback_ladder():
    """The filename ladder must still recognise every name in the library.

    ``neuromuscular_`` and ``workout_`` are the two prefixes the ladder has
    never matched — they predate this scheme and are excluded here rather than
    silently fixed, because widening the ladder reclassifies live files.
    """
    from classify_library_content import filename_classify

    unmatched = [n for n in _names()
                 if filename_classify(n) == "mixed"
                 and not n.startswith(("pyramid_", "intervals_",
                                       "neuromuscular_", "workout_"))]
    assert unmatched == [], (
        f"{len(unmatched)} names fall through the ladder to 'mixed': "
        f"{unmatched[:5]}")


@pytest.mark.skipif(not WORKOUTS.is_dir(), reason="no library checked out")
def test_names_are_unique_case_insensitively():
    """macOS and Windows filesystems fold case; the git index does not."""
    lowered: dict[str, str] = {}
    clashes = []
    for n in _names():
        prev = lowered.setdefault(n.lower(), n)
        if prev != n:
            clashes.append((prev, n))
    assert clashes == [], f"case-folding collisions: {clashes[:5]}"
