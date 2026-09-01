#!/usr/bin/env python3.12
"""Dedupe ZWO library via structure hash.

The structure hash captures only factual / functional content:
an ordered list of (segment_type, duration_sec, power_low, power_high).
Creative fields (<name>, <description>, <author>, <textevent>, <image>,
<video>) are ignored. Power is quantized to 0.05 FTP buckets and
duration to 5-second buckets to allow near-duplicates to collide.

CLI: python3 scripts/dedupe_zwo_library.py --index workouts/
     Produces workouts/.structure_index.json mapping
     hash -> first-file-seen.

Also exposes `structure_hash(zwo_path) -> str` for import by
`import_github_workouts.py` and `generate_gap_workouts.py`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


_DUR_BUCKET_SEC = 5
_POW_BUCKET = 0.05
_STRUCTURE_TAGS = (
    "Warmup",
    "Cooldown",
    "Ramp",
    "SteadyState",
    "IntervalsT",
    "FreeRide",
    "MaxEffort",
    "SolidState",
    "RestDay",
)


def _qdur(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return int(round(n / _DUR_BUCKET_SEC)) * _DUR_BUCKET_SEC


def _qpow(v) -> float:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return 0.0
    # Values above 5 look like raw watts sentinel; coerce to 0
    # (we target fractional FTP ZWOs).
    return round(p / _POW_BUCKET) * _POW_BUCKET


def _segment_tuple(el: ET.Element) -> tuple:
    """Reduce a workout segment to canonical fact tuple."""
    tag = el.tag
    if tag == "SteadyState":
        return (
            tag,
            _qdur(el.get("Duration", 0)),
            _qpow(el.get("Power", 0)),
        )
    if tag in ("Warmup", "Cooldown", "Ramp"):
        return (
            tag,
            _qdur(el.get("Duration", 0)),
            _qpow(el.get("PowerLow", 0)),
            _qpow(el.get("PowerHigh", 0)),
        )
    if tag == "IntervalsT":
        return (
            tag,
            int(el.get("Repeat", 1) or 1),
            _qdur(el.get("OnDuration", 0)),
            _qdur(el.get("OffDuration", 0)),
            _qpow(el.get("OnPower", 0)),
            _qpow(el.get("OffPower", 0)),
        )
    if tag == "FreeRide":
        return (tag, _qdur(el.get("Duration", 0)))
    if tag in ("MaxEffort", "SolidState", "RestDay"):
        return (
            tag,
            _qdur(el.get("Duration", 0)),
            _qpow(el.get("Power", 0)),
        )
    # Unknown / creative layer -> drop
    return ()


def structure_hash(zwo_path: str | Path) -> str:
    """Return sha1 hexdigest of canonical structure of a ZWO file."""
    path = Path(zwo_path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        # Malformed file -> hash the bytes (still dedupeable against itself)
        return hashlib.sha1(path.read_bytes()).hexdigest()
    workout = root.find("workout")
    if workout is None:
        return hashlib.sha1(b"").hexdigest()
    segs: list[tuple] = []
    for el in workout:
        if el.tag in _STRUCTURE_TAGS:
            t = _segment_tuple(el)
            if t:
                segs.append(t)
    canonical = repr(tuple(segs)).encode("utf-8")
    return hashlib.sha1(canonical).hexdigest()


def build_index(workouts_dir: Path) -> dict[str, str]:
    """Index every *.zwo in workouts_dir: hash -> first filename seen."""
    index: dict[str, str] = {}
    collisions = 0
    for fp in sorted(workouts_dir.glob("*.zwo")):
        h = structure_hash(fp)
        if h in index:
            collisions += 1
            continue
        index[h] = fp.name
    print(
        f"Indexed {len(index)} unique structures "
        f"({collisions} intra-library collisions skipped) "
        f"from {workouts_dir}",
        file=sys.stderr,
    )
    return index


def write_index(workouts_dir: Path, index: dict[str, str]) -> Path:
    out = workouts_dir / ".structure_index.json"
    out.write_text(json.dumps(index, indent=2, sort_keys=True))
    return out


def load_index(workouts_dir: Path) -> dict[str, str]:
    p = workouts_dir / ".structure_index.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Dedupe ZWO library via structure hash.")
    ap.add_argument(
        "--index",
        type=Path,
        default=Path("workouts"),
        help="Directory containing *.zwo; writes .structure_index.json",
    )
    args = ap.parse_args()
    if not args.index.is_dir():
        print(f"ERROR: {args.index} is not a directory", file=sys.stderr)
        return 2
    idx = build_index(args.index)
    out = write_index(args.index, idx)
    print(f"Wrote {out} with {len(idx)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
