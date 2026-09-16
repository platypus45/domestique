"""Does routing week construction through TrainingWeek produce better plans?

A/B over the same riders: `_USE_TRAINING_WEEK` off (the build-then-repair
chain) vs on (constraints consulted at commit time). Both are audited with
plan_invariants, which is the only thing here entitled to say "better".

It also arms the seal in audit mode, so every post-pass that edits a session
after its week committed it is logged by name. That list is the remaining
work: each entry is a pass that has to become a constraint.

Run:  .venv/bin/python tests/probe_owner_vs_legacy.py [n_riders]
"""
import logging
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import training_planner as tp        # noqa: E402
import week_plan as wp               # noqa: E402
import plan_invariants as pi         # noqa: E402
from probe_entry_point_parity import riders   # noqa: E402


def sweep(n, use_owner, rng_seed=20260910):
    rng = random.Random(rng_seed)
    tp._USE_TRAINING_WEEK = use_owner
    tally, ratios, raised = {}, [], 0
    for goal, salt, ctl in riders(n, rng):
        try:
            _ph, weeks = tp.generate_plan(goal, seed_salt=salt, current_ctl=ctl)
        except Exception:                                     # noqa: BLE001
            raised += 1
            continue
        for v in pi.audit(weeks, goal):
            tally[v.rule] = tally.get(v.rule, 0) + 1
        for w in weeks:
            if w.tss_target > 0:
                ratios.append(sum(s.tss_estimate for s in w.sessions) / w.tss_target)
    return tally, ratios, raised


def main(n=40):
    wp._install_seal(tp.PlannedSession)
    # Silence the per-write warning; the tally in wp.SEAL_TRIPS is the output.
    logging.getLogger("week_plan").setLevel(logging.ERROR)

    import statistics
    rows = []
    for label, flag in (("legacy chain", False), ("TrainingWeek", True)):
        tally, ratios, raised = sweep(n, flag)
        rows.append((label, tally, ratios, raised))

    rules = sorted({r for _l, t, _r, _x in rows for r in t})
    print(f"\n{n} riders, generate_plan only\n")
    print(f"{'':<22}" + "".join(f"{r:>22}" for r in rules) + f"{'raised':>9}")
    for label, tally, _ratios, raised in rows:
        print(f"{label:<22}" + "".join(f"{tally.get(r, 0):>22}" for r in rules) + f"{raised:>9}")

    print(f"\n{'':<22}{'weeks':>8}{'median':>10}{'p90':>10}{'max':>10}   prescribed TSS / target")
    for label, _t, ratios, _x in rows:
        if not ratios:
            continue
        s = sorted(ratios)
        p90 = s[int(0.9 * (len(s) - 1))]
        print(f"{label:<22}{len(s):>8}{statistics.median(s):>10.2f}{p90:>10.2f}{max(s):>10.2f}")

    tp._USE_TRAINING_WEEK = False
    if wp.SEAL_TRIPS:
        print("\npost-passes that edited a session AFTER its week sealed it:")
        for fn, n_hits in sorted(wp.SEAL_TRIPS.items(), key=lambda kv: -kv[1]):
            print(f"   {n_hits:>7}  {fn}")
        print("\n   each of these is a pass that still has to become a constraint")
    else:
        print("\nno post-pass tripped the seal")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
