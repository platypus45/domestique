"""v2.0.7 — auto-refit the remaining week when a HARD session is missed.

Covers tp.refit_remaining_week (the core) + the app-layer missed-hard tier in
_apply_plan_update (latch / idempotency). Per IP_missed_hard_refit.md §Tests.

IMPORTANT: the planner sampler is NON-DETERMINISTIC even with fixed seeds
(set/dict ordering — see the planner-test-nondeterminism memory). These tests
assert INVARIANTS, never exact workout picks:
  * remaining-trainable-day COUNT is unchanged
  * no week ever exceeds get_budget_for_phase(phase).hit_count_max hard days
  * no two hard days fall within 48h (the sampler's spacing guard)
  * past / done / pinned sessions are byte-identical after a refit
  * a missed EASY session leaves the week identical
  * idempotent: same absence twice = no second change (latch)
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import training_planner as tp


# ── builders ──────────────────────────────────────────────────────────────────

def _this_monday(ref: date | None = None) -> date:
    """Monday of the week containing ``ref`` (default: today), so the built
    week is the CURRENT week and ``today`` lands inside it."""
    d = ref or date.today()
    return d - timedelta(days=d.weekday())


def _session(day: date, *, stype: str, dur: int, tss: float,
             status: str = "pending", zwo: str = "",
             **extra) -> tp.PlannedSession:
    return tp.PlannedSession(
        day=day, day_name=day.strftime("%A"), session_type=stype,
        duration_min=dur, tss_estimate=tss, description="",
        zwo_file=zwo, status=status, **extra,
    )


def _week(start: date, sessions: list[tp.PlannedSession], *,
          phase: str = "build1", week_num: int = 2) -> tp.PlannedWeek:
    return tp.PlannedWeek(
        week_num=week_num, start=start, end=start + timedelta(days=6),
        phase=phase, tss_target=600.0, is_stepback=False, sessions=sessions,
        hit_per_week=tp.get_budget_for_phase(phase).hit_count_max,
    )


def _goal() -> tp.Goal:
    # Mon rest; train Tue..Sun. Generous daily caps so the sampler has room.
    return tp.Goal(
        goal_type="general", hours_per_week=10.0,
        max_weekday_hours=2.0, max_weekend_hours=3.5,
        rest_days=[0], available_days=[1, 2, 3, 4, 5, 6],
        daily_max_hours={0: 0.0, 1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5, 5: 3.0, 6: 3.0},
    )


def _hard_days(week: tp.PlannedWeek) -> list[int]:
    """Weekday offsets (0=Mon) of HIT sessions in the week."""
    return [(s.day - week.start).days for s in week.sessions if tp._session_is_hit(s)]


def _eff_hard(s) -> bool:
    """Mirror of the in-refit helper: a MISSED hard imposed no load, so it is
    NOT effective-hard for spacing/cap purposes; DONE + PENDING hards are."""
    return tp._session_is_hit(s) and getattr(s, "status", "pending") != "missed"


def _eff_hard_days(week: tp.PlannedWeek) -> list[int]:
    """Weekday offsets of EFFECTIVE-hard sessions (missed excluded)."""
    return sorted((s.day - week.start).days
                  for s in week.sessions if _eff_hard(s))


def _eff_hard_count(week: tp.PlannedWeek) -> int:
    return sum(1 for s in week.sessions if _eff_hard(s))


def _full_week(monday: date, *, missed_offset: int | None,
               missed_type: str = "vo2max") -> list[tp.PlannedSession]:
    """A canonical Tue/Thu hard + endurance week. If missed_offset is set, that
    day is marked status='missed' with the given hard/easy type."""
    out = []
    for off in range(7):
        d = monday + timedelta(days=off)
        if off == 0:
            out.append(_session(d, stype="rest", dur=0, tss=0))
        elif off in (1, 3):  # Tue / Thu = hard
            out.append(_session(d, stype="vo2max", dur=60, tss=75,
                                zwo="vo2.zwo"))
        else:
            out.append(_session(d, stype="z2", dur=90, tss=68, zwo="z2.zwo"))
    if missed_offset is not None:
        d = monday + timedelta(days=missed_offset)
        out[missed_offset] = _session(d, stype=missed_type, dur=60, tss=75,
                                      status="missed", zwo="missed.zwo")
    return out


# ── core: tp.refit_remaining_week ───────────────────────────────────────────

class TestMissedHardMidWeekRefits(unittest.TestCase):
    """Miss Tue's VO2 → remaining days re-fit; spacing + HIT cap NOT violated,
    remaining-day count unchanged, a hard type still present in the week."""

    def test_refit_respects_guards(self):
        # Pin 'today' to Wednesday so Tue is past+missed and Thu..Sun remain.
        monday = _this_monday()
        today = monday + timedelta(days=2)  # Wed
        sessions = _full_week(monday, missed_offset=1, missed_type="vo2max")
        week = _week(monday, sessions)
        before_remaining = [s for s in week.sessions
                            if s.day >= today and s.session_type != "rest"]
        n_before = len(before_remaining)

        weeks, info = tp.refit_remaining_week(_goal(), [week], today, seed_salt=7)

        cur = weeks[0]
        # Remaining-trainable-day count unchanged (we never add/remove days).
        n_after = len([s for s in cur.sessions
                       if s.day >= today and s.session_type != "rest"])
        self.assertEqual(n_after, n_before)
        # HIT cap invariant.
        cap = tp.get_budget_for_phase(cur.phase).hit_count_max
        self.assertLessEqual(tp._week_hit_count(cur), cap)
        # 48h spacing invariant across EFFECTIVE-hard days (the v2.0.7
        # contract this file's own _eff_hard mirrors: a MISSED hard imposed
        # no load, so it does not constrain spacing — refit may legally
        # place a new hard adjacent to the missed day). The old blanket
        # _hard_days assertion included the zero-load missed day and only
        # held by draw luck; the post-3.2.2 availability tightening shifted
        # the draw and exposed it.
        hd = sorted((s.day - cur.start).days
                    for s in cur.sessions if _eff_hard(s))
        for a, b in zip(hd, hd[1:]):
            self.assertGreaterEqual(b - a, 2,
                                    f"effective-hard days {a} and {b} within 48h")
        # A hard session still exists somewhere this week (stimulus not dropped
        # entirely when there's room).
        self.assertGreaterEqual(tp._week_hit_count(cur), 1)
        self.assertEqual(info["action"], "refitted")


class TestCapsFullNoSpike(unittest.TestCase):
    """No catch-up spike: the weekly cap is enforced on EFFECTIVE-hard days
    (v2.0.7 Fix 2 — a missed hard imposed no load, so it does not consume a cap
    slot; ``_week_hit_count`` still counts the zero-load missed day, so the
    invariant is asserted on the effective-hard count)."""

    def test_no_extra_hard_day_when_full(self):
        monday = _this_monday()
        today = monday + timedelta(days=1)  # Tue — Tue..Sun remain
        # Build a week with a missed hard (Mon, past) plus two pending hards.
        # base phase: hit_count_max == 1.
        sessions = []
        for off in range(7):
            d = monday + timedelta(days=off)
            if off == 0:
                # Past day, missed hard.
                sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                         status="missed", zwo="m.zwo"))
            elif off == 3:  # Thu hard already present
                sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                         zwo="vo2.zwo"))
            elif off in (5,):  # Sat hard already present
                sessions.append(_session(d, stype="threshold", dur=75, tss=110,
                                         zwo="thr.zwo"))
            else:
                sessions.append(_session(d, stype="z2", dur=90, tss=68,
                                         zwo="z2.zwo"))
        week = _week(monday, sessions, phase="base")  # cap = 1
        cap = tp.get_budget_for_phase("base").hit_count_max

        weeks, _info = tp.refit_remaining_week(_goal(), [week], today, seed_salt=3)

        # Effective-hard (load-imposing) days must not exceed the cap.
        self.assertLessEqual(_eff_hard_count(weeks[0]), cap)


class TestCrossBoundary48hSpacing(unittest.TestCase):
    """The sampler only spaces its OWN picks. After splicing them next to a
    FROZEN hard day (a missed or user-pinned hard), no two hard days in the week
    may be <48h apart — across many seeds (sampler is non-deterministic)."""

    def test_no_hard_pair_within_48h_with_frozen_hard(self):
        for seed in range(25):
            monday = _this_monday()
            today = monday + timedelta(days=3)  # Thu
            sessions = []
            for off in range(7):
                d = monday + timedelta(days=off)
                if off == 0:
                    sessions.append(_session(d, stype="rest", dur=0, tss=0))
                elif off == 2:  # Wed = missed hard (past, frozen)
                    sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                             status="missed", zwo="m.zwo"))
                elif off == 4:  # Fri = user-pinned hard (frozen)
                    sessions.append(_session(d, stype="threshold", dur=70,
                                             tss=105, zwo="pinned.zwo",
                                             user_moved=True))
                else:
                    sessions.append(_session(d, stype="z2", dur=90, tss=68,
                                             zwo="z2.zwo"))
            week = _week(monday, sessions, phase="build1")
            weeks, _info = tp.refit_remaining_week(
                _goal(), [week], today, seed_salt=seed)
            hd = sorted(_hard_days(weeks[0]))
            for a, b in zip(hd, hd[1:]):
                self.assertGreaterEqual(
                    b - a, 2, f"seed {seed}: hard days {a},{b} within 48h")
            # Frozen days never demoted.
            self.assertEqual(weeks[0].sessions[4].zwo_file, "pinned.zwo")
            self.assertTrue(weeks[0].sessions[4].user_moved)
            self.assertEqual(weeks[0].sessions[2].status, "missed")


class TestMissedEasyNoChange(unittest.TestCase):
    """A missed EASY session must not trigger any refit (hard-only)."""

    def test_missed_easy_is_no_op(self):
        monday = _this_monday()
        today = monday + timedelta(days=2)  # Wed
        sessions = _full_week(monday, missed_offset=2, missed_type="z2")  # Wed easy
        # Wed is 'today' & past-ish; make the missed easy be Tue instead.
        sessions = _full_week(monday, missed_offset=None)
        tue = monday + timedelta(days=1)
        sessions[1] = _session(tue, stype="z2", dur=90, tss=68,
                               status="missed", zwo="easy_missed.zwo")
        week = _week(monday, sessions)
        snapshot = [(s.day, s.session_type, s.duration_min, s.zwo_file)
                    for s in week.sessions]

        weeks, info = tp.refit_remaining_week(_goal(), [week], today, seed_salt=9)

        after = [(s.day, s.session_type, s.duration_min, s.zwo_file)
                 for s in weeks[0].sessions]
        self.assertEqual(after, snapshot)
        self.assertEqual(info["action"], "no_change")


class TestFrozenDaysUntouched(unittest.TestCase):
    """Past / done / user-pinned sessions are byte-identical after a refit."""

    def test_past_done_pinned_preserved(self):
        monday = _this_monday()
        today = monday + timedelta(days=3)  # Thu — Mon..Wed are past
        sessions = _full_week(monday, missed_offset=1, missed_type="vo2max")
        # Wed = completed (done) endurance; Fri = user-pinned hard.
        wed = monday + timedelta(days=2)
        fri = monday + timedelta(days=4)
        sessions[2] = _session(wed, stype="z2", dur=80, tss=60,
                               status="done", zwo="done.zwo")
        sessions[4] = _session(fri, stype="threshold", dur=70, tss=105,
                               zwo="pinned.zwo", user_moved=True)
        week = _week(monday, sessions)

        def snap(s):
            return (s.day, s.session_type, s.duration_min, s.tss_estimate,
                    s.zwo_file, s.status, s.user_moved)
        wed_before = snap(week.sessions[2])
        fri_before = snap(week.sessions[4])
        tue_before = snap(week.sessions[1])  # past missed — also frozen
        mon_before = snap(week.sessions[0])

        weeks, _info = tp.refit_remaining_week(_goal(), [week], today, seed_salt=5)
        cur = weeks[0]

        self.assertEqual(snap(cur.sessions[2]), wed_before)  # done untouched
        self.assertEqual(snap(cur.sessions[4]), fri_before)  # pinned untouched
        self.assertEqual(snap(cur.sessions[1]), tue_before)  # past untouched
        self.assertEqual(snap(cur.sessions[0]), mon_before)  # rest untouched


class TestAntiChurnDeterministic(unittest.TestCase):
    """Same inputs + same seed = identical result, and unchanged remaining days
    keep their original zwo_file (anti-churn)."""

    def test_idempotent_same_seed_and_unchanged_keep_file(self):
        monday = _this_monday()
        today = monday + timedelta(days=2)  # Wed

        def build():
            return _week(monday, _full_week(monday, missed_offset=1))

        w1, _ = tp.refit_remaining_week(_goal(), [build()], today, seed_salt=11)
        w2, _ = tp.refit_remaining_week(_goal(), [build()], today, seed_salt=11)
        sig = lambda wk: [(s.session_type, s.duration_min, s.zwo_file)
                          for s in wk[0].sessions]
        self.assertEqual(sig(w1), sig(w2))

        # Any remaining day that came back with the SAME type+duration as the
        # original must keep a non-empty matched file (never blanked by churn).
        orig = build()
        out, _ = tp.refit_remaining_week(_goal(), [orig := _week(
            monday, _full_week(monday, missed_offset=1))], today, seed_salt=11)
        for off in range(7):
            s = out[0].sessions[off]
            if s.session_type in ("rest", "recovery"):
                continue
            self.assertTrue(s.zwo_file, f"day {off} lost its workout file")


# ── v2.0.7 HIGH-severity fixes: effective-hard spacing + redistribution ──────

# These assert INVARIANTS over MANY PYTHONHASHSEED values (the sampler is
# non-deterministic — see the module docstring). Each loop iteration is a fresh
# week build + refit at a different seed_salt, which also varies dict/set order.

class TestFrozenPairLaterViolationRepaired(unittest.TestCase):
    """Fix 1 — two ADJACENT FROZEN DONE hards are a pre-existing user state the
    refit cannot demote (frozen↔frozen, legitimately <48h). The OLD code's
    next()+break short-circuited on that pair and left a LATER frozen↔REMAINING
    violation unrepaired. The fix SCANS past the frozen pair, so after refit NO
    REPAIRABLE pair remains: every adjacent eff-hard pair with a remaining
    member is ≥48h apart (the frozen↔frozen pair is the only allowed exception).
    """

    def _remaining_offsets(self, week, today):
        return {(s.day - week.start).days for s in week.sessions
                if s.day >= today
                and getattr(s, "session_type", "") != "rest"
                and not tp._refit_session_frozen(s, today)}

    def test_repairable_eff_hard_pair_never_within_48h(self):
        for seed in range(60):
            monday = _this_monday()
            today = monday + timedelta(days=3)  # Thu — Mon..Wed frozen/past
            sessions = []
            for off in range(7):
                d = monday + timedelta(days=off)
                if off in (1, 2):
                    # Tue+Wed: two adjacent FROZEN DONE hards (<48h, untouchable
                    # — the blocker that used to short-circuit the whole pass).
                    sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                             status="done", zwo="done_hard.zwo"))
                else:
                    sessions.append(_session(d, stype="z2", dur=90, tss=68,
                                             zwo="z2.zwo"))
            week = _week(monday, sessions, phase="build1")
            weeks, _info = tp.refit_remaining_week(
                _goal(), [week], today, seed_salt=seed)
            cur = weeks[0]
            # The frozen DONE pair is left untouched (sacrosanct).
            self.assertEqual(cur.sessions[1].status, "done")
            self.assertEqual(cur.sessions[2].status, "done")
            self.assertEqual(cur.sessions[1].zwo_file, "done_hard.zwo")
            self.assertEqual(cur.sessions[2].zwo_file, "done_hard.zwo")
            # No REPAIRABLE eff-hard pair within 48h: any too-close pair must be
            # frozen↔frozen (neither member is a remaining trainable day).
            rem = self._remaining_offsets(cur, today)
            ehd = _eff_hard_days(cur)
            for a, b in zip(ehd, ehd[1:]):
                if b - a >= 2:
                    continue
                self.assertFalse(
                    (a in rem) or (b in rem),
                    f"seed {seed}: REPAIRABLE eff-hard pair {a},{b} left "
                    f"<48h (Fix 1 short-circuit). remaining={sorted(rem)}")


def _goal_roomy() -> tp.Goal:
    """Like _goal() but with generous daily caps so the cap (not the day-cap) is
    the binding constraint — needed to exercise full redistribution to the cap."""
    return tp.Goal(
        goal_type="general", hours_per_week=12.0,
        max_weekday_hours=3.0, max_weekend_hours=4.0,
        rest_days=[0], available_days=[1, 2, 3, 4, 5, 6],
        daily_max_hours={0: 0.0, 1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0, 5: 4.0, 6: 4.0},
    )


class TestMissedFreesSlotRedistributed(unittest.TestCase):
    """Fix 2b — a missed hard FREES its cap slot; the refit REDISTRIBUTES that
    stimulus onto a SAFE remaining day. Scenario: a frozen DONE hard (Tue) + a
    MISSED hard (Wed) with the rest of the week trainable and the per-phase cap
    above the surviving frozen-hard count. After refit the effective-hard count
    reaches the cap (the freed slot is re-owed, not dropped), eff_hard_count <=
    cap, and 48h spacing holds. Pre-fix this maxed out one short of the cap
    (missed stimulus dropped) — this asserts the redistribution actually fires.
    """

    def test_redistribution_reaches_cap(self):
        cap = tp.get_budget_for_phase("build1").hit_count_max
        reached_cap = 0
        for seed in range(60):
            monday = _this_monday()
            today = monday + timedelta(days=3)  # Thu — Thu..Sun remain
            sessions = []
            for off in range(7):
                d = monday + timedelta(days=off)
                if off == 1:        # Tue: frozen DONE hard (counts toward cap)
                    sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                             status="done", zwo="done.zwo"))
                elif off == 2:      # Wed: MISSED hard (frees its slot)
                    sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                             status="missed", zwo="m.zwo"))
                else:
                    sessions.append(_session(d, stype="z2", dur=120, tss=90,
                                             zwo="z2.zwo"))
            week = _week(monday, sessions, phase="build1")
            weeks, _info = tp.refit_remaining_week(
                _goal_roomy(), [week], today, seed_salt=seed)
            cur = weeks[0]
            # Cap invariant (missed excluded) + 48h invariant ALWAYS hold.
            self.assertLessEqual(_eff_hard_count(cur), cap)
            ehd = _eff_hard_days(cur)
            for a, b in zip(ehd, ehd[1:]):
                self.assertGreaterEqual(
                    b - a, 2, f"seed {seed}: eff-hard {a},{b} within 48h")
            # The missed Wed must stay missed; the frozen Tue stays done.
            self.assertEqual(cur.sessions[1].status, "done")
            self.assertEqual(cur.sessions[2].status, "missed")
            if _eff_hard_count(cur) >= cap:
                reached_cap += 1
        # Redistribution must drive the week back UP to the cap on (nearly) every
        # seed — the freed slot is re-owed onto a remaining day. A loose
        # threshold keeps this robust to the rare seed where no safe slot exists.
        self.assertGreaterEqual(
            reached_cap, 55,
            f"redistribution rarely reached the cap ({reached_cap}/60) — the "
            f"freed missed slot is being dropped, not re-owed (Fix 2b)")


class TestMissedAdjacentDoesNotBlock(unittest.TestCase):
    """Fix (missed excluded from spacing) — a missed hard ADJACENT (next day) to
    a remaining day must NOT block that remaining day from being hard (the missed
    day imposed no load), and must NOT force a spurious demotion of it."""

    def test_missed_adjacent_does_not_force_demotion(self):
        allowed_hard_adjacent = False
        for seed in range(60):
            monday = _this_monday()
            today = monday + timedelta(days=2)  # Wed — Wed..Sun remain
            # Tue (past) missed hard; Wed remaining. Wed is ADJACENT to the
            # missed Tue. Since missed imposes no load, Wed may be hard.
            sessions = []
            for off in range(7):
                d = monday + timedelta(days=off)
                if off == 0:
                    sessions.append(_session(d, stype="rest", dur=0, tss=0))
                elif off == 1:
                    sessions.append(_session(d, stype="vo2max", dur=60, tss=75,
                                             status="missed", zwo="m.zwo"))
                else:
                    sessions.append(_session(d, stype="z2", dur=90, tss=68,
                                             zwo="z2.zwo"))
            week = _week(monday, sessions, phase="build1")
            cap = tp.get_budget_for_phase("build1").hit_count_max
            weeks, _info = tp.refit_remaining_week(
                _goal(), [week], today, seed_salt=seed)
            cur = weeks[0]
            # Invariants: cap (missed excluded) + 48h between EFFECTIVE hards.
            self.assertLessEqual(_eff_hard_count(cur), cap)
            ehd = _eff_hard_days(cur)
            for a, b in zip(ehd, ehd[1:]):
                self.assertGreaterEqual(
                    b - a, 2, f"seed {seed}: eff-hard {a},{b} within 48h")
            # The missed day stays missed (never resurrected or demoted-over).
            self.assertEqual(cur.sessions[1].status, "missed")
            # Wed (off 2) is adjacent to missed Tue (off 1). If Wed is hard,
            # the missed day did NOT block it — that's the whole point.
            if _eff_hard(cur.sessions[2]):
                allowed_hard_adjacent = True
        self.assertTrue(
            allowed_hard_adjacent,
            "a remaining day adjacent to a MISSED hard was never allowed to be "
            "hard — missed day is wrongly blocking spacing")


# ── app-layer tier: latch / idempotency through _apply_plan_update ───────────

class TestAppTierLatch(unittest.TestCase):
    """The app tier refits once, sets missed_refit_latch, and is a no-op on the
    second identical adapt (no re-churn)."""

    def _plan_dict(self, monday: date) -> dict:
        import app
        sessions = _full_week(monday, missed_offset=1)
        wk = _week(monday, sessions)
        return {
            "goal": {"type": "general", "hours_per_week": 10.0,
                     "rest_days": [0], "available_days": [1, 2, 3, 4, 5, 6],
                     "max_weekday_hours": 2.0, "max_weekend_hours": 3.5},
            "weeks": [{
                "week_num": wk.week_num,
                "start": wk.start.isoformat(), "end": wk.end.isoformat(),
                "phase": wk.phase, "tss_target": wk.tss_target,
                "is_stepback": False,
                "sessions": [app._planned_session_to_json(s) for s in wk.sessions],
            }],
        }

    def test_refit_then_latched_noop(self):
        import app
        monday = _this_monday()
        today = monday + timedelta(days=2)  # Wed
        plan = self._plan_dict(monday)

        # First adapt: empty activities so reconcile is a no-op and our
        # pre-marked 'missed' status drives the tier. No gap (1 missed day in a
        # single week) so we don't divert into the rebuild tier.
        plan1, action1, _info1, _msg1 = app._apply_plan_update(
            plan, training={"ctl": 40, "tsb": 5}, activities=[], today=today,
            allow_regen=True, gap_debounce=True,
        )
        self.assertEqual(action1, "refitted")
        self.assertIn("missed_refit_latch", plan1)
        latch_key = plan1["missed_refit_latch"]["key"]

        # Second adapt on the now-latched plan + same absence → must NOT refit
        # again (falls through to reforecast/rebalance).
        plan2, action2, _info2, _msg2 = app._apply_plan_update(
            plan1, training={"ctl": 40, "tsb": 5}, activities=[], today=today,
            allow_regen=True, gap_debounce=True,
        )
        self.assertNotEqual(action2, "refitted")
        self.assertEqual(plan2["missed_refit_latch"]["key"], latch_key)


if __name__ == "__main__":
    unittest.main()
