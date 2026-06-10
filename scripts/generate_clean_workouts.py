#!/usr/bin/env python3.12
"""Generate CLEAN, canonical, copyright-free gap-filler workouts (additive).

Design (grilled — /tmp/MASTER_DECISIONS_clean_library.md):
  * Structure/science only — our own <author>Domestique Library</author>, generic
    content-derived names. No scraping, no third-party files.
  * CANONICAL cleanliness: round total durations, round power %, round interval
    durations, sensible recovery ratios, proper warmup/cooldown ramps.
  * Intervals emitted as <IntervalsT Repeat=N ...> so reps are EXACT (the
    classifier reads Repeat directly; the FIT transcode + visualiser both expand
    it) — fixes the "N intervals → (N-1)× in the title" undercount.
  * CLASSIFY-BEFORE-WRITE: every candidate is run through the live content
    classifier and kept ONLY if its primary == the intended class, its title's
    rep/duration match the content, and its total lands on a round boundary.
    This guarantees no mislabeled / wrong-duration file ever ships, regardless
    of power-band guesses.
  * Variable warmup/cooldown so the TOTAL lands on a round boundary.
  * Deduped against the existing 3054-file structure index (never deletes).
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dedupe_zwo_library import structure_hash, load_index  # noqa: E402
import classify_library_content as C  # noqa: E402

WORKOUTS_DIR = Path(__file__).resolve().parent.parent / "workouts"
ROUND_TOTALS = (30, 40, 45, 50, 60, 75, 90, 105, 120, 150, 180)


def _fmt_pw(p: float) -> str:
    return f"{p:.2f}".rstrip("0").rstrip(".") or "0"


def _emit_intervals(intended_label: str, reps: int, on_s: int, off_s: int,
                    on_pw: float, off_pw: float, total_min: int) -> "str | None":
    """Build a clean ZWO: warmup ramp + IntervalsT + cooldown ramp, with the
    warmup/cooldown sized so the TOTAL == total_min (round). Returns ZWO text or
    None if the bookend can't be split into sane 5–15-min ramps."""
    work_sec = reps * (on_s + off_s)
    bookend = total_min * 60 - work_sec
    # Need 10–28 min of combined warmup+cooldown, each 5–15 min.
    if not (600 <= bookend <= 1680):
        return None
    cd = max(300, min(900, int(round((bookend * 0.4) / 30) * 30)))
    wu = bookend - cd
    if not (300 <= wu <= 900):
        return None
    body = (
        f'        <Warmup Duration="{wu}" PowerLow="0.50" PowerHigh="0.75" pace="0" />\n'
        f'        <IntervalsT Repeat="{reps}" OnDuration="{on_s}" OffDuration="{off_s}" '
        f'OnPower="{_fmt_pw(on_pw)}" OffPower="{_fmt_pw(off_pw)}" pace="0" />\n'
        f'        <Cooldown Duration="{cd}" PowerLow="0.65" PowerHigh="0.45" pace="0" />\n'
    )
    return _wrap(body)


def _emit_blocks(reps: int, on_s: int, off_s: int, on_pw: float, off_pw: float,
                 n_blocks: int, block_rec_s: int, total_min: int) -> "str | None":
    """POLARIZED macro-block session (Rønnestad-style): warmup + N macro blocks
    of [IntervalsT] separated by EASY (Z1/low-Z2) recovery + cooldown. The
    between-rep recovery (off_pw) AND the between-block recovery are both EASY
    (~50%), so the session is genuinely polarized — hard VO2 work, easy
    everything else, NO tempo/threshold grey zone. Bookends sized to land
    total_min round."""
    block_work = reps * (on_s + off_s)
    inner = n_blocks * block_work + (n_blocks - 1) * block_rec_s
    bookend = total_min * 60 - inner
    if not (600 <= bookend <= 1680):
        return None
    cd = max(300, min(900, int(round((bookend * 0.4) / 30) * 30)))
    wu = bookend - cd
    if not (300 <= wu <= 900):
        return None
    body = f'        <Warmup Duration="{wu}" PowerLow="0.50" PowerHigh="0.75" pace="0" />\n'
    for b in range(n_blocks):
        body += (f'        <IntervalsT Repeat="{reps}" OnDuration="{on_s}" '
                 f'OffDuration="{off_s}" OnPower="{_fmt_pw(on_pw)}" '
                 f'OffPower="{_fmt_pw(off_pw)}" pace="0" />\n')
        if b < n_blocks - 1:
            body += (f'        <SteadyState Duration="{block_rec_s}" '
                     f'Power="{_fmt_pw(0.50)}" pace="0" />\n')  # easy between blocks
    body += f'        <Cooldown Duration="{cd}" PowerLow="0.65" PowerHigh="0.45" pace="0" />\n'
    return _wrap(body)


def _polarized_candidates():
    """Rønnestad / polarized VO2 macro-block specs. EASY (≈50%) recovery between
    reps AND between blocks — no grey-zone filler. Accept the hard-aerobic
    family (vo2max / vo2_short / anaerobic); reject if it lands grey-zone."""
    HARD = {"vo2max", "vo2_short", "anaerobic"}
    # (reps, on_s, off_s, on_pw, off_pw, n_blocks, block_rec_s)
    specs = [
        (13, 30, 15, 1.18, 0.50, 3, 180),   # classic Rønnestad 30/15 ×3 blocks
        (13, 30, 15, 1.16, 0.50, 3, 180),
        (10, 30, 15, 1.18, 0.50, 3, 180),
        (8, 30, 30, 1.15, 0.50, 3, 240),    # 30/30 micro
        (8, 40, 20, 1.15, 0.50, 3, 180),    # 40/20 ×3
        (6, 40, 20, 1.18, 0.50, 4, 180),    # 40/20 ×4 blocks
        (5, 60, 60, 1.15, 0.50, 3, 300),    # 1min on / 1min EASY ×3 (user's, polarized)
        (4, 60, 60, 1.12, 0.50, 3, 300),    # 4×1min ×3 macro-blocks, easy recovery
        (4, 60, 60, 1.15, 0.50, 4, 240),
        (5, 120, 120, 1.12, 0.50, 3, 300),  # 2min on / 2min easy ×3
        (4, 180, 180, 1.10, 0.50, 3, 300),  # 3min on / 3min easy ×3
    ]
    for reps, on_s, off_s, on_pw, off_pw, nb, brec in specs:
        yield (HARD, reps, on_s, off_s, on_pw, off_pw, nb, brec)


def _emit_steady(blocks: list[tuple[int, float]], total_min: int) -> "str | None":
    """Steady workout (endurance/tempo/recovery): warmup ramp + steady blocks +
    cooldown ramp, bookends sized to land total_min."""
    work_sec = sum(d for d, _ in blocks)
    bookend = total_min * 60 - work_sec
    if not (600 <= bookend <= 1680):
        return None
    cd = max(300, min(900, int(round((bookend * 0.4) / 30) * 30)))
    wu = bookend - cd
    if not (300 <= wu <= 900):
        return None
    body = f'        <Warmup Duration="{wu}" PowerLow="0.45" PowerHigh="0.65" pace="0" />\n'
    for d, p in blocks:
        body += f'        <SteadyState Duration="{d}" Power="{_fmt_pw(p)}" pace="0" />\n'
    body += f'        <Cooldown Duration="{cd}" PowerLow="0.60" PowerHigh="0.40" pace="0" />\n'
    return _wrap(body)


def _wrap(body: str) -> str:
    # Name/description are placeholders — the REAL generic display_name is set by
    # the classifier after acceptance, so the stored <name> stays content-derived.
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n<workout_file>\n"
        "    <author>Domestique Library</author>\n"
        "    <name>Domestique workout</name>\n"
        "    <description>Generated structured workout.</description>\n"
        "    <sportType>bike</sportType>\n    <workout>\n"
        f"{body}    </workout>\n</workout_file>\n"
    )


def _interval_candidates():
    """Yield (intended_class, reps, on_s, off_s, on_pw, off_pw) canonical specs.
    Powers pinned (grill) to land cleanly inside the intended classifier zone."""
    # threshold 95–100%, varied rec (180/240/300s)
    for reps, on_m in [(2, 20), (3, 15), (4, 10), (4, 8), (3, 12), (5, 8),
                       (2, 30), (3, 10), (2, 25), (4, 12), (6, 6), (5, 10),
                       (3, 8), (2, 15), (3, 20)]:
        for pw in (0.95, 1.00):
            for rec in (180, 300):
                yield ("threshold", reps, on_m * 60, rec, pw, 0.55)
    # sweet_spot 90%, varied rec
    for reps, on_m in [(2, 20), (3, 15), (4, 12), (3, 12), (4, 10), (2, 25),
                       (3, 10), (2, 30), (5, 10), (4, 8), (3, 20), (2, 15)]:
        for rec in (180, 300):
            yield ("sweet_spot", reps, on_m * 60, rec, 0.90, 0.55)
    # tempo_intervals 85%, 3-min rec (round; below the 88% SS floor)
    for reps, on_m in [(3, 10), (4, 10), (3, 12), (4, 8), (5, 8), (3, 15)]:
        yield ("tempo_intervals", reps, on_m * 60, 180, 0.85, 0.55)
    # vo2max 110/115% (round; cap reps at the hot end; rec ≥ work)
    for reps, on_s, pw, off_s in [
        (4, 240, 1.10, 240), (5, 240, 1.10, 240), (5, 180, 1.15, 180),
        (6, 180, 1.10, 180), (8, 120, 1.15, 150), (6, 180, 1.15, 180),
        (4, 300, 1.10, 300), (5, 300, 1.10, 240), (8, 120, 1.10, 120),
    ]:
        yield ("vo2max", reps, on_s, off_s, pw, 0.55)
    # anaerobic 130–150%, ≥3:1 recovery, capped reps
    for reps, on_s, pw, off_s in [
        (6, 30, 1.50, 120), (8, 30, 1.40, 90), (6, 45, 1.40, 180),
        (8, 30, 1.50, 120), (6, 60, 1.30, 180), (8, 45, 1.35, 135),
    ]:
        yield ("anaerobic", reps, on_s, off_s, pw, 0.50)
    # neuromuscular 200% short sprints, long rec
    for reps, on_s, off_s in [(8, 15, 165), (10, 15, 165), (12, 10, 110),
                              (10, 20, 160), (8, 20, 220), (12, 15, 105)]:
        yield ("neuromuscular", reps, on_s, off_s, 2.00, 0.50)
    # over_under: under 90% (round; <0.91 leaves Z4) / over 105%, alternating
    for reps, under_s, over_s, u_pw, o_pw in [
        (9, 120, 60, 0.90, 1.05), (12, 120, 60, 0.90, 1.05),
        (8, 90, 60, 0.90, 1.05), (10, 120, 45, 0.90, 1.05),
        (12, 90, 45, 0.90, 1.05), (9, 150, 60, 0.90, 1.10),
    ]:
        # On = under (longer), Off = over — alternation drives the OU detector.
        yield ("over_under", reps, under_s, over_s, u_pw, o_pw)


def _steady_candidates():
    """Yield (intended_class, [(dur_s, pw), ...]) for steady-type sessions.
    Note: bookend (warmup+cooldown) is sized in _emit_steady to land round, so
    the steady block here is total*60 - a nominal bookend; emit adjusts."""
    # endurance 65–72%, long (steady)
    for pw in (0.65, 0.68, 0.72):
        for total in (60, 75, 90, 105, 120, 150, 180):
            yield ("endurance", [(total * 60 - 1500, pw)], total)
    # endurance with surges (still Z2-dominant): main Z2 + N×1min @85
    for total, n in [(75, 5), (90, 6), (120, 8), (105, 6), (150, 8)]:
        main = total * 60 - 1500 - n * 120
        blocks = [(main, 0.70)]
        for _ in range(n):
            blocks += [(60, 0.85), (60, 0.62)]
        yield ("endurance", blocks, total)
    # tempo steady 80%, moderate
    for pw in (0.78, 0.80, 0.83):
        for total in (45, 60, 75, 90):
            yield ("tempo", [(total * 60 - 1200, pw)], total)
    # recovery 50–56%
    for pw in (0.50, 0.52, 0.55):
        for total in (20, 30, 40, 45):
            yield ("recovery", [(total * 60 - 900, pw)], total)


def run() -> dict:
    index = load_index(WORKOUTS_DIR)
    stats = {"tried": 0, "written": 0, "rej_class": 0, "rej_round": 0,
             "rej_dup": 0, "rej_emit": 0, "by_class": {}}
    accepted: list[Path] = []

    def _try(intended: str, zwo_text: "str | None"):
        stats["tried"] += 1
        if not zwo_text:
            stats["rej_emit"] += 1
            return
        tmp = WORKOUTS_DIR / ".tmp_clean_candidate.zwo"
        tmp.write_text(zwo_text, encoding="utf-8")
        try:
            res = C.classify_zwo_v104(tmp)
            primary = res.get("primary")
            feats = res.get("features") or {}
            dur_s = feats.get("duration_s") or 0
            total_min = round(dur_s / 60)
            # classify-before-write gates. ``intended`` may be a single class
            # or a set of acceptable classes (polarized VO2 blocks legitimately
            # land as vo2max OR vo2_short depending on rep length — both are the
            # genuinely-hard polarized outcome we want; grey-zone results
            # threshold/tempo/sweet_spot are still rejected).
            ok = (primary in intended) if isinstance(intended, (set, tuple, frozenset)) \
                else (primary == intended)
            if not ok:
                stats["rej_class"] += 1; return
            if total_min not in ROUND_TOTALS:
                stats["rej_round"] += 1; return
            h = structure_hash(tmp)
            if h in index:
                stats["rej_dup"] += 1; return
            dn = res.get("display_name") or ""
            # final flat generic filename
            slug = primary
            fname = f"{slug}_clean_{total_min}min.zwo"
            v = 1
            final = WORKOUTS_DIR / fname
            while final.exists():
                v += 1
                final = WORKOUTS_DIR / f"{slug}_clean_{total_min}min_v{v}.zwo"
            # write with the generic classifier display_name as <name>
            zwo_named = zwo_text.replace(
                "<name>Domestique workout</name>",
                f"<name>{html.escape(dn or (slug + ' ' + str(total_min) + 'min'))}</name>")
            final.write_text(zwo_named, encoding="utf-8")
            index[h] = final.name
            accepted.append(final)
            stats["written"] += 1
            stats["by_class"][primary] = stats["by_class"].get(primary, 0) + 1
        finally:
            tmp.unlink(missing_ok=True)

    # interval candidates × round totals
    for intended, reps, on_s, off_s, on_pw, off_pw in _interval_candidates():
        for total in ROUND_TOTALS:
            zwo = _emit_intervals(intended, reps, on_s, off_s, on_pw, off_pw, total)
            if zwo:
                _try(intended, zwo)
    # steady candidates (total already chosen)
    for intended, blocks, total in _steady_candidates():
        zwo = _emit_steady(blocks, total)
        _try(intended, zwo)
    # polarized macro-block candidates × round totals (Rønnestad VO2 + easy Z2)
    for intended, reps, on_s, off_s, on_pw, off_pw, nb, brec in _polarized_candidates():
        for total in ROUND_TOTALS:
            zwo = _emit_blocks(reps, on_s, off_s, on_pw, off_pw, nb, brec, total)
            if zwo:
                _try(intended, zwo)

    return stats


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
