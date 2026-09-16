#!/usr/bin/env python3
"""Does a rider get the week the science says they should?

Two numbers govern a planned week and they come from different places:

  * `phase.weekly_tss_target` — derived per ATHLETE from their CTL and
    available hours. This is what the dashboard shows and what the adherence
    counter measures against.
  * `BUDGETS[phase].tss_per_week` and its z1z2/z3/z4/z5plus minute rows — a
    FIXED table (`training_planner.py:1879`), authored for a ~10h/week rider,
    with the Seiler/Rønnestad distribution baked in as absolute minutes.

`sample_week_workouts` — the sampler that produces the sessions a rider
actually rides — verifies its output against the SECOND one
(`training_planner.py:6668`, `target_tss = budget.tss_per_week`). So a rider
whose phase target is 272 has their week checked against 600.

This probe prints, per athlete volume and phase: the athlete's target, what was
delivered, and the realized intensity distribution against the phase's own
polarized target. Run it before and after any budget change.

    tests/probe_budget_fidelity.py
"""
import os
import pathlib
import sys
from datetime import date, timedelta

if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

RECENT_WEEKLY_TSS = 300.0   # pinned; see the call to generate_plan below

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import training_planner as tp  # noqa: E402

_LIB = None


def _zone_minutes(session):
    """Zone minutes for a session, read from the MATCHED WORKOUT's own zone
    profile -- not from its session_type label.

    This distinction is the whole measurement. Seiler's 80/20 is a share of
    TIME IN ZONE, and a 60-minute VO2max session is not 60 minutes of Z5: it is
    a warm-up, some hard intervals, recoveries between them and a cool-down,
    and most of its minutes are Z1/Z2. Attributing the whole session to the band
    of its label overstates hard time by a factor of two or more, and the first
    version of this probe did exactly that -- it reported build weeks at "49%
    hard" that were nothing of the sort.

    Uses training_planner's own `_row_zone_minutes` so the probe and the
    sampler's budget-fit cannot disagree about what a workout contains.
    """
    global _LIB
    if _LIB is None:
        _LIB = {r.get("File"): r for r in tp.load_workout_library()}
    row = _LIB.get(getattr(session, "zwo_file", "") or "")
    if row is None:
        # Unmatched slot: fall back to the label, all-in-one-band. Counted so
        # the row below can say how much of the week is guesswork.
        d = float(session.duration_min or 0)
        b = tp._SESSION_TYPE_TO_BAND.get(session.session_type, "easy")
        key = ("z1z2" if b == "easy" else "z3" if b == "tempo_ss" else "z4")
        return {"z1z2": 0.0, "z3": 0.0, "z4": 0.0, "z5plus": 0.0} | {key: d}, True
    z = tp._row_zone_minutes(row)
    # The planner scales a matched file to the slot's duration; scale the zone
    # minutes the same way or a trimmed session is counted at its file length.
    file_min = float(row.get("Duration(min)", 0) or 0)
    slot_min = float(session.duration_min or 0)
    if file_min > 0 and slot_min > 0 and abs(slot_min - file_min) > 1:
        k = slot_min / file_min
        z = {kk: vv * k for kk, vv in z.items()}
    return z, False


def _goal(hours_per_day, rest=(6,), goal_type="event", target=None):
    avail = [d for d in range(7) if d not in rest]
    return tp.Goal(
        goal_type=goal_type, rest_days=list(rest), available_days=avail,
        daily_max_hours={d: hours_per_day for d in avail},
        hours_per_week=hours_per_day * len(avail),
        max_weekday_hours=hours_per_day, max_weekend_hours=hours_per_day,
        target_date=target, event_name="budget probe",
        event_km=140.0, event_climb_m=1400.0,
    )


def _distribution(week):
    """Realized (easy, z3, z4plus) as PERCENT OF MINUTES IN ZONE."""
    acc = {"z1z2": 0.0, "z3": 0.0, "z4": 0.0, "z5plus": 0.0}
    unmatched = 0.0
    for s in week.sessions:
        if s.session_type == "rest":
            continue
        z, miss = _zone_minutes(s)
        for k in acc:
            acc[k] += z.get(k, 0.0)
        if miss:
            unmatched += float(s.duration_min or 0)
    tot = sum(acc.values()) or 1.0
    # Three-zone fold: z4 (91-105% FTP) is the MIDDLE, not the hard pole.
    return ({"easy": 100 * acc["z1z2"] / tot,
             "z3": 100 * (acc["z3"] + acc["z4"]) / tot,
             "hard": 100 * acc["z5plus"] / tot},
            tot, 100 * unmatched / tot)


def main():
    today = date.today()
    target_date = today + timedelta(weeks=14)
    print("\n  Each row: one week of a real generate_plan, by athlete daily hours.")
    print("  tss    = week target vs delivered (and the miss)")
    print("  easy%  = share of MINUTES easy / z3 / z4+, against the phase's")
    print("           own PHASE_POLARIZED_TARGETS row\n")
    hdr = (f"  {'hours/day':<10}{'phase':<9}{'table':>7}{'target':>8}{'got':>7}"
           f"{'miss':>8}   {'easy/z3/hard delivered':<26}{'target':<18}{'hrs':>5}{'unmat':>6}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    worst = []
    for hours in (0.75, 1.0, 1.5, 2.0, 3.0):
        goal = _goal(hours, target=target_date)
        try:
            # recent_weekly_tss pinned: generate_plan SELF-FETCHES it from the
            # local rides store when it is None, which would make this probe
            # read a different number on every machine and drift as the user
            # rides. Pinned, the ACWR ceiling (Gabbett: recent x 1.3) is what
            # bounds the targets, which is the production path for anyone with
            # ride history.
            phases, weeks = tp.generate_plan(goal, seed_salt=1, current_ctl=45.0,
                                             recent_weekly_tss=RECENT_WEEKLY_TSS)
        except Exception as e:
            print(f"  {hours:<10}ERROR {type(e).__name__}: {e}")
            continue
        seen = set()
        for w in weeks:
            # FULL weeks only. The opening week of a plan generated mid-week is
            # short by design, and its target is prorated -- scoring it against
            # a seven-day phase shape reports a 56% "miss" that is nothing but
            # the week being four days long. The first version of this probe
            # did that and the low-volume rows were meaningless.
            if (w.end - w.start).days < 6:
                continue
            if w.phase in seen or w.is_stepback or w.phase in ("taper",):
                continue
            seen.add(w.phase)
            b = tp.get_budget_for_phase(w.phase)
            _mins = tp.week_available_minutes(goal, w.start)
            _model = tp.active_model_for_phase(w.phase)
            _hits = tp.hit_slots_for_volume(_mins, b.hit_count_max)
            _pt = tp.tid_target_pct(_model, w.phase, _mins, _hits)
            got = sum(float(s.tss_estimate or 0) for s in w.sessions)
            tgt = float(w.tss_target or 0)
            miss = (got - tgt) / tgt * 100 if tgt else 0.0
            dist, mins, unm = _distribution(w)
            pt = {k: int(round(v)) for k, v in _pt.items()}
            print(f"  {hours:<10}{w.phase:<9}{b.tss_per_week:>7}{tgt:>8.0f}{got:>7.0f}"
                  f"{miss:>+7.0f}%   "
                  f"{dist['easy']:>5.0f}/{dist['z3']:>4.0f}/{dist['hard']:>4.0f}          "
                  f"{pt['z1_pct']:>3}/{pt['z2_pct']:>3}/{pt['z3_pct']:>3}     "
                  f"{mins/60:>4.1f}{unm:>6.0f}%")
            worst.append((abs(miss), hours, w.phase, miss,
                          dist['easy'] - pt['z1_pct'],
                          dist['hard'] - pt['z3_pct']))
    import statistics
    tss_miss = [abs(w[3]) for w in worst]
    easy_gap = [w[4] for w in worst]
    hard_gap = [w[5] for w in worst]
    print(f"\n  mean |TSS miss|      {statistics.mean(tss_miss):>5.1f}%"
          f"   (worst {max(tss_miss):.0f}%)")
    print(f"  mean easy-share gap  {statistics.mean(easy_gap):>+5.1f} pts"
          f"   (a plan easier than its phase asks for is positive)")
    print(f"  mean hard-share gap  {statistics.mean(hard_gap):>+5.1f} pts")
    print(f"  weeks >10 pts off easy target: "
          f"{sum(1 for g in easy_gap if abs(g) > 10)} of {len(easy_gap)}")
    print(f"  weeks >8 pts SHORT of hard target: "
          f"{sum(1 for g in hard_gap if g < -8)} of {len(hard_gap)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
