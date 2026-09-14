"""Hermetic environment and entry-point drivers for the planner gates.

Import this BEFORE training_planner. It makes a planner run a function of its
inputs and nothing else:

  * DOMESTIQUE_HOME points at a fresh empty directory, so no run reads the
    athlete's data. The old golden held only on one machine: match_zwo seeds
    from the ICU athlete id, which came from the real profile's .env
    (notes/review/gates.md GATE-2).
  * DOMESTIQUE_NO_NET=1.
  * "today" is frozen inside the planner and moved explicitly with at(). The
    old golden changed on the next calendar day with no code change -- 16
    cases, every one of them an end-to-end generate.

The drivers put each entry point into its REAL body. The old parity probe fed
recalculate / extend / refit a plan built that same moment, for continuous
riders only, so all three returned their input and three columns of its table
were generate's plan audited again (GATE-1). characterize_planner.py and
probe_entry_point_parity.py share these drivers, so the two gates cannot drift
apart in how they call the planner.

The re-exec with PYTHONHASHSEED=0 is the caller's job: it has to happen before
any import.
"""
import copy
import dataclasses
import datetime as _dt
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
os.environ["DOMESTIQUE_HOME"] = tempfile.mkdtemp(prefix="domestique-gate-")
os.environ["DOMESTIQUE_NO_NET"] = "1"
sys.path.insert(0, str(HERE.parent / "src"))

import config  # noqa: E402

config.ICU_ATHLETE_ID = "gate-athlete"      # pins match_zwo's seed input

import training_planner as tp  # noqa: E402
import plan_invariants as pi  # noqa: E402

ANCHOR = _dt.date(2026, 9, 14)      # a Monday, fixed forever
SEED = 20260914
ATHLETE = {"ftp": 240, "weight_kg": 72}
_TODAY = [ANCHOR]
import clock as _clock  # noqa: E402
_clock.freeze(ANCHOR)


class _FrozenDate(_dt.date):
    @classmethod
    def today(cls):
        d = _TODAY[0]
        return cls(d.year, d.month, d.day)


tp.date = _FrozenDate


def at(d):
    _TODAY[0] = d
    # The product reads src/clock.py alone since 04da2976; tp.date above is
    # kept for the planner's own type annotations and the tests that read it.
    import clock
    clock.freeze(d)


def today():
    return _TODAY[0]


def owner_modes():
    """While both week builders exist, every gate runs each of them."""
    return (0, 1) if hasattr(tp, "_USE_TRAINING_WEEK") else (0,)


def set_owner(mode):
    if hasattr(tp, "_USE_TRAINING_WEEK"):
        tp._USE_TRAINING_WEEK = bool(mode)


def _day(n):
    return ANCHOR + _dt.timedelta(days=n)


# ── athletes ─────────────────────────────────────────────────────────────

def continuous(rest=(5, 6), hours=2.0, focus="both", mode="auto"):
    avail = [d for d in range(7) if d not in rest]
    return tp.Goal(goal_type="continuous", rest_days=list(rest), available_days=avail,
                   daily_max_hours={d: hours for d in avail}, max_weekday_hours=hours,
                   max_weekend_hours=hours, focus=focus, plan_mode=mode)


def event(weekend_h=5.0, weeks=14, mode="auto", goal_type="event", target_ctl=None):
    """A granfondo with NO per-day dict: the weekday/weekend defaults are what
    cap it, which is the path the old matrix never exercised."""
    return tp.Goal(goal_type=goal_type, event_type="granfondo", event_km=200,
                   event_climb_m=3500, target_date=ANCHOR + _dt.timedelta(weeks=weeks),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=weekend_h,
                   available_days=list(range(7)), rest_days=[0], daily_max_hours={},
                   plan_weeks=weeks, plan_mode=mode, target_ctl=target_ctl)


def rides(first, last, tss=80.0):
    """One ride a day in [first, last], carrying power time-in-zone as well as
    TSS. The old fixture carried TSS only, which left the zone half of the
    ridden-load subtraction invisible (dupes.md DUP-2)."""
    tiz = {"z1": 1800, "z2": 1800, "z3": 300, "z4": 420, "z5": 240, "z6": 0, "z7": 0}
    out, d = [], first
    while d <= last:
        out.append({"date": d.isoformat(), "tss": tss, "time_in_zone": dict(tiz)})
        d += _dt.timedelta(days=1)
    return out


def _week_of(weeks, d):
    return next((w for w in weeks if w.start <= d <= w.end), None)


def own_some(weeks, t):
    """What the athlete already owns in the week containing t: a session
    ridden, one dismissed, one dragged to another day. Regeneration has to
    plan around all three and rewrite none of them."""
    w = _week_of(weeks, t)
    if w is None:
        return
    past = [s for s in w.sessions if s.session_type != "rest" and s.day < t]
    ahead = [s for s in w.sessions if s.session_type != "rest" and s.day >= t]
    if past:
        past[0].status = "done"
        past[0].completion_matches = [{"activity_id": "gate-1", "match_score": 1.0,
                                       "tss": float(past[0].tss_estimate or 0)}]
    if len(past) > 1:
        past[1].status = "dismissed"
        past[1].dismissed_at = f"{t.isoformat()}T08:00:00"
    if ahead:
        ahead[-1].user_moved = True
        ahead[-1].moved_from = (ahead[-1].day - _dt.timedelta(days=1)).isoformat()


def miss_a_hard_day(weeks, t):
    """A hard session before t in its week, marked missed: what triggers refit."""
    w = _week_of(weeks, t)
    past = [s for s in (w.sessions if w else []) if s.session_type != "rest" and s.day < t]
    if not past:
        return
    hard = next((s for s in past if s.session_type in
                 ("vo2max", "threshold", "sweetspot", "overunder", "sprint")), None)
    if hard is None:
        hard = past[-1]
        hard.session_type = "vo2max"
    hard.status = "missed"


# ── riders and drivers ──────────────────────────────────────────────────

class Rider:
    def __init__(self, name, make_goal, drivers, salt=SEED, ctl=45.0, rwt=320.0):
        self.name, self.make_goal, self.drivers = name, make_goal, tuple(drivers)
        self.salt, self.ctl, self.rwt = salt, ctl, rwt

    def goal(self):
        return self.make_goal()          # fresh each call: entry points annotate goals

    def base(self):
        at(ANCHOR)
        return tp.generate_plan(self.goal(), seed_salt=self.salt, current_ctl=self.ctl,
                                recent_weekly_tss=self.rwt, athlete=ATHLETE)[1]


def _generate(r, base):
    return copy.deepcopy(base), None, ANCHOR


def _generate_thursday(r, base):
    t, rs = _day(3), rides(ANCHOR, _day(2))
    at(t)
    return tp.generate_plan(r.goal(), seed_salt=r.salt, current_ctl=r.ctl,
                            recent_weekly_tss=r.rwt, athlete=ATHLETE,
                            activities=rs)[1], rs, t


def _regenerate(days):
    def drive(r, base):
        t = _day(days)
        wk = copy.deepcopy(base)
        own_some(wk, t)
        rs = rides(max(ANCHOR, t - _dt.timedelta(days=10)), t - _dt.timedelta(days=1))
        at(t)
        return tp.regenerate_from_today(r.goal(), wk, r.ctl, activities=rs,
                                        seed_salt=r.salt, athlete=ATHLETE,
                                        recent_weekly_tss=r.rwt)[1], rs, t
    return drive


def _extend(days):
    def drive(r, base):
        t = _day(days)
        rs = rides(t - _dt.timedelta(days=7), t - _dt.timedelta(days=1))
        at(t)
        return tp.extend_continuous_plan(r.goal(), copy.deepcopy(base), r.ctl,
                                         recent_activities=rs, athlete=ATHLETE,
                                         recent_weekly_tss=r.rwt,
                                         seed_salt=r.salt)[1], rs, t
    return drive


def _recalculate(days):
    def drive(r, base):
        t = _day(days)
        rs = rides(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1))
        at(t)
        return tp.recalculate_plan(r.goal(), copy.deepcopy(base), r.ctl,
                                   recent_activities=rs, athlete=ATHLETE,
                                   recent_weekly_tss=r.rwt)[1], rs, t
    return drive


def _refit(days):
    def drive(r, base):
        t = _day(days)
        wk = copy.deepcopy(base)
        miss_a_hard_day(wk, t)
        at(t)
        return tp.refit_remaining_week(r.goal(), wk, t, seed_salt=r.salt,
                                       athlete=ATHLETE)[0], None, t
    return drive


def _reforecast(days, tsb=None, avail=False):
    """tsb: a Training Stress Balance held for the past two weeks, which is
    what reaches the TSB downshift loop -- the one that rewrote sessions the
    athlete had dragged or dismissed (dupes.md DUP-23).
    avail: the rider zeroes tomorrow and frees 3 h the day after, which is
    what reaches the availability block -- the pass that inflated every
    production plan until it was capped (9138110d). Without a reading or an
    override the entry point changes nothing, and the self-test said so for
    every reforecast@21 case (the audit's test lens, 2026-09-14)."""
    def drive(r, base):
        t = _day(days)
        rs = rides(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1))
        series = (None if tsb is None else
                  {t - _dt.timedelta(days=i): float(tsb) for i in range(15)})
        overrides = None
        if avail:
            overrides = {(t + _dt.timedelta(days=1)).isoformat(): 0.0,
                         (t + _dt.timedelta(days=2)).isoformat(): 3.0}
        wk = copy.deepcopy(base)
        if tsb is not None:
            own_some(wk, t)
        at(t)
        return tp.reforecast(r.goal(), wk, tsb_series=series, recent_activities=rs,
                             availability_overrides=overrides)[0], rs, t
    return drive


def _stored(weeks, goal):
    """The plan as the app keeps it on disk: the goal block under the names the
    generate endpoint writes, and every session field (the regenerate and
    auto-recalc writer). Built by field name, not through the planner's codec:
    a gate that reads the plan through the code it judges cannot see that code
    lose something."""
    g = goal
    names = [f.name for f in dataclasses.fields(tp.PlannedSession)]

    def session(s):
        return {**{k: getattr(s, k) for k in names}, "day": s.day.isoformat()}
    return {
        "goal": {
            "type": g.goal_type,
            "event_date": g.target_date.isoformat() if g.target_date else None,
            "event_name": g.event_name, "event_km": g.event_km,
            "event_climb": g.event_climb_m, "event_type": g.event_type,
            "hours_per_week": g.hours_per_week, "max_weekday_hours": g.max_weekday_hours,
            "max_weekend_hours": g.max_weekend_hours, "rest_days": list(g.rest_days),
            "available_days": list(g.available_days),
            "daily_max_hours": {str(k): float(v) for k, v in g.daily_max_hours.items()},
            "plan_weeks": g.plan_weeks, "distribution": g.distribution,
            "block_periodization": g.block_periodization,
            "vo2_microintervals_only": g.vo2_microintervals_only, "events": [],
            "plan_mode": g.plan_mode, "template_id": g.template_id,
            "custom_bands": dict(g.custom_bands), "start_date": None,
            "entry_mode": None, "phase_weeks": None, "focus": g.focus,
        },
        "weeks": [{"week_num": w.week_num, "start": w.start.isoformat(),
                   "end": w.end.isoformat(), "phase": w.phase, "tss_target": w.tss_target,
                   "is_stepback": w.is_stepback,
                   "sessions": [session(s) for s in w.sessions]} for w in weeks],
    }


def _read_stored(plan):
    """The stored plan as objects again, for the fingerprint and the auditor:
    by field name, for the same reason."""
    sf = {f.name for f in dataclasses.fields(tp.PlannedSession)}
    wf = {f.name for f in dataclasses.fields(tp.PlannedWeek)}
    weeks = []
    for w in plan["weeks"]:
        sessions = [tp.PlannedSession(**{**{k: v for k, v in s.items() if k in sf},
                                         "day": _dt.date.fromisoformat(s["day"])})
                    for s in w["sessions"]]
        weeks.append(tp.PlannedWeek(**{**{k: v for k, v in w.items() if k in wf},
                                       "start": _dt.date.fromisoformat(w["start"]),
                                       "end": _dt.date.fromisoformat(w["end"]),
                                       "sessions": sessions}))
    return weeks


def _reforecast_dict(days, tsb, syncs=3):
    """What the app runs on every ride sync: reforecast the STORED plan and
    write it back -- here three syncs in a row under a projected TSB of -40.
    The object driver above goes through neither the plan's reader, which
    dropped `adapted` so the same session was downgraded again on every sync
    (dupes.md DUP-22), nor its goal block, which lost the rider's hours
    (DUP-27)."""
    def drive(r, base):
        t = _day(days)
        wk = copy.deepcopy(base)
        own_some(wk, t)
        plan = _stored(wk, r.goal())
        rs = rides(t - _dt.timedelta(days=14), t - _dt.timedelta(days=1))
        series = {t + _dt.timedelta(days=i): float(tsb) for i in range(7 * 20)}
        at(t)
        for _ in range(syncs):
            tp.reforecast_dict(plan, tsb_series=series, recent_activities=rs)
        return _read_stored(plan), rs, t
    return drive


# Adapters answer about one day or one week and return a projection, not a
# plan. They are fingerprinted as data, and not audited as plans.

def _daily_adapt(days):
    def drive(r, base):
        t = _day(days)
        w = copy.deepcopy(_week_of(base, t))
        at(t)
        wk, info = tp.daily_adapt_plan(w, rides(t - _dt.timedelta(days=3), t - _dt.timedelta(days=1)),
                                       today=t, tsb=-35.0)
        return {"projection": {"week": fingerprint([wk]), "info": info}}
    return drive


def _adjust_today(days):
    """Soreness 7: the G5 gate. An 'ordinary' readiness never reaches it, which
    is why its mutation survived every gate (gates.md U02)."""
    def drive(r, base):
        t = _day(days)
        s = next((x for x in _week_of(base, t).sessions
                  if x.day >= t and x.session_type not in ("rest", "recovery")), None)
        if s is None:
            return {"projection": None}
        at(t)
        out, reason = tp.adjust_today_session(copy.deepcopy(s), {"score": 55},
                                              daily_log_today={"soreness": 7})
        return {"projection": {"session": [out.session_type, int(out.duration_min or 0),
                                           round(float(out.tss_estimate or 0), 1),
                                           out.zwo_file or ""], "reason": reason}}
    return drive


def _rematch(days):
    def drive(r, base):
        t = _day(days)
        at(t)
        return {"projection": tp.rematch_week(copy.deepcopy(_week_of(base, t)),
                                              rides(ANCHOR, t - _dt.timedelta(days=1)), today=t)}
    return drive


DRIVERS = {
    "generate": _generate,
    "generate-thu": _generate_thursday,
    "regenerate@3": _regenerate(3),
    "regenerate@17": _regenerate(17),
    # Thirty days on, the appended window reaches week 8 -- a stepback -- so a
    # fault in extend's stepback handling has something to change (gates.md E06).
    "extend@9": _extend(9),
    "extend@30": _extend(30),
    "recalculate@21": _recalculate(21),
    "refit@3": _refit(3),
    # Thursday of week 4: the current week is itself a stepback (gates.md E08).
    "refit@24": _refit(24),
    "reforecast@21": _reforecast(21, avail=True),
    "reforecast-tsb@3": _reforecast(3, tsb=-40),
    "reforecast-dict@3": _reforecast_dict(3, tsb=-40),
    "daily-adapt@3": _daily_adapt(3),
    "adjust-today@3": _adjust_today(3),
    "rematch@3": _rematch(3),
}
# Drivers that edit an existing plan: returning the base unchanged means the
# case did not exercise its entry point.
EDITORS = frozenset(DRIVERS) - {"generate", "generate-thu", "daily-adapt@3",
                                "adjust-today@3", "rematch@3"}
ADAPTERS = ("reforecast-tsb@3", "reforecast-dict@3", "daily-adapt@3", "adjust-today@3",
            "rematch@3")

CONTINUOUS_DRIVERS = ("generate", "generate-thu", "regenerate@3", "extend@9", "refit@3")
EVENT_DRIVERS = ("generate", "generate-thu", "regenerate@17", "recalculate@21",
                 "refit@3", "reforecast@21")


def characterization_riders():
    out = []
    shapes = [("mon-fri", (5, 6)), ("weekend-rider", (0,)), ("every-day", ()),
              ("three-day", (2, 4, 5, 6))]
    extra = {("mon-fri", 1.0): ("extend@30", "refit@24"),
             ("every-day", 3.0): ("extend@30", "refit@24"),
             ("mon-fri", 3.0): ADAPTERS}
    for sname, rest in shapes:
        for hours in (1.0, 3.0):
            out.append(Rider(f"cont/{sname}/{hours:g}h",
                             lambda r=rest, h=hours: continuous(r, h),
                             CONTINUOUS_DRIVERS + extra.get((sname, hours), ())))
    for focus in ("ftp", "vo2"):
        out.append(Rider(f"cont/focus-{focus}", lambda f=focus: continuous((5, 6), 2.0, focus=f),
                         ("generate", "regenerate@3", "refit@3")))
    for wk in (5.0, 3.0):
        out.append(Rider(f"event/wkend{wk:g}h", lambda w=wk: event(w),
                         EVENT_DRIVERS + (ADAPTERS if wk == 5.0 else ()),
                         ctl=50.0, rwt=380.0))
    for mode in ("fixed_core", "template"):
        out.append(Rider(f"blueprint/{mode}", lambda m=mode: event(4.0, weeks=10, mode=m),
                         ("generate", "regenerate@17", "refit@3"), ctl=50.0, rwt=380.0))
    out.append(Rider("ctl/target60",
                     lambda: event(4.0, weeks=12, goal_type="ctl", target_ctl=60.0),
                     ("generate", "regenerate@17"), ctl=45.0, rwt=320.0))
    return out


# ── what a gate records ─────────────────────────────────────────────────

def fingerprint(weeks):
    """Each week's dates, phase, targets and stepback flag; each session's day,
    type, minutes, TSS, SERVED FILE and status. The served file is the point:
    without it, switching off match_zwo's content checks reported 0 changed
    (gates.md GATE-3)."""
    out = []
    for w in weeks:
        net = getattr(w, "net_tss_target", None)
        out.append([str(w.start), str(w.end), str(getattr(w, "phase", "")),
                    round(float(w.tss_target or 0), 1),
                    None if net is None else round(float(net), 1),
                    bool(w.is_stepback),
                    [[str(s.day), s.session_type, int(s.duration_min or 0),
                      round(float(s.tss_estimate or 0), 1), s.zwo_file or "",
                      getattr(s, "status", "pending")] for s in w.sessions]])
    return out


def findings(weeks, goal, rides_=None, today_=None):
    return sorted(f"{v.rule}:{v.week_num}"
                  for v in pi.audit(weeks, goal, rides=rides_, today=today_))
