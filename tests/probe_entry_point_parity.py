#!/usr/bin/env python3
"""Do the planner's entry points obey the same rules?

Builds plausible riders -- continuous AND event goals -- generates a base plan
for each, drives every entry point into its real body (tests/_gate_env.py, the
same drivers the characterization uses) and audits each result with
plan_invariants, given the rides and the "today" it was built with.

The previous version fed recalculate / extend / refit a plan built that same
moment, for continuous riders only, so all three returned their input: three
of its five columns were generate's plan audited again (notes/review/gates.md
GATE-1). This one also counts, per entry point, how many riders it actually
changed, and says so loudly when an entry point did nothing.

Not a unittest: it is a measurement.

Run:  .venv/bin/python tests/probe_entry_point_parity.py [n_riders] [--owner]
"""
import os
import pathlib
import random
import sys

if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _gate_env as env  # noqa: E402

tp = env.tp


def riders(n, rng):
    """Plausible athletes, not adversarial ones -- the point is whether the
    entry points disagree for ordinary riders, not whether they survive junk."""
    for i in range(n):
        n_avail = rng.randint(3, 7)
        avail = sorted(rng.sample(range(7), n_avail))
        rest = tuple(d for d in range(7) if d not in avail)
        salt, ctl = rng.randint(0, 9999), rng.choice([30.0, 45.0, 55.0, 70.0])
        if rng.random() < 0.6:
            h = rng.choice([1.0, 1.5, 2.0, 3.0])
            yield env.Rider(f"c{i}", lambda r=rest, h=h: env.continuous(r, h),
                            env.CONTINUOUS_DRIVERS, salt=salt, ctl=ctl,
                            rwt=rng.choice([200.0, 320.0, 450.0]))
        else:
            wk, weeks = rng.choice([2.5, 3.5, 5.0]), rng.choice([10, 14, 18])
            yield env.Rider(f"e{i}", lambda w=wk, n=weeks: env.event(w, weeks=n),
                            env.EVENT_DRIVERS, salt=salt, ctl=ctl,
                            rwt=rng.choice([250.0, 380.0, 500.0]))


def main(n=40):
    env.set_owner(1 if "--owner" in sys.argv else 0)
    rng = random.Random(20260910)
    tally, ran, noop, errors = {}, {}, {}, {}
    for r in riders(n, rng):
        try:
            base = r.base()
        except Exception as e:                                   # noqa: BLE001
            errors.setdefault("base", []).append(f"{type(e).__name__}: {e}")
            continue
        base_fp = env.fingerprint(base)
        for d in r.drivers:
            ran[d] = ran.get(d, 0) + 1
            try:
                weeks, rides, today = env.DRIVERS[d](r, base)
            except Exception as e:                               # noqa: BLE001
                errors.setdefault(d, []).append(f"{type(e).__name__}: {e}")
                continue
            if d in env.EDITORS and env.fingerprint(weeks) == base_fp:
                noop[d] = noop.get(d, 0) + 1
            for rule in {f.split(":")[0] for f in env.findings(weeks, r.goal(), rides, today)}:
                tally.setdefault(d, {})[rule] = tally.get(d, {}).get(rule, 0) + 1

    order = [d for d in env.DRIVERS if d in ran]
    rules = sorted({x for v in tally.values() for x in v})
    w = max((len(x) for x in rules + ["changed nothing"]), default=10) + 2
    mode = "owner ON" if "--owner" in sys.argv else "owner off"
    print(f"\n{n} riders, {mode} -- riders with at least one violation, per entry point\n")
    print(f"{'':<{w}}" + "".join(f"{d:>15}" for d in order))
    print(f"{'riders run':<{w}}" + "".join(f"{ran[d]:>15}" for d in order))
    print("-" * (w + 15 * len(order)))
    for x in rules:
        print(f"{x:<{w}}" + "".join(f"{tally.get(d, {}).get(x, 0):>15}" for d in order))
    if not rules:
        print("(no violations anywhere)")
    print("-" * (w + 15 * len(order)))
    print(f"{'changed nothing':<{w}}" + "".join(
        f"{(noop.get(d, 0) if d in env.EDITORS else '-'):>15}" for d in order))
    dead = [d for d in order if d in env.EDITORS and noop.get(d, 0) == ran[d]]
    if dead:
        print(f"\n!! {dead}: returned the base plan for EVERY rider -- the column "
              "above audits generate's plan, not this entry point")
    for d, errs in errors.items():
        print(f"\n{d} raised on {len(errs)} riders: {sorted(set(errs))[:3]}")
    return 1 if dead else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(int(args[0]) if args else 40))
