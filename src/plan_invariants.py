"""What must be true of a plan, regardless of which entry point built it.

The planner has several entry points -- generate, regenerate, recalculate,
extend, refit, reforecast -- and each assembled its own sequence of passes, so
the same rider could get a plan obeying different rules depending on which
button they pressed.

This module states the rules once, as checks over a finished plan, so a rule
can be verified rather than assumed to have been applied. It reads plans; it
never edits them.

Independence. It does not import the planner and does not trust any number the
planner computed about its own week. It judges what the athlete RECEIVES: the
served workout's content (the library's own classification of the file), the
goal's declared caps, and the rides. The first version kept a copy of the
planner's hard-type list "so a divergence would surface" -- but both copies
judged by LABEL, so they shared the one blind spot that mattered (an endurance
slot serving VO2 content), and it graded each week against the planner's own
stamped net budget, so a week the planner had wrongly emptied graded clean
(notes/review/owner.md OWN-12, dupes.md DUP-6).

Status-aware. A missed or dismissed session was not ridden: it costs no load
and no recovery, so it neither counts toward a week nor blocks a hard day.

``today`` limits the budget rules to what is still ahead. A regenerated plan
keeps its past weeks, and the pending sessions left in them are history, not
prescriptions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Hard by prescription -- the athlete was told to ride hard.
HARD_TYPES = frozenset({"vo2max", "threshold", "sweetspot", "overunder", "sprint",
                        "anaerobic", "hill_repeats", "ftp_test", "race",
                        "double_threshold"})
# Hard by content, whatever the slot is called: the classifier's primary class
# of the file the athlete will actually ride.
HARD_CONTENT = frozenset({"threshold", "threshold_ladder", "vo2max", "vo2_ladder",
                          "vo2_short", "over_under", "sweet_spot",
                          "sweet_spot_ladder", "anaerobic", "neuromuscular",
                          "double_threshold", "ftp_test"})
# An easy slot may hold only easy content.
EASY_TYPES = frozenset({"z2", "long_z2", "recovery"})
EASY_CONTENT = frozenset({"endurance", "endurance_intervals", "recovery"})
NOT_RIDDEN = frozenset({"missed", "dismissed"})

MIN_HARD_GAP_HOURS = 48
# Hard work may take at most this share of the week's budget: constraint #1
# of the decided precedence (week_plan.py). Not applied in the taper, which
# holds intensity while it cuts volume.
HARD_SHARE = 0.60
# What any open day can carry as easy riding (z2, ~45 TSS/h). Used only to
# say "the week could have held more", never to size anything.
EASY_TSS_PER_MIN = 0.75

_CLASSIFICATION = Path(__file__).resolve().parent / "workouts" / ".content_classification.json"
_classes: dict | None = None

# Used only when a file is missing from the classification table; clear
# prefixes only, so an unknown file is judged unknown rather than guessed.
_PREFIX_CLASS = (
    ("vo2_short", "vo2_short"), ("vo2max_short", "vo2_short"), ("vo2", "vo2max"),
    ("supra_threshold", "threshold"), ("threshold", "threshold"),
    ("sweetspot", "sweet_spot"), ("sweet_spot", "sweet_spot"),
    ("over_under", "over_under"), ("overunder", "over_under"),
    ("anaerobic", "anaerobic"), ("sprints", "neuromuscular"),
    ("recovery", "recovery"), ("z2_", "endurance"), ("endurance", "endurance"),
)


def content_class(zwo_file: str) -> str:
    """The library's own classification of a served file, '' when unknown."""
    global _classes
    if not zwo_file:
        return ""
    if _classes is None:
        try:
            _classes = json.loads(_CLASSIFICATION.read_text(encoding="utf-8")).get(
                "classifications", {})
        except (OSError, ValueError):
            _classes = {}
    name = zwo_file.split("/")[-1]
    ent = _classes.get(name)
    if ent:
        return str(ent.get("primary") or "").lower()
    low = name.lower()
    return next((cc for p, cc in _PREFIX_CLASS if low.startswith(p)), "")


@dataclass
class Violation:
    rule: str
    week_num: int
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] wk{self.week_num}: {self.detail}"


def _ridden(s) -> bool:
    return s.session_type != "rest" and getattr(s, "status", "pending") not in NOT_RIDDEN


def _pending(s) -> bool:
    return s.session_type != "rest" and getattr(s, "status", "pending") == "pending"


def _is_race(s) -> bool:
    return bool(getattr(s, "is_race", False))


def _hard(s) -> bool:
    return _ridden(s) and (s.session_type in HARD_TYPES
                           or content_class(s.zwo_file) in HARD_CONTENT)


def _ahead(s, today) -> bool:
    return today is None or s.day >= today


def _is_available(day, goal) -> bool:
    wd = day.weekday()
    if wd in (getattr(goal, "rest_days", None) or []):
        return False
    avail = getattr(goal, "available_days", None)
    return not avail or wd in avail


def _day_cap_h(day, goal):
    fn = getattr(goal, "max_hours_for_day", None)
    return fn(day.weekday()) if fn else (goal.daily_max_hours or {}).get(day.weekday())


def _ride_tss(rides, lo, hi) -> float:
    total = 0.0
    for a in rides or []:
        d = str(a.get("date") or a.get("start_date_local") or "")[:10]
        if d and lo.isoformat() <= d <= hi.isoformat():
            try:
                total += float(a.get("tss") or a.get("icu_training_load") or 0)
            except (TypeError, ValueError):
                pass
    return total


def budget(week, rides=None, today=None) -> float:
    """What is left of the week's budget, derived here rather than read off the
    planner.

    A stub week (shorter than seven days) carries a prorated target; the
    calendar week's gross is recovered from the span. With rides, what the
    athlete already rode in that calendar week comes off the gross, and the
    stub's own prorated share still caps what may be prescribed on top.
    Without rides, what the plan put on the days already behind ``today`` is
    taken as ridden -- refit, for one, is handed no rides, and grading its
    Thursday-to-Sunday against the whole week's target called a correctly
    sized remainder "under-delivered".
    """
    target = float(week.tss_target or 0)
    if rides is None:
        if today is None or today <= week.start:
            return target
        behind = sum(float(s.tss_estimate or 0) for s in week.sessions
                     if _ridden(s) and s.day < today and not _is_race(s))
        return max(0.0, target - behind)
    span = (week.end - week.start).days + 1
    gross = target * 7 / span if 0 < span < 7 else target
    monday = week.start - timedelta(days=week.start.weekday())
    return max(0.0, min(target, gross - _ride_tss(rides, monday, week.end)))


def _load(week, rides, today, hard_only=False) -> float:
    """Load the plan still asks for. With rides, done sessions ARE the rides,
    so only pending sessions count."""
    keep = _pending if rides is not None else _ridden
    return sum(float(s.tss_estimate or 0) for s in week.sessions
               if keep(s) and _ahead(s, today) and not _is_race(s)
               and (not hard_only or _hard(s)))


def _weeks_ahead(weeks, today):
    return [w for w in weeks if today is None or w.end >= today]


def check_hard_day_spacing(weeks) -> list[Violation]:
    """No two hard days closer than 48 h, across week boundaries too.

    Gabbett 2016 / Seiler: the recovery cost of intensity is what caps hard
    sessions. Hard is judged by type OR served content. A race-week opener sits
    the day before the race by design, and a same-day double counts once.
    """
    days = {}
    for w in weeks:
        for s in w.sessions:
            if _hard(s) and not getattr(s, "is_opener", False):
                days.setdefault(s.day, (s, w.week_num))
    out = []
    ordered = sorted(days)
    for a, b in zip(ordered, ordered[1:]):
        gap_h = (b - a).days * 24
        if gap_h < MIN_HARD_GAP_HOURS:
            sa, sb = days[a][0], days[b][0]
            out.append(Violation(
                "hard_day_spacing", days[b][1],
                f"{a} {sa.session_type}({content_class(sa.zwo_file) or '-'}) -> "
                f"{b} {sb.session_type}({content_class(sb.zwo_file) or '-'}) "
                f"= {gap_h}h apart"))
    return out


def check_weekly_volume(weeks, rides=None, today=None, tolerance=1.15) -> list[Violation]:
    """Prescribed TSS must not overshoot the week's budget."""
    out = []
    for w in _weeks_ahead(weeks, today):
        got, ceil = _load(w, rides, today), budget(w, rides, today)
        if (ceil > 0 and got > ceil * tolerance) or (ceil <= 0 and got > 30):
            out.append(Violation(
                "weekly_volume", w.week_num,
                f"{got:.0f} TSS prescribed vs {ceil:.0f} budget"
                + (f" (+{100*(got/ceil-1):.0f}%)" if ceil > 0 else "")))
    return out


def check_under_delivery(weeks, goal, rides=None, today=None) -> list[Violation]:
    """A week with budget left and days open must prescribe a fair part of it.

    The mirror of weekly_volume, and the one the first auditor lacked: a
    planner that subtracts the athlete's rides twice hands them four rest days
    and every overshoot check stays silent. Expected is the budget, capped by
    what the open days could carry as easy riding; under half of it is a
    violation. Race weeks are exempt -- the race is the week.
    """
    out = []
    for w in _weeks_ahead(weeks, today):
        if any(_is_race(s) for s in w.sessions):
            continue
        first = w.start if today is None else max(w.start, today)
        days = [first + timedelta(days=i) for i in range((w.end - first).days + 1)]
        cap = sum((_day_cap_h(d, goal) or 0) * 60 * EASY_TSS_PER_MIN
                  for d in days if _is_available(d, goal))
        expected = min(budget(w, rides, today), cap)
        got = sum(float(s.tss_estimate or 0) for s in w.sessions
                  if _pending(s) and s.day >= first)
        if expected >= 60 and got < 0.5 * expected:
            out.append(Violation(
                "under_delivery", w.week_num,
                f"{got:.0f} TSS prescribed vs {expected:.0f} the week could carry"))
    return out


def check_rest_days_respected(weeks, goal) -> list[Violation]:
    """A day the athlete said they cannot ride must carry no session."""
    out = []
    for w in weeks:
        for s in w.sessions:
            if _pending(s) and not _is_race(s) and not _is_available(s.day, goal):
                out.append(Violation(
                    "rest_days", w.week_num,
                    f"{s.day} ({s.day.strftime('%a')}) has {s.session_type} "
                    f"but is not an available day"))
    return out


def check_daily_duration_cap(weeks, goal) -> list[Violation]:
    """No session longer than the athlete said that day can hold.

    The cap is the goal's own rule -- per-day override, else the weekday or
    weekend default. Reading only the per-day dict made this blind to every
    rider whose limits come from the defaults (dupes.md DUP-10).
    """
    out = []
    for w in weeks:
        for s in w.sessions:
            if not _pending(s) or _is_race(s):
                continue
            cap_h = _day_cap_h(s.day, goal)
            if cap_h is not None and s.duration_min > cap_h * 60 + 1:
                out.append(Violation(
                    "daily_duration_cap", w.week_num,
                    f"{s.day} {s.session_type} {s.duration_min}min "
                    f"> {cap_h*60:.0f}min cap"))
    return out


def check_slot_file_coherence(weeks) -> list[Violation]:
    """A rest slot carries no workout file.

    Every session, not just the training ones: the fault this catches is a
    slot DOWNGRADED to rest while keeping the hard file it was carrying.
    """
    out = []
    for w in weeks:
        for s in w.sessions:
            if s.session_type == "rest" and s.zwo_file:
                out.append(Violation("slot_file_coherence", w.week_num,
                                     f"{s.day} rest slot carries {s.zwo_file}"))
    return out


def check_easy_slot_content(weeks) -> list[Violation]:
    """An easy slot must serve easy content.

    The defect behind the hard cap being enforced on labels: a z2 slot served a
    threshold file is a hard day the week's spacing and budget never saw.
    """
    out = []
    for w in weeks:
        for s in w.sessions:
            if not (_pending(s) and s.session_type in EASY_TYPES and s.zwo_file):
                continue
            # A race-week opener is a short easy ride carrying two or three
            # race-pace touches the day before the race, by design (the taper's
            # "short and sharp" last days, Mujika & Padilla 2003).
            if getattr(s, "is_opener", False):
                continue
            cc = content_class(s.zwo_file)
            if cc and cc not in EASY_CONTENT:
                out.append(Violation("easy_slot_content", w.week_num,
                                     f"{s.day} {s.session_type} <- {cc} ({s.zwo_file})"))
    return out


def check_hard_share(weeks, rides=None, today=None, tolerance=1.10) -> list[Violation]:
    """Hard work stays within its share of the week's budget, outside the taper."""
    out = []
    for w in _weeks_ahead(weeks, today):
        if str(getattr(w, "phase", "")).lower() == "taper":
            continue
        ceil = budget(w, rides, today)
        if ceil < 60:
            continue
        hard = _load(w, rides, today, hard_only=True)
        if hard > HARD_SHARE * ceil * tolerance:
            out.append(Violation(
                "hard_share", w.week_num,
                f"{hard:.0f} TSS hard vs {HARD_SHARE*ceil:.0f} allowed "
                f"({HARD_SHARE:.0%} of {ceil:.0f})"))
    return out


# Weeks whose load is reduced by design: the taper, a regenerate's recovery
# ramp (named "recon" and "recovery_ramp" by build_recovery_ramp), and the
# consolidation week that closes a non-event plan (Mujika 2010). The
# planner's rhythm and ramp read the same list.
UNLOAD_PHASES = ("taper", "recon", "recovery_ramp", "consolidation")
# What the planner writes on a day the rider is away (training_planner and
# week_plan both mark it), which is how a holiday is told from a week the
# rider has already ridden.
REST_UNAVAILABLE = "Rest (unavailable)"


def _field(o):
    return o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))


def _day(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def asks_nothing(w) -> bool:
    """A whole week of rest days the rider is away for: a holiday, which
    unloads them whether it is ahead of today or behind it.

    Not every week of rest days. A plan made on a Monday after a 420 TSS ride
    prescribes nothing for the days left of that week, and that week loaded
    the rider (the third part 3 review, M1). Nor a shorter row: it says
    nothing about its calendar week.
    """
    get = _field(w)
    start, end, sessions = get("start"), get("end"), get("sessions") or []
    if start is None or end is None or not sessions:
        return False
    if (_day(end) - _day(start)).days + 1 < 7:
        return False
    away = False
    for s in sessions:
        sget = _field(s)
        if sget("session_type", "") != "rest":
            return False
        away = away or sget("description", "") == REST_UNAVAILABLE
    return away


def _is_full_week(w) -> bool:
    """Six days or more: a plan's first week, starting mid-week, is not a week
    to compare a block against. A Thursday start gives a two-day W1 with no
    room for a rider's usual rest days, and counting it as a build would judge
    a recovery week against a week that is short, not hard. (It is not what
    made the old rest-day check depend on the weekday: all 44 failing recovery
    weeks of the 28-date sweep still fail with partial weeks excluded. That was
    the load floor, below.)"""
    return (w.end - w.start).days >= 6


def _training(w):
    """Sessions that are training load: not rest, not a race, not an FTP test.
    A test is a measurement placed where the legs are fresh -- often in the
    recovery week itself -- and counting its maximal effort as the week's
    intensity makes a recovery week look harder than the block it unloads."""
    return [s for s in w.sessions
            if s is not None and s.session_type not in ("rest", "ftp_test")
            and not _is_race(s)]


def stepback_looks_lighter(deload, builds) -> "tuple[bool, str]":
    """Does a recovery week LOOK lighter than the build weeks of its block?

    The rule the rider sees, and one definition of it (the planner used rest
    days, this module used TSS, and they disagreed). Lighter by the first lever
    that fits:

      * more rest days than every full build week; or
      * where the rider's available days leave no room for another rest day
        without dropping under the load floor (the floor wins: a recovery week
        too light is how the build weeks after it ramped at 1.7x), lighter by
        LOAD TYPE -- no hard session (an FTP test aside) while every full build
        week carries hard work; or, where some build week carries none, less
        load than the lightest full build week.

    That second lever accepts a recovery week with no extra rest day and even
    more minutes than a build, provided it is all easy: the owner's decision
    that the load floor wins over the rest-day count.

    Two other measures of "load type" were tried and are wrong. An average
    intensity over the week read a build week holding VO2max, threshold and a
    sprint as EASIER than an all-Z2 recovery week, because a 300-minute Z2 ride
    diluted it. Total load alone read a three-day rider's all-Z2 recovery week
    (126 TSS) as heavier than a build week holding a sweet-spot session and an
    FTP test (88 TSS once the test was dropped): the week anyone would call
    harder. So it is hard work first, and load only when a build week has none.

    Measured over 28 start dates (Sep 14 - Oct 11 2026) for three rider
    shapes, the rest-day rule alone failed 12 of 28 for each, on fixed weekdays
    that depend on the rider's availability. In all 44 failing recovery weeks
    (a start date can fail two) the planner's rest-day loop had stopped at the
    load floor, and none of those recovery
    weeks held hard work apart from an FTP test: the week was lighter, and the
    check was looking at the one axis the floor had fixed. Partial weeks are
    compared with nothing.
    """
    full = [b for b in builds if _is_full_week(b)]
    if not full or not _is_full_week(deload):
        return True, "no full build week to compare with"

    def rests(w):
        return sum(1 for s in w.sessions if s is not None and s.session_type == "rest")

    if rests(deload) > max(rests(b) for b in full):
        return True, "more rest days"
    train = _training(deload)
    if any(s.session_type in HARD_TYPES for s in train):
        return False, "no rest day to spare, and it still carries a hard session"

    def hard_minutes(w):
        # HARD_TYPES counts an FTP test as hard, so a build week's test is hard
        # work. Only the recovery week may hold one (_training drops it there),
        # because that is where tests are placed on purpose, on fresh legs.
        return sum(float(s.duration_min or 0) for s in w.sessions
                   if s is not None and not _is_race(s) and s.session_type in HARD_TYPES)

    least_hard = min(hard_minutes(b) for b in full)
    if least_hard > 0:
        return True, (f"lighter load type: all easy, against at least "
                      f"{least_hard:.0f} hard minutes in every build week")

    # A block containing an all-easy week: no hard work to be lighter than, so
    # the recovery week has to carry less load than every full build week.
    def tss(w):
        return sum(float(s.tss_estimate or 0) for s in _training(w))

    mine, theirs = tss(deload), min(tss(b) for b in full)
    if mine < theirs:
        return True, (f"lighter load type: all easy, {mine:.0f} TSS against "
                      f"{theirs:.0f} in its lightest full build week")
    return False, (f"no more rest days than its builds, and {mine:.0f} TSS against "
                   f"{theirs:.0f} in its lightest full build week")


def check_stepback_lightest(weeks, today=None) -> list[Violation]:
    """An unload week is lighter than the load weeks of its block.

    Issurin's 3:1 loading: the unload week exists to let the three before it
    be absorbed, so it has to be the lightest of them. The block is the load
    weeks since the last unload: a stepback, an unload phase or a holiday.
    Counted from stepbacks alone, a regenerate's recovery weeks sat in the
    block, and an unload lighter than every load week before it was flagged.
    Weeks that began before ``today`` are history -- half-ridden,
    half-dismissed -- and are compared with nothing.
    """
    out, block = [], []
    for w in weeks:
        if today is not None and w.start < today:
            continue
        if str(getattr(w, "phase", "")).lower() in UNLOAD_PHASES or asks_nothing(w):
            block = []
            continue
        full = (w.end - w.start).days >= 6
        load = sum(float(s.tss_estimate or 0) for s in w.sessions
                   if _ridden(s) and not _is_race(s))
        if not full or load <= 0:
            continue
        if w.is_stepback:
            if block and load > min(block) + 1:
                out.append(Violation("stepback_lightest", w.week_num,
                                     f"unload week {load:.0f} TSS > lightest load week "
                                     f"{min(block):.0f} of its block"))
            block = []
        else:
            block.append(load)
    return out


def check_stepback_looks_lighter(weeks, today=None) -> list[Violation]:
    """A recovery week looks lighter than its block, to the rider.

    check_stepback_lightest asks whether it carries less TSS; this asks whether
    the rider can SEE it -- more rest days, or a lighter load type where their
    availability leaves no rest day to spare. Same block rule as the TSS check:
    the load weeks since the last unload, with weeks already begun left alone.
    """
    out, block = [], []
    for w in weeks:
        if today is not None and w.start < today:
            continue
        if str(getattr(w, "phase", "")).lower() in UNLOAD_PHASES or asks_nothing(w):
            block = []
            continue
        if w.is_stepback:
            ok, why = stepback_looks_lighter(w, block)
            if block and not ok:
                out.append(Violation("stepback_looks_lighter", w.week_num, why))
            block = []
        else:
            block.append(w)
    return out


# Acute:chronic workload ratio (Gabbett 2016): past ~1.3x the load the rider
# has been carrying, injury risk climbs, and from 1.5x it is the danger zone.
# The load carried is a 28-day exponentially weighted mean of daily load
# (Williams et al. 2017; Murray, Gabbett et al. 2017), in TSS a week. The
# 4-week rolling mean it replaces read a two-week holiday as two zeros that
# held the weeks back down and then dropped out of the window: 248, 236,
# 157, then an unload (the second part 3 review, M-5).
ACWR_SWEET_SPOT = 1.3
ACWR_DANGER = 1.5
_CHRONIC_DAY_WEIGHT = 2 / (28 + 1)


def chronic_after(chronic: float, weekly_load: float, days: int = 7) -> float:
    """The load carried, in TSS a week, after ``days`` at ``weekly_load``."""
    keep = (1 - _CHRONIC_DAY_WEIGHT) ** max(0, int(days))
    return chronic * keep + float(weekly_load or 0) * (1 - keep)


def check_acwr(weeks, chronic, today=None, tolerance=1.05,
               limit=ACWR_SWEET_SPOT) -> list[Violation]:
    """No week asks more than ``limit`` x the load the rider carries into it:
    the top of the sweet spot, in every phase, or ACWR_DANGER, the line no
    week may cross.

    The load carried starts at ``chronic`` and follows the plan's own weeks
    (chronic_after): a plan that raises the chronic load may raise the acute
    with it, which is what a ramp is. A row is scaled to a full week and
    checked from four days; a shorter one still counts toward the load
    carried. The taper is history only: its race is the point of the plan,
    not a dose. Weeks behind ``today`` are skipped, since ``chronic`` holds
    what the rider did. Not part of audit(), which has no rider: a plan does
    not carry their chronic load.
    """
    carried, out = float(chronic), []
    for w in _weeks_ahead(weeks, today):
        days = (w.end - w.start).days + 1
        if days <= 0:
            continue
        load = sum(float(s.tss_estimate or 0) for s in w.sessions if _ridden(s)) * 7 / days
        phase = str(getattr(w, "phase", "")).lower()
        if (days >= 4 and phase != "taper" and carried > 0
                and load > limit * carried * tolerance + 1):
            out.append(Violation("acwr", w.week_num,
                                 f"{load:.0f} TSS a week against {carried:.0f} carried "
                                 f"({load / carried:.2f}x; {limit}x allowed)"))
        carried = chronic_after(carried, load, days)
    return out


def projected_ctl(weeks, ctl, today=None) -> list[tuple[int, float]]:
    """The CTL each week's prescription leaves the rider at, from ``ctl`` on
    ``today``: the 42-day exponentially weighted mean of daily TSS of
    Banister's model, as Coggan's performance manager computes it. Days behind
    ``today`` are skipped, since ``ctl`` already holds them."""
    out = []
    for w in weeks:
        day = w.start if today is None else max(w.start, today)
        if day > w.end:
            continue
        tss: dict = {}
        for s in w.sessions:
            if _ridden(s):
                tss[s.day] = tss.get(s.day, 0.0) + float(s.tss_estimate or 0)
        while day <= w.end:
            ctl += (tss.get(day, 0.0) - ctl) / 42
            day += timedelta(days=1)
        out.append((w.week_num, ctl))
    return out


def check_no_empty_training_week(weeks, goal, rides=None, today=None) -> list[Violation]:
    """A week with a real budget must prescribe something."""
    out = []
    for w in _weeks_ahead(weeks, today):
        first = w.start if today is None else max(w.start, today)
        # A week with no day the athlete can ride is CORRECTLY empty -- the
        # opening stub of a Thursday plan for a Mon/Tue/Wed rider often is.
        ridable = any(_is_available(first + timedelta(days=i), goal)
                      for i in range((w.end - first).days + 1))
        ceil = budget(w, rides, today)
        if ridable and ceil >= 50 and not any(_ridden(s) and s.day >= first
                                              for s in w.sessions):
            out.append(Violation("empty_week", w.week_num,
                                 f"budget {ceil:.0f} TSS, no sessions"))
    return out


ALL_CHECKS = (
    ("hard_day_spacing", lambda ws, g, r, t: check_hard_day_spacing(ws)),
    ("weekly_volume", lambda ws, g, r, t: check_weekly_volume(ws, r, t)),
    ("under_delivery", lambda ws, g, r, t: check_under_delivery(ws, g, r, t)),
    ("rest_days", lambda ws, g, r, t: check_rest_days_respected(ws, g)),
    ("daily_duration_cap", lambda ws, g, r, t: check_daily_duration_cap(ws, g)),
    ("slot_file_coherence", lambda ws, g, r, t: check_slot_file_coherence(ws)),
    ("easy_slot_content", lambda ws, g, r, t: check_easy_slot_content(ws)),
    ("hard_share", lambda ws, g, r, t: check_hard_share(ws, r, t)),
    ("stepback_lightest", lambda ws, g, r, t: check_stepback_lightest(ws, t)),
    ("stepback_looks_lighter", lambda ws, g, r, t: check_stepback_looks_lighter(ws, t)),
    ("empty_week", lambda ws, g, r, t: check_no_empty_training_week(ws, g, r, t)),
)


def audit(weeks, goal, rides=None, today=None) -> list[Violation]:
    """Every rule, over a finished plan. Empty list means the plan is legal.

    ``rides`` (the athlete's activities) turns on the ridden-aware budget;
    ``today`` limits the budget rules to days still ahead.
    """
    out = []
    for _name, fn in ALL_CHECKS:
        out.extend(fn(weeks, goal, rides, today))
    return out


def summary(weeks, goal, rides=None, today=None) -> str:
    v = audit(weeks, goal, rides, today)
    if not v:
        return "clean"
    by_rule: dict[str, int] = {}
    for x in v:
        by_rule[x.rule] = by_rule.get(x.rule, 0) + 1
    return ", ".join(f"{k}x{n}" for k, n in sorted(by_rule.items()))
