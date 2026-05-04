#!/usr/bin/env python3
"""
regen_tempo_steady.py — v4.3.0 B7

Reads /tmp/tempo_audit.json (produced by audit_tempo_workouts.py) and
rewrites every file classified as 'ramping_undesired' to a single
SteadyState in the main body, flanked by the original Warmup/Cooldown
wrappers (preserved verbatim if present, synthesized otherwise).

Main body becomes ONE <SteadyState Power="0.82" Duration="<sum>"/>
where <sum> is the total seconds of the previous main body. Filename
unchanged so all existing plan references still resolve.

Author tag set to "Domestique Library". Description rewritten to
reflect new structure.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKOUTS_DIR = REPO_ROOT / "workouts"
AUDIT_PATH = Path("/tmp/tempo_audit.json")

TARGET_POWER = 0.82  # 82% FTP — solid mid-tempo (zone 3)
WRAPPER_TAGS = {"Warmup", "Cooldown"}


def _segment_total_duration(seg: ET.Element) -> int:
    """Total clock seconds for a segment (handles IntervalsT repetition)."""
    if seg.tag == "IntervalsT":
        try:
            on_d = float(seg.attrib.get("OnDuration", 0))
            off_d = float(seg.attrib.get("OffDuration", 0))
            repeat = float(seg.attrib.get("Repeat", 1))
            return int((on_d + off_d) * repeat)
        except Exception:
            return 0
    try:
        return int(float(seg.attrib.get("Duration", 0) or 0))
    except Exception:
        return 0


def _format_xml(root: ET.Element) -> str:
    """Pretty-print with 4-space indent, matching the existing library style."""
    ET.indent(root, space="    ", level=0)
    body = ET.tostring(root, encoding="unicode")
    return f"<?xml version='1.0' encoding='utf-8'?>\n{body}\n"


def _rewrite(zwo_path: Path) -> tuple[bool, str]:
    """Rewrite one file. Returns (changed, message)."""
    try:
        tree = ET.parse(zwo_path)
    except ET.ParseError as e:
        return False, f"parse_error: {e}"

    root = tree.getroot()
    workout_el = root.find("workout")
    if workout_el is None:
        return False, "missing_workout_element"

    segments = list(workout_el)

    # Extract leading Warmup wrappers and trailing Cooldown wrappers (preserve verbatim).
    leading_wrappers: list[ET.Element] = []
    trailing_wrappers: list[ET.Element] = []
    body = list(segments)
    while body and body[0].tag in WRAPPER_TAGS:
        leading_wrappers.append(body.pop(0))
    while body and body[-1].tag in WRAPPER_TAGS:
        trailing_wrappers.insert(0, body.pop())

    main_body_seconds = sum(_segment_total_duration(s) for s in body)
    if main_body_seconds <= 0:
        # Defensive: nothing to rewrite. Skip rather than create a 0-second SteadyState.
        return False, "main_body_seconds==0; skipped"

    # Build the new <workout> element preserving wrappers verbatim.
    new_workout = ET.Element("workout")
    for w in leading_wrappers:
        new_workout.append(w)
    new_workout.append(
        ET.Element(
            "SteadyState",
            attrib={
                "Duration": str(main_body_seconds),
                "Power": f"{TARGET_POWER:g}",
                "pace": "0",
            },
        )
    )
    for w in trailing_wrappers:
        new_workout.append(w)

    # Replace <workout> contents in-place
    for child in list(workout_el):
        workout_el.remove(child)
    for new_child in list(new_workout):
        workout_el.append(new_child)

    # Force author + description
    author_el = root.find("author")
    if author_el is None:
        author_el = ET.SubElement(root, "author")
        # Move it to the top — author conventionally appears first
        root.remove(author_el)
        root.insert(0, author_el)
    author_el.text = "Domestique Library"

    desc_el = root.find("description")
    minutes = main_body_seconds // 60
    seconds_rem = main_body_seconds % 60
    if seconds_rem:
        dur_str = f"{minutes}min {seconds_rem}s"
    else:
        dur_str = f"{minutes}min"
    new_desc = (
        f"Steady tempo: {dur_str} at {int(TARGET_POWER * 100)}% FTP. "
        f"v4.3.0 B7 rewrite — original main body had ramps; library policy "
        f"is steady-state Z3 work for tempo."
    )
    if desc_el is None:
        desc_el = ET.SubElement(root, "description")
    desc_el.text = new_desc

    out = _format_xml(root)
    zwo_path.write_text(out)
    return True, f"rewrote {dur_str} steady"


def main() -> int:
    if not AUDIT_PATH.exists():
        print(f"Audit file not found: {AUDIT_PATH}. Run audit_tempo_workouts.py first.", file=sys.stderr)
        return 1
    records = json.loads(AUDIT_PATH.read_text())
    targets = [r["file"] for r in records if r["shape"] == "ramping_undesired"]
    if not targets:
        print("No ramping_undesired files in audit. Nothing to do.")
        return 0

    print(f"Rewriting {len(targets)} ramping_undesired tempo files to steady-state…")
    changed_count = 0
    for fname in targets:
        path = WORKOUTS_DIR / fname
        if not path.exists():
            print(f"  SKIP missing: {fname}")
            continue
        changed, msg = _rewrite(path)
        marker = "OK " if changed else "SKIP"
        print(f"  {marker} {fname}: {msg}")
        if changed:
            changed_count += 1
    print(f"\nDone. {changed_count}/{len(targets)} files rewritten.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
