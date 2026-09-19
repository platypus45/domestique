#!/usr/bin/env python3
"""Does the same plan come out the same twice?

The characterization harness only works because it pins PYTHONHASHSEED=0.
Without that pin, 43 of its 57 cases differed run to run -- which means the
app's own /api/plan/regenerate is not reproducible either: two identical
regenerations hand the rider different workouts.

This probe asks the question directly, in the ordinary interpreter with no
pinning, by running the planner in TWO SEPARATE CHILD PROCESSES and diffing
their output. Same process would prove nothing: str hashing is randomised per
process, not per call.

    tests/probe_plan_reproducibility.py            2 children, report the diff
    tests/probe_plan_reproducibility.py --runs 5   more children
"""
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

CHILD = r'''
import json, os, sys
root = os.environ["PROBE_ROOT"]
sys.path.insert(0, root + "/src")
sys.path.insert(0, root + "/tests")
import characterize_planner as cp
print("@@@" + json.dumps(cp.collect(), sort_keys=True))
'''


def run_child(root: str) -> dict:
    env = dict(os.environ)
    # The point of the probe: no pinning, and a different hash seed each time,
    # which is what a real uvicorn worker gets.
    env.pop("PYTHONHASHSEED", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PROBE_ROOT"] = root
    out = subprocess.run([sys.executable, "-c", CHILD],
                         capture_output=True, text=True, env=env, timeout=900)
    for line in out.stdout.splitlines():
        if line.startswith("@@@"):
            return json.loads(line[3:])
    sys.exit(f"child produced no result:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")


def main() -> int:
    runs = 2
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])
    root = str(HERE.parent)

    results = [run_child(root) for _ in range(runs)]
    base = results[0]
    unstable = set()
    for other in results[1:]:
        unstable |= {k for k in base if base[k] != other.get(k)}

    total = len(base)
    print(f"\n  {runs} separate processes, no PYTHONHASHSEED pin")
    print(f"  {len(unstable)} of {total} cases differ between runs")
    for k in sorted(unstable)[:10]:
        print(f"    ── {k}")
    if len(unstable) > 10:
        print(f"    ... and {len(unstable) - 10} more")
    if not unstable:
        print("  Reproducible: two regenerations give the rider the same plan.\n")
        return 0
    print("\n  NOT reproducible: regenerating twice gives different workouts.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
