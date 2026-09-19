#!/usr/bin/env python3
"""Release gate: a FIT workout exported by the FROZEN app, decoded by an
independent decoder (fitparse, not fit_tool), must carry the workout's real
step lengths.

v3.11.7 -- every build since fit-tool 0.9.16 (2026-08-05) wrote each step
1000x too long: the code scaled seconds to milliseconds itself and 0.9.16
scales again. The dev interpreter had 0.9.15, the build environments 0.9.16,
and no gate looked at what the bundle actually wrote.

    check_fit_durations.py <file.fit> <expected_seconds>
"""
import sys

import fitparse


def main() -> int:
    path, expected = sys.argv[1], float(sys.argv[2])
    steps = list(fitparse.FitFile(path).get_messages("workout_step"))
    total = sum((s.get_value("duration_time") or 0) for s in steps)
    ok = bool(steps) and abs(total - expected) <= max(60.0, expected * 0.02)
    print(f"FIT export: {len(steps)} steps, {total:,.0f} s decoded, "
          f"{expected:,.0f} s expected -> {'OK' if ok else 'WRONG'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
