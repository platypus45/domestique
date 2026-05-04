#!/usr/bin/env python3
"""
audit_tempo_workouts.py — v4.3.0 B7

Walks workouts/tempo_*.zwo and classifies each file by shape:
  - steady              : only <SteadyState> in main body (warmup/cooldown ok), main_body_avg in tempo zone (0.76-0.90)
  - progression         : has <Ramp> in main body AND filename includes '_progression_' (acceptable)
  - ramping_undesired   : has <Ramp> in main body AND filename does NOT include '_progression_'
  - mixed               : main body contains <IntervalsT> (intervals/mixed-zone, out-of-category)

Outputs /tmp/tempo_audit.json — list of {file, shape, main_body_avg_pct, has_ramp, segment_count}.
Prints summary: N steady / N progression / N ramping_undesired / N mixed.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from glob import glob
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = REPO_ROOT / "workouts"
OUTPUT_PATH = Path("/tmp/tempo_audit.json")

# ZWO segment tags treated as warmup/cooldown wrappers (not part of "main body")
WRAPPER_TAGS = {"Warmup", "Cooldown"}
# Tags considered "main body" content
MAIN_BODY_TAGS = {"SteadyState", "Ramp", "IntervalsT", "FreeRide"}


def _segment_avg_power(seg: ET.Element) -> float | None:
    """Time-averaged power fraction (0-1) for a single segment, or None if not parseable."""
    tag = seg.tag
    duration = float(seg.attrib.get("Duration", 0) or 0)
    if duration <= 0:
        return None
    if tag == "SteadyState":
        p = seg.attrib.get("Power")
        if p is None:
            return None
        return float(p)
    if tag in ("Ramp", "Warmup", "Cooldown"):
        lo = seg.attrib.get("PowerLow")
        hi = seg.attrib.get("PowerHigh")
        if lo is None or hi is None:
            return None
        return (float(lo) + float(hi)) / 2.0
    if tag == "IntervalsT":
        try:
            on_p = float(seg.attrib.get("OnPower", 0))
            off_p = float(seg.attrib.get("OffPower", 0))
            on_d = float(seg.attrib.get("OnDuration", 0))
            off_d = float(seg.attrib.get("OffDuration", 0))
            repeat = float(seg.attrib.get("Repeat", 1))
            total = (on_d + off_d) * repeat
            if total <= 0:
                return None
            return (on_p * on_d + off_p * off_d) / (on_d + off_d)
        except Exception:
            return None
    if tag == "FreeRide":
        return None
    return None


def _segment_total_duration(seg: ET.Element) -> float:
    """Total clock seconds for a segment (handles IntervalsT repetition)."""
    tag = seg.tag
    if tag == "IntervalsT":
        try:
            on_d = float(seg.attrib.get("OnDuration", 0))
            off_d = float(seg.attrib.get("OffDuration", 0))
            repeat = float(seg.attrib.get("Repeat", 1))
            return (on_d + off_d) * repeat
        except Exception:
            return 0.0
    try:
        return float(seg.attrib.get("Duration", 0) or 0)
    except Exception:
        return 0.0


def _classify(zwo_path: Path) -> dict:
    """Parse one .zwo file and return its audit record."""
    try:
        tree = ET.parse(zwo_path)
    except ET.ParseError as e:
        return {
            "file": zwo_path.name,
            "shape": "mixed",
            "main_body_avg_pct": None,
            "has_ramp": False,
            "segment_count": 0,
            "error": f"parse_error: {e}",
        }

    root = tree.getroot()
    workout_el = root.find("workout")
    if workout_el is None:
        return {
            "file": zwo_path.name,
            "shape": "mixed",
            "main_body_avg_pct": None,
            "has_ramp": False,
            "segment_count": 0,
            "error": "missing_workout_element",
        }

    segments = list(workout_el)
    segment_count = len(segments)

    # Strip leading Warmup wrapper(s) and trailing Cooldown wrapper(s) to identify the main body.
    main_body = list(segments)
    while main_body and main_body[0].tag in WRAPPER_TAGS:
        main_body.pop(0)
    while main_body and main_body[-1].tag in WRAPPER_TAGS:
        main_body.pop()

    has_ramp = any(seg.tag == "Ramp" for seg in main_body)
    has_intervals = any(seg.tag == "IntervalsT" for seg in main_body)
    has_freeride = any(seg.tag == "FreeRide" for seg in main_body)

    # Compute time-weighted avg power across the main body
    weighted_sum = 0.0
    total_dur = 0.0
    for seg in main_body:
        avg = _segment_avg_power(seg)
        dur = _segment_total_duration(seg)
        if avg is None or dur <= 0:
            continue
        weighted_sum += avg * dur
        total_dur += dur
    main_body_avg = (weighted_sum / total_dur) if total_dur > 0 else None

    name_has_progression = "_progression_" in zwo_path.name.lower()

    # Classify
    if has_intervals or has_freeride:
        # Tempo file with IntervalsT or FreeRide in main body — out-of-category
        # (misnamed "tempo" but really mixed/free; out of v4.3.0 scope to rename)
        shape = "mixed"
    elif has_ramp:
        shape = "progression" if name_has_progression else "ramping_undesired"
    else:
        # No ramp, no intervals — pure SteadyState body. Now check if avg is in tempo zone.
        if main_body_avg is None:
            shape = "mixed"
        elif 0.76 <= main_body_avg <= 0.90:
            shape = "steady"
        else:
            # Steady-only body but not in tempo range — out-of-category
            shape = "mixed"

    return {
        "file": zwo_path.name,
        "shape": shape,
        "main_body_avg_pct": round(main_body_avg * 100, 1) if main_body_avg is not None else None,
        "has_ramp": has_ramp,
        "segment_count": segment_count,
    }


def main() -> int:
    files = sorted(glob(str(WORKOUTS_DIR / "tempo_*.zwo")))
    if not files:
        print(f"No tempo_*.zwo files found in {WORKOUTS_DIR}", file=sys.stderr)
        return 1

    records = [_classify(Path(p)) for p in files]
    OUTPUT_PATH.write_text(json.dumps(records, indent=2))

    counts = {"steady": 0, "progression": 0, "ramping_undesired": 0, "mixed": 0}
    for r in records:
        counts[r["shape"]] = counts.get(r["shape"], 0) + 1

    total = len(records)
    print(
        f"Tempo audit ({total} files): "
        f"{counts['steady']} steady / "
        f"{counts['progression']} progression / "
        f"{counts['ramping_undesired']} ramping_undesired / "
        f"{counts['mixed']} mixed"
    )
    print(f"Detail written to {OUTPUT_PATH}")

    if counts["ramping_undesired"]:
        print("\nFiles classified as ramping_undesired (sample up to 10):")
        sample = [r["file"] for r in records if r["shape"] == "ramping_undesired"][:10]
        for f in sample:
            print(f"  - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
