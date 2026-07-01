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


def burst_count(zwo: Path) -> int:
    """Number of >=BURST-FTP efforts. A hard bit can be the OnPower OR the
    OffPower of an IntervalsT (some files put the burst in the 'off' slot)."""
    try:
        w = ET.parse(zwo).getroot().find("workout")
    except Exception:
        return 0
    if w is None:
        return 0
    n = 0
    for el in w:
        a = el.attrib
        if el.tag == "SteadyState":
            if float(a.get("Power", 0) or 0) >= BURST:
                n += 1
        elif el.tag == "IntervalsT":
            hi = max(float(a.get("OnPower", 0) or 0), float(a.get("OffPower", 0) or 0))
            if hi >= BURST:
                n += int(a.get("Repeat", 1) or 1)
        # Warmup/Cooldown/Ramp/FreeRide deliberately ignored — a ramp is not a burst.
    return n


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
        and burst_count(WK / fn) >= MIN_BURSTS
    ]
    tset = set(targets)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(targets)} endurance/recovery files with >={MIN_BURSTS} "
          f"bursts (>={BURST} FTP) -> {TARGET}")
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
