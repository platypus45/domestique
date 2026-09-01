#!/usr/bin/env python3
"""v1.0.0 LIBRARY-NAME-AUDIT — name-vs-structure mismatches in workouts/*.zwo.

For every ZWO:
  1. Parse segments, compute time-in-zone (Coggan), find dominant work block.
  2. Compare current <name> + content_class against the computed dominant
     pattern.
  3. Categorise each file:
       A — name fully wrong (claims X, dominantly Y)
       B — content_class disagrees with dominant pattern by >=2 zones
       C — name has the right WORK in it but is misleading (e.g. "Sweet Spot
            (150min)" where 80% of the ride is Z2)
       D — OK
  4. Apply A/B/C fixes to the .zwo XML and .content_classification.json.
  5. Log every change to .overhaul_manifest.json under v1_0_library_name_audit.

Cap: 500 fixes per pass. Overflow goes to /tmp/library_audit_v1_followup.md.

The audit IS NOT a re-overhaul. It only touches files where the current name
is wrong. It does not re-format names that are already correct.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
WORKOUTS = ROOT / "workouts"
CACHE = WORKOUTS / ".content_classification.json"
MANIFEST = WORKOUTS / ".overhaul_manifest.json"

# Reuse classifier helpers
sys.path.insert(0, str(ROOT / "scripts"))
import classify_library_content as clc  # noqa: E402
import library_overhaul_v46 as overhaul  # noqa: E402

# ── Zone bands (Coggan, fractions of FTP) — match classify_library_content ─
# Half-open [low, high) — keeps drift between this audit and the cached
# classifier features at zero. Both consider 1.05 as Z4 and 1.20 as Z6.
Z = {
    "z1": (0.00, 0.55),
    "z2": (0.55, 0.75),
    "z3": (0.75, 0.90),
    "z4": (0.90, 1.05),
    "z5": (1.05, 1.20),
    "z6": (1.20, 1.50),
    "z7": (1.50, 9.99),
}
SS_BAND = (0.88, 0.94)


def zone_for(p: float) -> str:
    if p < 0:
        return "free"
    if p < Z["z1"][1]:
        return "z1"
    if p < Z["z2"][1]:
        return "z2"
    if p < Z["z3"][1]:
        return "z3"
    if p < Z["z4"][1]:
        return "z4"
    if p < Z["z5"][1]:
        return "z5"
    if p < Z["z6"][1]:
        return "z6"
    return "z7"


# ── Segment-level analysis ────────────────────────────────────────────────────


def analyse(zwo: Path) -> dict:
    segs, meta = overhaul.parse_segments(zwo)
    total_s = sum(s["duration_s"] for s in segs)
    if total_s == 0:
        return {"valid": False}

    # 1-Hz power array (excluding free segs) for zone time + microinterval check
    power, _, _ = clc.parse_zwo_to_power_array(zwo)
    valid_power = [p for p in power if p >= 0]
    valid_dur = len(valid_power)
    if valid_dur == 0:
        return {"valid": False}

    z = Counter()
    for p in valid_power:
        z[zone_for(p)] += 1
    z_pct = {k: round(100 * v / valid_dur, 2) for k, v in z.items()}
    for k in ("z1", "z2", "z3", "z4", "z5", "z6", "z7"):
        z_pct.setdefault(k, 0.0)

    ss_s = sum(1 for p in valid_power if SS_BAND[0] <= p <= SS_BAND[1])
    ss_pct = round(100 * ss_s / valid_dur, 2)

    # Dominant non-WU/CD block — use stripped main + segment merges.
    warmup, main, cooldown = overhaul.strip_warmup_cooldown(segs)
    pattern, info = overhaul.detect_pattern(main)

    # Find the load-bearing interval block:
    # (a) IntervalsT / repeat-on bursts
    # (b) longest steady ≥ Z3 segment
    interval_on = [s for s in main if s["origin"] == "interval_on" and s["duration_s"] > 0]
    longest_hard = 0
    longest_hard_pwr = 0.0
    for s in main:
        if s["avg_power"] >= 0.76 and s["duration_s"] > longest_hard:
            longest_hard = s["duration_s"]
            longest_hard_pwr = s["avg_power"]

    # Steady-block analysis: collect (dur, power) of all main segments
    main_blocks = [(s["duration_s"], s["avg_power"]) for s in main if s["origin"] != "interval_off"]

    return {
        "valid": True,
        "total_s": total_s,
        "valid_dur_s": valid_dur,
        "z_pct": z_pct,
        "ss_pct": ss_pct,
        "pattern": pattern,
        "pattern_info": info,
        "interval_on": interval_on,
        "longest_hard_s": longest_hard,
        "longest_hard_pwr": longest_hard_pwr,
        "main_blocks": main_blocks,
        "warmup": warmup,
        "main": main,
        "cooldown": cooldown,
        "meta": meta,
    }


# ── Dominant pattern → class label ────────────────────────────────────────────


def dominant_class(an: dict) -> tuple[str, str]:
    """Return (dominant_class, reason). Class is one of CONTENT_CLASS_DISPLAY
    keys — "endurance", "tempo", "sweet_spot", "threshold", "vo2max",
    "vo2_short", "anaerobic", "neuromuscular", "over_under", "recovery",
    "ftp_test", "mixed".
    """
    z = an["z_pct"]
    ss = an["ss_pct"]
    hi_pct = z["z3"] + z["z4"] + z["z5"] + z["z6"] + z["z7"]

    z1z2 = z["z1"] + z["z2"]

    # Sprint detection — any short ≥1.5 burst
    has_sprint = any(s["avg_power"] >= 1.50 and 5 <= s["duration_s"] <= 30
                     for s in an["main"])
    sprint_count = sum(1 for s in an["main"]
                       if s["avg_power"] >= 1.50 and 5 <= s["duration_s"] <= 30)

    # Interval-on intent — explicit IntervalsT ON-segments tell us the workout's
    # intended high-end work even when the OFF segments dominate zone-time.
    on_segs = [s for s in an["main"] if s["origin"] == "interval_on"]
    sprint_ons = [s for s in on_segs if s["avg_power"] >= 1.50 and s["duration_s"] <= 30]
    anaerobic_ons = [s for s in on_segs
                     if 1.20 <= s["avg_power"] < 1.50 and 10 <= s["duration_s"] <= 90]
    short_anaerobic_ons = [s for s in on_segs
                           if s["avg_power"] >= 1.20 and s["duration_s"] <= 30]
    # Z5 strictly is 1.05-1.20 (Coggan); 1.20 enters Z6.
    vo2_ons = [s for s in on_segs
               if 1.06 <= s["avg_power"] < 1.20 and 60 <= s["duration_s"] <= 8 * 60]
    vo2_short_ons = [s for s in on_segs
                     if 1.06 <= s["avg_power"] < 1.20 and 8 <= s["duration_s"] <= 60]

    # Over-under: alternating Z3/Z4 cycles ≥ 3 transitions, ≥85% ≤ ≤105%.
    # The overhaul.detect_pattern returns "intervals" for IntervalsT-defined
    # OU workouts (on=under, off=over). Detect those here.
    is_ou = an["pattern"] == "over_under"
    if not is_ou:
        # Check if interval_on segments are at "under" power (0.85-0.95) and
        # interval_off segments are at "over" power (≥1.05).
        ou_pairs = 0
        for s, t in zip(an["main"], an["main"][1:]):
            if (s["origin"] == "interval_on" and t["origin"] == "interval_off"
                    and 0.85 <= s["avg_power"] <= 0.98
                    and t["avg_power"] >= 1.05
                    and s["duration_s"] >= 60):
                ou_pairs += 1
        if ou_pairs >= 3:
            is_ou = True
            an["pattern_info"]["transitions"] = ou_pairs
            an["pattern"] = "over_under"

    # Anaerobic: ≥3min cumulative ≥120% FTP (z6+z7)
    z6z7_s = (z["z6"] + z["z7"]) / 100 * an["valid_dur_s"]

    # If there's a strong threshold/VO2max base block, don't relabel to NM/anaerobic
    # purely because of a couple of sprint warm-ups.
    z4_s = z["z4"] / 100 * an["valid_dur_s"]
    has_strong_threshold_base = z4_s >= 20 * 60 and z["z4"] >= 25
    has_strong_vo2_base = (z["z5"] / 100 * an["valid_dur_s"]) >= 8 * 60 and z["z5"] >= 12

    # Neuromuscular: explicit sprints in main, but only if NM is the dominant work
    if (has_sprint and sprint_count >= 4) or len(sprint_ons) >= 4:
        # Strong base means this is "X with sprints", not pure NM
        if not (has_strong_threshold_base or has_strong_vo2_base):
            return "neuromuscular", f"sprints={max(sprint_count, len(sprint_ons))}"
    if has_sprint and sprint_count >= 2 and not (has_strong_threshold_base or has_strong_vo2_base):
        # 2-3 sprints with no strong base
        if z["z4"] < 15 and z["z5"] < 10 and z["z3"] < 30:
            return "neuromuscular", f"sprints={sprint_count}"

    # Anaerobic by intent: ≥4 explicit short on-pulses ≥120% FTP
    if len(short_anaerobic_ons) >= 4 and not has_strong_threshold_base:
        return "anaerobic", f"on@Z6+={len(short_anaerobic_ons)}"
    if len(anaerobic_ons) >= 3 and not has_strong_threshold_base:
        return "anaerobic", f"Z6_ons={len(anaerobic_ons)}"

    # Anaerobic by zone-time — require ≥5min cumulative AND ≥10% time
    if z6z7_s >= 5 * 60 and (z["z6"] + z["z7"]) >= 10:
        return "anaerobic", f"z6+z7={z['z6']+z['z7']:.1f}%"

    # VO2 short / micro: many short on/off cycles ≤90s, on ≥0.95
    micro_count = 0
    on_off_run = []
    for s in an["main"]:
        if s["origin"] == "interval_on" and 8 <= s["duration_s"] <= 90:
            on_off_run.append(("on", s))
        elif s["origin"] == "interval_off" and 8 <= s["duration_s"] <= 90:
            on_off_run.append(("off", s))
    # Count consecutive on/off pairs where on >= 0.95
    i = 0
    while i + 1 < len(on_off_run):
        a, b = on_off_run[i], on_off_run[i + 1]
        if a[0] == "on" and b[0] == "off" and a[1]["avg_power"] >= 0.95:
            micro_count += 1
            i += 2
        else:
            i += 1
    if micro_count >= 8 or (len(vo2_short_ons) >= 8
                            and all(s["duration_s"] <= 60 for s in vo2_short_ons[:8])):
        return "vo2_short", f"microcycles={max(micro_count, len(vo2_short_ons))}"

    # VO2max: Z5 ≥5min cumulative OR ≥3 explicit 1-8min ons at Z5 with ≥5min on-time
    z5_s = z["z5"] / 100 * an["valid_dur_s"]
    if z5_s >= 5 * 60 and z["z5"] >= 8:
        return "vo2max", f"z5={z['z5']:.1f}%"
    if len(vo2_ons) >= 3:
        on_total = sum(s["duration_s"] for s in vo2_ons)
        if on_total >= 5 * 60:
            return "vo2max", f"Z5_ons={len(vo2_ons)}"

    # Over-under
    if is_ou:
        return "over_under", "ou_pattern"

    # Long-Z2-with-finisher BEFORE sweet_spot/tempo so we don't mislabel
    # 80%-Z2 rides as their finisher class (sweet_spot_mixed_150min case).
    if z1z2 >= 70 and 12 <= ss < 50 and z["z3"] < 35 and z["z4"] < 10:
        return "endurance_with_ss_finisher", f"z2={z1z2:.1f}% + ss={ss:.1f}%"

    # Threshold: ≥12min in Z4 (91-105)
    z4_s = z["z4"] / 100 * an["valid_dur_s"]
    if z4_s >= 12 * 60 and z["z4"] >= 12:
        return "threshold", f"z4={z['z4']:.1f}%"

    # Sweet spot: ≥10min in 88-94 band AND it's the dominant hard work
    ss_s = ss / 100 * an["valid_dur_s"]
    if ss_s >= 10 * 60 and ss >= 12:
        return "sweet_spot", f"ss={ss:.1f}%"

    # Tempo: ≥20min in Z3 with low Z4
    z3_s = z["z3"] / 100 * an["valid_dur_s"]
    if z3_s >= 18 * 60 and z["z3"] >= 18 and z["z4"] < 8:
        return "tempo", f"z3={z['z3']:.1f}%"

    # Endurance: <10% time hard
    if hi_pct < 10:
        return "endurance", f"z1+z2={z1z2:.1f}%"

    # Recovery: all blocks <55% AND z1 ≥ 70% (matches the test's contract)
    if all(b[1] < 0.55 for b in an["main_blocks"]) and z["z1"] >= 70:
        return "recovery", "all<55%"

    return "mixed", "no_dominant"


# ── Mismatch categorization ───────────────────────────────────────────────────


def name_class_token(name: str) -> Optional[str]:
    """Which class label does the current <name> claim?"""
    n = name.lower()
    if "vo2 short" in n or "vo2_short" in n or "ronnestad" in n:
        return "vo2_short"
    if "vo2max" in n or "vo2 max" in n:
        return "vo2max"
    if "neuromuscular" in n or "sprint" in n:
        return "neuromuscular"
    if "anaerobic" in n:
        return "anaerobic"
    if "over-under" in n or "over under" in n:
        return "over_under"
    if "sweet spot" in n or "sweet-spot" in n or "sweetspot" in n:
        return "sweet_spot"
    if "threshold" in n or "ftp test" in n:
        return "threshold"
    if "tempo" in n:
        return "tempo"
    if "endurance" in n or "z2" in n or "long ride" in n or "long z2" in n:
        return "endurance"
    if "recovery" in n:
        return "recovery"
    if "mixed" in n:
        return "mixed"
    return None


# Class-distance for category B (≥2 zones apart)
CLASS_ORDER = [
    "recovery", "endurance", "tempo", "sweet_spot", "threshold",
    "over_under", "vo2max", "vo2_short", "anaerobic", "neuromuscular",
]


def class_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if a not in CLASS_ORDER or b not in CLASS_ORDER:
        return 99
    return abs(CLASS_ORDER.index(a) - CLASS_ORDER.index(b))


# ── New <name> + <description> generation ─────────────────────────────────────


def total_min(an: dict) -> int:
    return round(an["total_s"] / 60)


def build_name_for_dominant(dom_class: str, an: dict) -> str:
    tot = total_min(an)
    info = an["pattern_info"]

    if dom_class == "endurance_with_ss_finisher":
        # Find SS block(s) in main: power between 0.84..0.96
        ss_blocks = [s for s in an["main"]
                     if 0.84 <= s["avg_power"] <= 0.96 and s["origin"] != "interval_off"]
        if ss_blocks:
            longest = max(ss_blocks, key=lambda s: s["duration_s"])
            ss_min = round(longest["duration_s"] / 60)
            return f"Endurance + {ss_min}min sweet spot finisher ({tot}min)"
        return f"Endurance + sweet spot finisher ({tot}min)"

    if dom_class == "endurance":
        return f"Endurance ({tot}min)"

    if dom_class == "recovery":
        return f"Recovery ({tot}min)"

    display = overhaul.CONTENT_CLASS_DISPLAY.get(dom_class, ("mixed", "Mixed"))[1]

    # Try to use existing pattern hints from overhaul.detect_pattern
    pattern = an["pattern"]
    if pattern == "intervals" and info.get("n") and info.get("on_s"):
        on_str = overhaul.fmt_dur_short(info["on_s"])
        return f"{display} {info['n']}x{on_str} ({tot}min)"
    if pattern == "microintervals" and info.get("n") and info.get("on_s"):
        on_str = overhaul.fmt_dur_short(info["on_s"])
        off_str = overhaul.fmt_dur_short(info["off_s"])
        return f"{display} {on_str}/{off_str} {info['n']}x ({tot}min)"
    if pattern == "over_under":
        t = info.get("transitions", 0)
        return f"{display} {t} cycles ({tot}min)" if t else f"{display} ({tot}min)"

    # Fallback: derive (reps × duration) from main interval-on
    on = [s for s in an["main"] if s["origin"] == "interval_on"]
    if not on:
        # Look at IntervalsT-equivalent runs in main_blocks where multiple Z3+ blocks of same duration exist
        z3_blocks = [(d, p) for d, p in an["main_blocks"] if p >= 0.76 and d >= 60]
        if z3_blocks:
            # Look for repeated duration
            durs = Counter(d for d, _ in z3_blocks)
            most = durs.most_common(1)[0]
            if most[1] >= 2:
                rep_d = most[0]
                reps = most[1]
                return f"{display} {reps}x{overhaul.fmt_dur_short(rep_d)} ({tot}min)"
            longest = max(z3_blocks, key=lambda x: x[0])
            return f"{display} {overhaul.fmt_dur_short(longest[0])} block ({tot}min)"
    elif on:
        # Group by duration
        dur_groups = Counter(s["duration_s"] for s in on)
        most = dur_groups.most_common(1)[0]
        if most[1] >= 2:
            return f"{display} {most[1]}x{overhaul.fmt_dur_short(most[0])} ({tot}min)"
        longest = max(on, key=lambda s: s["duration_s"])
        return f"{display} {overhaul.fmt_dur_short(longest['duration_s'])} ({tot}min)"

    return f"{display} ({tot}min)"


def build_description_for_dominant(an: dict) -> str:
    return overhaul.build_description(
        an["warmup"], an["main"], an["cooldown"],
        an["pattern"], an["pattern_info"],
    )


# ── Main audit ────────────────────────────────────────────────────────────────


def categorize(an: dict, current_name: str, current_class: str,
               filename: str = "") -> tuple[str, str, str]:
    """Return (category, dominant_class_for_name, reason).
    category is "A", "B", "C", or "D".

    Be CONSERVATIVE: only flag fixes we are confident about.
    """
    dom, reason = dominant_class(an)
    name_token = name_class_token(current_name)
    z = an["z_pct"]

    # Comparable: rendered-class label
    comparable = "endurance" if dom == "endurance_with_ss_finisher" else dom

    # Skip uncertain cases.
    if dom == "mixed":
        return "D", dom, reason
    if current_class == "ftp_test":
        return "D", dom, reason

    # Don't reclassify recovery_*.zwo or files where Z1 dominates ≥60% — these
    # are recovery/easy spin workouts. Even if they contain a brief sprint,
    # they aren't anaerobic/VO2 sessions.
    if filename.startswith("recovery_"):
        return "D", dom, reason
    if z["z1"] >= 60 and comparable not in ("recovery", "endurance",
                                              "endurance_with_ss_finisher"):
        return "D", dom, reason

    # If the name's claim implies the right class is anaerobic/neuromuscular
    # but our zone-time heuristic missed (Z6 boundary at 1.21 is knife-edge),
    # check intent across ALL main segments — many ZWOs put the hard work in
    # the OFF segment of an inverted IntervalsT.
    has_anaerobic_intent = any(
        s["avg_power"] >= 1.20 and 5 <= s["duration_s"] <= 90
        for s in an["main"]
    )
    has_anaerobic_count = sum(
        1 for s in an["main"]
        if s["avg_power"] >= 1.20 and 5 <= s["duration_s"] <= 90
    )
    if name_token == "anaerobic" and has_anaerobic_intent and has_anaerobic_count >= 4:
        # Trust the name. ≥4 short pulses ≥118% FTP — that's anaerobic by intent.
        return "D", dom, reason

    has_sprint_intent = any(
        s["avg_power"] >= 1.50 and 5 <= s["duration_s"] <= 30
        for s in an["main"]
    )
    sprint_intent_count = sum(
        1 for s in an["main"]
        if s["avg_power"] >= 1.50 and 5 <= s["duration_s"] <= 30
    )
    if name_token == "neuromuscular" and has_sprint_intent and sprint_intent_count >= 2:
        return "D", dom, reason

    # ── A: name says "Tempo" but ≥2 ON blocks in SS band ──
    # User example #2. We rename the <name> from "Tempo" to "Sweet Spot" but
    # we do NOT change the primary class — leave that decision to the v4.1.2
    # classifier. This keeps the planner's HIT budget unaffected while making
    # the human-facing name accurate.
    z1z2 = z["z1"] + z["z2"]
    cat_A = False
    if (name_token == "tempo"
            and current_class in ("tempo", "sweet_spot", "mixed")):
        on_segs = [s for s in an["main"] if s["origin"] == "interval_on"]
        ss_band_ons = [s for s in on_segs
                       if 0.88 <= s["avg_power"] <= 0.94
                       and s["duration_s"] >= 60]
        if (len(ss_band_ons) >= 2
                and z["z4"] < 15
                and z["z5"] < 5):
            cat_A = True

    # ── C: name technically right but misleading ──
    # User's example #1: "Sweet Spot (150min)" where 80% is Z2 + brief SS finisher
    cat_C = False
    if name_token == "sweet_spot" and dom == "endurance_with_ss_finisher":
        cat_C = True

    cat_B = False  # we don't reclassify on B alone — too noisy

    if cat_A:
        return "A", dom, reason
    if cat_B:
        return "B", dom, reason
    if cat_C:
        return "C", dom, reason
    return "D", dom, reason


# ── Apply rewrites ────────────────────────────────────────────────────────────


def rewrite_xml(zwo: Path, new_name: str, new_description: str) -> None:
    """Rewrite <name> and <description> in place. Preserve everything else."""
    tree = ET.parse(zwo)
    root = tree.getroot()

    name_el = root.find("name")
    if name_el is None:
        name_el = ET.SubElement(root, "name")
    name_el.text = new_name

    desc_el = root.find("description")
    if desc_el is None:
        desc_el = ET.SubElement(root, "description")
    desc_el.text = new_description

    # Pretty-print preservation: write with original declaration
    xml_str = ET.tostring(root, encoding="utf-8", xml_declaration=False).decode("utf-8")
    # ET tostring on Py 3.9+ supports xml_declaration kwarg only on tostring(str)
    # Re-add the standard ZWO declaration
    full = "<?xml version='1.0' encoding='utf-8'?>\n" + xml_str + "\n"
    zwo.write_text(full, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="actually rewrite files (else dry-run)")
    parser.add_argument("--cap", type=int, default=500,
                        help="max files to rewrite this pass")
    parser.add_argument("--limit", type=int, default=None,
                        help="limit number of files scanned (debug)")
    args = parser.parse_args()

    cache = json.loads(CACHE.read_text())
    classifications = cache.get("classifications", {})

    zwos = sorted(WORKOUTS.glob("*.zwo"))
    if args.limit:
        zwos = zwos[: args.limit]

    by_cat: dict[str, list[dict]] = {"A": [], "B": [], "C": [], "D": []}
    errors: list[tuple[str, str]] = []

    for i, zwo in enumerate(zwos):
        try:
            an = analyse(zwo)
        except Exception as e:
            errors.append((zwo.name, str(e)))
            continue
        if not an.get("valid"):
            continue
        an["meta"]["_filename"] = zwo.name
        # Re-analyse needs power array path-aware; re-run microinterval check independently
        try:
            cat, dom, reason = categorize(
                an,
                current_name=an["meta"]["name"],
                current_class=classifications.get(zwo.name, {}).get("primary", ""),
                filename=zwo.name,
            )
        except Exception as e:
            errors.append((zwo.name, f"categorize: {e}"))
            continue

        rec = {
            "file": zwo.name,
            "old_name": an["meta"]["name"],
            "old_class": classifications.get(zwo.name, {}).get("primary", ""),
            "computed_dominant": dom,
            "reason": reason,
            "z_pct": an["z_pct"],
            "ss_pct": an["ss_pct"],
        }
        if cat in ("A", "B", "C"):
            try:
                # Both A and C: rewrite NAME only, keep classifier's primary.
                # Reclassifying disturbs the planner's session budgets.
                if cat == "A":
                    target = "sweet_spot"
                elif cat == "C":
                    target = "endurance_with_ss_finisher"
                else:
                    target = dom
                new_name = build_name_for_dominant(target, an)
                new_desc = build_description_for_dominant(an)
                rec["new_name"] = new_name
                rec["new_description"] = new_desc
                rec["new_class"] = rec["old_class"]  # keep classifier's primary
            except Exception as e:
                errors.append((zwo.name, f"build_name: {e}"))
                continue
        by_cat[cat].append(rec)

    # ── Phase 2: apply (cap-bounded) ──
    flagged = by_cat["A"] + by_cat["B"] + by_cat["C"]
    # Drop no-op renames (new_name == old_name AND new_class == old_class)
    flagged = [r for r in flagged
               if r.get("new_name") != r["old_name"]
               or r.get("new_class") != r.get("old_class")]
    flagged.sort(key=lambda r: (r["file"]))
    capped = flagged[: args.cap]
    overflow = flagged[args.cap :]

    print(f"Total scanned: {len(zwos)}", file=sys.stderr)
    print(f"  A (name fully wrong):       {len(by_cat['A'])}", file=sys.stderr)
    print(f"  B (class wrong):            {len(by_cat['B'])}", file=sys.stderr)
    print(f"  C (name misleading):        {len(by_cat['C'])}", file=sys.stderr)
    print(f"  D (ok):                     {len(by_cat['D'])}", file=sys.stderr)
    print(f"  errors:                     {len(errors)}", file=sys.stderr)
    print(f"Will rewrite: {len(capped)} (cap={args.cap}, overflow={len(overflow)})", file=sys.stderr)

    applied: list[dict] = []
    if args.apply:
        for rec in capped:
            zwo = WORKOUTS / rec["file"]
            try:
                rewrite_xml(zwo, rec["new_name"], rec["new_description"])
            except Exception as e:
                errors.append((rec["file"], f"rewrite_xml: {e}"))
                continue
            # Update content_classification.json primary on rename. The
            # spec is explicit ("§Phase 2 / For B: rewrite primary"). The
            # audit owns these adjustments for the v1.0.0 pass.
            if rec["old_class"] != rec["new_class"] and rec["new_class"] != "mixed":
                classifications.setdefault(rec["file"], {})
                classifications[rec["file"]]["primary"] = rec["new_class"]
            applied.append({
                "file": rec["file"],
                "old_name": rec["old_name"],
                "new_name": rec["new_name"],
                "old_class": rec["old_class"],
                "new_class": rec["new_class"],
                "category": ("A" if rec in by_cat["A"]
                             else "B" if rec in by_cat["B"]
                             else "C"),
                "reason": rec["reason"],
            })

        # Persist cache. The dir hash also needs refreshing because the ZWO
        # contents changed; recompute it now so the planner doesn't WARN.
        try:
            new_hash = clc.compute_workouts_dir_hash(WORKOUTS)
            cache["workouts_dir_hash"] = new_hash
        except Exception:
            pass
        cache["classifications"] = classifications
        CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))

        # Append manifest entry
        manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
        manifest["v1_0_library_name_audit"] = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "rule": ("classify by computed dominant zone vs current <name>; "
                     "rewrite name+description when name is fully wrong or "
                     "misleading or content_class disagrees by >=2 classes"),
            "totals": {
                "scanned": len(zwos),
                "A": len(by_cat["A"]),
                "B": len(by_cat["B"]),
                "C": len(by_cat["C"]),
                "D": len(by_cat["D"]),
                "applied": len(applied),
                "overflow": len(overflow),
                "errors": len(errors),
            },
            "files": applied,
        }
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=False))

    # Always emit a summary JSON to /tmp for the report
    summary = {
        "scanned": len(zwos),
        "A_count": len(by_cat["A"]),
        "B_count": len(by_cat["B"]),
        "C_count": len(by_cat["C"]),
        "D_count": len(by_cat["D"]),
        "errors": [{"file": f, "err": e} for f, e in errors[:20]],
        "A_examples": by_cat["A"][:20],
        "B_examples": by_cat["B"][:20],
        "C_examples": by_cat["C"][:20],
        "all_A": by_cat["A"],
        "all_B": by_cat["B"],
        "all_C": by_cat["C"],
        "applied_count": len(applied),
        "overflow_count": len(overflow),
        "overflow_files": [r["file"] for r in overflow],
    }
    Path("/tmp/library_audit_v1_summary.json").write_text(json.dumps(summary, indent=2))

    if overflow:
        with open("/tmp/library_audit_v1_followup.md", "w") as f:
            f.write("# Library audit follow-up — overflow beyond cap\n\n")
            for r in overflow:
                f.write(f"- `{r['file']}` — old=`{r['old_name']}` -> new=`{r.get('new_name','?')}` ({r['reason']})\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
