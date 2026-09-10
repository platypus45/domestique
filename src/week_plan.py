"""One object owns what a week's training is.

Why this exists
---------------
The planner grew five entry points -- generate, regenerate, recalculate,
extend, refit -- and each assembled its own sequence of enforcement passes.
Measured on the real module, that was 12, 5, 6, 6 and 3 passes respectively,
so the rules a plan obeyed depended on which button the athlete pressed. A
40-rider sweep showed the consequence: back-to-back hard days appeared only in
the regenerate path, because the 48 h spacing pass was in generate's list and
not in regenerate's.

The deeper failure is repair-after-the-fact. A session was built by one of four
functions and then edited by up to six more; the median delivered session was
touched by three. Because each pass could silently overrule the previous, the
last writer won by accident of ordering. And a repair pass cannot fix a
decision made without the constraint: `_enforce_weekly_volume_ceiling` may
shrink easy rides but never hard ones, so when the sampler placed 175 TSS of
intensity into a 112 TSS week, deleting *every* easy minute still left the week
56% over its own ceiling. The pass ran, did its best, and the athlete got a
week at 2.04x target.

The model here
--------------
`TrainingWeek` is the only thing that writes to a `PlannedSession`.
Constraints are consulted *before* a slot is committed, never applied as edits
after. `plan()` decides a week from scratch; `replan(ridden=...)` decides it
again with the work the athlete has already done subtracted from the budget.

Committing goes through one function, `_commit`, so a rule added there applies
to every entry point at once -- which is the property the five hand-assembled
pass lists did not have.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

log = logging.getLogger(__name__)

# Types the planner treats as hard. Duplicated deliberately in
# plan_invariants: there the list is the auditor's, here it is the planner's,
# and a silent divergence between them should surface as a failing audit
# rather than be hidden by a shared import.
HARD_TYPES = frozenset({
    "vo2max", "threshold", "sweetspot", "overunder", "sprint",
    "anaerobic", "hill_repeats", "ftp_test", "race",
})

MIN_HARD_GAP_DAYS = 2          # 48 h between hard sessions (Gabbett 2016)

# Hard work may claim at most this share of the week's TSS ceiling. The rest
# has to remain for the aerobic base, which is the part that actually carries
# the adaptation in every three-zone model. Without a cap at decision time the
# sampler can fill a small week entirely with intensity and leave the volume
# pass an impossible job.
HARD_CEILING_SHARE = 0.60

# Shrinking a hard session preserves the interval stimulus; below this it stops
# being the session it claims to be and is demoted instead. Seiler's argument
# that the number and quality of hard sessions matters more than their length
# is what makes shrink-before-drop the right order.
MIN_HARD_MIN = 30
# Same reasoning as training_planner._EASE_FOR_RECOVERY_TYPE: easing for
# recovery must land somewhere the athlete can recover, not at tempo.
DEMOTE_TO = "z2"

# Below this an easy ride is not worth the athlete changing for; the slot
# becomes rest instead. Mirrors the old volume pass's _VOLUME_MIN_SESSION_MIN.
_MIN_EASY_MIN = 20

# Roughly what a _MIN_EASY_MIN ride costs, at an easy ~0.7 TSS/min. Held back
# for each slot still waiting to be committed, so an early session cannot spend
# the whole week. Without the reserve the priority order simply moved the
# starvation around: the long ride took everything left after the hard days and
# 26 later slots became rest, while the median week still delivered only 0.91 of
# its ceiling -- sessions cut AND budget unused, the worst of both.
_RESERVE_TSS_PER_SLOT = 14.0

# The floor on zone-1 share of a week's TIME, measured on the workouts actually
# served rather than on the slot labels. Every three-zone model in the
# literature -- polarized, pyramidal, threshold alike -- puts the clear
# majority of training time in zone 1; Rosenblat 2025 finds POL and PYR
# indistinguishable for VO2max precisely because both do. A week that drops
# below this has stopped being any of them.
MIN_EASY_SHARE = 0.55


# who -> number of writes to an already-sealed session. Populated whether or
# not STRICT_SEAL is on, so a run can be asked which passes still overrule the
# owner without having to fail on the first one.
SEAL_TRIPS: dict[str, int] = {}


def _offending_caller() -> str:
    """The first frame outside this module -- the pass doing the writing."""
    import sys as _sys
    f = _sys._getframe(2)
    while f is not None:
        if f.f_globals.get("__name__") != __name__:
            return f"{f.f_globals.get('__name__', '?')}.{f.f_code.co_name}"
        f = f.f_back
    return "?"


def pin(session, reason: str):
    """Declare that a plan-level policy has DECIDED this session's shape.

    The owner re-asserts its constraints after the plan-level policies run, so
    that none of them can win merely by running last. A few of them should win
    anyway, because they know something the constraint does not: the event
    long-ride progression grows the weekend ride toward the event's own
    duration, and the week's TSS ceiling has no idea the athlete has signed up
    for a 200 km granfondo. Left unpinned, the owner shrank that ride straight
    back to the budget and the specificity was lost.

    So such a policy says so explicitly. That is the difference between a
    decision and a pass that happened to run late -- and it stays visible,
    because the reason travels with the session.
    """
    object.__setattr__(session, "_policy_pin", reason)
    return session


def is_pinned(session) -> bool:
    return bool(getattr(session, "_policy_pin", ""))


def is_immutable(session) -> bool:
    """Sessions no planning pass may rewrite, for any reason.

    A race (FC3) has exactly one sanctioned write path -- _mark_race_days and
    the goal-level add/edit-race flow -- and is immutable to every clamp,
    rescale, demote and rematch in the planner. An athlete-owned session is the
    same contract for a different reason. Both still count against the week's
    budget and both still block a neighbouring hard day: planning AROUND them
    is the point.
    """
    import training_planner as tp
    if is_pinned(session):
        return True
    try:
        if tp._protect_race(session):
            return True
    except Exception:                                          # noqa: BLE001
        pass
    return is_athlete_owned(session)


def is_athlete_owned(session) -> bool:
    """Has the athlete taken ownership of this session?

    True once they have moved it, had it adapted for them, ridden it, missed
    it, or dismissed it. The planner may plan AROUND such a session -- it
    counts against the week's budget and blocks a neighbouring hard day -- but
    it must never rewrite one. Regenerating a plan and silently undoing the
    session the rider dragged to Thursday is the fastest way to make them stop
    trusting the planner.
    """
    if getattr(session, "user_moved", False) or getattr(session, "adapted", False):
        return True
    return getattr(session, "status", "pending") != "pending"


class SealedSessionError(RuntimeError):
    """Something edited a session after its week had committed it."""


# Strict mode turns the tripwire into an exception. Tests run strict; the app
# does not, because the UI legitimately edits sessions after planning (drag,
# swap-type, completion status) and those go through amend().
STRICT_SEAL = False


def _install_seal(session_cls) -> None:
    """Teach PlannedSession to refuse edits once its week has sealed it.

    Idempotent, and a no-op for any session never sealed -- which is every
    session built by code that has not migrated to TrainingWeek yet.
    """
    if getattr(session_cls, "_seal_installed", False):
        return
    original = session_cls.__setattr__

    def guarded(self, name, value):
        if getattr(self, "_sealed", False) and not name.startswith("_"):
            who = _offending_caller()
            msg = (f"sealed session {getattr(self, 'day', '?')} written by "
                   f"{who}: set {name!r}={value!r:.60s} outside amend() -- a "
                   f"later pass is overruling the planner")
            if STRICT_SEAL:
                raise SealedSessionError(msg)
            log.warning("%s", msg)
            SEAL_TRIPS[who] = SEAL_TRIPS.get(who, 0) + 1
        original(self, name, value)

    session_cls.__setattr__ = guarded
    session_cls._seal_installed = True


@dataclass
class PlanState:
    """Accumulators that belong to the whole plan, not to one week.

    Novelty bookkeeping, the library and its index, and the per-class pick
    counts are shared across weeks by design -- a workout used in week 2
    should not reappear in week 3. Grouping them here keeps them out of
    WeekContext, which is genuinely per-week.
    """
    library: list = field(default_factory=list)
    pool_index: dict | None = None
    used_names: dict = field(default_factory=dict)
    plan_pick_counts: dict = field(default_factory=dict)
    class_session_counts: dict = field(default_factory=dict)
    class_distinct_files: dict = field(default_factory=dict)
    seen_cc_dur_tuples: set = field(default_factory=set)
    recent_hit_by_phase: dict = field(default_factory=dict)
    plan_total_weeks: int = 0
    # Weeks after which a used workout name re-enters the "fresh" pool. Mirrors
    # training_planner._USED_NAMES_ROLLING_WEEKS; without the eviction the owner
    # kept every name used since week 1 and starved the sampler in long plans.
    used_names_window: int = 12


@dataclass
class WeekContext:
    """Everything needed to decide one week, gathered once.

    `ridden` is the whole point of the redesign: a week the athlete is already
    partway through is not a blank slate, and the object that decides the week
    is the one that should know it.
    """
    week_num: int
    start: date
    phase: object
    goal: object
    is_stepback: bool = False
    seed_salt: int = 0
    week_in_phase: int = 0
    prev_week_sessions: list = field(default_factory=list)
    ridden: list = field(default_factory=list)
    unavailable: object = None          # callable(date) -> bool, or None
    emphasis_profile: str | None = None
    block_focus: str | None = None
    # "auto" | "fixed_core" | "template" -- a blueprint mode has its week laid
    # out by expand_blueprint_week rather than sampled, and must not have a
    # block focus applied on top of the blueprint's own structure.
    plan_mode: str = "auto"
    event_targets: dict | None = None
    # Sessions from an existing plan that fall inside this week and belong to
    # the athlete rather than the planner -- ones they moved, swapped, already
    # rode, or dismissed. Re-planning may work around them; it may not rewrite
    # them.
    preserved: list = field(default_factory=list)
    # The caller's authoritative TSS ceiling for this week, when it has one.
    # generate_plan scales the phase target by ACWR and by the stepback factor
    # before the week is built; without this the object would re-read the raw
    # phase number and plan to a target 75% higher than the one the athlete's
    # recent load justifies.
    tss_ceiling: float | None = None

    @property
    def end(self) -> date:
        return self.start + timedelta(days=6)


class TrainingWeek:
    """The sole owner of a week's sessions.

    Typical use::

        tw = TrainingWeek(ctx, state)
        pw = tw.plan()                      # a fresh week
        pw = tw.replan(ridden=activities)   # the same week, minus work done

    Nothing outside this class writes to the sessions it produces. That is
    enforced rather than documented: `plan()` seals what it returns, and a
    later write raises under STRICT_SEAL and logs otherwise.
    """

    def __init__(self, ctx: WeekContext, state: PlanState | None = None):
        self.ctx = ctx
        self.state = state or PlanState()
        self.week = None                # the PlannedWeek, once planned
        self._committed: list = []      # sessions accepted so far, in day order
        self._tss = 0.0                 # total committed
        self._hard_tss = 0.0            # committed hard work only
        self._lib_index: dict = {}      # filename -> library row, built lazily
        self._pending_slots = 0         # trainable slots still awaiting commit

    # ── budget ───────────────────────────────────────────────────────────
    @property
    def ceiling(self) -> float:
        """The week's TSS ceiling, net of work already ridden inside it.

        Subtracting here rather than in each caller is what stops a Thursday
        regenerate from prescribing a fresh week on top of the 369 TSS the
        athlete had already put in that same calendar week.
        """
        import training_planner as tp
        if self.ctx.tss_ceiling is not None:
            gross = float(self.ctx.tss_ceiling)
        elif getattr(self.week, "tss_target", 0.0):
            # plan_week has already applied the stepback discount to this
            # number. Applying it again here took a 217 TSS unload week down to
            # 156 -- a 48% cut off the base week rather than Issurin's 28%,
            # which is deep enough to detrain.
            gross = float(self.week.tss_target)
        else:
            gross = float(getattr(self.ctx.phase, "weekly_tss_target", 0.0) or 0.0)
            if self.ctx.is_stepback:
                gross *= 0.72
        # From the Monday of the containing calendar week: an opening stub
        # week starts mid-week, and the rides before its cursor are part of the
        # load the athlete is carrying into it.
        done = tp._completed_tss_in(self.ctx.ridden,
                                    tp._monday_on_or_before(self.ctx.start),
                                    self.ctx.end)
        return max(0.0, gross - done)

    def _hard_days_committed(self) -> list[date]:
        prev = [s.day for s in (self.ctx.prev_week_sessions or [])
                if getattr(s, "session_type", "") in HARD_TYPES]
        return prev + [s.day for s in self._committed
                       if s.session_type in HARD_TYPES]

    # ── the single writer ────────────────────────────────────────────────
    def _commit(self, session):
        """Accept a session into the week, after every constraint has spoken.

        This is the only place a session's content is decided. Each clamp
        below used to be a separate post-pass that ran in some entry points
        and not others; here they run once, for everyone.
        """
        goal = self.ctx.goal

        # 0. The athlete's own sessions pass through untouched. They still
        #    count against the week's budget and still block a neighbouring
        #    hard day -- planning around them is the point -- but no rule here
        #    may rewrite one.
        if is_immutable(session):
            return self._accept(session)

        # 1. Days the athlete cannot ride carry nothing.
        if self.ctx.unavailable and self.ctx.unavailable(session.day):
            return self._accept(self._as_rest(session, "Rest (unavailable)"))
        wd = session.day.weekday()
        if wd in (goal.rest_days or []) or (
                goal.available_days and wd not in goal.available_days):
            return self._accept(self._as_rest(session, "Rest -- recovery takes priority"))

        if session.session_type == "rest":
            return self._accept(self._as_rest(session, session.description))

        # 2. Per-day duration cap, and the per-TYPE ceiling. Both are upper
        #    bounds on the same number and the tighter one wins: a 3-hour
        #    Saturday does not make a 3-hour VO2max session sensible.
        cap_min = self._day_cap_min(session.day)
        type_ceiling = self._type_ceiling_min(session)
        limit = min([x for x in (cap_min, type_ceiling) if x is not None],
                    default=None)
        if limit is not None and session.duration_min > limit:
            why = ("clamped to the day's available time"
                   if limit == cap_min else
                   f"clamped to the ceiling for a {session.session_type} session")
            self._rescale(session, limit, why)

        # 3. 48 h between hard sessions -- checked against what is already
        #    committed AND the previous week, so the rule holds across the
        #    week boundary rather than only inside it.
        if session.session_type in HARD_TYPES:
            for d in self._hard_days_committed():
                if abs((session.day - d).days) < MIN_HARD_GAP_DAYS:
                    self._demote(session, "eased: 48 h from the neighbouring hard day")
                    break

        # 4. Hard work must fit the ceiling with room left for the base -- but
        #    NOT during a taper. A taper deliberately holds intensity while
        #    cutting volume (Mujika & Padilla 2003: reducing intensity is the
        #    one thing that loses the adaptation the taper exists to express),
        #    so applying the hard-share cap here would eat exactly the sessions
        #    the taper is built around. This
        #    is the constraint the volume pass could never enforce, because by
        #    the time it ran the hard sessions were already placed and it was
        #    forbidden to touch them.
        if session.session_type in HARD_TYPES and not self._is_taper():
            room = self.ceiling * HARD_CEILING_SHARE - self._hard_tss
            if room <= 0:
                self._demote(session, "eased: the week's intensity budget is spent")
            elif session.tss_estimate > room:
                shrunk = self._minutes_for_tss(session, room)
                if shrunk >= MIN_HARD_MIN:
                    self._rescale(session, shrunk, "shortened to fit the week's intensity budget")
                else:
                    self._demote(session, "eased: would not fit as a full hard session")

        # 5. The week total. Constraint 4 caps intensity; without this the
        #    easy days walk straight past the ceiling, which is what the old
        #    volume pass was left to clean up after the fact.
        #
        #    Each slot still waiting keeps a reserve, so this session takes its
        #    share rather than everything that is left. A week is a set of
        #    sessions, not a queue draining a budget.
        room = self.ceiling - self._tss
        share = room - self._pending_slots * _RESERVE_TSS_PER_SLOT
        if session.tss_estimate > share:
            fitted = self._minutes_for_tss(session, max(share, 0.0))
            if fitted >= _MIN_EASY_MIN:
                self._rescale(session, fitted, "shortened to fit the week's load")
            elif room > 0 and self._minutes_for_tss(session, room) >= _MIN_EASY_MIN:
                # The reserve would starve this slot, but the week can still
                # carry a short ride here. A short session beats a lost one --
                # and never a LONGER one: min() because a session already under
                # the floor must not be grown to reach it, which is how a
                # deload week came back heavier than the build week beside it.
                self._rescale(session, min(_MIN_EASY_MIN, session.duration_min or _MIN_EASY_MIN),
                              "shortened to the minimum the week can carry")
            else:
                return self._accept(self._as_rest(
                    session, "Rest -- no room left in the week"))

        return self._accept(session)

    # ── commit helpers, all writing through the same door ────────────────
    def _accept(self, session):
        self._committed.append(session)
        self._tss += float(session.tss_estimate or 0.0)
        if session.session_type in HARD_TYPES:
            self._hard_tss += float(session.tss_estimate or 0.0)
        return session

    def _as_rest(self, session, why):
        session.session_type = "rest"
        session.duration_min = 0
        session.tss_estimate = 0
        session.description = why
        # A rest slot must not keep the hard workout file it was carrying --
        # that mismatch is what _enforce_slot_file_coherence existed to sweep
        # up afterwards, and it is free to prevent here.
        session.zwo_file = ""
        session.zwo_name = ""
        return session

    def _is_taper(self) -> bool:
        return str(getattr(self.ctx.phase, "name", "") or "").lower() == "taper"

    def _type_ceiling_min(self, session) -> float | None:
        """The longest this TYPE of session should ever run.

        Read through the served file's content class first, then the slot's
        own type -- the file is what the athlete rides, and an endurance slot
        can be holding a VO2 workout.
        """
        import training_planner as tp
        ceilings = getattr(tp, "TYPE_CEILING", None) or {}
        cc = ""
        if session.zwo_file:
            try:
                cc = tp._content_class_for_zwo(session.zwo_file) or ""
            except Exception:                                  # noqa: BLE001
                cc = ""
        v = ceilings.get(cc) or ceilings.get(session.session_type)
        return float(v) if v else None

    def _day_cap_min(self, day) -> float | None:
        goal = self.ctx.goal
        caps = getattr(goal, "daily_max_hours", None) or {}
        h = caps.get(day.weekday())
        if h is None:
            h = (getattr(goal, "max_weekend_hours", None)
                 if day.weekday() >= 5 else
                 getattr(goal, "max_weekday_hours", None))
        return None if h is None else float(h) * 60.0

    def _rescale(self, session, new_min, why):
        """Change a session's length, keeping TSS proportional.

        The attached file described the old length, so it is dropped and the
        caller's match step refills it. Silently keeping it is how a 91-minute
        .zwo ended up on a 50-minute slot.
        """
        old = max(1, int(session.duration_min or 1))
        new_min = max(1, int(round(new_min)))
        if new_min == old:
            return
        session.tss_estimate = round(float(session.tss_estimate or 0.0) * new_min / old, 1)
        session.duration_min = new_min
        session.zwo_file = ""
        session.zwo_name = ""
        session.description = f"{session.session_type} ({new_min}min) -- {why}"

    def _minutes_for_tss(self, session, tss_room) -> int:
        rate = float(session.tss_estimate or 0.0) / max(1, int(session.duration_min or 1))
        return int(tss_room / rate) if rate > 0 else 0

    def _demote(self, session, why):
        """Turn a hard session into the easiest thing that still trains.

        Demoting rather than deleting keeps the day's aerobic volume, which is
        what the athlete's week is mostly made of anyway.
        """
        old_type = session.session_type
        session.session_type = DEMOTE_TO
        session.tss_estimate = round(float(session.tss_estimate or 0.0) * 0.8, 1)
        session.zwo_file = ""
        session.zwo_name = ""
        session.description = f"{DEMOTE_TO} ({session.duration_min}min) -- {why}"
        log.debug("week %s: %s on %s demoted to %s (%s)",
                  self.ctx.week_num, old_type, session.day, DEMOTE_TO, why)

    # ── sealing ──────────────────────────────────────────────────────────
    def seal(self):
        """Freeze what this week decided. Later writers are the bug."""
        for s in self._committed:
            object.__setattr__(s, "_sealed", True)
        return self.week

    @staticmethod
    def amend(session):
        """Context manager for the edits that are legitimately outside the
        planner: the athlete dragging a session, marking it done, swapping its
        type. Everything else that trips the seal is a pass overruling the
        planner and should be turned into a constraint instead.
        """
        class _Amend:
            def __enter__(self_inner):
                object.__setattr__(session, "_sealed", False)
                return session

            def __exit__(self_inner, *exc):
                object.__setattr__(session, "_sealed", True)
                return False
        return _Amend()

    # ── the two public verbs ─────────────────────────────────────────────
    def plan(self, library: list | None = None, seal: bool = True):
        """Decide this week from scratch.

        The skeleton (rest days, availability, any scheduled test) and the
        content proposals still come from the planner's existing science --
        this class changes who *decides*, not what the training is. Every
        proposal then passes through `_commit`, which is where the constraints
        live.
        """
        import training_planner as tp

        ctx, st = self.ctx, self.state
        if library is not None:
            st.library = library

        self.week = tp.plan_week(
            ctx.week_num, ctx.start, ctx.phase, ctx.goal, ctx.is_stepback,
            prev_week_sessions=ctx.prev_week_sessions or None,
            seed_salt=ctx.seed_salt,
            completed_tss=tp._completed_tss_in(ctx.ridden, ctx.start, ctx.end),
        )
        # Clip and prorate BEFORE anything is sized against the target. An
        # opening stub week runs Thu..Sun but carries a full week's target
        # until this runs; doing it afterwards, as the caller used to, meant
        # the week was filled with seven days of work and then relabelled as
        # four. Not idempotent -- it prorates by span each time -- so the
        # caller must not repeat it.
        if ctx.phase is not None:
            tp._clip_week_to_phase(self.week, ctx.phase, ctx.start)

        # Novelty window: names older than the rolling window go back in the
        # pool. The legacy loop did this before every sample; skipping it made
        # a 24-week plan progressively run out of workouts it was willing to
        # reuse.
        stale = [n for n, wk in st.used_names.items()
                 if ctx.week_num - wk >= st.used_names_window]
        for n in stale:
            st.used_names.pop(n, None)

        budget = tp.get_budget_for_phase(ctx.phase.name)
        budget = tp.scale_budget_to_week(
            budget, self.ceiling,
            tp.week_available_minutes(ctx.goal, ctx.start),
            model=tp.active_model_for_phase(ctx.phase.name),
            phase_name=ctx.phase.name,
            spent_zones=tp._completed_zones_in(ctx.ridden, ctx.start, ctx.end),
        )

        # What this week's own budget can afford, recorded for the plan-level
        # policies. A phase floor that reads the PHASE table instead put a
        # sprint into a week whose budget was already spent.
        self.week.hit_allowance = int(budget.hit_count_max)

        # A blueprint mode owns its week's structure, so no block focus is
        # applied on top of it -- the two are mutually exclusive by design.
        block_focus = (None if ctx.plan_mode in ("fixed_core", "template")
                       else ctx.block_focus)
        self.week.block_focus = block_focus

        if ctx.plan_mode in ("fixed_core", "template"):
            # FS1 -- the blueprint engine builds a deterministic repeatable
            # week in the same 7-slot shape the sampler produces, so
            # everything downstream is unchanged.
            proposals = tp.expand_blueprint_week(
                phase=ctx.phase, budget=budget, week_num=ctx.week_num,
                week_start=ctx.start,
                available_days=ctx.goal.available_days,
                rest_days=ctx.goal.rest_days,
                daily_max_hours=ctx.goal.daily_max_hours,
                max_weekday_hours=ctx.goal.max_weekday_hours,
                max_weekend_hours=ctx.goal.max_weekend_hours,
                is_stepback=ctx.is_stepback,
                week_in_phase=ctx.week_in_phase, goal=ctx.goal,
            )
        else:
            proposals = tp.sample_week_workouts(
                phase=ctx.phase, budget=budget, library=st.library,
                used_names=st.used_names, week_num=ctx.week_num,
                seed_salt=ctx.seed_salt, week_start=ctx.start,
                available_days=ctx.goal.available_days,
                rest_days=ctx.goal.rest_days,
                daily_max_hours=ctx.goal.daily_max_hours,
                max_weekday_hours=ctx.goal.max_weekday_hours,
                max_weekend_hours=ctx.goal.max_weekend_hours,
                is_stepback=ctx.is_stepback, pool_index=st.pool_index,
                week_in_phase=ctx.week_in_phase,
                recent_hit_types=st.recent_hit_by_phase.setdefault(ctx.phase.name, []),
                seen_cc_dur_tuples=st.seen_cc_dur_tuples,
                plan_pick_counts=st.plan_pick_counts,
                class_session_counts=st.class_session_counts,
                class_distinct_files=st.class_distinct_files,
                plan_total_weeks=st.plan_total_weeks,
                goal_type=getattr(ctx.goal, "goal_type", "general"),
                emphasis_profile=ctx.emphasis_profile,
                block_focus=block_focus,
            )

        # Trim the per-phase HIT rotation to the last ~4 weeks of picks
        # (<=3 HIT/wk x 4). Left ungrowing, an old pick keeps suppressing its
        # own class for the rest of the plan.
        _rot = st.recent_hit_by_phase.setdefault(ctx.phase.name, [])
        if len(_rot) > 12:
            del _rot[:len(_rot) - 12]

        # A scheduled test outranks a sampled workout: the sampler's pool
        # excludes test protocols, so a slot plan_week marked ftp_test would
        # otherwise be silently overwritten with ordinary intensity.
        owned = {s.day: s for s in (ctx.preserved or []) if is_immutable(s)}
        chosen = []
        for i, skeleton in enumerate(self.week.sessions):
            keep = owned.get(getattr(skeleton, "day", None))
            if keep is not None:
                chosen.append(keep)
                continue
            if (getattr(skeleton, "session_type", "") == "ftp_test"
                    or is_immutable(skeleton)):
                chosen.append(skeleton)
                continue
            proposed = proposals[i] if i < len(proposals) else None
            chosen.append(proposed if proposed is not None else skeleton)

        self.week.net_tss_target = self.ceiling
        self._commit_all(sorted(chosen, key=lambda s: s.day))
        return self.seal() if seal else self.week

    def _processing_order(self, sessions):
        """The order slots compete for the week's budget.

        Not day order. Committing Monday-to-Sunday means whatever falls last in
        the week is what gets shrunk, and for most athletes that is the weekend
        long ride -- the one session whose length IS the training effect. An
        event plan came back with a 150-minute long ride against a five-hour
        Saturday because Tuesday through Friday had already spent the budget.

        So: the athlete's own sessions and races first (they are facts, not
        proposals), then hard sessions, then the week's longest ride, then
        everything else. Within each tier, day order, so the 48 h rule still
        eases the LATER of two neighbouring hard days.
        """
        easy = [s for s in sessions
                if not is_immutable(s) and s.session_type not in HARD_TYPES
                and s.session_type != "rest"]
        longest = max(easy, key=lambda s: (s.duration_min or 0), default=None)

        def tier(s):
            if is_immutable(s):
                return 0
            if s.session_type in HARD_TYPES:
                return 1
            if longest is not None and s is longest:
                return 2
            return 3
        return sorted(sessions, key=lambda s: (tier(s), s.day))

    def _clamp_to_limits(self, session):
        """Duration bounds, re-applied. Called after the rematch as well.

        _rescale drops the file it invalidated, the rematch answers with a new
        one, and that new file can be longer than the slot -- which is how a
        77-minute anaerobic workout kept landing on a slot whose ceiling is 50.
        The bound has to be re-asserted on what was actually attached.
        """
        if is_immutable(session) or session.session_type == "rest":
            return
        limit = min([x for x in (self._day_cap_min(session.day),
                                 self._type_ceiling_min(session)) if x is not None],
                    default=None)
        if limit is not None and (session.duration_min or 0) > limit:
            old = max(1, int(session.duration_min or 1))
            new = max(1, int(round(limit)))
            session.tss_estimate = round(
                float(session.tss_estimate or 0.0) * new / old, 1)
            session.duration_min = new

    def _commit_all(self, sessions):
        """Run every session through the one door, from a clean slate.

        Re-runnable by design: `finish()` calls it again after the plan-level
        policies have had their say, so the constraints are always the last
        word rather than whichever pass happened to run last.
        """
        import training_planner as tp
        self._committed, self._tss, self._hard_tss = [], 0.0, 0.0
        ordered = self._processing_order(sessions)
        trainable = sum(1 for s in ordered
                        if s.session_type != "rest" and not is_immutable(s))
        out = []
        for s in ordered:
            if s.session_type != "rest" and not is_immutable(s):
                trainable -= 1
            self._pending_slots = trainable      # slots still to come after this
            object.__setattr__(s, "_sealed", False)
            out.append(self._commit(s))
        # Anything left without a workout gets one -- easing and shrinking both
        # drop the file they invalidated, and a session with no .zwo is not a
        # session the athlete can ride. The athlete's own sessions and races are
        # not rematched.
        for s in out:
            if (s.session_type not in ("rest", "ftp_test") and not s.zwo_file
                    and self.state.library and not is_immutable(s)):
                try:
                    tp.match_zwo(s, self.state.library, seed_salt=self.ctx.seed_salt)
                    # NOTE: clearing the file when its CONTENT is hard but the
                    # slot is easy was tried here and made things worse (6
                    # failures -> 12): an unmatched slot is re-filled elsewhere
                    # and the HIT cap breaches moved rather than went away. The
                    # slot-vs-content mismatch is real, but it belongs where
                    # match_zwo chooses, not in a post-hoc clear.
                except Exception:                              # noqa: BLE001
                    log.debug("rematch failed for %s", s.day, exc_info=True)
            self._clamp_to_limits(s)
        out.sort(key=lambda s: s.day)
        self._committed.sort(key=lambda s: s.day)
        self.week.sessions = out
        return out

    def _zone_minutes(self):
        """Easy / moderate / hard minutes, from the files actually attached.

        Reads the served workout, not the slot label: a file matched to an
        endurance slot can carry threshold content, and the label would say
        the week is easy while the athlete rides something else.
        """
        if not self._lib_index:
            self._lib_index = {(r.get("File") or ""): r
                               for r in (self.state.library or []) if r.get("File")}
        easy = mod = hard = 0.0
        for s in self._committed:
            row = self._lib_index.get(s.zwo_file or "")
            if not row:
                continue
            fd = float(row.get("Duration(min)", 0) or 0)
            if fd <= 0:
                continue
            k = float(s.duration_min or 0) / fd
            g = lambda i: float(row.get(f"Z{i}%", 0) or 0) / 100 * fd * k  # noqa: E731
            easy += g(1) + g(2)
            mod += g(3) + g(4)
            hard += g(5) + g(6)
        return easy, mod, hard

    def easy_share(self) -> float:
        e, m, h = self._zone_minutes()
        tot = e + m + h
        return (e / tot) if tot > 0 else 1.0

    def _restore_the_easy_floor(self):
        """Ease hard sessions until the week is a three-zone week again.

        Inside the owner and re-committed each time, so the budget, spacing
        and day caps all still hold afterwards -- which is the difference
        between this and a post-pass that fixes one property by breaking
        another. Bounded by the number of hard sessions.
        """
        for _ in range(len(self._committed)):
            if self.easy_share() >= MIN_EASY_SHARE:
                return
            hard = [s for s in self._committed
                    if (s.session_type in HARD_TYPES or s.session_type == DEMOTE_TO)
                    and not is_immutable(s)]
            if not hard:
                return
            # The biggest contributor first: easing it moves the share most per
            # session changed, so the week keeps as much of its intensity as
            # the floor allows.
            worst = max(hard, key=lambda s: float(s.tss_estimate or 0))
            if worst.session_type == DEMOTE_TO:
                self._as_rest(worst, "Rest -- the week needed its easy majority back")
            else:
                self._demote(worst, "eased: the week had lost its easy majority")
            self._commit_all(sorted(self.week.sessions, key=lambda s: s.day))

    def finish(self):
        """Re-assert every constraint over whatever the plan-level policies did.

        The phase floors, the FTP-test injector and the taper rules are
        legitimately plan-level: they can only be decided once every week
        exists. They are proposals all the same. Running them through _commit
        again means a policy may ask for intensity in a week that cannot carry
        it and simply not get it, instead of silently winning because it ran
        last -- which is how a sprint and a VO2max landed in a week whose
        budget was already spent.
        """
        if self.week is None:
            return None
        self._commit_all(sorted(self.week.sessions, key=lambda s: s.day))
        self._restore_the_easy_floor()
        return self.seal()

    def replan(self, ridden: list | None = None, library: list | None = None):
        """Decide this week again, with the work already done subtracted.

        `ridden` replaces the context's activity list when given, so a caller
        that has just refreshed from the athlete's log does not have to
        rebuild the context. Passing nothing re-plans against whatever the
        context already knew, which is what a plain "regenerate" wants.
        """
        if ridden is not None:
            self.ctx.ridden = ridden
        return self.plan(library=library)

    # ── reporting ────────────────────────────────────────────────────────
    def explain(self) -> str:
        """One line per decision, for when a week looks wrong and the question
        is which constraint produced it."""
        out = [f"week {self.ctx.week_num} {self.ctx.start} "
               f"ceiling {self.ceiling:.0f} TSS "
               f"(hard cap {self.ceiling * HARD_CEILING_SHARE:.0f})"]
        for s in self._committed:
            if s.session_type == "rest":
                continue
            tag = "HARD" if s.session_type in HARD_TYPES else "easy"
            out.append(f"  {s.day.strftime('%a')} {tag} {s.session_type:<12}"
                       f"{s.duration_min:>4}min {s.tss_estimate:>6.1f} TSS  {s.description[:60]}")
        out.append(f"  total {self._tss:.0f} TSS (hard {self._hard_tss:.0f})")
        return "\n".join(out)
