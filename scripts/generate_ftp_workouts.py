#!/usr/bin/env python3.12
"""Generate ZWO workouts across FTP categories.

Produces 30-min and 45-min variants (3 each) for 7 categories:
  recovery, endurance, sweet_spot, threshold, vo2, over_under, sprints

Output: workouts/{category}_{duration}min_v{N}.zwo
Total: 7 categories x 2 durations x 3 variants = 42 files.
"""
from __future__ import annotations

import random
from pathlib import Path

WORKOUT_DIR = Path(__file__).parent / "workouts"

# (base_if for steady blocks, work/recovery pattern for intervals)
CATEGORIES: dict[str, dict] = {
    "recovery":   {"base_if": 0.55, "intervals": None,                         "label": "Recovery Spin"},
    "endurance":  {"base_if": 0.65, "intervals": None,                         "label": "Endurance Z2"},
    "sweet_spot": {"base_if": 0.70, "intervals": [(420, 0.90), (180, 0.55)],   "label": "Sweet Spot"},
    "threshold":  {"base_if": 0.70, "intervals": [(600, 1.00), (240, 0.55)],   "label": "Threshold"},
    "vo2":        {"base_if": 0.65, "intervals": [(180, 1.15), (120, 0.55)],   "label": "VO2max"},
    "over_under": {"base_if": 0.70, "intervals": [(120, 0.85), (60, 1.10)],    "label": "Over-Under"},
    "sprints":    {"base_if": 0.65, "intervals": [(30,  1.50), (90, 0.55)],    "label": "Sprints"},
}


def _fmt_power(p: float) -> str:
    """Format power as decimal string without trailing zeros."""
    return f"{p:.2f}".rstrip("0").rstrip(".") or "0"


def build_steady(duration_sec: int, power: float) -> str:
    return f'        <SteadyState Duration="{duration_sec}" Power="{_fmt_power(power)}" pace="0" />\n'


def build_warmup(duration_sec: int, start_pw: float, end_pw: float) -> str:
    return (
        f'        <Warmup Duration="{duration_sec}" PowerLow="{_fmt_power(start_pw)}" '
        f'PowerHigh="{_fmt_power(end_pw)}" pace="0" />\n'
    )


def build_cooldown(duration_sec: int, start_pw: float, end_pw: float) -> str:
    return (
        f'        <Cooldown Duration="{duration_sec}" PowerLow="{_fmt_power(start_pw)}" '
        f'PowerHigh="{_fmt_power(end_pw)}" pace="0" />\n'
    )


def compute_tss(blocks: list[tuple[int, float]], duration_min: int) -> int:
    """Rough TSS estimate from block list: sum((sec/3600) * IF^2 * 100)."""
    tss = 0.0
    for sec, power in blocks:
        tss += (sec / 3600.0) * (power ** 2) * 100.0
    return int(round(tss))


def build_workout_blocks(category: str, duration_min: int, variant: int) -> list[tuple[int, float]]:
    """Return list of (duration_sec, power) tuples making up the workout."""
    random.seed(f"{category}_{duration_min}_{variant}")
    cfg = CATEGORIES[category]
    total_sec = duration_min * 60
    blocks: list[tuple[int, float]] = []

    # Warmup (5 min for 30-min, 8 min for 45-min)
    wu_sec = 300 if duration_min == 30 else 480
    cd_sec = 180 if duration_min == 30 else 300
    body_sec = total_sec - wu_sec - cd_sec

    if cfg["intervals"] is None:
        # Steady / endurance / recovery: vary power slightly across segments
        base = cfg["base_if"]
        if category == "recovery":
            # Gentle ramp variations 0.50-0.60
            targets = [base - 0.05, base, base + 0.03, base + 0.05, base]
        else:  # endurance
            targets = [base - 0.05, base, base + 0.05, base + 0.08, base]
        # Add variant perturbation
        variant_shift = (variant - 1) * 0.02
        seg_len = body_sec // len(targets)
        remainder = body_sec - seg_len * len(targets)
        for i, t in enumerate(targets):
            extra = remainder if i == len(targets) - 1 else 0
            blocks.append((seg_len + extra, max(0.45, t + variant_shift)))
    else:
        work_sec, work_pw = cfg["intervals"][0]
        rec_sec, rec_pw = cfg["intervals"][1]
        pair_sec = work_sec + rec_sec

        # Variant tweaks: alter work power or interval count
        if variant == 0:
            # Standard
            pass
        elif variant == 1:
            work_pw += 0.03
            work_sec = max(20, int(work_sec * 0.9))
            pair_sec = work_sec + rec_sec
        else:
            work_pw -= 0.02
            work_sec = int(work_sec * 1.1)
            pair_sec = work_sec + rec_sec

        # Fit as many pairs as possible into body
        n_pairs = max(1, body_sec // pair_sec)
        used = n_pairs * pair_sec
        leftover = body_sec - used
        # Pre-interval easy spin uses leftover
        if leftover > 30:
            blocks.append((leftover, cfg["base_if"]))
        for _ in range(n_pairs):
            blocks.append((work_sec, work_pw))
            blocks.append((rec_sec, rec_pw))

    return blocks, wu_sec, cd_sec


def build_zwo(category: str, duration_min: int, variant: int) -> str:
    cfg = CATEGORIES[category]
    blocks, wu_sec, cd_sec = build_workout_blocks(category, duration_min, variant)

    # Warmup/cooldown power ranges per category
    if category == "recovery":
        wu_lo, wu_hi, cd_hi, cd_lo = 0.45, 0.55, 0.55, 0.45
    elif category == "endurance":
        wu_lo, wu_hi, cd_hi, cd_lo = 0.50, 0.65, 0.65, 0.50
    elif category in ("sweet_spot", "threshold", "over_under"):
        wu_lo, wu_hi, cd_hi, cd_lo = 0.50, 0.75, 0.65, 0.45
    else:  # vo2, sprints
        wu_lo, wu_hi, cd_hi, cd_lo = 0.50, 0.80, 0.70, 0.45

    all_blocks = [(wu_sec, (wu_lo + wu_hi) / 2)] + blocks + [(cd_sec, (cd_hi + cd_lo) / 2)]
    tss = compute_tss(all_blocks, duration_min)

    # Description: "N min @ PP% FTP" joined
    desc_parts = [f"Warmup {wu_sec // 60}min"]
    for sec, pw in blocks:
        desc_parts.append(f"{sec // 60 if sec >= 60 else sec}{'min' if sec >= 60 else 's'} @ {int(pw * 100)}% FTP")
    desc_parts.append(f"Cooldown {cd_sec // 60}min")
    description = " | ".join(desc_parts) + f" | Est. TSS {tss}"

    name = f"{cfg['label']} {duration_min}min v{variant + 1}"

    body = ""
    body += build_warmup(wu_sec, wu_lo, wu_hi)
    for sec, pw in blocks:
        body += build_steady(sec, pw)
    body += build_cooldown(cd_sec, cd_hi, cd_lo)

    zwo = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<workout_file>\n"
        "    <author>ChickenCycling</author>\n"
        f"    <name>{name}</name>\n"
        f"    <description>{description}</description>\n"
        "    <sportType>bike</sportType>\n"
        "    <workout>\n"
        f"{body}"
        "    </workout>\n"
        "</workout_file>\n"
    )
    return zwo


def main() -> None:
    WORKOUT_DIR.mkdir(exist_ok=True)
    count = 0
    for cat in CATEGORIES:
        for dur in (30, 45):
            for v in range(3):
                path = WORKOUT_DIR / f"{cat}_{dur}min_v{v}.zwo"
                path.write_text(build_zwo(cat, dur, v))
                count += 1
    print(f"Generated {count} ZWO workouts in {WORKOUT_DIR}")


if __name__ == "__main__":
    main()
