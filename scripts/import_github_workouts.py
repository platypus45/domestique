#!/usr/bin/env python3.12
"""Import ZWO workouts from GitHub public-domain / permissive repos.

Sources (from plan_libs_sources.md §6 and MASTER_DECISIONS §4.1):
  - https://github.com/macgrrl/zwift-workouts               (Unlicense)
  - https://github.com/michaelahlers/michaelahlers-zwift-workouts (MIT)

Both licenses permit redistribution and modification. We re-author the
ZWO with `<author>Domestique Library</author>` and regenerate
`<name>`/`<description>` from structure per MASTER_DECISIONS §4.1b.
All `<textevent>`, `<TextNotification>`, `<image>`, `<video>` children
are stripped.

Output: workouts/*.zwo (new files only) + workouts/.github_imports_manifest.json
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Local import (sibling script)
sys.path.insert(0, str(Path(__file__).parent))
from dedupe_zwo_library import structure_hash, load_index  # noqa: E402

REPOS = [
    {
        "slug": "macgrrl_zwift-workouts",
        "url": "https://github.com/macgrrl/zwift-workouts.git",
        "license": "Unlicense",
    },
    {
        "slug": "michaelahlers_zwift-workouts",
        "url": "https://github.com/michaelahlers/michaelahlers-zwift-workouts.git",
        "license": "MIT",
    },
]

TMP_ROOT = Path("/tmp/github_imports")
WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "workouts"
MANIFEST = WORKOUTS_DIR / ".github_imports_manifest.json"


def _clone(repo: dict) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    dest = TMP_ROOT / repo["slug"]
    if dest.exists():
        shutil.rmtree(dest)
    print(f"Cloning {repo['url']} -> {dest}", file=sys.stderr)
    r = subprocess.run(
        ["git", "clone", "--depth", "1", repo["url"], str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        print(f"  clone failed: {r.stderr}", file=sys.stderr)
    return dest


def _classify_and_describe(root: ET.Element) -> tuple[str, str, str, int]:
    """Inspect <workout> segments -> (type_slug, structure_sig, desc_parts_str, total_min)."""
    workout = root.find("workout")
    if workout is None:
        return "endurance", "steady", "No intervals found.", 0

    segs: list[dict] = []
    for el in workout:
        t = el.tag
        if t == "SteadyState":
            try:
                segs.append({
                    "tag": t,
                    "dur": int(float(el.get("Duration", 0))),
                    "lo": float(el.get("Power", 0)),
                    "hi": float(el.get("Power", 0)),
                })
            except ValueError:
                continue
        elif t in ("Warmup", "Cooldown", "Ramp"):
            try:
                segs.append({
                    "tag": t,
                    "dur": int(float(el.get("Duration", 0))),
                    "lo": float(el.get("PowerLow", 0)),
                    "hi": float(el.get("PowerHigh", 0)),
                })
            except ValueError:
                continue
        elif t == "IntervalsT":
            try:
                rep = int(el.get("Repeat", 1) or 1)
                on_d = int(float(el.get("OnDuration", 0)))
                off_d = int(float(el.get("OffDuration", 0)))
                on_p = float(el.get("OnPower", 0))
                off_p = float(el.get("OffPower", 0))
            except ValueError:
                continue
            for _ in range(rep):
                segs.append({"tag": "SteadyState", "dur": on_d, "lo": on_p, "hi": on_p})
                segs.append({"tag": "SteadyState", "dur": off_d, "lo": off_p, "hi": off_p})
        elif t == "FreeRide":
            try:
                segs.append({
                    "tag": t,
                    "dur": int(float(el.get("Duration", 0))),
                    "lo": 0.65,
                    "hi": 0.65,
                })
            except ValueError:
                continue

    if not segs:
        return "endurance", "steady", "No intervals found.", 0

    total_sec = sum(s["dur"] for s in segs)
    total_min = int(round(total_sec / 60))

    # Classify
    work = segs[1:-1] if len(segs) >= 3 else segs
    max_p = max((s["hi"] for s in work), default=segs[0]["hi"])
    # Shortest "high" on_dur
    high_on = min(
        (s["dur"] for s in work if (s["lo"] + s["hi"]) / 2 >= 1.0),
        default=60,
    )
    avg_p = sum(((s["lo"] + s["hi"]) / 2) * s["dur"] for s in work) / max(
        1, sum(s["dur"] for s in work)
    )

    if max_p >= 1.50 and high_on <= 20:
        slug = "sprints"
    elif max_p >= 1.20 and high_on <= 60:
        slug = "anaerobic"
    elif max_p >= 1.06:
        slug = "vo2"
    elif max_p >= 0.95:
        slug = "threshold"
    elif max_p >= 0.88:
        slug = "sweet_spot"
    elif max_p >= 0.76:
        slug = "tempo"
    elif max_p >= 0.56:
        slug = "endurance"
    else:
        slug = "recovery"

    # Over-under detection
    if len(work) >= 4:
        ups = sum(1 for s in work if (s["lo"] + s["hi"]) / 2 >= 1.0)
        downs = sum(1 for s in work if 0.7 <= (s["lo"] + s["hi"]) / 2 < 1.0)
        if ups >= 2 and downs >= 2 and abs(ups - downs) <= 2:
            slug = "over_under"

    # Pyramid detection
    if len(work) >= 5:
        powers = [round((s["lo"] + s["hi"]) / 2, 2) for s in work if s["dur"] >= 60]
        if len(powers) >= 5:
            mid = len(powers) // 2
            left = powers[: mid + 1]
            right = powers[mid:]
            if left == sorted(left) and right == sorted(right, reverse=True):
                slug = "pyramid"

    # Repeating (on, off) structure for signature
    i = 0
    best = None
    while i + 1 < len(segs):
        on = segs[i]
        off = segs[i + 1]
        j = i
        reps = 0
        while (
            j + 1 < len(segs)
            and abs(segs[j]["dur"] - on["dur"]) <= 2
            and abs(segs[j + 1]["dur"] - off["dur"]) <= 2
        ):
            j += 2
            reps += 1
        if reps >= 2 and (best is None or reps > best[0]):
            best = (reps, on["dur"])
        i = max(i + 1, j)
    if best:
        reps, on_sec = best
        if on_sec >= 60:
            sig = f"{reps}x{int(round(on_sec/60))}min"
        else:
            sig = f"{reps}x{on_sec}s"
    else:
        sig = "steady"

    # Description
    parts = []
    i = 0
    n = len(segs)
    while i < n:
        s = segs[i]
        if i + 1 < n:
            on = segs[i]
            off = segs[i + 1]
            j = i
            reps = 0
            while (
                j + 1 < n
                and abs(segs[j]["dur"] - on["dur"]) <= 2
                and abs(segs[j + 1]["dur"] - off["dur"]) <= 2
                and abs(((segs[j]["lo"] + segs[j]["hi"]) / 2)
                        - ((on["lo"] + on["hi"]) / 2)) < 0.02
                and abs(((segs[j + 1]["lo"] + segs[j + 1]["hi"]) / 2)
                        - ((off["lo"] + off["hi"]) / 2)) < 0.02
            ):
                j += 2
                reps += 1
            if reps >= 2:
                avg_on = int(((on["lo"] + on["hi"]) / 2) * 100)
                avg_off = int(((off["lo"] + off["hi"]) / 2) * 100)
                parts.append(
                    f"{reps} x {_fmt_time(on['dur'])} @ {avg_on}% FTP / "
                    f"{_fmt_time(off['dur'])} @ {avg_off}% FTP"
                )
                i += reps * 2
                continue
        if abs(s["hi"] - s["lo"]) > 0.05:
            parts.append(
                f"{_fmt_time(s['dur'])} ramp {int(s['lo']*100)}-{int(s['hi']*100)}% FTP"
            )
        else:
            parts.append(
                f"{_fmt_time(s['dur'])} @ {int(((s['lo']+s['hi'])/2)*100)}% FTP"
            )
        i += 1
    parts.append(f"Total {total_min} min")
    return slug, sig, " | ".join(parts), total_min


def _fmt_time(sec: int) -> str:
    if sec >= 60:
        m = sec / 60
        if abs(m - round(m)) < 0.01:
            return f"{int(round(m))} min"
        return f"{m:.1f} min"
    return f"{sec} sec"


def _restyle(zwo_path: Path, repo_slug: str) -> tuple[str, str, str, int] | None:
    """Read a source ZWO, strip + regenerate metadata, return (text, slug, sig, total_min)."""
    try:
        tree = ET.parse(zwo_path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    if root.tag != "workout_file":
        return None

    slug, sig, desc, total_min = _classify_and_describe(root)
    if total_min < 5:
        return None  # Too short per hard rules

    # Strip removed elements from every segment
    workout = root.find("workout")
    if workout is None:
        return None
    strip_tags = {"textevent", "TextNotification", "image", "video"}
    for seg in list(workout):
        for child in list(seg):
            if child.tag in strip_tags:
                seg.remove(child)

    # Strip top-level creative fields
    for tag in ("tags", "category", "subcategory", "description", "name", "author"):
        for el in root.findall(tag):
            root.remove(el)
    # Also strip image/video top-level
    for tag in ("image", "video"):
        for el in root.findall(tag):
            root.remove(el)

    # Rebuild in canonical order: author, name, description, sportType, workout
    new_root = ET.Element("workout_file")
    a = ET.SubElement(new_root, "author")
    a.text = "Domestique Library"
    n = ET.SubElement(new_root, "name")
    # Name from structure
    label = {
        "recovery": "Recovery",
        "endurance": "Endurance",
        "tempo": "Tempo",
        "sweet_spot": "Sweet Spot",
        "threshold": "Threshold",
        "vo2": "VO2max",
        "anaerobic": "Anaerobic",
        "sprints": "Sprints",
        "over_under": "Over-Under",
        "pyramid": "Pyramid",
    }.get(slug, slug.title())
    sig_display = sig.replace("min", "min").replace("s", "s")
    n.text = f"{label} {sig_display} ({total_min}min)"
    d = ET.SubElement(new_root, "description")
    d.text = desc
    sp = ET.SubElement(new_root, "sportType")
    sp.text = "bike"

    # Copy workout with stripped children
    new_workout = ET.SubElement(new_root, "workout")
    for seg in list(workout):
        new_seg = ET.SubElement(new_workout, seg.tag, dict(seg.attrib))
        # No child text/events carried forward
        _ = new_seg
    # Serialize
    xml_bytes = ET.tostring(new_root, encoding="utf-8", xml_declaration=False)
    text = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        + _prettify(xml_bytes.decode("utf-8"))
        + "\n"
    )
    return text, slug, sig, total_min


def _prettify(xml_str: str) -> str:
    """Minimal pretty-print: newline between top-level children, 4-space indent body."""
    # Insert newlines after opening <workout_file> and after each top-level child
    lines = []
    lines.append("<workout_file>")
    # Extract inside content
    m = re.match(r"<workout_file>(.*)</workout_file>", xml_str, re.DOTALL)
    if not m:
        return xml_str
    inner = m.group(1)
    # Split top-level children
    depth = 0
    buf = []
    for ch in inner:
        buf.append(ch)
        if ch == "<":
            depth_shift = 1
        elif ch == ">" and buf[-2:] != list("/>"):
            pass
    # Fallback: insert \n after each >
    pretty = re.sub(r"><", ">\n    <", inner)
    # Indent top-level once
    pretty_lines = pretty.split("\n")
    out = []
    for ln in pretty_lines:
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("<workout>"):
            out.append("    <workout>")
        elif ln.startswith("</workout>"):
            out.append("    </workout>")
        elif ln.startswith("<Warmup") or ln.startswith("<Cooldown") or ln.startswith("<Ramp") or ln.startswith("<SteadyState") or ln.startswith("<IntervalsT") or ln.startswith("<FreeRide") or ln.startswith("<MaxEffort") or ln.startswith("<SolidState") or ln.startswith("<RestDay"):
            out.append("        " + ln)
        else:
            out.append("    " + ln)
    return "<workout_file>\n" + "\n".join(out) + "\n</workout_file>"


def import_all() -> dict:
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index(WORKOUTS_DIR)
    manifest: list[dict] = []
    stats = {
        "repos_cloned": 0,
        "files_examined": 0,
        "files_written": 0,
        "files_skipped_dedupe": 0,
        "files_skipped_too_short": 0,
        "files_parse_failed": 0,
    }

    for repo in REPOS:
        dest = _clone(repo)
        if not dest.exists():
            continue
        stats["repos_cloned"] += 1
        for zwo_path in sorted(dest.rglob("*.zwo")):
            stats["files_examined"] += 1
            out = _restyle(zwo_path, repo["slug"])
            if out is None:
                stats["files_parse_failed"] += 1
                continue
            text, slug, sig, total_min = out
            if total_min < 5:
                stats["files_skipped_too_short"] += 1
                continue

            fname = f"{slug}_{sig}_{total_min}min.zwo"
            tmp = WORKOUTS_DIR / f".tmp_{fname}"
            tmp.write_text(text)
            h = structure_hash(tmp)
            if h in index:
                stats["files_skipped_dedupe"] += 1
                print(
                    f"DEDUPE: {repo['slug']}/{zwo_path.name} matches {index[h]}",
                    file=sys.stderr,
                )
                tmp.unlink(missing_ok=True)
                continue

            final_path = WORKOUTS_DIR / fname
            if final_path.exists():
                v = 2
                while (WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo").exists():
                    v += 1
                final_path = WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo"
            tmp.rename(final_path)
            index[h] = final_path.name
            stats["files_written"] += 1
            manifest.append(
                {
                    "source_repo": repo["slug"],
                    "source_url": repo["url"],
                    "source_license": repo["license"],
                    "source_filename": zwo_path.name,
                    "imported_filename": final_path.name,
                    "hash": h,
                }
            )
            print(
                f"  imported {final_path.name} <- {repo['slug']}/{zwo_path.name}",
                file=sys.stderr,
            )

    # Persist index + manifest
    (WORKOUTS_DIR / ".structure_index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True)
    )
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return stats


if __name__ == "__main__":
    result = import_all()
    print(json.dumps(result, indent=2))
