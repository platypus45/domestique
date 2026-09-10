"""A plan is a function of its goal, even while another plan is being built.

The distribution model and the microintervals-only preference used to live in
module globals, set per request from whichever goal was being planned. The
server runs requests on a thread pool, so a generate and a concurrent
auto-recalc built -- and saved -- plans under each other's model
(notes/review/state.md STA-1: 54 of 54 budget lookups foreign).

Two plans for two models are built on two threads at once and compared with the
same goals planned one after the other. On the code with the globals, every
trial produced a plan its goal did not determine.
"""
import threading
from datetime import date, timedelta

import training_planner as tp

MODELS = ("polarized", "threshold")


def _goal(model):
    return tp.Goal(goal_type="event", distribution=model, event_type="granfondo",
                   event_km=160, event_climb_m=2000,
                   target_date=date.today() + timedelta(weeks=14), hours_per_week=10.0,
                   max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=14)


def _build(model):
    _phases, weeks = tp.generate_plan(_goal(model), seed_salt=17, current_ctl=50.0,
                                      recent_weekly_tss=400.0,
                                      athlete={"ftp": 240, "weight_kg": 72})
    return [(s.day, s.session_type, s.duration_min, s.tss_estimate, s.zwo_file)
            for w in weeks for s in w.sessions]


def test_a_concurrent_plan_is_the_plan_its_goal_describes():
    serial = {m: _build(m) for m in MODELS}
    assert serial["polarized"] != serial["threshold"], "the two models must differ"
    for _trial in range(2):
        got = {}
        threads = [threading.Thread(target=lambda m=m: got.__setitem__(m, _build(m)))
                   for m in MODELS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        moved = [m for m in MODELS if got[m] != serial[m]]
        assert not moved, f"built alongside another plan, {moved} came out different"
