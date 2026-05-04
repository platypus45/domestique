#!/usr/bin/env python3.12
"""Generate procedural gap-filler ZWO workouts.

Per MASTER_DECISIONS §4.2, the existing 1797-file library is thin on:
  1. Pyramids / ladders (literally zero exist): target 25 x 30-45 min
  2. Short VO2 (4x3, 5x3, 3x4, 6x2):             target 25 x 30-45 min
  3. Short threshold (2x10, 3x8, 4x5):           target 25 x 30-45 min
  4. Over-unders with varied ratios:             target 25 x 30-45 min
  5. Neuromuscular sprints (6-12s all-out):      target 15 files
  6. Short sweet spot (2x15, 3x10):              target 15 files

All files are written with <author>Domestique Library</author>,
structure-based <name> and <description> only, no <textevent> /
<image> / <video> children, and are deduped against the existing
structure index before writing.

Style mirrors generate_ftp_workouts.py (lightweight XML string build).
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

# Local import (sibling script)
sys.path.insert(0, str(Path(__file__).parent))
from dedupe_zwo_library import structure_hash, load_index  # noqa: E402


WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "workouts"


def _fmt_pw(p: float) -> str:
    return f"{p:.2f}".rstrip("0").rstrip(".") or "0"


def _fmt_time(sec: int) -> str:
    if sec >= 60:
        m = sec / 60
        if abs(m - round(m)) < 0.01:
            return f"{int(round(m))} min"
        return f"{m:.1f} min"
    return f"{sec} sec"


def _emit(
    blocks: list[dict],
    slug: str,
    sig: str,
    descriptor_parts: list[str],
) -> tuple[str, int]:
    """Render blocks -> ZWO string. blocks entries:
    {kind: Warmup|Cooldown|Ramp|SteadyState, dur, lo, hi}
    """
    total_sec = sum(b["dur"] for b in blocks)
    total_min = int(round(total_sec / 60))
    body = ""
    for b in blocks:
        if b["kind"] == "Warmup":
            body += (
                f'        <Warmup Duration="{b["dur"]}" '
                f'PowerLow="{_fmt_pw(b["lo"])}" PowerHigh="{_fmt_pw(b["hi"])}" pace="0" />\n'
            )
        elif b["kind"] == "Cooldown":
            body += (
                f'        <Cooldown Duration="{b["dur"]}" '
                f'PowerLow="{_fmt_pw(b["lo"])}" PowerHigh="{_fmt_pw(b["hi"])}" pace="0" />\n'
            )
        elif b["kind"] == "Ramp":
            body += (
                f'        <Ramp Duration="{b["dur"]}" '
                f'PowerLow="{_fmt_pw(b["lo"])}" PowerHigh="{_fmt_pw(b["hi"])}" pace="0" />\n'
            )
        else:
            body += (
                f'        <SteadyState Duration="{b["dur"]}" '
                f'Power="{_fmt_pw(b["lo"])}" pace="0" />\n'
            )

    label = {
        "pyramid": "Pyramid",
        "vo2": "VO2max",
        "threshold": "Threshold",
        "over_under": "Over-Under",
        "sprints": "Sprints",
        "sweet_spot": "Sweet Spot",
    }[slug]
    name = f"{label} {sig} ({total_min}min)"
    desc = " | ".join(descriptor_parts) + f" | Total {total_min} min"

    zwo = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workout_file>\n"
        "    <author>Domestique Library</author>\n"
        f"    <name>{html.escape(name)}</name>\n"
        f"    <description>{html.escape(desc)}</description>\n"
        "    <sportType>bike</sportType>\n"
        "    <workout>\n"
        f"{body}"
        "    </workout>\n"
        "</workout_file>\n"
    )
    return zwo, total_min


# ---- Pyramid generators ----
def gen_pyramids() -> list[tuple[str, str, list[dict], list[str]]]:
    """25 pyramids of various durations and power targets."""
    out = []
    # Ladder step durations (minutes) and counts
    # Each pyramid: 1-2-3-4-3-2-1 style or 1-2-3-4-5-4-3-2-1
    variants = [
        # (steps_minutes_list, work_pw, rec_pw, rec_sec, wu_sec, cd_sec, desc_label)
        ([1, 2, 3, 2, 1], 1.00, 0.55, 90, 600, 300, "1-2-3-2-1 @ 100%"),
        ([1, 2, 3, 4, 3, 2, 1], 0.98, 0.55, 90, 600, 300, "1-2-3-4-3-2-1 @ 98%"),
        ([1, 2, 3, 4, 5, 4, 3, 2, 1], 0.95, 0.55, 60, 300, 300, "full ladder @ 95%"),
        ([1, 2, 3, 4, 3, 2, 1], 1.05, 0.55, 120, 600, 300, "1-2-3-4-3-2-1 @ 105%"),
        ([2, 3, 4, 3, 2], 1.00, 0.55, 120, 600, 300, "2-3-4-3-2 @ 100%"),
        ([1, 2, 3, 4, 5, 4, 3, 2, 1], 0.92, 0.55, 60, 600, 300, "full ladder @ 92%"),
        ([1, 3, 5, 3, 1], 1.05, 0.58, 90, 600, 300, "1-3-5-3-1 @ 105%"),
        ([2, 4, 6, 4, 2], 0.95, 0.58, 120, 600, 300, "2-4-6-4-2 @ 95%"),
        ([1, 2, 3, 4, 3, 2, 1], 0.88, 0.55, 60, 300, 300, "1-2-3-4-3-2-1 @ 88%"),
        ([3, 4, 5, 4, 3], 0.95, 0.60, 120, 480, 300, "3-4-5-4-3 @ 95%"),
        ([1, 2, 3, 4, 3, 2, 1], 1.10, 0.55, 180, 600, 300, "1-2-3-4-3-2-1 @ 110%"),
        ([2, 3, 4, 5, 4, 3, 2], 0.95, 0.58, 90, 600, 300, "2-3-4-5-4-3-2 @ 95%"),
        ([1, 2, 3, 2, 1], 1.10, 0.55, 90, 600, 300, "1-2-3-2-1 @ 110%"),
        ([1, 2, 3, 4, 3, 2, 1], 1.00, 0.60, 60, 300, 300, "1-2-3-4-3-2-1 @ 100% tight rec"),
        ([2, 3, 4, 3, 2], 1.05, 0.55, 120, 600, 300, "2-3-4-3-2 @ 105%"),
        ([1, 2, 3, 4, 5, 4, 3, 2, 1], 1.00, 0.55, 60, 600, 300, "full ladder @ 100%"),
        ([1, 2, 3, 2, 1], 1.15, 0.50, 120, 600, 300, "1-2-3-2-1 @ 115%"),
        ([3, 4, 5, 6, 5, 4, 3], 0.90, 0.58, 120, 600, 300, "3-4-5-6-5-4-3 @ 90%"),
        ([1, 2, 3, 4, 3, 2, 1], 0.95, 0.58, 90, 600, 300, "1-2-3-4-3-2-1 @ 95%"),
        ([2, 3, 4, 5, 4, 3, 2], 0.98, 0.58, 120, 600, 300, "2-3-4-5-4-3-2 @ 98%"),
        ([1, 3, 5, 7, 5, 3, 1], 0.92, 0.58, 90, 600, 300, "1-3-5-7-5-3-1 @ 92%"),
        ([1, 2, 3, 4, 3, 2, 1], 1.02, 0.55, 75, 480, 300, "1-2-3-4-3-2-1 @ 102%"),
        ([2, 3, 4, 5, 4, 3, 2], 0.95, 0.55, 90, 480, 300, "2-3-4-5-4-3-2 @ 95% short rec"),
        ([1, 2, 3, 4, 5, 4, 3, 2, 1], 0.90, 0.60, 45, 300, 180, "full ladder @ 90% tight"),
        ([1, 2, 3, 2, 1], 1.05, 0.55, 60, 480, 300, "1-2-3-2-1 @ 105% short rec"),
    ]
    for steps, work_pw, rec_pw, rec_sec, wu_sec, cd_sec, desc_label in variants:
        blocks = [{"kind": "Warmup", "dur": wu_sec, "lo": 0.50, "hi": 0.70}]
        desc_parts = [f"Warmup {_fmt_time(wu_sec)}"]
        for step in steps:
            s_sec = step * 60
            blocks.append({"kind": "SteadyState", "dur": s_sec, "lo": work_pw, "hi": work_pw})
            blocks.append({"kind": "SteadyState", "dur": rec_sec, "lo": rec_pw, "hi": rec_pw})
            desc_parts.append(
                f"{_fmt_time(s_sec)} @ {int(work_pw*100)}% / {_fmt_time(rec_sec)} @ {int(rec_pw*100)}%"
            )
        # Remove the trailing rec (prefer a proper cooldown)
        if blocks[-1]["kind"] == "SteadyState" and blocks[-1]["lo"] == rec_pw:
            blocks.pop()
            desc_parts[-1] = desc_parts[-1].split(" / ")[0]
        blocks.append({"kind": "Cooldown", "dur": cd_sec, "lo": 0.60, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd_sec)}")

        steps_str = "-".join(str(s) for s in steps)
        sig = f"ladder_{steps_str}"
        out.append(("pyramid", sig, blocks, desc_parts))
    return out


# ---- VO2 short generators ----
def gen_short_vo2() -> list[tuple[str, str, list[dict], list[str]]]:
    out = []
    # Structures: 4x3, 5x3, 3x4, 6x2
    variants = [
        (4, 180, 120, 1.15, 600, 300, "4x3min @ 115% / 2min @ 55%"),
        (4, 180, 180, 1.10, 600, 300, "4x3min @ 110% / 3min @ 55%"),
        (4, 180, 120, 1.20, 480, 300, "4x3min @ 120% / 2min @ 55%"),
        (5, 180, 120, 1.10, 600, 300, "5x3min @ 110% / 2min @ 55%"),
        (5, 180, 120, 1.15, 600, 300, "5x3min @ 115% / 2min @ 55%"),
        (5, 180, 180, 1.08, 600, 300, "5x3min @ 108% / 3min @ 55%"),
        (3, 240, 180, 1.15, 600, 300, "3x4min @ 115% / 3min @ 55%"),
        (3, 240, 180, 1.20, 600, 300, "3x4min @ 120% / 3min @ 55%"),
        (3, 240, 240, 1.10, 600, 300, "3x4min @ 110% / 4min @ 55%"),
        (6, 120, 90, 1.20, 600, 300, "6x2min @ 120% / 90sec @ 55%"),
        (6, 120, 120, 1.15, 480, 300, "6x2min @ 115% / 2min @ 55%"),
        (6, 120, 60, 1.20, 600, 300, "6x2min @ 120% / 60sec @ 55%"),
        (4, 180, 150, 1.12, 600, 300, "4x3min @ 112% / 2.5min @ 55%"),
        (5, 180, 150, 1.10, 600, 300, "5x3min @ 110% / 2.5min @ 55%"),
        (3, 300, 180, 1.10, 600, 300, "3x5min @ 110% / 3min @ 55%"),
        (4, 240, 180, 1.08, 600, 300, "4x4min @ 108% / 3min @ 55%"),
        (4, 150, 120, 1.20, 600, 300, "4x2.5min @ 120% / 2min @ 55%"),
        (5, 120, 90, 1.20, 600, 300, "5x2min @ 120% / 90sec @ 55%"),
        (6, 90, 90, 1.25, 480, 300, "6x90sec @ 125% / 90sec @ 55%"),
        (4, 180, 120, 1.18, 480, 300, "4x3min @ 118% / 2min @ 55%"),
        (5, 120, 60, 1.25, 600, 300, "5x2min @ 125% / 60sec @ 50%"),
        (4, 240, 120, 1.10, 600, 300, "4x4min @ 110% / 2min @ 55%"),
        (3, 180, 120, 1.25, 480, 300, "3x3min @ 125% / 2min @ 55%"),
        (5, 150, 120, 1.15, 600, 300, "5x2.5min @ 115% / 2min @ 55%"),
        (4, 210, 150, 1.12, 600, 300, "4x3.5min @ 112% / 2.5min @ 55%"),
    ]
    for reps, on_d, off_d, on_p, wu, cd, desc_label in variants:
        blocks = [{"kind": "Warmup", "dur": wu, "lo": 0.50, "hi": 0.75}]
        desc_parts = [f"Warmup {_fmt_time(wu)}"]
        for _ in range(reps):
            blocks.append({"kind": "SteadyState", "dur": on_d, "lo": on_p, "hi": on_p})
            blocks.append({"kind": "SteadyState", "dur": off_d, "lo": 0.55, "hi": 0.55})
        desc_parts.append(
            f"{reps} x {_fmt_time(on_d)} @ {int(on_p*100)}% / {_fmt_time(off_d)} @ 55%"
        )
        blocks.append({"kind": "Cooldown", "dur": cd, "lo": 0.65, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd)}")

        if on_d >= 60:
            on_tag = f"{int(round(on_d/60))}min"
        else:
            on_tag = f"{on_d}s"
        sig = f"{reps}x{on_tag}"
        out.append(("vo2", sig, blocks, desc_parts))
    return out


# ---- Short threshold ----
def gen_short_threshold() -> list[tuple[str, str, list[dict], list[str]]]:
    out = []
    variants = [
        (2, 600, 180, 1.00, 600, 300),
        (2, 600, 240, 1.00, 600, 300),
        (2, 600, 180, 0.98, 480, 300),
        (2, 600, 300, 0.95, 600, 300),
        (2, 600, 180, 1.02, 600, 300),
        (3, 480, 180, 1.00, 600, 300),
        (3, 480, 240, 0.98, 600, 300),
        (3, 480, 180, 1.02, 600, 300),
        (3, 480, 120, 1.00, 480, 300),
        (3, 480, 180, 0.96, 600, 300),
        (4, 300, 180, 1.00, 600, 300),
        (4, 300, 180, 1.02, 600, 300),
        (4, 300, 120, 0.98, 600, 300),
        (4, 300, 180, 1.05, 600, 300),
        (4, 300, 240, 0.96, 600, 300),
        (2, 720, 180, 0.98, 480, 300),
        (3, 420, 180, 1.00, 600, 300),
        (3, 420, 150, 1.02, 480, 300),
        (4, 360, 180, 1.00, 480, 300),
        (4, 240, 120, 1.00, 480, 300),
        (5, 240, 120, 1.00, 480, 300),
        (5, 240, 180, 0.98, 600, 300),
        (3, 540, 180, 0.97, 600, 300),
        (2, 660, 180, 1.00, 600, 300),
        (4, 330, 150, 1.00, 600, 300),
    ]
    for reps, on_d, off_d, on_p, wu, cd in variants:
        blocks = [{"kind": "Warmup", "dur": wu, "lo": 0.50, "hi": 0.75}]
        desc_parts = [f"Warmup {_fmt_time(wu)}"]
        for _ in range(reps):
            blocks.append({"kind": "SteadyState", "dur": on_d, "lo": on_p, "hi": on_p})
            blocks.append({"kind": "SteadyState", "dur": off_d, "lo": 0.55, "hi": 0.55})
        desc_parts.append(
            f"{reps} x {_fmt_time(on_d)} @ {int(on_p*100)}% / {_fmt_time(off_d)} @ 55%"
        )
        blocks.append({"kind": "Cooldown", "dur": cd, "lo": 0.65, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd)}")
        if on_d >= 60:
            sig = f"{reps}x{int(round(on_d/60))}min"
        else:
            sig = f"{reps}x{on_d}s"
        out.append(("threshold", sig, blocks, desc_parts))
    return out


# ---- Over-unders with varied ratios ----
def gen_over_unders() -> list[tuple[str, str, list[dict], list[str]]]:
    out = []
    # (blocks_count, under_sec, over_sec, under_pw, over_pw, set_gap, set_count, wu, cd)
    variants = [
        (5, 120, 60,  0.90, 1.05, 240, 2, 600, 300),
        (5, 60,  30,  0.92, 1.10, 180, 2, 600, 300),
        (4, 180, 60,  0.90, 1.05, 300, 2, 600, 300),
        (4, 60,  120, 0.95, 1.05, 240, 2, 600, 300),
        (5, 120, 60,  0.88, 1.10, 300, 2, 600, 300),
        (6, 30,  30,  0.95, 1.15, 240, 2, 600, 300),
        (5, 90,  90,  0.90, 1.05, 240, 2, 600, 300),
        (3, 240, 60,  0.90, 1.05, 300, 2, 600, 300),
        (4, 120, 120, 0.90, 1.05, 240, 2, 600, 300),
        (5, 120, 30,  0.92, 1.10, 240, 2, 600, 300),
        (6, 60,  30,  0.92, 1.10, 180, 3, 480, 300),
        (4, 150, 60,  0.90, 1.08, 240, 2, 600, 300),
        (5, 60,  60,  0.92, 1.08, 180, 2, 600, 300),
        (3, 240, 120, 0.88, 1.05, 300, 2, 600, 300),
        (4, 180, 30,  0.90, 1.15, 240, 2, 600, 300),
        (6, 30,  15,  0.95, 1.20, 180, 2, 600, 300),
        (5, 90,  30,  0.90, 1.10, 240, 2, 480, 300),
        (4, 120, 60,  0.88, 1.10, 300, 2, 600, 300),
        (3, 180, 90,  0.90, 1.05, 300, 2, 600, 300),
        (5, 60,  60,  0.88, 1.05, 240, 2, 600, 300),
        (4, 120, 90,  0.90, 1.05, 240, 2, 600, 300),
        (6, 45,  30,  0.92, 1.12, 180, 2, 600, 300),
        (5, 105, 45,  0.90, 1.08, 240, 2, 600, 300),
        (4, 90,  30,  0.95, 1.15, 240, 2, 600, 300),
        (3, 300, 60,  0.88, 1.10, 300, 2, 600, 300),
    ]
    for pairs, under_d, over_d, under_p, over_p, gap, sets, wu, cd in variants:
        blocks = [{"kind": "Warmup", "dur": wu, "lo": 0.50, "hi": 0.75}]
        desc_parts = [f"Warmup {_fmt_time(wu)}"]
        for s in range(sets):
            for _ in range(pairs):
                blocks.append({"kind": "SteadyState", "dur": under_d, "lo": under_p, "hi": under_p})
                blocks.append({"kind": "SteadyState", "dur": over_d, "lo": over_p, "hi": over_p})
            if s < sets - 1:
                blocks.append({"kind": "SteadyState", "dur": gap, "lo": 0.55, "hi": 0.55})
        desc_parts.append(
            f"{sets} sets x {pairs} x ({_fmt_time(under_d)} @ {int(under_p*100)}% + "
            f"{_fmt_time(over_d)} @ {int(over_p*100)}%) / {_fmt_time(gap)} @ 55% between sets"
        )
        blocks.append({"kind": "Cooldown", "dur": cd, "lo": 0.65, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd)}")
        sig = f"{sets}x{pairs}_under{under_d}s_over{over_d}s"
        out.append(("over_under", sig, blocks, desc_parts))
    return out


# ---- Neuromuscular sprints (6-12s all-out) ----
def gen_neuro_sprints() -> list[tuple[str, str, list[dict], list[str]]]:
    out = []
    # Neuromuscular: very short, very high, long recovery
    variants = [
        (10, 6,  180, 1.70, 600, 300),
        (10, 8,  180, 1.60, 600, 300),
        (10, 10, 180, 1.55, 600, 300),
        (8,  12, 180, 1.50, 600, 300),
        (12, 6,  180, 1.75, 600, 300),
        (8,  10, 240, 1.60, 600, 300),
        (10, 6,  240, 1.80, 600, 300),
        (6,  10, 300, 1.65, 600, 300),
        (12, 8,  150, 1.65, 600, 300),
        (8,  6,  180, 1.80, 600, 300),
        (10, 12, 180, 1.45, 600, 300),
        (6,  12, 300, 1.55, 600, 300),
        (10, 8,  240, 1.65, 600, 300),
        (8,  12, 240, 1.50, 600, 300),
        (12, 10, 150, 1.55, 600, 300),
    ]
    for reps, on_d, off_d, on_p, wu, cd in variants:
        blocks = [{"kind": "Warmup", "dur": wu, "lo": 0.50, "hi": 0.75}]
        desc_parts = [f"Warmup {_fmt_time(wu)}"]
        for _ in range(reps):
            blocks.append({"kind": "SteadyState", "dur": on_d, "lo": on_p, "hi": on_p})
            blocks.append({"kind": "SteadyState", "dur": off_d, "lo": 0.50, "hi": 0.50})
        desc_parts.append(
            f"{reps} x {on_d}s @ {int(on_p*100)}% / {_fmt_time(off_d)} @ 50%"
        )
        blocks.append({"kind": "Cooldown", "dur": cd, "lo": 0.65, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd)}")
        sig = f"{reps}x{on_d}s"
        out.append(("sprints", sig, blocks, desc_parts))
    return out


# ---- Short sweet spot (2x15, 3x10) ----
def gen_short_sweet_spot() -> list[tuple[str, str, list[dict], list[str]]]:
    out = []
    variants = [
        (2, 900, 180, 0.90, 600, 300),
        (2, 900, 240, 0.90, 600, 300),
        (2, 900, 180, 0.88, 600, 300),
        (2, 900, 300, 0.92, 600, 300),
        (2, 900, 180, 0.94, 600, 300),
        (3, 600, 180, 0.90, 600, 300),
        (3, 600, 240, 0.90, 600, 300),
        (3, 600, 180, 0.88, 600, 300),
        (3, 600, 120, 0.92, 600, 300),
        (3, 600, 180, 0.94, 600, 300),
        (2, 1080, 240, 0.90, 600, 300),
        (3, 540, 180, 0.92, 600, 300),
        (3, 720, 240, 0.88, 480, 300),
        (2, 1200, 300, 0.90, 600, 300),
        (3, 480, 120, 0.92, 480, 300),
    ]
    for reps, on_d, off_d, on_p, wu, cd in variants:
        blocks = [{"kind": "Warmup", "dur": wu, "lo": 0.50, "hi": 0.75}]
        desc_parts = [f"Warmup {_fmt_time(wu)}"]
        for _ in range(reps):
            blocks.append({"kind": "SteadyState", "dur": on_d, "lo": on_p, "hi": on_p})
            blocks.append({"kind": "SteadyState", "dur": off_d, "lo": 0.55, "hi": 0.55})
        desc_parts.append(
            f"{reps} x {_fmt_time(on_d)} @ {int(on_p*100)}% / {_fmt_time(off_d)} @ 55%"
        )
        blocks.append({"kind": "Cooldown", "dur": cd, "lo": 0.65, "hi": 0.45})
        desc_parts.append(f"Cooldown {_fmt_time(cd)}")
        sig = f"{reps}x{int(round(on_d/60))}min"
        out.append(("sweet_spot", sig, blocks, desc_parts))
    return out


def run() -> dict:
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index(WORKOUTS_DIR)
    per_cat = {
        "pyramid": 0,
        "vo2": 0,
        "threshold": 0,
        "over_under": 0,
        "sprints": 0,
        "sweet_spot": 0,
    }
    stats = {
        "generated": 0,
        "written": 0,
        "skipped_dedupe": 0,
        "skipped_too_short": 0,
        "per_category_written": per_cat,
    }

    all_groups = [
        gen_pyramids(),
        gen_short_vo2(),
        gen_short_threshold(),
        gen_over_unders(),
        gen_neuro_sprints(),
        gen_short_sweet_spot(),
    ]

    for group in all_groups:
        for slug, sig, blocks, desc_parts in group:
            stats["generated"] += 1
            zwo_text, total_min = _emit(blocks, slug, sig, desc_parts)
            if total_min < 5:
                stats["skipped_too_short"] += 1
                continue

            fname = f"{slug}_{sig}_{total_min}min.zwo"
            tmp = WORKOUTS_DIR / f".tmp_{fname}"
            tmp.write_text(zwo_text)
            h = structure_hash(tmp)
            if h in index:
                stats["skipped_dedupe"] += 1
                tmp.unlink(missing_ok=True)
                continue

            final = WORKOUTS_DIR / fname
            if final.exists():
                v = 2
                while (WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo").exists():
                    v += 1
                final = WORKOUTS_DIR / f"{fname[:-4]}_v{v}.zwo"
            tmp.rename(final)
            index[h] = final.name
            stats["written"] += 1
            per_cat[slug] += 1

    # Persist updated index
    (WORKOUTS_DIR / ".structure_index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True)
    )
    return stats


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
