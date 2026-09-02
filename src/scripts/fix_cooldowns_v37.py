#!/usr/bin/env python3
"""v3.7.0 — make every trailing cooldown genuinely a cooldown.

The defect: 1647 of 3672 <Cooldown> segments START at a HIGHER power than the
segment before them ends, so the rider steps UP into the "cooldown". A rider
finishing a 6x2min VO2max session at 0.28 FTP was handed a ramp beginning at
0.75 FTP — 186 W at his FTP — and 32 cooldowns start at or above 0.85 FTP,
two of them above 1.00. A segment that raises your power is not a cooldown,
whatever the tag says.

THE RULE (two lines, monotone by construction):

    start = min(CD_START_MAX, prev_end)
    end   = min(CD_END,       start)

where ``prev_end`` is the end power of the last preceding segment that has a
target power (IntervalsT contributes OffPower; a trailing FreeRide is looked
THROUGH). Under that definition the pre-fix step-up count is 1647 — an earlier
figure of 1596 came from a different prev_end convention and is retired.

WHY THESE NUMBERS
  start 0.60 — blood-lactate clearance is fastest at 80-100% of the first
    lactate threshold and is no better than sitting still at 40% of it
    (Devlin 2014 PMID 24739289, Menzies 2010 PMID 20544484). LT1 sits near
    0.80 x FTP in trained cyclists, putting the clearance optimum around
    0.64 FTP. 0.60 sits just under that centre, which is the deliberate
    choice: vagal reactivation is essentially abolished at and above the
    first threshold, and individual LT1 scatters +/-10-15 points of FTP, so
    the cheaper error is to be slightly too easy. It costs almost nothing
    measurable — the whole segment stays inside the clearance band — and it
    keeps a rider whose LT1 is at the low end from doing tempo work at the
    end of a session that already emptied them.
  end 0.45 — 40% of LT1 (~0.32 FTP) is statistically indistinguishable from
    passive rest, so the whole segment stays above the "no better than
    stopping" floor.
  never above prev_end — definitional, and the only rule here with no
    research caveat attached.

DELIBERATELY NOT DONE
  * IntervalsT OffPower is never touched. Clearance-optimal intensity BETWEEN
    reps is counterproductive: at 80-110% of LT power between intervals,
    trained cyclists produced significantly less time above 90% of maximal
    minute power and rated the session harder (Fennell & Hopker 2021,
    PMID 33098020). This script only ever edits a trailing <Cooldown>.
  * No cooldown is lengthened, shortened, added or removed. Per-file total
    prescribed duration is unchanged for every file, so plan fitting and the
    workout matcher see no difference.

Raw-text single-attribute substitution (the capacity_cap.cap_zwo_text
pattern), NOT an ElementTree round-trip: round-tripping rewrites the bytes of
files it has no semantic reason to touch.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CD_START_MAX = 0.60
CD_END = 0.45

_CD_ATTRS = re.compile(
    r'(<Cooldown\b[^>]*?PowerLow=")([^"]*)("[^>]*?PowerHigh=")([^"]*)(")')
# "Cooldown: 5min from 65% to 45% FTP"  /  "Cooldown: 5min @ 45% FTP"
_CD_CLAUSE = re.compile(
    r'Cooldown:\s*(?P<dur>[^|<]*?)\s*(?:from\s*(?P<a>\d+)\s*%\s*to\s*'
    r'(?P<b>\d+)\s*%|@\s*(?P<flat>\d+)\s*%)\s*FTP')


def prev_end_power(elements, idx) -> "float | None":
    """End power of the last targeted segment before ``idx``.

    IntervalsT contributes OffPower — the rider's power as the cooldown
    begins is the recovery leg, not the effort. A trailing FreeRide carries
    no target and is looked through.
    """
    for k in range(idx - 1, -1, -1):
        tag = elements[k].tag.split("}")[-1]
        if tag == "FreeRide":
            # v3.11.3: a free ride right before the cooldown means the rider
            # was at whatever they could hold (the FTP tests: a maximal
            # effort), not at the easy segment further back — the power the
            # cooldown steps down FROM is unknown, so the direction rule
            # cannot apply. Walking past it to an earlier easy segment
            # falsely flagged every test file's cooldown as "stepping up".
            return None
        if tag == "SteadyState":
            v = elements[k].get("Power")
            return float(v) if v is not None else None
        if tag in ("Warmup", "Cooldown", "Ramp"):
            v = elements[k].get("PowerHigh")
            return float(v) if v is not None else None
        if tag == "IntervalsT":
            v = elements[k].get("OffPower")
            return float(v) if v is not None else None
    return None


def target(text: str) -> "tuple[float, float, float, float] | None":
    """(old_start, old_end, new_start, new_end) or None if nothing to do."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    workout = root.find(".//workout")
    if workout is None:
        return None
    elements = list(workout)
    cds = [i for i, e in enumerate(elements)
           if e.tag.split("}")[-1] == "Cooldown"]
    if not cds:
        return None
    i = cds[-1]
    lo, hi = elements[i].get("PowerLow"), elements[i].get("PowerHigh")
    if lo is None or hi is None:
        return None
    lo, hi = float(lo), float(hi)
    start = min(lo, CD_START_MAX)
    prev = prev_end_power(elements, i)
    if prev is not None:
        start = min(start, prev)
    # Collapses to a flat easy spin when the rider was already below the end
    # target — never a ramp UP from 0.28 to 0.45.
    end = min(hi, CD_END, start)
    if abs(start - lo) < 1e-9 and abs(end - hi) < 1e-9:
        return None
    return lo, hi, start, end


def rewrite(text: str, start: float, end: float) -> str:
    """Substitute the two attributes; leave every other byte alone."""
    def sub(m):
        return f"{m.group(1)}{start:.2f}{m.group(3)}{end:.2f}{m.group(5)}"
    # count=0 is safe: no library file has more than one Cooldown, and the
    # invariant test pins that.
    out = _CD_ATTRS.sub(sub, text)

    # The embedded <description> restates the structure in prose and is shown
    # in trainer apps and pushed to intervals.icu. A power edit without a
    # prose edit ships a user-visible lie.
    def clause(m):
        dur = m.group("dur").strip()
        if abs(start - end) < 0.02:
            return f"Cooldown: {dur} @ {round(end * 100)}% FTP"
        return (f"Cooldown: {dur} from {round(start * 100)}% "
                f"to {round(end * 100)}% FTP")
    return _CD_CLAUSE.sub(clause, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="workouts", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(args.dir.glob("*.zwo"))
    if not files:
        print(f"no .zwo under {args.dir}", file=sys.stderr)
        return 1

    changed = stepups = absurd = 0
    worst: list[tuple[float, str, float, float, float, float]] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        t = target(text)
        if t is None:
            continue
        lo, hi, start, end = t
        changed += 1
        if start < lo:
            stepups += 1
        if lo >= 0.85:
            absurd += 1
        worst.append((lo - start, path.name, lo, hi, start, end))
        if args.apply:
            path.write_text(rewrite(text, start, end), encoding="utf-8")
        if args.limit and changed >= args.limit:
            break

    worst.sort(reverse=True)
    verb = "rewrote" if args.apply else "would rewrite"
    print(f"{verb} {changed} of {len(files)} files "
          f"(start lowered on {stepups}; {absurd} started >= 0.85 FTP)")
    for drop, name, lo, hi, start, end in worst[:12]:
        print(f"  -{drop:.2f}  {name:52s} {lo:.2f}->{hi:.2f}  =>  "
              f"{start:.2f}->{end:.2f}")
    if not args.apply:
        print("\ndry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
