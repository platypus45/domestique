#!/usr/bin/env python3
"""Pin what the planner hands a rider, so a refactor can be judged.

WHY. Every step of the backend overhaul has to answer one question: did the
plan a rider receives change? Unit tests answer "is this function right"; this
answers "did anything move", which is what a refactor is judged on -- and,
once behaviour is meant to change, WHAT moved, so it can be read before it is
blessed.

HOW. Everything runs hermetically (tests/_gate_env.py: empty data home, no
network, frozen "today", pinned athlete id), over two matrices:

  builders  plan_week and generate_weekly_plan over availability x target x
            stepback x completed load x week number. Cheap; they pin the week
            skeleton and the home page's week.
  entries   every entry point driven into its real body -- generate (Monday,
            and Thursday with Mon-Wed already ridden), regenerate around owned
            sessions, extend with a horizon gap (and one reaching a stepback),
            recalculate on an event goal, refit after a missed hard day (and in
            a stepback week), reforecast with and without a TSB crash, the
            stored-plan reforecast over three syncs (the path every sync takes,
            through the plan's reader and goal block), and the day/week
            adapters -- for continuous, event, blueprint and CTL goals.
            Run once per value of _USE_TRAINING_WEEK while both week builders
            exist.

A fingerprint is each week's dates, phase, targets and stepback flag, and each
session's day, type, minutes, TSS, SERVED FILE and status. A change is reported
as LOAD (anything but the file moved) or CONTENT (only the served file moved):
a step that is meant to change what is served, and nothing else, can be checked
for exactly that. Each plan case also records plan_invariants' findings,
reported apart from the fingerprint: a case can be unchanged and still illegal,
and a change can make one legal.

    tests/characterize_planner.py              compare with the golden
    tests/characterize_planner.py --bless      record the current output
    tests/characterize_planner.py --self-test  prove the harness can see

--bless only after reading the diff: every blessed line is a change to what
somebody is told to ride.

--self-test perturbs each entry point's output in turn and requires that
entry's cases to move, plants internal faults and requires each to be seen,
and fails if any editing case returned its base plan untouched -- a case that
does not exercise its entry point is how three columns of the old parity table
turned into decoration (notes/review/gates.md GATE-1).
"""
import contextlib
import json
import os
import pathlib
import sys
from datetime import timedelta

# Re-exec with hash randomisation off before importing the planner, and never
# leave a .pyc behind: a stale one once served a MUTATED planner while the
# source on disk read correct. Only when RUN, never on import.
if __name__ == "__main__" and os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _gate_env as env  # noqa: E402  (hermetic setup happens on import)

tp = env.tp
GOLDEN = HERE / "characterization.json"
ANCHOR, SEED = env.ANCHOR, env.SEED
SESSION_TYPES = ["z2", "threshold", "vo2max", "sweetspot", "overunder", "tempo"]


def _goal(rest_days, hours, goal_type="continuous"):
    avail = [d for d in range(7) if d not in rest_days]
    return tp.Goal(goal_type=goal_type, rest_days=list(rest_days), available_days=avail,
                   daily_max_hours={d: hours for d in avail})


def _phase(target, name="continuous"):
    return tp.Phase(name=name, start=ANCHOR, end=ANCHOR + timedelta(weeks=6), weeks=6,
                    focus="", weekly_tss_target=target, z2_pct=78, hit_per_week=2,
                    session_types=list(SESSION_TYPES))


def builder_cases():
    out = []
    shapes = [("mon-fri", (5, 6)), ("weekend-rider", (0,)), ("every-day", ()),
              ("three-day", (2, 4, 5, 6))]
    for hours in (1.0, 2.0, 3.0):
        for sname, rest in shapes:
            for target in (150, 272, 450):
                out.append((f"week/{sname}/{hours:g}h/target{target}",
                            lambda r=rest, h=hours, t=target: tp.plan_week(
                                1, ANCHOR, _phase(t), _goal(r, h), False, seed_salt=SEED)))
    for sname, rest in shapes:
        out.append((f"stepback/{sname}", lambda r=rest: tp.plan_week(
            4, ANCHOR, _phase(272), _goal(r, 3.0), True, seed_salt=SEED)))
    for done in (0, 80, 272, 400):
        out.append((f"completed/{done}", lambda c=done: tp.plan_week(
            1, ANCHOR, _phase(272), _goal((5, 6), 3.0), False,
            seed_salt=SEED, completed_tss=c)))
    for wk in (1, 2, 3, 5, 8):
        out.append((f"weeknum/{wk}", lambda w=wk: tp.plan_week(
            w, ANCHOR, _phase(272), _goal((5, 6), 3.0), False, seed_salt=SEED)))
    for sname, rest in shapes:
        for target in (200, 350):
            out.append((f"weekly/{sname}/target{target}",
                        lambda r=rest, t=target: tp.generate_weekly_plan(
                            goal=_goal(r, 3.0), current_phase=_phase(t), current_ctl=40.0)))
    return out


def collect_builders():
    env.at(ANCHOR)
    got = {}
    for name, fn in builder_cases():
        try:
            got[name] = env.fingerprint([fn()])[0]
        except Exception as e:                                   # noqa: BLE001
            got[name] = {"error": f"{type(e).__name__}: {e}"}
    return got


def collect_entries(mode):
    """-> ({case: {"fp", "inv"} | {"projection"}}, [editing cases that changed nothing])."""
    env.set_owner(mode)
    got, noops = {}, []
    for rider in env.characterization_riders():
        try:
            base = rider.base()
        except Exception as e:                                   # noqa: BLE001
            for d in rider.drivers:
                got[f"{rider.name}/{d}"] = {"error": f"base: {type(e).__name__}: {e}"}
            continue
        base_fp = env.fingerprint(base)
        for dname in rider.drivers:
            key = f"{rider.name}/{dname}"
            try:
                out = env.DRIVERS[dname](rider, base)
                if isinstance(out, dict):
                    got[key] = {"projection": json.loads(json.dumps(
                        out.get("projection"), default=str, sort_keys=True))}
                    continue
                weeks, rides, today = out
                fp = env.fingerprint(weeks)
                got[key] = {"fp": fp, "inv": env.findings(weeks, rider.goal(), rides, today)}
                if dname in env.EDITORS and fp == base_fp:
                    noops.append(key)
            except Exception as e:                               # noqa: BLE001
                got[key] = {"error": f"{type(e).__name__}: {e}"}
    env.set_owner(0)
    return got, noops


def collect():
    out = {"builders": collect_builders()}
    for m in env.owner_modes():
        out[f"owner{m}"] = collect_entries(m)[0]
    return out


# ── reporting ────────────────────────────────────────────────────────────

def _fp(v):
    if not isinstance(v, dict):
        return v
    return v.get("fp", v.get("projection", v))


def _load_only(fp):
    """The fingerprint without the served file: what LOAD looks like."""
    if not isinstance(fp, list) or not fp or not isinstance(fp[0], list):
        return fp
    return [w[:6] + [[s[:4] + s[5:] for s in w[6]]] for w in fp]


def _first_diff(a, b):
    """The first week and session that differ, as short lines."""
    if not (isinstance(a, list) and isinstance(b, list)
            and a and b and isinstance(a[0], list) and len(a[0]) > 6):
        return [f"{json.dumps(a, default=str)[:150]}", f"-> {json.dumps(b, default=str)[:150]}"]
    for i in range(max(len(a), len(b))):
        wa = a[i] if i < len(a) else None
        wb = b[i] if i < len(b) else None
        if wa == wb:
            continue
        if not (wa and wb):
            return [f"week {i}: {wa and wa[:6]} -> {wb and wb[:6]}"]
        lines = []
        if wa[:6] != wb[:6]:
            lines.append(f"week {i} head {wa[:6]} -> {wb[:6]}")
        for j in range(max(len(wa[6]), len(wb[6]))):
            sa = wa[6][j] if j < len(wa[6]) else None
            sb = wb[6][j] if j < len(wb[6]) else None
            if sa != sb:
                lines.append(f"week {i} {sa} -> {sb}")
                break
        return lines
    return ["(same weeks, different length)"]


def compare(want, got):
    rc = 0
    for section in sorted(set(want) | set(got)):
        w, g = want.get(section, {}), got.get(section, {})
        added, removed = sorted(set(g) - set(w)), sorted(set(w) - set(g))
        changed = sorted(k for k in set(w) & set(g) if _fp(w[k]) != _fp(g[k]))
        content_only = [k for k in changed
                        if _load_only(_fp(w[k])) == _load_only(_fp(g[k]))]
        inv_new, inv_gone = {}, {}
        if section != "builders":
            for k in set(w) & set(g):
                a = set(w[k].get("inv", [])) if isinstance(w[k], dict) else set()
                b = set(g[k].get("inv", [])) if isinstance(g[k], dict) else set()
                if b - a:
                    inv_new[k] = sorted(b - a)
                if a - b:
                    inv_gone[k] = sorted(a - b)
        if not (added or removed or changed or inv_new or inv_gone):
            print(f"  {section}: {len(g)} cases, all unchanged")
            continue
        # A new invariant hit fails the compare like a fingerprint change:
        # until 2026-09-14 it was only printed, and 93 hits were blessed
        # into the golden without the gate ever refusing one.
        if added or removed or changed or inv_new:
            rc = 1
        print(f"  {section}: {len(changed)} changed ({len(changed) - len(content_only)} load, "
              f"{len(content_only)} content only), {len(added)} added, "
              f"{len(removed)} removed (of {len(w)})")
        for k in changed[:15]:
            tag = "content" if k in content_only else "load"
            print(f"    ── {k}  [{tag}]")
            for line in _first_diff(_fp(w[k]), _fp(g[k])):
                print(f"         {line}")
        if len(changed) > 15:
            print(f"    ... and {len(changed) - 15} more")
        for k in added:
            print(f"    ++ {k}")
        for k in removed:
            print(f"    -- {k}")
        n_new = sum(len(v) for v in inv_new.values())
        n_gone = sum(len(v) for v in inv_gone.values())
        if n_new or n_gone:
            print(f"    invariants: {n_new} new, {n_gone} resolved")
            by_rule: dict = {}
            for v in inv_new.values():
                for f in v:
                    r = f.split(':', 1)[0]
                    by_rule[r] = by_rule.get(r, 0) + 1
            if by_rule:
                print('    new by rule: ' + ', '.join(f'{r} {n}' for r, n in sorted(by_rule.items())))
        for k, v in sorted(inv_new.items())[:10]:
            print(f"    !! {k}: new {v}")
        for k, v in sorted(inv_gone.items())[:10]:
            print(f"    ok {k}: resolved {v}")
    if rc:
        print("\n  If every change above is intended, re-run with --bless.")
    return rc


# ── self-test: the harness must see what it claims to see ──────────────

@contextlib.contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def _all(*cms):
    with contextlib.ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        yield


def _shorten_first_pending(out):
    items = out if isinstance(out, tuple) else (out,)
    plan = next((x for x in items if isinstance(x, dict) and "weeks" in x), None)
    if plan is not None:                        # reforecast_dict edits the stored plan
        for w in plan["weeks"]:
            for s in w.get("sessions", []):
                if (s.get("session_type") != "rest" and (s.get("duration_min") or 0) > 15
                        and s.get("status", "pending") == "pending"
                        and s.get("day", "") >= env.today().isoformat()):
                    s["duration_min"] -= 5
                    return out
        return out
    weeks = next((x for x in items if isinstance(x, list) and x
                  and hasattr(x[0], "sessions")), [])
    for w in weeks:
        for s in w.sessions:
            if (s.session_type != "rest" and (s.duration_min or 0) > 15
                    and getattr(s, "status", "pending") == "pending"
                    and s.day >= env.today()):
                s.duration_min -= 5
                return out
    return out


def _perturbing(fname):
    orig = getattr(tp, fname)

    def wrapper(*a, **k):
        return _shorten_first_pending(orig(*a, **k))
    return _patched(tp, fname, wrapper)


def self_test():
    fails = []
    clean, noops = collect_entries(0)
    if noops:
        fails.append(f"{len(noops)} editing cases changed nothing: {noops[:6]}")
    errors = sorted(k for k, v in clean.items() if "error" in v)
    if errors:
        fails.append(f"{len(errors)} cases raised: {errors[:4]}")

    def moved(ref, got):
        return {k for k in ref if got.get(k) != ref[k]}

    def of(*drivers):
        return {k for k in clean if k.rsplit("/", 1)[-1] in drivers}

    # 1. each entry point's output is actually observed. `inner`: drivers whose
    #    entry point calls this one, and so may move with it. A generate
    #    perturbation changes every base plan, so it moves everything (None).
    for fname, drivers, inner in (
            ("regenerate_from_today", ("regenerate@3", "regenerate@17"), ()),
            ("extend_continuous_plan", ("extend@9", "extend@30"), ()),
            ("recalculate_plan", ("recalculate@21",), ()),
            ("refit_remaining_week", ("refit@3", "refit@24"), ()),
            ("reforecast", ("reforecast@21", "reforecast-tsb@3"), ("reforecast-dict@3",)),
            ("reforecast_dict", ("reforecast-dict@3",), ()),
            ("generate_plan", ("generate", "generate-thu"), None)):
        with _perturbing(fname):
            m = moved(clean, collect_entries(0)[0])
        mine = of(*drivers)
        missed = mine - m
        stray = (m - mine - of(*inner)) if inner is not None else set()
        status = "ok" if not missed and not stray else "FAIL"
        print(f"  [{status}] perturb {fname:<24} {len(mine & m)}/{len(mine)} own cases moved"
              + (f", {len(stray)} other cases moved" if stray else ""))
        if missed:
            fails.append(f"{fname}: {len(missed)} own cases blind, e.g. {sorted(missed)[:3]}")
        if stray:
            fails.append(f"{fname}: moved cases of other entry points {sorted(stray)[:3]}")

    # 2. faults inside the planner are seen -- and, where a rule exists for
    #    the fault, the auditor names it
    z2 = dict(tp.TSS_PER_HOUR)
    z2["z2"] += 1
    zero_zones = {"z1z2": 0.0, "z3": 0.0, "z4": 0.0, "z5plus": 0.0}
    faults = [
        ("TSS_PER_HOUR z2 45->46", 0, _patched(tp, "TSS_PER_HOUR", z2), None),
        ("match_zwo ignores content", 0,
         _patched(tp, "file_admissible", lambda slot_type, row: True), None),
        ("ridden load ignored", 0,
         _all(_patched(tp, "_completed_tss_in", lambda *a, **k: 0.0),
              _patched(tp, "_completed_zones_in", lambda *a, **k: dict(zero_zones))), None),
    ]
    if hasattr(tp, "_apply_long_ride_target"):
        orig_lr = tp._apply_long_ride_target
        faults.append(("long ride ignores weekend cap", 0,
                       _patched(tp, "_apply_long_ride_target",
                                lambda sessions, target_min, max_weekend_min, is_stepback:
                                orig_lr(sessions, target_min, 10 ** 6, is_stepback)),
                       "daily_duration_cap"))
    if 1 in env.owner_modes():
        import week_plan
        faults.append(("owner 48h off-by-one", 1,
                       _patched(week_plan, "MIN_HARD_GAP_DAYS", 1), None))
    clean1 = collect_entries(1)[0] if 1 in env.owner_modes() else {}
    for label, mode, patch, rule in faults:
        ref = clean if mode == 0 else clean1
        with patch:
            got = collect_entries(mode)[0]
        n = len(moved(ref, got))
        named = ""
        if rule:
            hit = sum(1 for k in ref if isinstance(got.get(k), dict)
                      and any(f.startswith(rule + ":") for f in
                              set(got[k].get("inv", [])) - set(ref[k].get("inv", []))))
            named = f", {rule} newly fired in {hit} cases"
        print(f"  [{'ok' if n else 'FAIL'}] fault {label:<30} {n} cases moved{named}")
        if not n:
            fails.append(f"fault not seen: {label}")

    print(f"\n  self-test: {'PASS' if not fails else 'FAIL'}")
    for f in fails:
        print(f"    - {f}")
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    got = collect()
    if "--bless" in sys.argv or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  wrote {GOLDEN.name}: "
              + ", ".join(f"{s} {len(v)}" for s, v in sorted(got.items())))
        return 0
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if "builders" not in want:
        print("  golden predates the hermetic format; run --bless once on unchanged code")
        return 1
    return compare(want, got)


if __name__ == "__main__":
    sys.exit(main())
