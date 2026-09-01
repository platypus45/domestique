#!/usr/bin/env python3
"""v3.9.0 — give every workout a filename that describes its structure.

The library had 408 files called ``threshold_clean_<N>min.zwo`` and 234 called
``endurance_clean_<N>min.zwo``, distinguished only by total duration. Sorted in
a folder they are indistinguishable, and the one thing a rider wants to know
from the name — what the session actually IS — was the one thing missing.

The new shape is the one the Rønnestad family already uses:

    <class>_<structure>_<intensity>pct_<duration>min.zwo
    threshold_4x8min-4min_98pct_62min.zwo
    vo2_short_3x13x30s-15s_113pct_55min.zwo
    endurance_steady_65pct_90min.zwo

``structure`` is read from the prescription, not from the old name:
  * repeated work set   -> ``<reps>x<on>[-<recovery>]`` (``4x8min-4min``)
  * set broken into series -> ``<series>x<reps>x<on>[-<recovery>]``
    (``3x13x30s-15s`` — a Ronnestad 3x13, NOT 39 straight reps: the
    between-series recoveries are most of what makes the protocol work,
    and "3x13" is what the rider searches the folder for)
  * distinct rungs, no repeat -> ``ladder<rungs>``
  * no work at all      -> ``steady``
``intensity`` is the main set's target as a percentage of FTP; for a ladder it
is the top rung. It is omitted only when the file has no target power at all.

RENAMING IS THE DANGEROUS PART, so this script owns the whole migration:
saved plans reference workouts by filename, and so do four on-disk caches and
the test suite. A rename that moves only the files leaves a rider's plan
pointing at names that no longer exist. Every reference is rewritten in the
same pass, and --check re-reads them afterwards to prove none was missed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import structure_fidelity as sf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORKOUTS = ROOT / "workouts"
CACHES = [
    ".library_index.json", ".content_classification.json",
    ".structure_index.json", ".workout_facts.json", ".golden_set.json",
    ".ftp_tests_manifest.json", ".overhaul_manifest.json",
    ".github_imports_manifest.json",
]

WORK_FLOOR = 0.76      # at or above this a segment is work, not filler
PRIMER_MARGIN = 0.20   # work more than this far under the peak may be a primer
PRIMER_MAX_SHARE = 0.25  # ...and a primer holds at most this share of the work
SERIES_GAP_RATIO = 3   # a gap this many x the in-set recovery breaks a series
SERIES_GAP_MIN_S = 60  # ...and must be at least this long in absolute terms


def _fmt_secs(s: int) -> str:
    """30 -> '30s', 480 -> '8min'. Only whole minutes collapse."""
    return f"{s // 60}min" if s >= 60 and s % 60 == 0 else f"{s}s"


def structure_of(segments: list[dict]) -> "tuple[str, int | None]":
    """(structure token, main-set target as whole % FTP), read from the file.

    ONE algorithm for every file. The parser has already expanded IntervalsT
    into interval_on/interval_off, so a declared interval set and a set written
    as loose SteadyStates look identical here — which matters, because the old
    names could not tell them apart: `anaerobic_18x2min_60min.zwo` is really
    90s at 97% alternating with 30s at 119%, has no IntervalsT at all, and runs
    66 minutes not 60.

    A work segment is anything at or above WORK_FLOOR; its REST is whatever
    sub-floor segment follows. Group by (work, target), drop cadence primers,
    and let the group holding the most work seconds name the file — a session
    is its main set, not the finisher bolted on the end.
    """
    segs = [s for s in segments if (s.get("dur_s") or 0) > 0]
    work = [s for s in segs
            if s.get("lo") is not None and float(s["lo"]) >= WORK_FLOOR]
    peak = max((float(s["lo"]) for s in work), default=0.0)

    # Keyed on the WORK only. Keying on the rest as well shattered every
    # descending-recovery session into one group per rep — twelve 30s sprints
    # off 160s..110s recoveries read "1x30s" — because no two reps shared a
    # key. The rest is recorded per rep instead and only reaches the name when
    # it is the same every time.
    groups: dict = defaultdict(lambda: [0, 0, [], []])  # -> secs, reps, rests, idx
    for i, s in enumerate(segs):
        mid = s.get("lo")
        if mid is None or float(mid) < WORK_FLOOR:
            continue
        rest = 0
        if i + 1 < len(segs):
            nxt = segs[i + 1]
            if (nxt.get("lo") or 0) < WORK_FLOOR:
                rest = int(nxt["dur_s"])
        g = groups[(int(s["dur_s"]), round(100 * float(mid)))]
        g[0] += int(s["dur_s"])
        g[1] += 1
        g[2].append(rest)
        g[3].append(i)
    if not groups:
        # No work segment at all: a steady ride. Name it by the duration-
        # weighted intensity of its BODY (warm-up and cool-down excluded), so
        # a 30-minute spin at 55% and one at 62% are still told apart. Every
        # name carries an intensity — no field is ever silently dropped.
        body = [x for x in segs
                if x.get("kind") not in ("warmup", "cooldown")
                and x.get("lo") is not None]
        if not body:
            body = [x for x in segs if x.get("lo") is not None]
        if body:
            tot = sum(int(x["dur_s"]) for x in body)
            mean = sum(int(x["dur_s"]) * float(x["lo"]) for x in body) / tot
            return "steady", round(100 * mean)
        return "steady", None
    # A session's identity is its REPEATED SET, not its longest block. Ranking
    # purely by work seconds made an 11x1min session read "1x15min", because
    # one 15-minute block outweighs eleven one-minute reps. So prefer a real
    # set (>=3 reps), then a pair, and only then a lone block — and inside
    # each tier take the group holding the most work.
    # A CADENCE PRIMER is written as ordinary SteadyStates, not inside the
    # <Warmup>, so it cannot be excluded by segment kind — and at 82% it clears
    # WORK_FLOOR. A 5-rung anaerobic ladder running 108%..154% opens with
    # 3x30s at 82%, which was the only REPEATED group in the file, so the file
    # was named "3x30s @ 82pct": a lie about a session whose real work is at
    # 154%, and the reason two unrelated ladders collided onto one name.
    #
    # A primer is SMALL *and* EASY, and both halves are load-bearing. Filtering
    # on intensity alone promoted a 2x10s @ 175% finisher over a 15x105s @ 90%
    # main set — the finisher is the peak, but it is not the session.
    total_work = sum(v[0] for v in groups.values()) or 1
    biggest = max(groups.values(), key=lambda v: v[0])[0]
    cand = {k: v for k, v in groups.items()
            if not (k[1] <= round(100 * (peak - PRIMER_MARGIN))
                    and v[0] < PRIMER_MAX_SHARE * total_work
                    and v[1] >= 2
                    and v[0] < biggest)}
    if not cand:                                  # never filter everything away
        cand = dict(groups)

    for floor in (3, 2, 1):
        tier = {k: v for k, v in cand.items() if v[1] >= floor}
        if tier:
            (on, pct), (_secs, reps, rests, idx) = max(tier.items(),
                                                       key=lambda kv: kv[1][0])
            break
    if reps == 1:
        # No repeated set anywhere. If the work is instead a run of distinct
        # rungs, say so — a ladder IS its rung count, and naming it after its
        # single longest rung would hide every other effort in the session.
        if len(cand) >= 3:
            return f"ladder{len(cand)}", round(100 * peak)
        return f"1x{_fmt_secs(on)}", pct
    series = _series_count(segs, idx)
    # A single recovery length is part of the prescription and belongs in the
    # name; a varying one cannot be stated in one token, so it is left out
    # rather than averaged into a number the file does not contain.
    uniform = set(rests[:-1] or rests)              # trailing rep often has none
    tail = (f"-{_fmt_secs(next(iter(uniform)))}"
            if len(uniform) == 1 and next(iter(uniform)) else "")
    if series > 1 and reps % series == 0:
        # 4x9x30s-15s, not 36x30s-15s. Ronnestad 4x9 and a straight 36-rep
        # block are different sessions — the between-series recoveries are
        # most of what makes the protocol work — and the rider searches the
        # folder for "4x9".
        return f"{series}x{reps // series}x{_fmt_secs(on)}{tail}", pct
    return f"{reps}x{_fmt_secs(on)}{tail}", pct


def _series_count(segs: list[dict], idx: list[int]) -> int:
    """How many SERIES the main set is broken into, from the gaps between reps.

    A gap that dwarfs the ordinary between-rep recovery is a series break.
    "Dwarfs" is >= SERIES_GAP_RATIO x the median in-set recovery and at least
    SERIES_GAP_MIN_S, so a 15s micro-recovery next to a 180s series recovery
    reads as a break while merely uneven recoveries do not.
    """
    if len(idx) < 4:
        return 1
    gaps = []
    for a, b in zip(idx, idx[1:]):
        gaps.append(sum(int(segs[k]["dur_s"] or 0) for k in range(a + 1, b)))
    inset = sorted(g for g in gaps if g > 0)
    if not inset:
        return 1
    med = inset[len(inset) // 2]
    breaks = sum(1 for g in gaps
                 if g >= max(SERIES_GAP_MIN_S, SERIES_GAP_RATIO * med))
    return breaks + 1


# Everything ahead of the structure token, longest first so "sweet_spot" wins
# over "sweet" and "vo2_short" over "vo2".
#
# These are not decoration. Three fallback ladders classify a workout by
# matching the head of its filename (classify_library_content.filename_classify,
# training_planner._classify_protocol, training_planner._session_type_from_row),
# and two more read a PROTOCOL out of it: capacity_cap._RAMP_TEST_RE exempts
# "ftp_test_ramp" from power capping because a ramp is ridden to failure, and
# fitness_estimation.detect_ftp_test_shape treats "ftp_test_coggan" as an
# authoritative Coggan-20 tag. Shorten one of these and the file stops being
# the thing it is: a maximal test gets clamped to the rider's envelope, or
# stops scoring an FTP at all.
#
# Every multi-word entry here is one this rename already broke once —
# "supra_threshold" collapsed to "supra" (32 files reclassified to "mixed"),
# "ftp_test_coggan" and "vo2max_short" were dropped outright.
_PREFIXES = sorted({
    "ftp_test_ramp_20w_step", "ftp_test_ramp_10w_step", "ftp_test_ramp",
    "ftp_test_coggan", "ftp_test_cts", "ftp_test",
    "vo2max_short", "vo2_short", "vo2max", "sweet_spot",
    "sweetspot", "over_under", "endurance_intervals", "tempo_intervals",
    "threshold_ladder", "vo2_ladder", "neuromuscular", "anaerobic",
    "supra_threshold",
    "threshold", "endurance", "recovery", "tempo", "sprints", "sprint",
    "intervals", "climb", "z2", "ramp", "warmup", "mixed",
}, key=len, reverse=True)


# Markers that sit AFTER the class and still carry meaning. The tempo shape
# rule forbids a ramping tempo file unless the name declares it a progression,
# so dropping this marker turned 34 legitimate progressions into violations.
_INFIX_MARKERS = ("progression",)


def _existing_prefix(name: str) -> str:
    stem = name[:-4] if name.endswith(".zwo") else name
    for pre in _PREFIXES:
        # The whole-stem case is real: the library's original ramp test was
        # called exactly "ftp_test_ramp.zwo", with nothing after the marker,
        # so a prefix test that insists on a trailing "_" skips straight past
        # it and the file loses its exemption.
        if stem == pre or stem.startswith(pre + "_"):
            infix = [m for m in _INFIX_MARKERS if f"_{m}" in stem[len(pre):]]
            return "_".join([pre] + infix)
    return stem.split("_")[0]


def new_name(path: Path, cls: str) -> str:
    segs = sf.parse_zwo_file(path) or []
    total = sum(int(s.get("dur_s") or 0) for s in segs)
    mins = max(1, round(total / 60))
    struct, pct = structure_of(segs)
    parts = [cls or "workout", struct]
    if pct:
        parts.append(f"{pct}pct")
    parts.append(f"{mins}min")
    return "_".join(p for p in parts if p) + ".zwo"


def build_map(files: list[Path]) -> "dict[str, str]":
    mapping: "dict[str, str]" = {}
    taken: set = set()
    for p in sorted(files):
        # The EXISTING class prefix is preserved, not recomputed. Renaming by
        # content class is a reclassification, and it showed: files re-prefixed
        # into a class picked up that class's shape rules and broke 18 tests
        # that had nothing to do with naming. The complaint here was that names
        # do not say what a session IS — so add the structure and leave the
        # classification alone. Re-prefixing by true class is a separate job
        # with its own blast radius.
        cls = _existing_prefix(p.name)
        cand = new_name(p, cls)
        if cand in taken:
            # Same class, same structure, same duration: a genuine variant.
            # Number them rather than inventing a difference that is not there.
            stem, ext = cand[:-4], ".zwo"
            i = 2
            while f"{stem}_v{i}{ext}" in taken:
                i += 1
            cand = f"{stem}_v{i}{ext}"
        taken.add(cand)
        mapping[p.name] = cand
    return mapping


def rewrite_references(mapping: "dict[str, str]", apply: bool) -> int:
    """Rewrite every filename reference outside workouts/: caches, tests, the
    scripts that name files, and the rider's saved plans."""
    import os
    targets: list[Path] = [WORKOUTS / c for c in CACHES]
    targets += sorted((ROOT / "tests").rglob("*.py"))
    targets += sorted((ROOT / "scripts").rglob("*.py"))
    # ...and the JSON ledgers beside them. scripts/reclassify_sustained_labels
    # .json is a 66-entry {filename: class} manifest that three tests read as
    # their fixture; sweeping only *.py left every key pointing at a file that
    # no longer exists, and the classifier answered "endurance, confidence 0"
    # for all of them rather than saying the file was missing.
    targets += sorted((ROOT / "scripts").rglob("*.json"))
    targets += [ROOT / "app.py", ROOT / "training_planner.py"]
    home = Path(os.environ.get("DOMESTIQUE_HOME", Path.home() / ".domestique"))
    if home.exists():
        targets += [p for p in home.rglob("*.json")
                    if "rides" not in p.parts and "wellness" not in p.parts]
    pat = re.compile("|".join(re.escape(k) for k in sorted(
        mapping, key=len, reverse=True)))
    touched = 0
    for t in targets:
        if not t.exists() or t.name == Path(__file__).name:
            continue
        try:
            txt = t.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not pat.search(txt):
            continue
        out = pat.sub(lambda m: mapping[m.group(0)], txt)
        if out != txt:
            touched += 1
            if apply:
                t.write_text(out, encoding="utf-8")
    return touched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="verify no stale reference survives")
    args = ap.parse_args()

    files = sorted(WORKOUTS.glob("*.zwo"))
    mapping = {k: v for k, v in build_map(files).items() if k != v}
    print(f"{len(files)} workouts, {len(mapping)} would be renamed")

    if args.check:
        stale = [k for k in mapping]
        bad = 0
        for t in list((ROOT / "tests").rglob("*.py")) + [WORKOUTS / c for c in CACHES]:
            if not t.exists():
                continue
            txt = t.read_text(encoding="utf-8", errors="ignore")
            for old in stale:
                if old in txt:
                    print(f"  STALE {old} in {t.relative_to(ROOT)}")
                    bad += 1
        print("clean" if not bad else f"{bad} stale references")
        return 1 if bad else 0

    # What matters is whether the name tells the files apart, so measure the
    # name WITHOUT its _vN disambiguator: how many files still need a bare
    # number to be unique, and how big the worst such cluster is.
    def stem(n):
        return re.sub(r"_v\d+\.zwo$", ".zwo", n)
    b = Counter(stem(f.name) for f in files)
    a = Counter(stem(mapping.get(f.name, f.name)) for f in files)
    dup_b = sum(v - 1 for v in b.values() if v > 1)
    dup_a = sum(v - 1 for v in a.values() if v > 1)
    print(f"  files needing a _vN to be unique: {dup_b} -> {dup_a}")
    print(f"  worst identical-name cluster:     {b.most_common(1)[0][1]} -> "
          f"{a.most_common(1)[0][1]}")
    for old, new in list(mapping.items())[:10]:
        print(f"    {old:46s} -> {new}")

    touched = rewrite_references(mapping, args.apply)
    print(f"  files with references to rewrite: {touched}")

    if args.apply:
        for old, new in mapping.items():
            subprocess.run(["git", "mv", f"workouts/{old}", f"workouts/{new}"],
                           cwd=ROOT, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  renamed {len(mapping)} files")
    else:
        print("\ndry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
