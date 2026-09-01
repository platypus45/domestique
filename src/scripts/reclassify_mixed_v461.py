#!/usr/bin/env python3
"""Wave 1A RECLASSIFY-MIXED-v461 — promote mixed-class ZWO files into reachable buckets.

After the v4.6.0 LIBRARY-OVERHAUL, 1069 of 3054 files are content_class="mixed".
Many of those files contain genuinely interesting microintervals, over-unders,
sprints, and sweet-spot work — but the planner cannot reach them because they
are not assigned to any specific intent bucket.

This pass re-examines each "mixed" entry and promotes it via:
  1. Spec-defined priority rules using secondary_flags + zone-time signals
     (Rønnestad-style microintervals → vo2_short / anaerobic, etc.)
  2. Zone-only fallback rules to catch the long tail of files where no
     `secondary_flags` triggered the structural classifier (mostly steady-state
     workouts that lacked enough hard-segment count to be classified by the
     classifier).
  3. Special "Rønnestad detection" via direct ZWO `<IntervalsT>` parse:
     cycle_period 30-90s + 10+ reps + work-power 95-115% FTP →
     `tags: ["is_ronnestad"]` and `<name>` updated to make the protocol
     obvious in the library.

Run:
    python3 scripts/reclassify_mixed_v461.py
    python3 scripts/reclassify_mixed_v461.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = ROOT / "workouts"
CACHE_PATH = WORKOUTS_DIR / ".content_classification.json"
MANIFEST_PATH = WORKOUTS_DIR / ".overhaul_manifest.json"

CLASS_DISPLAY = {
    "recovery": "Recovery",
    "endurance": "Endurance",
    "tempo": "Tempo",
    "sweet_spot": "Sweet Spot",
    "threshold": "Threshold",
    "vo2max": "VO2max",
    "vo2_short": "VO2 Short",
    "over_under": "Over-Under",
    "anaerobic": "Anaerobic",
    "neuromuscular": "Neuromuscular",
    "ftp_test": "FTP Test",
    "mixed": "Mixed",
}


def promote_mixed(entry: dict, filename: str = "") -> str:
    """Return the new content_class for a mixed entry. Spec rules + zone fallback.

    Respects existing library-consistency invariants:
      * recovery class: requires z1_pct ≥ 70 (test_recovery_avg_below_55)
      * vo2max class: requires z5 ≥ 5min (test_vo2max_intervals_in_band)
      * threshold class: requires z4 ≥ 10min (test_threshold_main_set_in_band)
      * sweet_spot class: requires sweet_spot ≥ 10min (test_sweet_spot_band_time)
      * over_under class: requires ou_transitions ≥ 3 OR pattern_over_under flag
        (test_over_under_alternation)
      * recovery_*.zwo files must stay in {recovery, mixed} (test_recovery_prefix_consistent)
      * neuromuscular_*/sprints_*.zwo files must stay neuromuscular (test_neuromuscular_prefix_consistent)
    """
    flags = entry.get("secondary_flags", {})
    feat = entry.get("features", {})
    total_dur_min = feat.get("duration_s", 0) / 60
    valid_dur_s = feat.get("valid_dur_s", feat.get("duration_s", 0))
    valid_dur_min = valid_dur_s / 60
    z1_pct = feat.get("z1_pct", 0)
    z2_pct = feat.get("z2_pct", 0)
    z3_pct = feat.get("z3_pct", 0)
    z4_pct = feat.get("z4_pct", 0)
    z5_pct = feat.get("z5_pct", 0)
    z6_pct = feat.get("z6_pct", 0)
    z7_pct = feat.get("z7_pct", 0)
    z3_min = z3_pct / 100 * valid_dur_min
    z4_min = z4_pct / 100 * valid_dur_min
    z5_min = z5_pct / 100 * valid_dur_min
    z6_min = z6_pct / 100 * valid_dur_min
    z7_min = z7_pct / 100 * valid_dur_min
    ss_pct = feat.get("sweet_spot_pct", 0)
    ss_min = ss_pct / 100 * valid_dur_min
    ou_transitions = feat.get("ou_transitions", 0)

    fn_lower = filename.lower()
    is_recovery_prefix = fn_lower.startswith("recovery_")

    # Recovery-prefix files can only become {recovery, mixed} per
    # test_recovery_prefix_consistent.
    if is_recovery_prefix:
        if z1_pct >= 70:
            return "recovery"
        return "mixed"

    # ── Spec rules (priority order) ──────────────────────────────────────────

    # Priority 1: pattern_microinterval (Rønnestad / Billat 30/30 / etc.)
    if flags.get("pattern_microinterval"):
        if z6_min >= 1:
            return "anaerobic"
        if z5_min >= 4:
            return "vo2_short"
        if z4_min >= 4:
            return "vo2_short"

    # Priority 2: pattern_over_under (also need ou_transitions ≥ 3 to satisfy
    # test_over_under_alternation; fall through to next rule if not).
    if flags.get("pattern_over_under") and ou_transitions >= 3:
        return "over_under"

    # Priority 3: sprints (Z7 anaerobic-capacity / neuromuscular)
    if flags.get("has_sprints") and z7_min >= 0.5:
        return "neuromuscular"

    # Priority 4: substantial Z6 anaerobic time
    if z6_min >= 3 and z5_min < 6:
        return "anaerobic"

    # Priority 5: VO2max (flag + dose). Require ≥5min Z5 to satisfy
    # test_vo2max_intervals_in_band.
    if flags.get("has_vo2_work") and z5_min >= 5:
        return "vo2max"

    # Priority 6: threshold (flag + dose). Require ≥10min Z4 to satisfy
    # test_threshold_main_set_in_band.
    if flags.get("has_threshold_work") and z4_min >= 10:
        return "threshold"

    # Priority 7: sweet spot (flag + dose). Require ≥10min in 0.84-0.94 band
    # to satisfy test_sweet_spot_band_time.
    if flags.get("has_sweet_spot_work") and ss_min >= 10:
        return "sweet_spot"

    # Priority 8: tempo (zone-time only)
    if z3_min >= 20:
        return "tempo"

    # ── Zone-only fallback (no flag set but clear zone signature) ────────────
    #
    # Targeting: prefer destinations that the planner already lists in
    # `type_to_fallback` for many slot types (Sweet Spot, Threshold, VO2max,
    # Over-Unders) so promoting OUT of "Mixed" doesn't shrink the practical
    # candidate pool for any slot. Avoid promoting marginal-Z3 files into
    # `tempo` (Tempo is only in the fallback pool of two slot types) — leave
    # them as `mixed` and let the planner's filename-prefix routing surface
    # them.

    # vo2max only if ≥5min Z5 (test_vo2max_intervals_in_band)
    if z5_min >= 5:
        return "vo2max"

    # threshold only if ≥10min Z4 AND not sweet-spot heavy
    if z4_min >= 10 and ss_min < 10:
        return "threshold"

    # sweet_spot only if ≥10min in band (test_sweet_spot_band_time strict)
    if ss_min >= 10:
        return "sweet_spot"

    # over_under fallback (high ou_transitions count even without flag)
    if ou_transitions >= 3:
        return "over_under"

    # tempo zone-only fallback (needs z3 ≥ 12min — relaxed from spec ≥20 but
    # strict enough to avoid over-promoting marginal-Z3 mixed files)
    if z3_min >= 12:
        return "tempo"

    # recovery only if z1_pct ≥ 70 (test_recovery_avg_below_55 strict)
    if z1_pct >= 70 and total_dur_min >= 10:
        return "recovery"

    # endurance if mostly Z1+Z2 (Endurance is in z2/long_z2/recovery fallback)
    if (z1_pct + z2_pct) >= 60 and total_dur_min >= 10:
        return "endurance"

    return "mixed"


def parse_zwo_intervals(zwo_path: Path) -> list[dict]:
    """Pull every <IntervalsT> tuple from a ZWO file.

    Returns: [{"reps": int, "on_s": int, "off_s": int, "on_p": float, "off_p": float}].
    """
    out: list[dict] = []
    try:
        tree = ET.parse(zwo_path)
    except Exception:
        return out
    root = tree.getroot()
    wk = root.find("workout")
    if wk is None:
        return out
    for seg in wk.findall("IntervalsT"):
        try:
            reps = int(seg.get("Repeat", 1))
            on_s = int(float(seg.get("OnDuration", 0) or 0))
            off_s = int(float(seg.get("OffDuration", 0) or 0))
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
        except (TypeError, ValueError):
            continue
        out.append({
            "reps": reps, "on_s": on_s, "off_s": off_s,
            "on_p": on_p, "off_p": off_p,
        })
    return out


def detect_ronnestad(zwo_path: Path) -> dict | None:
    """Detect Rønnestad-style microinterval block.

    Reference: Rønnestad et al. 2015, Scand J Med Sci Sports 25:143-151.
    Canonical: 30/15s or 40/20s, 10+ reps per block, 95-115% FTP work power.

    Returns dict with {"protocol": "30/15"|"40/20"|"micro", "reps": int,
    "on_s": int, "off_s": int, "on_p": float, "blocks": int} if detected,
    else None.
    """
    intervals = parse_zwo_intervals(zwo_path)
    if not intervals:
        return None
    ronnestad_blocks: list[dict] = []
    for iv in intervals:
        cycle = iv["on_s"] + iv["off_s"]
        if (iv["reps"] >= 10
                and 30 <= cycle <= 90
                and 0.95 <= iv["on_p"] <= 1.15):
            ronnestad_blocks.append(iv)
    if not ronnestad_blocks:
        return None
    # Pick the dominant block (most reps)
    main = max(ronnestad_blocks, key=lambda x: x["reps"])
    on_s = main["on_s"]
    off_s = main["off_s"]
    if on_s == 30 and off_s == 15:
        protocol = "30/15"
    elif on_s == 40 and off_s == 20:
        protocol = "40/20"
    else:
        protocol = f"{on_s}/{off_s}"
    return {
        "protocol": protocol,
        "reps": main["reps"],
        "on_s": on_s,
        "off_s": off_s,
        "on_p": main["on_p"],
        "blocks": len(ronnestad_blocks),
    }


def name_for_class(new_class: str, total_min: int, ronnestad: dict | None,
                   old_name: str) -> str:
    """Build the new <name>. Use Rønnestad protocol naming when applicable;
    otherwise re-label the class prefix while keeping any ``NxM`` pattern
    from the old name intact.
    """
    if ronnestad is not None:
        proto = ronnestad["protocol"]
        reps = ronnestad["reps"]
        blocks = ronnestad["blocks"]
        if blocks > 1:
            return (f"{CLASS_DISPLAY.get(new_class, 'Mixed')} "
                    f"Rønnestad-style {blocks}x{reps}x{proto}s ({total_min}min)")
        return (f"{CLASS_DISPLAY.get(new_class, 'Mixed')} "
                f"Rønnestad-style {reps}x{proto}s ({total_min}min)")

    # Replace leading word with class display, preserving the rest of the name
    display = CLASS_DISPLAY.get(new_class, "Mixed")
    parts = old_name.split(maxsplit=1)
    if len(parts) >= 2:
        return f"{display} {parts[1]}"
    return f"{display} ({total_min}min)"


def update_zwo_name(zwo_path: Path, new_name: str) -> bool:
    """Rewrite <name> in the ZWO file. Return True if changed."""
    tree = ET.parse(zwo_path)
    root = tree.getroot()
    name_el = root.find("name")
    if name_el is None:
        return False
    if (name_el.text or "").strip() == new_name:
        return False
    name_el.text = new_name
    # Preserve XML declaration
    tree.write(zwo_path, encoding="utf-8", xml_declaration=True)
    # Match existing single-quote XML decl style
    raw = zwo_path.read_text(encoding="utf-8")
    raw = raw.replace('<?xml version="1.0" encoding="utf-8"?>',
                      "<?xml version='1.0' encoding='utf-8'?>", 1)
    zwo_path.write_text(raw, encoding="utf-8")
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print before/after counts without writing files")
    args = p.parse_args()

    # ── Load classification cache
    with CACHE_PATH.open() as f:
        cache = json.load(f)
    classifications = cache["classifications"]

    before = Counter(v.get("primary", "?") for v in classifications.values())

    # ── Pass 1: promotion via secondary_flags + zone-time
    promotions: list[dict] = []
    ronnestad_hits: list[dict] = []

    for fn, entry in classifications.items():
        if entry.get("primary") != "mixed":
            # Still scan for Rønnestad in non-mixed (so we can rename them too)
            ronn = detect_ronnestad(WORKOUTS_DIR / fn)
            if ronn is not None:
                ronnestad_hits.append({"filename": fn,
                                       "old_class": entry.get("primary"),
                                       "new_class": entry.get("primary"),
                                       "ronnestad": ronn})
            continue

        new_class = promote_mixed(entry, filename=fn)
        ronn = detect_ronnestad(WORKOUTS_DIR / fn)

        # Rønnestad detection upgrades classification when it's microinterval
        # but the spec rules didn't fire (often because Z5/Z6 dose was below
        # the threshold the classifier uses but the protocol is still a clear
        # Rønnestad-style block).
        if ronn is not None:
            on_p = ronn["on_p"]
            if on_p >= 1.05:
                # 105-115% FTP → VO2max-targeted microintervals
                if new_class in ("mixed", "tempo", "endurance",
                                 "recovery", "sweet_spot"):
                    new_class = "vo2_short"
            else:
                # 95-105% FTP → threshold-shaped microintervals (still vo2_short)
                if new_class in ("mixed", "tempo", "endurance",
                                 "recovery", "sweet_spot"):
                    new_class = "vo2_short"

        rationale_bits: list[str] = []
        flags = entry.get("secondary_flags", {})
        feat = entry.get("features", {})
        z3 = feat.get("z3_pct", 0) / 100 * feat.get("duration_s", 0) / 60
        z4 = feat.get("z4_pct", 0) / 100 * feat.get("duration_s", 0) / 60
        z5 = feat.get("z5_pct", 0) / 100 * feat.get("duration_s", 0) / 60
        z6 = feat.get("z6_pct", 0) / 100 * feat.get("duration_s", 0) / 60
        z7 = feat.get("z7_pct", 0) / 100 * feat.get("duration_s", 0) / 60
        if flags.get("pattern_microinterval"):
            rationale_bits.append("microinterval")
        if flags.get("pattern_over_under"):
            rationale_bits.append("over_under_pattern")
        if flags.get("has_sprints"):
            rationale_bits.append(f"sprints+z7={z7:.1f}m")
        if z6 >= 1:
            rationale_bits.append(f"z6={z6:.1f}m")
        if z5 >= 3:
            rationale_bits.append(f"z5={z5:.1f}m")
        if z4 >= 4:
            rationale_bits.append(f"z4={z4:.1f}m")
        if z3 >= 8:
            rationale_bits.append(f"z3={z3:.1f}m")
        if ronn is not None:
            rationale_bits.append(f"ronnestad_{ronn['protocol']}")
        rationale = ",".join(rationale_bits) if rationale_bits else "no_signal"

        promotions.append({
            "filename": fn,
            "old_class": "mixed",
            "new_class": new_class,
            "rationale": rationale,
            "ronnestad": ronn,
        })
        if ronn is not None:
            ronnestad_hits.append({"filename": fn, "old_class": "mixed",
                                   "new_class": new_class, "ronnestad": ronn})

    # ── Pass 2: apply promotions to cache and rewrite ZWO names
    name_changes = 0
    for prom in promotions:
        fn = prom["filename"]
        new_class = prom["new_class"]
        entry = classifications[fn]
        if new_class != "mixed":
            entry["primary"] = new_class
        if prom["ronnestad"]:
            entry.setdefault("tags", []).append("is_ronnestad")
            entry["ronnestad_protocol"] = prom["ronnestad"]["protocol"]

        zwo_path = WORKOUTS_DIR / fn
        if not zwo_path.exists():
            continue

        # Compute new <name>
        try:
            tree = ET.parse(zwo_path)
        except Exception:
            continue
        old_name = (tree.getroot().findtext("name") or "").strip()
        total_min = round(entry.get("features", {}).get("duration_s", 0) / 60)
        new_name = name_for_class(new_class, total_min, prom["ronnestad"],
                                  old_name)

        if not args.dry_run and new_name != old_name:
            if update_zwo_name(zwo_path, new_name):
                name_changes += 1
        prom["old_name"] = old_name
        prom["new_name"] = new_name

    # ── Pass 3: also tag Rønnestad-style files that were never "mixed"
    for hit in ronnestad_hits:
        if hit.get("old_class") == "mixed":
            continue  # already handled in Pass 2
        fn = hit["filename"]
        entry = classifications[fn]
        if "is_ronnestad" not in entry.get("tags", []):
            entry.setdefault("tags", []).append("is_ronnestad")
            entry["ronnestad_protocol"] = hit["ronnestad"]["protocol"]
        zwo_path = WORKOUTS_DIR / fn
        if not zwo_path.exists():
            continue
        try:
            tree = ET.parse(zwo_path)
        except Exception:
            continue
        old_name = (tree.getroot().findtext("name") or "").strip()
        total_min = round(entry.get("features", {}).get("duration_s", 0) / 60)
        new_name = name_for_class(hit["new_class"], total_min,
                                  hit["ronnestad"], old_name)
        if "Rønnestad" in old_name:
            continue  # already named properly
        if not args.dry_run and new_name != old_name:
            if update_zwo_name(zwo_path, new_name):
                name_changes += 1

    after = Counter(v.get("primary", "?") for v in classifications.values())

    # ── Refresh workouts_dir_hash so planner doesn't log a stale-cache warning
    if not args.dry_run:
        try:
            import hashlib
            h = hashlib.sha256()
            for p in sorted(WORKOUTS_DIR.glob("*.zwo")):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0
                h.update(f"{p.name}:{mtime}\n".encode())
            cache["workouts_dir_hash"] = h.hexdigest()
        except Exception:
            pass

    # ── Save updated cache
    if not args.dry_run:
        with CACHE_PATH.open("w") as f:
            json.dump(cache, f, indent=2)

    # ── Append manifest entries
    manifest_entries: list[dict] = []
    for prom in promotions:
        if prom["new_class"] == "mixed" and prom["ronnestad"] is None:
            continue
        manifest_entries.append({
            "filename": prom["filename"],
            "old_class": "mixed",
            "new_class": prom["new_class"],
            "rationale": prom["rationale"],
            "is_ronnestad": prom["ronnestad"] is not None,
            "ronnestad_protocol": (prom["ronnestad"]["protocol"]
                                   if prom["ronnestad"] else None),
            "old_name": prom.get("old_name", ""),
            "new_name": prom.get("new_name", ""),
        })
    for hit in ronnestad_hits:
        if hit.get("old_class") == "mixed":
            continue
        manifest_entries.append({
            "filename": hit["filename"],
            "old_class": hit["old_class"],
            "new_class": hit["new_class"],
            "rationale": "ronnestad_only",
            "is_ronnestad": True,
            "ronnestad_protocol": hit["ronnestad"]["protocol"],
        })

    if not args.dry_run:
        with MANIFEST_PATH.open() as f:
            manifest = json.load(f)
        prior = manifest.get("v461_reclassify", {}) or {}
        prior_entries = prior.get("entries", []) if isinstance(prior, dict) else []
        # Merge: keep prior entries unless the file appears in new_entries.
        seen_files = {e.get("filename") for e in manifest_entries}
        merged_entries = manifest_entries + [
            e for e in prior_entries if e.get("filename") not in seen_files
        ]
        manifest["v461_reclassify"] = {
            "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "before": dict(before),
            "after": dict(after),
            "promoted": sum(1 for p in promotions if p["new_class"] != "mixed"),
            "ronnestad_count": len(ronnestad_hits),
            "entries": merged_entries,
        }
        with MANIFEST_PATH.open("w") as f:
            json.dump(manifest, f, indent=2)

    # ── Print summary
    print(f"=== Wave 1A RECLASSIFY-MIXED-v461 {'DRY-RUN ' if args.dry_run else ''}===")
    print(f"\nBefore (from cache):")
    for c in sorted(before.keys()):
        print(f"  {c}: {before[c]}")
    print(f"\nAfter:")
    for c in sorted(after.keys()):
        delta = after[c] - before.get(c, 0)
        sign = "+" if delta > 0 else ""
        print(f"  {c}: {after[c]} ({sign}{delta})")
    print(f"\nMixed promoted: "
          f"{sum(1 for p in promotions if p['new_class'] != 'mixed')}")
    print(f"Rønnestad-style detected: {len(ronnestad_hits)}")
    print(f"<name> changes written: {name_changes}")
    if ronnestad_hits[:3]:
        print(f"\nSample Rønnestad files:")
        for h in ronnestad_hits[:5]:
            r = h["ronnestad"]
            print(f"  {h['filename']} → {r['protocol']} "
                  f"({r['reps']} reps × {r['blocks']} blocks @ {r['on_p']*100:.0f}% FTP)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
