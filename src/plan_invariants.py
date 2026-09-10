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
from datetime import timedelta
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


def budget(week, rides=None) -> float:
    """The week's budget, derived here rather than read off the planner.

    A stub week (shorter than seven days) carries a prorated target; the
    calendar week's gross is recovered from the span. With rides, what the
    athlete already rode in that calendar week comes off the gross, and the
    stub's own prorated share still caps what may be prescribed on top.
    """
    target = float(week.tss_target or 0)
    if rides is None:
        return target
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
        got, ceil = _load(w, rides, today), budget(w, rides)
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
        expected = min(budget(w, rides), cap)
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
        ceil = budget(w, rides)
        if ceil < 60:
            continue
        hard = _load(w, rides, today, hard_only=True)
        if hard > HARD_SHARE * ceil * tolerance:
            out.append(Violation(
                "hard_share", w.week_num,
                f"{hard:.0f} TSS hard vs {HARD_SHARE*ceil:.0f} allowed "
                f"({HARD_SHARE:.0%} of {ceil:.0f})"))
    return out


def check_stepback_lightest(weeks) -> list[Violation]:
    """An unload week is lighter than the load weeks of its block."""
    out, block = [], []
    for w in weeks:
        full = (w.end - w.start).days >= 6
        load = sum(float(s.tss_estimate or 0) for s in w.sessions
                   if _ridden(s) and not _is_race(s))
        if not full or str(getattr(w, "phase", "")).lower() == "taper" or load <= 0:
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


def check_no_empty_training_week(weeks, goal, rides=None, today=None) -> list[Violation]:
    """A week with a real budget must prescribe something."""
    out = []
    for w in _weeks_ahead(weeks, today):
        first = w.start if today is None else max(w.start, today)
        # A week with no day the athlete can ride is CORRECTLY empty -- the
        # opening stub of a Thursday plan for a Mon/Tue/Wed rider often is.
        ridable = any(_is_available(first + timedelta(days=i), goal)
                      for i in range((w.end - first).days + 1))
        ceil = budget(w, rides)
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
    ("stepback_lightest", lambda ws, g, r, t: check_stepback_lightest(ws)),
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
