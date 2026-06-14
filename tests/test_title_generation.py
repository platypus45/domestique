"""Wave-2A tests for the truthful title generator (`scripts/title_from_zwo`).

These pin the four Wave-0 gates:

* Gate A — interval reps are SUMMED across recovery-separated blocks (the 15x /
  18x / 20x / 10x verified multi-block cases), and the 417 already-correct
  ``Nx`` names are reproduced, not undercounted.
* Gate B — non-uniform structures emit an EXISTING descriptor word
  (ramp / pyramid / mixed / progression / steady).
* Gate C — the class token is confidence-gated: conf < 0.6 yields a SOFT token,
  never a precise pattern claim (``over_under`` / ``vo2_short`` / ``anaerobic``).
* Multi-token classes (``over_under`` / ``sweet_spot`` / ``vo2_short``) are kept
  intact — never ``split('_')[0]``.

The class entry is produced by a fresh ``classify_zwo_v104`` so the tests run
against the current (Gate-A + Gate-D) classifier and need no on-disk JSON.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKOUTS = REPO_ROOT / "workouts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import classify_library_content as clc  # noqa: E402
from title_from_zwo import CONF_GATE, title_from_zwo  # noqa: E402

# Precise pattern class tokens a SOFT (low-confidence) title must never claim.
PRECISE_PATTERN_TOKENS = {
    "over_under", "vo2_short", "anaerobic", "vo2max",
    "threshold_ladder", "sweet_spot_ladder", "vo2_ladder", "tempo_ladder",
}

# Multi-token canonical classes that must survive whole into the title.
MULTI_TOKEN_CLASSES = {"over_under", "sweet_spot", "vo2_short",
                       "tempo_intervals", "supra_threshold"}


def _entry(fname: str) -> dict:
    return clc.classify_zwo_v104(WORKOUTS / fname)


def _name(fname: str) -> str:
    p = WORKOUTS / fname
    return title_from_zwo(p, _entry(fname))[0]


def _reps_in(name: str) -> int | None:
    """Pull the leading rep count out of an ``…_{N}x{ON}{unit}_…`` token."""
    m = re.search(r"_(\d+)x\d+(?:min|s)_", name)
    return int(m.group(1)) if m else None


def _require(fname: str):
    if not (WORKOUTS / fname).exists():
        pytest.skip(f"fixture {fname} not present in workouts/")


# ── (a) Gate A — verified multi-block rep sums ────────────────────────────────

@pytest.mark.parametrize("fname,expected_reps", [
    ("anaerobic_1min_15x_72min.zwo", 15),
    ("anaerobic_1min_18x_97min.zwo", 18),
    ("anaerobic_1min_20x_96min.zwo", 20),
    ("over_under_10x3min_80min.zwo", 10),
    ("anaerobic_10x3min_90min.zwo", 10),
])
def test_gate_a_multiblock_rep_sum(fname, expected_reps):
    """Reps must SUM across recovery-separated identical blocks, not report a
    single dominant block (the pre-fix parser emitted 5/6/10/5)."""
    _require(fname)
    name = _name(fname)
    assert _reps_in(name) == expected_reps, (
        f"{fname} -> {name}: expected {expected_reps}x summed reps")


def test_gate_a_signature_sums_directly():
    """Unit-level: the signature helper itself returns the summed rep count."""
    _require("anaerobic_1min_15x_72min.zwo")
    _power, _t, _m, segs = clc.parse_zwo_full(
        WORKOUTS / "anaerobic_1min_15x_72min.zwo")
    sig = clc._detect_interval_signature(segs)
    assert sig is not None and sig[0] == 15


# ── (b) garbage filename → title matches the REAL structure ───────────────────

def test_garbage_threshold_2x4min_gets_truthful_title():
    """`threshold_2x4min_30min` body is two 30s @95% surges over a Z2/Z3 base
    (classed endurance, conf 0.55). The regenerated title must NOT keep the
    bogus '2x4min' interval token and must NOT claim 'threshold'."""
    fname = "threshold_2x4min_30min.zwo"
    _require(fname)
    e = _entry(fname)
    assert e["confidence"] < CONF_GATE  # the file is genuinely low-confidence
    name = _name(fname)
    assert "2x4min" not in name, f"{name} still carries the false 2x4min token"
    assert not name.startswith("threshold_"), (
        f"{name} claims threshold for a low-confidence Z2/Z3 body")


# ── (c) descriptor words for non-uniform structures (Gate B) ──────────────────

def test_ramp_descriptor_word():
    """A ramp-test staircase (150%→600% FTP) is named with the `ramp`
    descriptor, not a fake NxMmin token."""
    fname = "anaerobic_ramp_38min.zwo"
    _require(fname)
    name = _name(fname)
    assert "_ramp_" in name, f"{name} should carry the ramp descriptor"
    assert _reps_in(name) is None, f"{name} must not fake an interval token"


def test_mixed_descriptor_word():
    """A body with ≥2 distinct hard interval shapes earns the `mixed`
    descriptor."""
    fname = "anaerobic_mixed_34min.zwo"
    _require(fname)
    name = _name(fname)
    assert "_mixed_" in name, f"{name} should carry the mixed descriptor"


def test_descriptor_words_are_from_existing_vocabulary():
    """Any descriptor token the namer emits must be one of the EXISTING grammar
    words — it must never invent a new structure word."""
    allowed_descriptors = {"ramp", "pyramid", "progression", "mixed",
                           "steady", "ou"}
    checked = 0
    for p in sorted(WORKOUTS.glob("*.zwo"))[:300]:
        e = clc.classify_zwo_v104(p)
        name = title_from_zwo(p, e)[0]
        # structure token is the segment between the class token and `_{N}min`.
        m = re.match(r".+_([a-z0-9]+)_\d+min\.zwo$", name)
        if not m:
            continue
        struct = m.group(1)
        if struct[0].isalpha() and "x" not in struct:
            assert struct in allowed_descriptors, (
                f"{name}: invented descriptor '{struct}'")
            checked += 1
    assert checked > 0  # we actually exercised some descriptor paths


# ── (d) confidence-gated soft token (Gate C) ──────────────────────────────────

def test_low_confidence_uses_soft_token():
    """conf < 0.6 must yield a generic/soft class token, never a precise
    pattern claim it isn't confident about."""
    fname = "threshold_2x4min_30min.zwo"
    _require(fname)
    e = _entry(fname)
    assert e["confidence"] < CONF_GATE
    token = _name(fname).split("_")[0]
    assert token not in PRECISE_PATTERN_TOKENS, (
        f"low-confidence file minted precise pattern token '{token}'")


def test_no_low_confidence_file_claims_precise_pattern():
    """Sweep: across the library, no conf<0.6 file may carry a precise pattern
    class token in its regenerated filename."""
    offenders = []
    for p in sorted(WORKOUTS.glob("*.zwo"))[:600]:
        e = clc.classify_zwo_v104(p)
        if e["primary"] is None or e["confidence"] >= CONF_GATE:
            continue
        name = title_from_zwo(p, e)[0]
        # leading class token, multi-token-aware.
        head = name.split("_")
        token = head[0]
        two = "_".join(head[:2])
        if token in PRECISE_PATTERN_TOKENS or two in PRECISE_PATTERN_TOKENS:
            offenders.append((p.name, e["confidence"], name))
    assert not offenders, f"low-conf files claimed precise patterns: {offenders[:5]}"


# ── (e) multi-token class tokens kept intact ──────────────────────────────────

def test_multitoken_class_kept_intact():
    """An over_under file (conf ≥ 0.6) keeps the full two-word class token —
    never `split('_')[0]` (which would yield a bare 'over')."""
    fname = "over_under_10x3min_80min.zwo"
    _require(fname)
    e = _entry(fname)
    assert e["primary"] == "over_under" and e["confidence"] >= CONF_GATE
    name = _name(fname)
    assert name.startswith("over_under_"), (
        f"{name} truncated the multi-token over_under class")
    assert not name.startswith("over_10x") and not name.startswith("over_under_under")


def test_over_under_steady_pair_uses_ou_descriptor():
    """An over_under body built from SteadyState pairs (no clean IntervalsT
    shape) must use the `ou` descriptor — NOT a false `{N}x` rep count latched
    from the unreliable steady-pair fallback."""
    fname = "over_under_1min_10x_64min.zwo"
    _require(fname)
    e = _entry(fname)
    # confirm there's no real IntervalsT block to trust.
    _p, _t, _m, segs = clc.parse_zwo_full(WORKOUTS / fname)
    assert not any(s["kind"] == "intervals" for s in segs)
    name = _name(fname)
    assert "_ou_" in name, f"{name} should use the ou descriptor"
    assert _reps_in(name) is None, f"{name} latched a false rep count"


def test_over_under_real_intervals_keeps_rep_count():
    """An over_under body with genuine IntervalsT blocks keeps its faithful
    `{N}x{ON}` token (not downgraded to `ou`)."""
    fname = "over_under_10x3min_80min.zwo"
    _require(fname)
    name = _name(fname)
    assert _reps_in(name) == 10, f"{name} lost the real 10x interval count"


def test_multitoken_class_token_never_truncated_across_library():
    """Every confident multi-token-class file keeps its full class token."""
    bad = []
    for p in sorted(WORKOUTS.glob("*.zwo"))[:600]:
        e = clc.classify_zwo_v104(p)
        if e["primary"] in MULTI_TOKEN_CLASSES and e["confidence"] >= CONF_GATE:
            name = title_from_zwo(p, e)[0]
            if not name.startswith(e["primary"] + "_"):
                bad.append((p.name, e["primary"], name))
    assert not bad, f"multi-token classes truncated: {bad[:5]}"


# ── regression — already-correct Nx names are reproduced, not undercounted ────

def test_regression_correct_nx_names_reproduced():
    """The 417-file undercount regression: the summed signature must NEVER be
    fewer reps than a single dominant block would yield. The old parser reported
    one block's ``repeat`` (e.g. 5) for a body that repeats that shape across
    several blocks (e.g. 15) — undercounting. The fix sums identical shapes, so
    the reported count must be ≥ the largest single matching block's repeat.

    Also verifies the rep COUNT round-trips exactly for the verified multi-block
    set, and that genuine multi-block files (>1 same-shape block) report the
    full SUM rather than a single block's count.
    """
    verified = {
        "anaerobic_1min_15x_72min.zwo": 15,
        "anaerobic_1min_18x_97min.zwo": 18,
        "anaerobic_1min_20x_96min.zwo": 20,
    }
    for fname, reps in verified.items():
        _require(fname)
        assert _reps_in(_name(fname)) == reps

    # Broad scan: the reported signature count for the DOMINANT interval shape
    # (the one with the most summed work-seconds — what the namer prints) must
    # equal the SUM of that shape's reps across all blocks, not a single block.
    # That is exactly the undercount the old dominant-block heuristic caused.
    mismatches = []
    multiblock_checked = 0
    for p in sorted(WORKOUTS.glob("*.zwo")):
        _power, _t, _meta, segs = clc.parse_zwo_full(p)
        iv = [s for s in segs if s["kind"] == "intervals"]
        if not iv:
            continue
        sig = clc._detect_interval_signature(segs)
        if sig is None:
            continue
        reported = sig[0]
        # Group blocks by ON shape (on_s, on_power). The dominant group (by
        # summed work-seconds, tie-break ON power) is what the namer reports.
        groups: dict[tuple, int] = {}
        for s in iv:
            key = (s.get("on_s", 0), round(s.get("on_power", 0.0), 2))
            groups[key] = groups.get(key, 0) + s.get("repeat", 1)
        dom_key, dom_sum = max(
            groups.items(),
            key=lambda kv: (kv[1] * kv[0][0], kv[0][1]),
        )
        # Was this shape split across >1 block (the multi-block case)?
        dom_block_count = sum(
            1 for s in iv
            if (s.get("on_s", 0), round(s.get("on_power", 0.0), 2)) == dom_key)
        if dom_block_count >= 2:
            multiblock_checked += 1
        if reported != dom_sum:
            mismatches.append((p.name, reported, dom_sum, dom_block_count))

    assert not mismatches, (
        f"{len(mismatches)} files where reported reps != summed dominant-shape "
        f"reps, e.g. {mismatches[:8]}")
    # We actually exercised the multi-block summing path on real files.
    assert multiblock_checked >= 100, (
        f"only {multiblock_checked} multi-block same-shape files exercised — "
        f"expected the ~417 undercount set")


# ── purity / determinism ──────────────────────────────────────────────────────

def test_namer_is_deterministic():
    fname = "anaerobic_1min_15x_72min.zwo"
    _require(fname)
    e = _entry(fname)
    p = WORKOUTS / fname
    a = title_from_zwo(p, e)
    b = title_from_zwo(p, e)
    assert a == b


def test_namer_does_not_mutate_inputs():
    fname = "over_under_10x3min_80min.zwo"
    _require(fname)
    e = _entry(fname)
    feats_before = dict(e["features"])
    title_from_zwo(WORKOUTS / fname, e)
    assert e["features"] == feats_before
