#!/usr/bin/env python3
"""Generate the library's derived caches, for a packaged build.

    python3 tools/build-library-caches.py [--check]

`workouts/.library_index.json` (3.4 MB) and `workouts/.workout_facts.json`
(1.1 MB) are derived from the .zwo files and rebuild themselves on demand, so
they are not tracked. A packaged build cannot rebuild them at runtime -- the
bundle is read-only -- and without them the classifier falls back and the HIT
pools empty, which the build scripts already refuse to ship. So the build
makes them here, from the same code the app uses, and then gates on them.

`--check` reports what exists without building, for a build script that wants
to fail early with its own message.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORKOUTS = SRC / "workouts"
DERIVED = (".library_index.json", ".workout_facts.json")


def _report() -> int:
    missing = [n for n in DERIVED if not (WORKOUTS / n).is_file()]
    for n in DERIVED:
        p = WORKOUTS / n
        print(f"  {n:28s} {'%9d bytes' % p.stat().st_size if p.is_file() else 'MISSING'}")
    return 1 if missing else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report presence and exit non-zero if any is missing")
    args = ap.parse_args()
    if args.check:
        return _report()

    sys.path.insert(0, str(SRC))
    import training_planner as tp        # noqa: E402
    import workout_facts                 # noqa: E402

    n_zwo = len(list(WORKOUTS.glob("*.zwo")))
    if n_zwo < 4000:
        print(f"FATAL: {n_zwo} .zwo files in {WORKOUTS} (<4000)", file=sys.stderr)
        return 2

    t0 = time.time()
    # Both caches are written as a side effect of the load that needs them,
    # which is the point: the build uses the app's own code path rather than a
    # second implementation that could drift from it.
    for name in DERIVED:
        (WORKOUTS / name).unlink(missing_ok=True)
    tp._WORKOUT_LIB_CACHE.clear()
    tp._WORKOUT_LIB_FAST_VALIDATOR.clear()
    rows = tp.load_workout_library()
    facts = workout_facts.load_facts(WORKOUTS)
    print(f"built {len(rows)} library rows and {len(facts)} fact rows "
          f"from {n_zwo} .zwo files in {time.time() - t0:.1f}s")
    return _report()


if __name__ == "__main__":
    sys.exit(main())
