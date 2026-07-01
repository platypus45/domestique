#!/usr/bin/env python3
"""Correct the sustained-hard workouts that ``classify_v104`` mislabels as
endurance/recovery.

Root cause: a workout whose hard main-set falls just under EVERY strict dose gate
(8-min VO2, 15-min threshold, 18-min over-under band, …) drops through to the
zone-dominance fallback, which routes by the single dominant zone and ignores the
cumulative/structural hard stimulus — so a 6×2 min threshold set inside a Z2 ride,
or a Billat 30/30, lands on ``endurance``/``recovery``. The planner can then serve
those on easy days.

This does NOT re-run the classifier (a full index rebuild drifts ~1600 unrelated
rows, and recovery_*.zwo are test-locked to recovery). It surgically corrects a
hand-verified set of files (labels reconciled from two independent classification
passes) in BOTH caches:

  * ``.content_classification.json`` — the entry is regenerated from fresh v104
    features with the primary FORCED to the verified label and the display_name
    regenerated to match (so it is schema-identical to a classifier entry).
  * ``.library_index.json`` — the row's ContentClass / Protocol / SecondaryFlags /
    ContentConfidence are patched in place (no full rebuild).

It also refreshes the display_name of the endurance_intervals ("strides") files,
whose names were left stale by scripts/reclassify_endurance_bursts.py.

Usage:
    python3 scripts/reclassify_sustained.py <reconciled_labels.json>          # dry-run
    python3 scripts/reclassify_sustained.py <reconciled_labels.json> --apply
"""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WK = ROOT / "workouts"
CC_PATH = WK / ".content_classification.json"
IDX_PATH = WK / ".library_index.json"

_spec = importlib.util.spec_from_file_location(
    "clf", ROOT / "scripts" / "classify_library_content.py")
clf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clf)

# Mirror training_planner._CONTENT_TO_PROTOCOL (index Protocol display).
CONTENT_TO_PROTOCOL = {
    "recovery": "Recovery", "endurance": "Endurance",
    "endurance_intervals": "Endurance + Strides",
    "tempo": "Tempo", "tempo_intervals": "Tempo Intervals",
    "tempo_ladder": "Tempo Ladder", "sweet_spot": "Sweet Spot",
    "sweet_spot_ladder": "Sweet Spot Ladder", "threshold": "Threshold",
    "threshold_ladder": "Threshold Ladder", "over_under": "Over-Unders",
    "vo2max": "VO2max", "vo2_ladder": "VO2 Ladder", "vo2_short": "VO2max",
    "anaerobic": "Anaerobic", "neuromuscular": "Sprint", "ftp_test": "FTP Test",
}


def rebuild_entry(fn: str, forced_primary: str) -> dict:
    """A full v104 classification entry with ``primary`` forced and the
    display_name regenerated for that primary (everything else fresh)."""
    entry = clf.classify_zwo_v104(WK / fn)          # fresh feat_out + secondary_flags
    power, tags, meta, segments = clf.parse_zwo_full(WK / fn)
    features = clf.extract_features_v104(power, segments)
    display_name = clf.generate_display_name(forced_primary, features, segments, meta=meta)
    coherent, suffixes = clf.objective_coherence(forced_primary, entry["secondary_flags"])
    if not coherent:
        display_name = f"{display_name} {' '.join(suffixes)}".rstrip()
    entry["primary"] = forced_primary
    entry["display_name"] = display_name
    entry["objective_coherent"] = coherent
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", help="JSON: {filename.zwo: content_class}")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    labels: dict[str, str] = json.loads(Path(args.labels).read_text())
    cc = json.loads(CC_PATH.read_text())
    idx = json.loads(IDX_PATH.read_text())
    classifications = cc["classifications"]
    rows = {r.get("File"): r for r in idx["rows"]}

    bad = [f for f in labels if f not in classifications]
    if bad:
        raise SystemExit(f"labels reference unknown files: {bad}")
    bad_cls = [c for c in labels.values() if c not in CONTENT_TO_PROTOCOL]
    if bad_cls:
        raise SystemExit(f"unknown content classes: {set(bad_cls)}")

    changes = []
    for fn, new_cls in sorted(labels.items()):
        old = classifications[fn].get("primary")
        entry = rebuild_entry(fn, new_cls)
        changes.append((fn, old, new_cls, entry))

    # Also refresh stale display_name on the strides files (primary already set).
    stride_fixes = []
    for fn, e in classifications.items():
        if e.get("primary") == "endurance_intervals" and fn not in labels:
            new_entry = rebuild_entry(fn, "endurance_intervals")
            if new_entry["display_name"] != e.get("display_name"):
                stride_fixes.append((fn, new_entry))

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: "
          f"{len(changes)} sustained-hard reclassified, "
          f"{len(stride_fixes)} stride display-names refreshed")
    from collections import Counter
    print("target distribution:", dict(Counter(c[2] for c in changes)))
    for fn, old, new, _ in changes:
        print(f"   {old:9} -> {new:16} {fn}")

    if not args.apply:
        return

    for fn, _old, new_cls, entry in changes:
        classifications[fn] = entry
        row = rows.get(fn)
        if row is not None:
            row["ContentClass"] = new_cls
            row["Protocol"] = CONTENT_TO_PROTOCOL[new_cls]
            row["SecondaryFlags"] = entry["secondary_flags"]
            row["ContentConfidence"] = entry["confidence"]
    for fn, entry in stride_fixes:
        classifications[fn] = entry

    CC_PATH.write_text(json.dumps(cc, indent=2))
    IDX_PATH.write_text(json.dumps(idx))
    print(f"patched {len(changes)} classifications + index rows, "
          f"refreshed {len(stride_fixes)} stride names")


if __name__ == "__main__":
    main()
