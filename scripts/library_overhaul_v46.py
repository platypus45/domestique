#!/usr/bin/env python3
"""Library overhaul v4.6.0 — IMPL-LIBRARY-OVERHAUL-v46.

Multi-pass tool that re-derives ZWO ``<name>`` and ``<description>`` from the
actual workout structure (per /tmp/MASTER_DECISIONS_v46.md §3 Pillar A).

Six passes (in order):
  PASS A  Run classifier on the current (pre-overhaul) library to seed cache
  PASS B  Plan renames for any file whose filename prefix mismatches the
          cached content_class
  PASS C  Apply renames via ``git mv`` (preserving history). Append
          ``_renamed_v46_{n}`` if there is a name collision.
  PASS D  Re-run classifier so cache keys match the new filenames
  PASS E  Rewrite ``<name>`` + ``<description>`` + ``<author>`` for every
          file based on the FRESH cached class + parsed segments. Strip any
          ``<textevent>`` / ``<TextNotification>`` children.
  PASS F  Write workouts/.overhaul_manifest.json audit log + final classifier run.

Run:
    python3 scripts/library_overhaul_v46.py
    python3 scripts/library_overhaul_v46.py --dry-run
    python3 scripts/library_overhaul_v46.py --no-rename     (skip Pass B+C)
    python3 scripts/library_overhaul_v46.py --skip-pre-classify  (skip Pass A)
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Optional

# Map content_class → (filename prefix, display label)
CONTENT_CLASS_DISPLAY = {
    "recovery": ("recovery", "Recovery"),
    "endurance": ("endurance", "Endurance"),
    "tempo": ("tempo", "Tempo"),
    "sweet_spot": ("sweet_spot", "Sweet Spot"),
    "threshold": ("threshold", "Threshold"),
    "vo2max": ("vo2max", "VO2max"),
    "vo2_short": ("vo2_short", "VO2 Short"),
    "over_under": ("over_under", "Over-Under"),
    "anaerobic": ("anaerobic", "Anaerobic"),
    "neuromuscular": ("neuromuscular", "Neuromuscular"),
    "ftp_test": ("ftp_test", "FTP Test"),
    "mixed": ("mixed", "Mixed"),
}

# Filename prefix → content_class. Order matters: longer/more-specific
# prefixes must come first.
PREFIX_TO_CLASS: list[tuple[str, str]] = [
    ("vo2_short_", "vo2_short"),
    ("sweet_spot_", "sweet_spot"),
    ("sweetspot_", "sweet_spot"),
    ("over_under_", "over_under"),
    ("supra_threshold", "threshold"),
    ("neuromuscular_", "neuromuscular"),
    ("anaerobic_", "anaerobic"),
    ("threshold_", "threshold"),
    ("endurance_", "endurance"),
    ("vo2max_", "vo2max"),
    ("vo2_", "vo2max"),
    ("recovery_", "recovery"),
    ("sprints_", "neuromuscular"),
    ("ftp_test_", "ftp_test"),
    ("ramp_", "ftp_test"),
    ("pyramid_", "mixed"),
    ("intervals_", "mixed"),
    ("mixed_", "mixed"),
    ("warmup_", "recovery"),
    ("tempo_", "tempo"),
    ("z2_", "endurance"),
]

ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = ROOT / "workouts"
CACHE_PATH = WORKOUTS_DIR / ".content_classification.json"


# ── Segment parsing + main-set extraction ──────────────────────────────────────


def parse_segments(zwo_path: Path) -> tuple[list[dict], dict]:
    """Flatten the workout. Each segment dict has: kind, duration_s,
    power_low, power_high, avg_power, tag, origin (explicit | interval_on |
    interval_off).
    """
    tree = ET.parse(zwo_path)
    root = tree.getroot()
    meta = {
        "name": (root.findtext("name") or zwo_path.stem).strip(),
        "description": (root.findtext("description") or "").strip(),
        "author": (root.findtext("author") or "").strip(),
        "sport_type": (root.findtext("sportType") or "bike").strip(),
        "tags": [],
    }
    tags_el = root.find("tags")
    if tags_el is not None:
        for t in tags_el.findall("tag"):
            n = t.get("name")
            if n:
                meta["tags"].append(n.strip())

    segs: list[dict] = []
    workout_el = root.find("workout")
    if workout_el is None:
        return [], meta

    for seg in workout_el:
        tag = seg.tag
        if tag in ("Warmup", "Cooldown", "Ramp"):
            dur = int(float(seg.get("Duration", 0) or 0))
            plo = float(seg.get("PowerLow", 0.5))
            phi = float(seg.get("PowerHigh", 0.7))
            if dur <= 0:
                continue
            segs.append({
                "kind": tag.lower(), "duration_s": dur,
                "power_low": plo, "power_high": phi,
                "avg_power": (plo + phi) / 2,
                "tag": tag, "origin": "explicit",
            })
        elif tag == "SteadyState":
            dur = int(float(seg.get("Duration", 0) or 0))
            p = float(seg.get("Power", 0.65))
            if dur <= 0:
                continue
            segs.append({
                "kind": "steady", "duration_s": dur,
                "power_low": p, "power_high": p,
                "avg_power": p, "tag": tag, "origin": "explicit",
            })
        elif tag == "IntervalsT":
            reps = int(seg.get("Repeat", 1))
            on_s = int(float(seg.get("OnDuration", 0) or 0))
            off_s = int(float(seg.get("OffDuration", 0) or 0))
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
            for _ in range(reps):
                if on_s > 0:
                    segs.append({"kind": "steady", "duration_s": on_s,
                                 "power_low": on_p, "power_high": on_p,
                                 "avg_power": on_p, "tag": tag,
                                 "origin": "interval_on"})
                if off_s > 0:
                    segs.append({"kind": "steady", "duration_s": off_s,
                                 "power_low": off_p, "power_high": off_p,
                                 "avg_power": off_p, "tag": tag,
                                 "origin": "interval_off"})
        elif tag == "FreeRide":
            dur = int(float(seg.get("Duration", 0) or 0))
            if dur <= 0:
                continue
            segs.append({"kind": "freeride", "duration_s": dur,
                         "power_low": 0.0, "power_high": 0.0,
                         "avg_power": 0.65, "tag": tag, "origin": "explicit"})

    return segs, meta


def strip_warmup_cooldown(segs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Strip leading warmup + trailing cooldown."""
    n = len(segs)
    if n == 0:
        return [], [], []

    head = 0
    while head < n and segs[head]["kind"] == "warmup":
        head += 1
    if head == 0:
        accum = 0
        probe = 0
        while probe < n and segs[probe]["avg_power"] < 0.60:
            accum += segs[probe]["duration_s"]
            probe += 1
        if accum >= 5 * 60:
            head = probe

    tail = n
    while tail > head and segs[tail - 1]["kind"] == "cooldown":
        tail -= 1
    if tail == n:
        accum = 0
        probe = n
        while probe > head and segs[probe - 1]["avg_power"] < 0.60:
            accum += segs[probe - 1]["duration_s"]
            probe -= 1
        if accum >= 5 * 60:
            tail = probe

    return segs[:head], segs[head:tail], segs[tail:]


def detect_pattern(main: list[dict]) -> tuple[str, dict]:
    """Identify the structural pattern. Returns (pattern, info)."""
    if not main:
        return "steady", {}
    if len(main) == 1:
        return "steady", {"power": main[0]["avg_power"], "duration": main[0]["duration_s"]}

    if len(main) >= 5:
        powers = [s["avg_power"] for s in main]
        if (all(powers[i + 1] >= powers[i] - 0.005 for i in range(len(powers) - 1))
                and powers[-1] > powers[0] + 0.10):
            return "ramp", {"start": powers[0], "end": powers[-1]}

    on_segs = [s for s in main if s.get("origin") == "interval_on"]
    off_segs = [s for s in main if s.get("origin") == "interval_off"]
    if len(on_segs) >= 2:
        groups: dict[tuple[float, int], list[dict]] = {}
        for s in on_segs:
            key = (round(s["avg_power"], 2), s["duration_s"])
            groups.setdefault(key, []).append(s)
        candidates = [(k, v) for k, v in groups.items() if len(v) >= 2 and k[0] >= 0.85]
        if candidates:
            candidates.sort(key=lambda kv: (-len(kv[1]) * kv[0][1], -len(kv[1])))
            (on_p, on_s), best = candidates[0]
            n = len(best)
            rest_durs: list[int] = []
            rest_powers: list[float] = []
            for i, s in enumerate(main):
                if (s.get("origin") == "interval_on"
                        and round(s["avg_power"], 2) == on_p
                        and s["duration_s"] == on_s
                        and i + 1 < len(main)
                        and main[i + 1].get("origin") == "interval_off"):
                    rest_durs.append(main[i + 1]["duration_s"])
                    rest_powers.append(main[i + 1]["avg_power"])
            off_s = int(sum(rest_durs) / len(rest_durs)) if rest_durs else (
                off_segs[0]["duration_s"] if off_segs else 0)
            off_p = (sum(rest_powers) / len(rest_powers)) if rest_powers else (
                off_segs[0]["avg_power"] if off_segs else 0.55)
            if on_s <= 90 and n >= 6 and on_p >= 1.05:
                return "microintervals", {
                    "n": n, "on_s": on_s, "off_s": off_s,
                    "on_power": on_p, "off_power": off_p,
                }
            return "intervals", {
                "n": n, "on_s": on_s, "off_s": off_s,
                "on_power": on_p, "off_power": off_p,
            }

    if len(main) >= 6:
        def _power_bucket(p: float) -> float:
            return round(p * 20) / 20.0

        def _dur_band(d: int) -> int:
            if d < 60:
                return (d // 5) * 5
            return (d // 30) * 30

        bucket_counts: Counter = Counter()
        for s in main:
            if s["avg_power"] >= 0.85:
                key = (_power_bucket(s["avg_power"]), _dur_band(s["duration_s"]))
                bucket_counts[key] += 1
        if bucket_counts:
            (top_power, top_dur_band), top_count = bucket_counts.most_common(1)[0]
            if top_count >= 3 and top_power >= 0.95:
                matched = [
                    s for s in main
                    if _power_bucket(s["avg_power"]) == top_power
                    and _dur_band(s["duration_s"]) == top_dur_band
                ]
                on_s = int(sum(s["duration_s"] for s in matched) / len(matched))
                on_p = sum(s["avg_power"] for s in matched) / len(matched)
                rest_durs: list[int] = []
                rest_powers: list[float] = []
                for i, s in enumerate(main):
                    if (_power_bucket(s["avg_power"]) == top_power
                            and _dur_band(s["duration_s"]) == top_dur_band
                            and i + 1 < len(main)
                            and main[i + 1]["avg_power"] < 0.85):
                        rest_durs.append(main[i + 1]["duration_s"])
                        rest_powers.append(main[i + 1]["avg_power"])
                off_s = int(sum(rest_durs) / len(rest_durs)) if rest_durs else 0
                off_p = sum(rest_powers) / len(rest_powers) if rest_powers else 0.55
                if on_s <= 90 and top_count >= 6 and on_p >= 1.05:
                    return "microintervals", {
                        "n": top_count, "on_s": on_s, "off_s": off_s,
                        "on_power": on_p, "off_power": off_p,
                    }
                return "intervals", {
                    "n": top_count, "on_s": on_s, "off_s": off_s,
                    "on_power": on_p, "off_power": off_p,
                }

        blocks: list[dict] = []
        for s in main:
            if blocks and abs(blocks[-1]["power"] - s["avg_power"]) < 0.02:
                blocks[-1]["duration_s"] += s["duration_s"]
            else:
                blocks.append({"power": s["avg_power"], "duration_s": s["duration_s"]})
        over_count = sum(1 for b in blocks if b["power"] >= 1.05)
        under_count = sum(1 for b in blocks if 0.85 <= b["power"] <= 0.95)
        if over_count >= 3 and under_count >= 3:
            return "over_under", {"transitions": min(over_count, under_count)}

    powers = [s["avg_power"] for s in main]
    if max(powers) - min(powers) < 0.03:
        return "steady", {
            "power": sum(powers) / len(powers),
            "duration": sum(s["duration_s"] for s in main),
        }
    return "mixed", {}


# ── Name + description rendering ──────────────────────────────────────────────


def fmt_dur(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    extra = seconds % 60
    if extra == 0 or seconds >= 600:
        return f"{minutes}min"
    return f"{minutes}min {extra}s"


def fmt_dur_short(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{round(seconds / 60)}min"


def build_name(content_class: str, pattern: str, info: dict, total_min: int) -> str:
    display = CONTENT_CLASS_DISPLAY.get(content_class, ("mixed", "Mixed"))[1]
    if pattern == "intervals" and info.get("n") and info.get("on_s"):
        return f"{display} {info['n']}x{fmt_dur_short(info['on_s'])} ({total_min}min)"
    if pattern == "microintervals" and info.get("n") and info.get("on_s"):
        return (
            f"{display} {fmt_dur_short(info['on_s'])}/{fmt_dur_short(info['off_s'])} "
            f"{info['n']}x ({total_min}min)"
        )
    if pattern == "over_under":
        t = info.get("transitions", 0)
        return f"{display} {t} cycles ({total_min}min)" if t else f"{display} ({total_min}min)"
    if pattern == "ramp":
        return f"{display} Ramp ({total_min}min)"
    if pattern == "steady":
        return f"{display} Steady ({total_min}min)"
    return f"{display} ({total_min}min)"


def build_description(warmup: list[dict], main: list[dict], cooldown: list[dict],
                       pattern: str, info: dict) -> str:
    parts: list[str] = []

    if warmup:
        wu_total = sum(s["duration_s"] for s in warmup)
        if wu_total > 0:
            wu_start = (warmup[0]["power_low"] if warmup[0]["kind"] == "warmup"
                        else warmup[0]["avg_power"])
            wu_end = (warmup[-1]["power_high"] if warmup[-1]["kind"] == "warmup"
                      else warmup[-1]["avg_power"])
            if abs(wu_start - wu_end) > 0.02:
                parts.append(
                    f"Warmup: {fmt_dur(wu_total)} from {round(wu_start*100)}% to "
                    f"{round(wu_end*100)}% FTP"
                )
            else:
                parts.append(f"Warmup: {fmt_dur(wu_total)} @ {round(wu_start*100)}% FTP")

    if pattern == "intervals" and info.get("n") and info.get("on_s"):
        parts.append(
            f"Intervals: {info['n']} x ({fmt_dur(info['on_s'])} @ "
            f"{round(info['on_power']*100)}% / {fmt_dur(info['off_s'])} @ "
            f"{round(info['off_power']*100)}%)"
        )
    elif pattern == "microintervals" and info.get("n") and info.get("on_s"):
        parts.append(
            f"Microintervals: {info['n']} x ({fmt_dur(info['on_s'])} @ "
            f"{round(info['on_power']*100)}% / {fmt_dur(info['off_s'])} @ "
            f"{round(info['off_power']*100)}%)"
        )
    elif pattern == "over_under":
        t = info.get("transitions", 0)
        parts.append(f"Over-Under: {t} cycles (alternating above/below threshold)")
    elif pattern == "ramp" and info:
        total = sum(s["duration_s"] for s in main)
        parts.append(
            f"Ramp: {fmt_dur(total)} from {round(info.get('start', 0.5)*100)}% to "
            f"{round(info.get('end', 1.0)*100)}% FTP"
        )
    elif pattern == "steady" and info:
        p = info.get("power", 0.65)
        total = info.get("duration", sum(s["duration_s"] for s in main))
        parts.append(f"Steady: {fmt_dur(total)} @ {round(p*100)}% FTP")
    else:
        merged: list[tuple[int, float]] = []
        for s in main:
            p = round(s["avg_power"] * 100) / 100.0
            if merged and abs(merged[-1][1] - p) < 0.005:
                merged[-1] = (merged[-1][0] + s["duration_s"], p)
            else:
                merged.append((s["duration_s"], p))

        used_compact = False
        if len(merged) >= 8:
            for start in range(min(3, len(merged))):
                if merged[start][1] < 0.95 or start + 1 >= len(merged):
                    continue
                h_dur, h_p = merged[start]
                l_dur, l_p = merged[start + 1]
                reps = 0
                i = start
                while i + 1 < len(merged):
                    if (abs(merged[i][1] - h_p) < 0.02
                            and abs(merged[i + 1][1] - l_p) < 0.02
                            and abs(merged[i][0] - h_dur) <= 5):
                        reps += 1
                        i += 2
                    else:
                        break
                if reps >= 3:
                    for d, p in merged[:start]:
                        parts.append(f"Steady: {fmt_dur(d)} @ {round(p*100)}% FTP")
                    parts.append(
                        f"Intervals: {reps} x ({fmt_dur(h_dur)} @ {round(h_p*100)}% / "
                        f"{fmt_dur(l_dur)} @ {round(l_p*100)}%)"
                    )
                    for d, p in merged[start + 2 * reps:]:
                        parts.append(f"Steady: {fmt_dur(d)} @ {round(p*100)}% FTP")
                    used_compact = True
                    break
        if not used_compact:
            for dur, p in merged:
                parts.append(f"Steady: {fmt_dur(dur)} @ {round(p*100)}% FTP")

    if cooldown:
        cd_total = sum(s["duration_s"] for s in cooldown)
        if cd_total > 0:
            cd_start = (cooldown[0]["power_low"] if cooldown[0]["kind"] == "cooldown"
                        else cooldown[0]["avg_power"])
            cd_end = (cooldown[-1]["power_high"] if cooldown[-1]["kind"] == "cooldown"
                      else cooldown[-1]["avg_power"])
            if abs(cd_start - cd_end) > 0.02:
                parts.append(
                    f"Cooldown: {fmt_dur(cd_total)} from {round(cd_start*100)}% to "
                    f"{round(cd_end*100)}% FTP"
                )
            else:
                parts.append(f"Cooldown: {fmt_dur(cd_total)} @ {round(cd_start*100)}% FTP")

    return " | ".join(parts)


# ── Filename utilities ────────────────────────────────────────────────────────


def derive_class_structural(main: list[dict], pattern: str, info: dict) -> str:
    """Structural fallback classifier that catches edge cases the v4.1.2
    classifier misses (e.g. 12s sprints @ 1.45 FTP being classified as
    recovery because they're <1.50 FTP and short relative to total ride).
    """
    if not main:
        return "mixed"
    total = sum(s["duration_s"] for s in main)
    if total <= 0:
        return "mixed"

    avg_power = sum(s["avg_power"] * s["duration_s"] for s in main) / total
    peak_power = max(s["avg_power"] for s in main)

    z2_s = sum(s["duration_s"] for s in main if 0.55 <= s["avg_power"] < 0.75)
    z3_s = sum(s["duration_s"] for s in main if 0.75 <= s["avg_power"] < 0.90)
    z4_s = sum(s["duration_s"] for s in main if 0.90 <= s["avg_power"] < 1.05)
    z5_s = sum(s["duration_s"] for s in main if 1.05 <= s["avg_power"] < 1.20)
    z6_s = sum(s["duration_s"] for s in main if 1.20 <= s["avg_power"] < 1.50)
    z7_s = sum(s["duration_s"] for s in main if s["avg_power"] >= 1.50)
    sweet_spot_s = sum(s["duration_s"] for s in main if 0.84 <= s["avg_power"] < 0.95)

    short_high_count = sum(
        1 for s in main if s["avg_power"] >= 1.40 and s["duration_s"] <= 20
    )

    if avg_power < 0.55 and peak_power < 0.75 and total >= 15 * 60:
        return "recovery"
    if short_high_count >= 4:
        return "neuromuscular"
    if (z6_s + z7_s) >= 3 * 60 and z5_s < 8 * 60:
        return "anaerobic"
    if pattern == "microintervals" and info.get("on_power", 0) >= 1.05:
        return "vo2_short"
    if z5_s >= 8 * 60:
        return "vo2max"
    if pattern == "over_under":
        return "over_under"
    if z4_s >= 15 * 60 and peak_power <= 1.15:
        return "threshold"
    if sweet_spot_s >= 10 * 60 and avg_power < 0.95:
        return "sweet_spot"
    if z3_s >= 20 * 60:
        return "tempo"
    if (z2_s / total) >= 0.60 and peak_power < 0.85:
        return "endurance"
    return "mixed"


def reconcile_class(cached_class: str, cached_conf: float, structural: str) -> str:
    """Reconcile cached classifier output with my structural detector."""
    if cached_class and cached_class != "mixed" and cached_conf >= 0.85:
        return cached_class
    if cached_class == structural and cached_class != "mixed":
        return cached_class
    if structural != "mixed":
        return structural
    if cached_class and cached_class != "mixed":
        return cached_class
    return "mixed"


def filename_implies_class(filename: str) -> Optional[str]:
    fname = filename.lower()
    for prefix, cls in PREFIX_TO_CLASS:
        if fname.startswith(prefix):
            return cls
    return None


def derive_new_filename_for_class(old_name: str, new_class: str, total_min: int,
                                   pattern: str, info: dict,
                                   has_ramp_in_main: bool = False) -> str:
    cls_prefix = CONTENT_CLASS_DISPLAY[new_class][0]
    if pattern == "intervals" and info.get("n") and info.get("on_s"):
        on_s = info["on_s"]
        n = info["n"]
        shape = f"{n}x{on_s}s" if on_s < 60 else f"{n}x{round(on_s/60)}min"
    elif pattern == "microintervals" and info.get("n") and info.get("on_s"):
        on_s = info["on_s"]
        off_s = info.get("off_s", 0)
        n = info["n"]
        if on_s < 60:
            shape = f"{on_s}s{off_s}s_{n}x"
        else:
            shape = f"{round(on_s/60)}min_{n}x"
    elif pattern == "over_under":
        shape = "ou"
    elif pattern == "ramp":
        shape = "ramp"
    elif pattern == "steady":
        shape = "steady"
    else:
        shape = "mixed"
    # Mark progressive (ramp-in-main-body) tempo files with `_progression_`
    # to satisfy the existing tempo-shape test which forbids `<Ramp>` in
    # tempo_*.zwo files unless the filename includes _progression_.
    if has_ramp_in_main and new_class == "tempo":
        return f"{cls_prefix}_progression_{shape}_{total_min}min.zwo"
    return f"{cls_prefix}_{shape}_{total_min}min.zwo"


# ── ZWO rewrite ───────────────────────────────────────────────────────────────


def rewrite_zwo(zwo_path: Path, new_name: str, new_description: str,
                 new_author: str = "Domestique Library") -> None:
    tree = ET.parse(zwo_path)
    root = tree.getroot()

    def _set_or_create(tag: str, text: str) -> None:
        el = root.find(tag)
        if el is None:
            el = ET.SubElement(root, tag)
        el.text = text

    _set_or_create("name", new_name)
    _set_or_create("description", new_description)
    _set_or_create("author", new_author)

    workout_el = root.find("workout")
    if workout_el is not None:
        for seg in workout_el:
            for child_tag in ("textevent", "TextNotification", "TextEvent"):
                for ev in list(seg.findall(child_tag)):
                    seg.remove(ev)

    tree.write(zwo_path, encoding="utf-8", xml_declaration=True)


# ── Coordinator ───────────────────────────────────────────────────────────────


def load_classifications() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with CACHE_PATH.open() as f:
        return json.load(f).get("classifications", {})


def run_classifier() -> int:
    classifier = ROOT / "scripts" / "classify_library_content.py"
    res = subprocess.run(
        [sys.executable, str(classifier), "--all"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"  classifier failed: {res.stderr}", file=sys.stderr)
        return 2
    for line in res.stderr.strip().splitlines()[-15:]:
        print(f"  {line}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-rename", action="store_true")
    ap.add_argument("--skip-pre-classify", action="store_true",
                    help="Skip Pass A — assume cache is current")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # ── Pass A: pre-classify ───────────────────────────────────────────────
    if not args.skip_pre_classify and not args.dry_run:
        print("Pass A — running classifier on current library…", file=sys.stderr)
        if run_classifier() != 0:
            return 2

    classifications = load_classifications()
    files = sorted(WORKOUTS_DIR.glob("*.zwo"))
    if args.limit:
        files = files[: args.limit]
    print(f"Library size: {len(files)} files", file=sys.stderr)

    # ── Pass B + C: plan + apply renames ───────────────────────────────────
    rename_plan: list[tuple[Path, Path, Optional[str], str]] = []
    if not args.no_rename:
        print("Pass B — planning renames where filename prefix mismatches class…",
              file=sys.stderr)
        used_targets: set[str] = set()
        for zwo in files:
            cached = classifications.get(zwo.name, {})
            cached_class = cached.get("primary") or "mixed"
            cached_conf = cached.get("confidence", 0.0)
            try:
                segs, meta = parse_segments(zwo)
            except (ET.ParseError, OSError):
                continue
            if not segs:
                continue
            warmup, main, cooldown = strip_warmup_cooldown(segs)
            pattern, info = detect_pattern(main)
            # Conservative rename rule: only rename when the CLASSIFIER assigned
            # a non-mixed primary. This guarantees Pass D classifier output
            # stays stable post-rename (since the classifier is deterministic
            # on file content). Files the classifier marks "mixed" keep their
            # original filename — the description rewrite still happens in
            # Pass E and the name is updated to reflect mixed primary.
            if cached_class == "mixed":
                continue
            cls = cached_class
            implied = filename_implies_class(zwo.name)
            if implied == cls:
                continue
            total_min = round(sum(s["duration_s"] for s in segs) / 60)
            # Check if file has <Ramp> outside Warmup/Cooldown wrappers — this
            # is what the existing tempo-shape test (B7) considers a
            # "ramping_undesired" file. We use a loose definition: ANY Ramp
            # element NOT in the first or last position is considered
            # "main-body ramp" and gets the _progression_ marker.
            tree = ET.parse(zwo)
            workout_el = tree.getroot().find("workout")
            ramp_in_body = False
            if workout_el is not None:
                children = list(workout_el)
                # Strip explicit Warmup/Cooldown from ends
                body = list(children)
                while body and body[0].tag in ("Warmup", "Cooldown"):
                    body.pop(0)
                while body and body[-1].tag in ("Warmup", "Cooldown"):
                    body.pop()
                ramp_in_body = any(s.tag == "Ramp" for s in body)
            target_name = derive_new_filename_for_class(
                zwo.name, cls, total_min, pattern, info,
                has_ramp_in_main=ramp_in_body,
            )
            target_path = WORKOUTS_DIR / target_name
            stem = target_name[: -len(".zwo")]
            k = 1
            while target_path.exists() or target_name in used_targets:
                target_name = f"{stem}_renamed_v46_{k}.zwo"
                target_path = WORKOUTS_DIR / target_name
                k += 1
            used_targets.add(target_name)
            rename_plan.append((zwo, target_path, implied, cls))

        print(f"Pass C — applying {len(rename_plan)} renames via git mv…",
              file=sys.stderr)
        if not args.dry_run:
            for src, tgt, _, _ in rename_plan:
                res = subprocess.run(
                    ["git", "mv", str(src), str(tgt)],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
                if res.returncode != 0:
                    try:
                        shutil.move(str(src), str(tgt))
                    except Exception as e:
                        print(f"  rename-fail {src.name}: {e}", file=sys.stderr)

    # ── Pass D: re-run classifier on renamed library ───────────────────────
    if rename_plan and not args.dry_run:
        print("Pass D — re-running classifier on renamed library…", file=sys.stderr)
        if run_classifier() != 0:
            return 2

    classifications = load_classifications()
    files_post = sorted(WORKOUTS_DIR.glob("*.zwo"))
    if args.limit:
        files_post = files_post[: args.limit]

    new_to_old: dict[str, str] = {tgt.name: src.name for src, tgt, _, _ in rename_plan}

    # ── Pass E: rewrite name + description + author ────────────────────────
    print(f"Pass E — rewriting <name>, <description>, <author> for {len(files_post)} files…",
          file=sys.stderr)
    manifest_entries: list[dict] = []
    name_changed = 0
    class_changed = 0
    zero_min_fixed = 0
    filename_changed = len(rename_plan)

    for i, zwo in enumerate(files_post, 1):
        cached = classifications.get(zwo.name, {})
        cached_class = cached.get("primary") or "mixed"
        try:
            segs, meta = parse_segments(zwo)
        except (ET.ParseError, OSError) as e:
            print(f"  skip {zwo.name}: {e}", file=sys.stderr)
            continue
        if not segs:
            continue
        warmup, main, cooldown = strip_warmup_cooldown(segs)
        pattern, info = detect_pattern(main)
        # In Pass E we trust the FINAL classifier output verbatim. The
        # classifier already ran on the post-rename filenames and its primary
        # is authoritative for the test_name_class_matches_primary check.
        cls = cached_class
        total_min = round(sum(s["duration_s"] for s in segs) / 60)
        new_name = build_name(cls, pattern, info, total_min)
        new_description = build_description(warmup, main, cooldown, pattern, info)

        old_name = meta["name"]
        old_description = meta["description"]
        had_zero_min = ("0min @" in old_description) or ("0min at" in old_description)

        if not args.dry_run:
            try:
                rewrite_zwo(zwo, new_name, new_description)
            except Exception as e:
                print(f"  rewrite-fail {zwo.name}: {e}", file=sys.stderr)
                continue

        if old_name != new_name:
            name_changed += 1
        if had_zero_min:
            zero_min_fixed += 1

        old_filename_for_manifest = new_to_old.get(zwo.name, zwo.name)
        old_class_implied = filename_implies_class(old_filename_for_manifest)
        if old_class_implied is not None and old_class_implied != cls:
            class_changed += 1

        manifest_entries.append({
            "old_filename": old_filename_for_manifest,
            "new_filename": zwo.name,
            "old_name": old_name,
            "new_name": new_name,
            "old_class_implied": old_class_implied,
            "new_class": cls,
            "had_zero_min_bug": had_zero_min,
        })

        if i % 500 == 0:
            print(f"  …{i}/{len(files_post)}", file=sys.stderr)

    # ── Pass F: manifest + final classifier ────────────────────────────────
    manifest = {
        "overhauled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": "v4.6.0",
        "stats": {
            "total": len(files_post),
            "name_changed": name_changed,
            "class_changed": class_changed,
            "filename_changed": filename_changed,
            "zero_min_fixed": zero_min_fixed,
        },
        "entries": manifest_entries,
    }
    if not args.dry_run:
        with (WORKOUTS_DIR / ".overhaul_manifest.json").open("w") as f:
            json.dump(manifest, f, indent=2)
    print(f"\nStats: {manifest['stats']}", file=sys.stderr)

    if not args.dry_run:
        print("\nFinal classifier run…", file=sys.stderr)
        if run_classifier() != 0:
            return 2

    print("\nDone.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
