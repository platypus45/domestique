"""P2 forever-guard — a label/facts contradiction is a test failure, not a
silent lie — plus regression pins for the three detect_ftp_test FP mechanisms
fixed in the same wave (ramp-FP / cts-FP / coggan-FP, audit 2026-07-05).

Full-library sweep is cheap: facts are precomputed, checks are pure.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import workout_facts as wf  # noqa: E402

WK = ROOT / "src" / "workouts"

# A4: the ledger-verified fused-threshold bodies, exempt BY NAME from the
# threshold-label t200 rule (their sprint caps were owner-clamped 2.0→1.45 in
# this wave, so the exemption is now dormant — kept to pin provenance).
FUSED_EXEMPT = frozenset({
    "neuromuscular_7x15s_140pct_62min.zwo",
    "neuromuscular_5x150s_80pct_62min.zwo",
    "neuromuscular_5x150s_80pct_62min_v2.zwo",
    "neuromuscular_3x12min_100pct_77min.zwo",
    "sprints_5x2min-1min_105pct_59min.zwo",
    "sprints_7x15s_140pct_60min.zwo",
})


def _clc():
    spec = importlib.util.spec_from_file_location(
        "clc_guard", ROOT / "src" / "scripts" / "classify_library_content.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod




@pytest.mark.skipif(not (WK / wf.FACTS_FILENAME).exists(),
                    reason="facts cache absent")
def test_full_library_label_facts_contract_green():
    """D2 guard: zero easy-label-hides-hard contradictions across the whole
    library, with the A4 exemptions pinned BY NAME. Facts rows may lead the
    classification cache for files that landed mid-session (they carry no
    label yet, so the audit skips them — new files get labels at
    integration, after which this sweep covers them too)."""
    facts = json.loads((WK / wf.FACTS_FILENAME).read_text())["facts"]
    cc = json.loads((WK / ".content_classification.json").read_text())["classifications"]
    idx = json.loads((WK / ".library_index.json").read_text())["rows"]
    tagged = frozenset(r["File"] for r in idx
                       if any((t or "").lower() == "ftp_test"
                              for t in (r.get("Tags") or [])))
    flagged = wf.audit_labels(facts, cc, threshold_exempt=FUSED_EXEMPT,
                              ftp_test_names=tagged)
    assert flagged == {}, (
        f"{len(flagged)} label/facts contradictions: "
        f"{dict(list(flagged.items())[:8])}")


def test_fused_exemption_requires_t200_ceiling():
    """The by-name exemption only holds within the 120s t200 ceiling."""
    row = {"n130_45": 0, "t200": 121, "t240": 0, "l150": 15, "t150": 121,
           "if": 0.9, "t130": 121}
    assert wf.label_contradictions(
        "threshold", row, threshold_exempt=frozenset({"x.zwo"}),
        fname="x.zwo") == ["t200>0"]
    row["t200"] = 120
    assert wf.label_contradictions(
        "threshold", row, threshold_exempt=frozenset({"x.zwo"}),
        fname="x.zwo") == []


# ── detect_ftp_test regression pins (the three FP mechanisms) ────────────────

def _steady(p, s):
    return [p] * s


def test_detector_ramp_fp_staircase_plus_openers_canary():
    """Mechanism 1: staircase warmup chained into 30s@120% openers must NOT
    read as a ramp test (peak <130%, steps interrupted by recovery)."""
    clc = _clc()
    power = []
    for p in (0.60, 0.68, 0.75, 0.83, 0.90):       # 5×2' staircase warmup
        power += _steady(p, 120)
    power += _steady(0.50, 120) + _steady(1.20, 30) + _steady(0.50, 90)
    power += (_steady(0.95, 180) + _steady(0.60, 60)) * 4   # interval body
    is_test, sub = clc.detect_ftp_test(power, z6_z7_s=0, sprint_count=0)
    assert not is_test


def test_detector_ramp_real_to_failure_still_detects():
    clc = _clc()
    power = []
    p = 0.56
    for _ in range(25):                             # contiguous 1' steps to 2.0
        power += _steady(round(p, 2), 60)
        p += 0.06
    power += _steady(0.40, 300)                     # nothing but recovery after
    is_test, sub = clc.detect_ftp_test(power, z6_z7_s=600, sprint_count=0)
    assert is_test and sub == "ramp"
    # BRIDGING pin: ascending plateaus separated by sub-30s recovery dips are
    # NOT a ramp (the dips are invisible to step detection, so the old rule
    # chained them; contiguity now breaks the chain).
    power2 = []
    for p in (0.60, 0.80, 1.00, 1.20, 1.36, 1.50):
        power2 += _steady(p, 60) + _steady(0.40, 20)
    power2 += _steady(0.40, 300)
    assert not clc.detect_ftp_test(power2, z6_z7_s=60, sprint_count=0)[0]
    # …and a ramp CAPPED at FTP is not a to-failure test (the 10w-step bug)
    power3 = []
    p = 0.20
    for _ in range(21):
        power3 += _steady(round(p, 2), 60)
        p += 0.04
    power3 += _steady(0.30, 600)
    assert not clc.detect_ftp_test(power3, z6_z7_s=0, sprint_count=0)[0]


def test_detector_cts_fp_2x10_at_95_canary():
    """Mechanism 2: 2×10-12' @95-106% threshold cruise blocks are not the
    CTS 2×8 protocol (blocks must be ~8' and >=100%)."""
    clc = _clc()
    wu = _steady(0.50, 600)
    cd = _steady(0.45, 300)
    for block_p, block_s in ((0.95, 600), (1.00, 600), (1.06, 720)):
        power = wu + _steady(block_p, block_s) + _steady(0.55, 600) \
            + _steady(block_p, block_s) + cd
        assert not clc.detect_ftp_test(power, 0, 0)[0], (block_p, block_s)
    # real CTS 2×8: two ~8' all-out (>=100%) blocks, ~10' apart
    power = wu + _steady(1.05, 480) + _steady(0.55, 600) + _steady(1.05, 480) + cd
    is_test, sub = clc.detect_ftp_test(power, 0, 0)
    assert is_test and sub == "cts_2x8"


def test_detector_coggan_fp_submax_and_fused_canary():
    """Mechanism 3: fixed submaximal blocks (30'@95, 20'@93) and fused
    interval bodies (4×10'@100 back-to-back) are not Coggan tests; the real
    protocol (openers + depletion + ~20'@>=100%) still detects."""
    clc = _clc()
    wu = _steady(0.50, 600)
    cd = _steady(0.45, 300)
    assert not clc.detect_ftp_test(wu + _steady(0.95, 1800) + cd, 0, 0)[0]
    assert not clc.detect_ftp_test(wu + _steady(0.93, 1200) + cd, 0, 0)[0]
    fused = wu + _steady(1.00, 600) + _steady(0.60, 300) + _steady(1.00, 1800) + cd
    assert not clc.detect_ftp_test(fused, 0, 0)[0]
    real = (wu + (_steady(1.00, 60) + _steady(0.50, 60)) * 3   # 3×1' openers
            + _steady(1.05, 300) + _steady(0.50, 600)          # depletion + easy
            + _steady(1.00, 1200) + cd)                        # the 20' test
    is_test, sub = clc.detect_ftp_test(real, 0, 0)
    assert is_test and sub == "sustained"


def test_detector_real_library_files_pinned():
    """The 8 real/tagged test files stay ftp_test fresh; the two cts-FP
    census-pending files stay threshold (detector fixed at the root, no
    classifier-rerun blocklist needed)."""
    clc = _clc()
    keep_ftp = [
        "ftp_test_coggan_3x1min-1min_95pct_59min.zwo", "ftp_test_coggan_3x1min-1min_95pct_59min_v2.zwo",
        "ftp_test_coggan_3x1min-1min_95pct_59min_v3.zwo", "ftp_test_ladder4_110pct_90min.zwo",
        "ftp_test_2x15s-4min_250pct_54min.zwo", "ftp_test_ramp_ladder21_200pct_35min.zwo",
        "ftp_test_ramp_20w_step_ladder23_256pct_43min.zwo", "ftp_test_ramp_10w_step_ladder20_152pct_52min.zwo",
    ]
    for fn in keep_ftp:
        p = WK / fn
        assert p.exists(), fn
        power, tags, meta, segments = clc.parse_zwo_full(p)
        feats = clc.extract_features_v104(power, segments)
        fresh, _, _ = clc.classify_v104(feats, tags=tags, segments=segments)
        assert fresh == "ftp_test", (fn, fresh)
    for fn in ("threshold_6x10min_100pct_85min.zwo",
               "threshold_1x20min_80pct_65min.zwo"):
        p = WK / fn
        power, tags, meta, segments = clc.parse_zwo_full(p)
        feats = clc.extract_features_v104(power, segments)
        fresh, _, _ = clc.classify_v104(feats, tags=tags, segments=segments)
        assert fresh == "threshold", (fn, fresh)
    # the fixed 10w ramp now genuinely detects as a to-failure ramp
    power, tags, meta, segments = clc.parse_zwo_full(WK / "ftp_test_ramp_10w_step_ladder20_152pct_52min.zwo")
    feats = clc.extract_features_v104(power, segments)
    assert feats["is_ftp_test"] and feats["ftp_test_subtype"] == "ramp"


def test_if_ceilings_are_np_units_and_guard_still_bites():
    """v3.5.0 rescaled the easy if-ceilings from RMS to NP units (0.75->0.78,
    0.80->0.84). That is a unit correction, not a relaxation: prove the guard
    still catches an easy label hiding real hard work via the STRUCTURAL rules,
    which are unit-independent and were deliberately left alone."""
    assert wf._EASY_STRICT_IF_CEILING == 0.78
    assert wf._EASY_Z2_IF_CEILING == 0.84
    # a 60 s rep at 140% under a recovery label — caught by n130_45, not by IF
    hard = {"if": 0.50, "n130_45": 1, "t200": 0, "l150": 0}
    assert "rep>=45s@130" in wf.label_contradictions("recovery", hard)
    assert "rep>=45s@130" in wf.label_contradictions("endurance", hard)
    # time at >=200% FTP under an easy label still trips regardless of IF
    supra = {"if": 0.50, "n130_45": 0, "t200": 30, "l150": 0}
    assert "t200>0" in wf.label_contradictions("recovery", supra)
    assert "t200>10" in wf.label_contradictions("endurance_intervals", supra)
    # and the rescaled ceiling itself still fires above its new line
    assert wf.label_contradictions("recovery", {"if": 0.79, "n130_45": 0,
                                                "t200": 0, "l150": 0}) == ["if>0.78"]
