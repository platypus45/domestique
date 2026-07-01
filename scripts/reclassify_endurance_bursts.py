#!/usr/bin/env python3
"""Reclassify endurance/recovery workouts that actually contain repeated hard
bursts as endurance_intervals ("Endurance + Strides") — they are NOT Zone 2.

The classifier keeps primary=endurance/recovery even when a workout carries short
high-power bursts (they're only "allowed secondary flags"), so a Z2-base ride with
6x 20s VO2 pops still shows as "Endurance / Z2". This scans the ZWO CONTENT (not
just the flags — short bursts don't trip has_sprints/has_vo2_work) and reclassifies
the offenders.

Surgical + idempotent: touches ONLY these files and ONLY the type fields, in BOTH
caches — .content_classification.json (primary) and .library_index.json
(ContentClass + Protocol). It does NOT re-run the full classifier, which is
non-deterministic (an unchanged re-run spuriously flips ~59 hard workouts to
endurance — see memory: warmup-migration-held / planner-test-nondeterminism).

Usage:
    python3 scripts/reclassify_endurance_bursts.py            # dry-run
    python3 scripts/reclassify_endurance_bursts.py --apply    # write
"""
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

WK = Path(__file__).resolve().parent.parent / "workouts"
BURST = 0.95            # >= 95% FTP = Z4/threshold and above — not Zone 2
MIN_BURSTS = 2          # repeated => intervals/strides, not one stray surge
SRC = {"endurance", "recovery"}
TARGET = "endurance_intervals"
TARGET_PROTOCOL = "Endurance + Strides"


MAX_STRIDE_S = 90       # a "stride" is a SHORT pop; longer = a real interval
MAX_HARD_TOTAL_S = 600  # >10 min of hard work = a real interval workout, not strides


def is_short_strides(zwo: Path) -> bool:
    """True only for genuine 'endurance + strides': >=2 hard pops (>=BURST FTP),
    every hard effort SHORT (<=MAX_STRIDE_S) and total hard time small
    (<MAX_HARD_TOTAL_S). A file with sustained hard efforts (5-min threshold
    blocks, 3x4min, over-unders) is a real interval workout — NOT strides — and is
    deliberately left alone (its true type needs the classifier, which is buggy;
    guessing here mislabels it, e.g. endurance_steady -> vo2max). A hard bit can
    be the OnPower OR the OffPower of an IntervalsT (some put the burst in 'off')."""
    try:
        w = ET.parse(zwo).getroot().find("workout")
    except Exception:
        return False
    if w is None:
        return False
    n = 0
    max_effort = 0.0
    hard_total = 0.0
    for el in w:
        a = el.attrib
        if el.tag == "SteadyState":
            if float(a.get("Power", 0) or 0) >= BURST:
                d = float(a.get("Duration", 0) or 0)
                n += 1; max_effort = max(max_effort, d); hard_total += d
        elif el.tag == "IntervalsT":
            r = int(a.get("Repeat", 1) or 1)
            on, off = float(a.get("OnDuration", 0) or 0), float(a.get("OffDuration", 0) or 0)
            onp, offp = float(a.get("OnPower", 0) or 0), float(a.get("OffPower", 0) or 0)
            if onp >= BURST:
                n += r; max_effort = max(max_effort, on); hard_total += r * on
            if offp >= BURST:
                n += r; max_effort = max(max_effort, off); hard_total += r * off
        # Warmup/Cooldown/Ramp/FreeRide ignored — a ramp is not a burst.
    return n >= MIN_BURSTS and max_effort <= MAX_STRIDE_S and hard_total < MAX_HARD_TOTAL_S


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    cc_path = WK / ".content_classification.json"
    idx_path = WK / ".library_index.json"
    cc = json.loads(cc_path.read_text())
    idx = json.loads(idx_path.read_text())

    targets = [
        fn for fn, e in cc["classifications"].items()
        if e.get("primary") in SRC and (WK / fn).exists()
        and is_short_strides(WK / fn)
    ]
    tset = set(targets)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(targets)} endurance/recovery files with >={MIN_BURSTS} SHORT "
          f"strides (<={MAX_STRIDE_S}s each, <{MAX_HARD_TOTAL_S}s total, >={BURST} FTP) -> {TARGET}")
    if not args.apply:
        for fn in sorted(targets)[:20]:
            print("   ", cc["classifications"][fn]["primary"], fn)
        if len(targets) > 20:
            print(f"    … +{len(targets) - 20} more")
        return

    for fn in targets:
        cc["classifications"][fn]["primary"] = TARGET
    n_rows = 0
    for row in idx["rows"]:
        if row.get("File") in tset:
            row["ContentClass"] = TARGET
            row["Protocol"] = TARGET_PROTOCOL
            n_rows += 1
    # Preserve each file's on-disk formatting (classification=indent 2, index=compact).
    cc_path.write_text(json.dumps(cc, indent=2))
    idx_path.write_text(json.dumps(idx))
    print(f"patched {len(targets)} classifications + {n_rows} index rows")


if __name__ == "__main__":
    main()
