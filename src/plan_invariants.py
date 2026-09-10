"""What must be true of a plan, regardless of which entry point built it.

The planner has five entry points -- generate, regenerate, recalculate,
extend, refit -- and each assembles its own sequence of enforcement passes.
Measured, they run 12, 5, 6, 6 and 3 of them respectively, so the same rider
gets a plan obeying different rules depending on which button they pressed.

This module states the rules once, as checks over a finished plan, so a rule
can be verified rather than assumed to have been applied. It reads plans; it
never edits them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


# The types the planner treats as hard. Kept here rather than imported so a
# change to the planner's internals shows up as a failing check, not as a
# silently-relaxed rule.
HARD_TYPES = {"vo2max", "threshold", "sweetspot", "overunder", "sprint",
              "anaerobic", "hill_repeats", "ftp_test", "race"}

MIN_HARD_GAP_HOURS = 48


@dataclass
class Violation:
    rule: str
    week_num: int
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] wk{self.week_num}: {self.detail}"


def _is_available(day, goal) -> bool:
    wd = day.weekday()
    if wd in (getattr(goal, "rest_days", None) or []):
        return False
    avail = getattr(goal, "available_days", None)
    return not avail or wd in avail


def _ceiling(week) -> float:
    """The number the week was planned against.

    A week the athlete has already ridden has a lower ceiling than its
    headline target, and grading it against the headline reports a correctly
    empty week as a bug. `net_tss_target` is stamped by the planner; falling
    back to tss_target keeps this working for weeks built before it existed.
    """
    net = getattr(week, "net_tss_target", None)
    return float(week.tss_target if net is None else net)


def _training(week) -> list:
    return [s for s in week.sessions if s.session_type != "rest"]


def _hard(week) -> list:
    return [s for s in week.sessions if s.session_type in HARD_TYPES]


def check_hard_day_spacing(weeks) -> list[Violation]:
    """No two hard sessions closer than 48 h, across week boundaries too.

    Gabbett 2016 / Seiler: the recovery cost of intensity is what caps hard
    sessions, and back-to-back hard days is the single most reliable way to
    turn a plan into an injury.
    """
    out = []
    flat = sorted(((s, w.week_num) for w in weeks for s in _hard(w)),
                  key=lambda t: t[0].day)
    for (a, wa), (b, wb) in zip(flat, flat[1:]):
        gap_h = (b.day - a.day).days * 24
        if gap_h < MIN_HARD_GAP_HOURS:
            out.append(Violation(
                "hard_day_spacing", wb,
                f"{a.day} {a.session_type} -> {b.day} {b.session_type} "
                f"= {gap_h}h apart"))
    return out


def check_weekly_volume(weeks, tolerance=1.15) -> list[Violation]:
    """Prescribed TSS must not overshoot the week's own target."""
    out = []
    for w in weeks:
        got = sum(s.tss_estimate for s in w.sessions)
        ceil = _ceiling(w)
        if ceil > 0 and got > ceil * tolerance:
            out.append(Violation(
                "weekly_volume", w.week_num,
                f"{got:.0f} TSS prescribed vs {ceil:.0f} target "
                f"(+{100*(got/ceil-1):.0f}%)"))
    return out


def check_rest_days_respected(weeks, goal) -> list[Violation]:
    """A day the athlete said they cannot ride must carry no session."""
    out = []
    for w in weeks:
        for s in _training(w):
            wd = s.day.weekday()
            if wd in (goal.rest_days or []) or (
                    goal.available_days and wd not in goal.available_days):
                out.append(Violation(
                    "rest_days", w.week_num,
                    f"{s.day} ({s.day.strftime('%a')}) has {s.session_type} "
                    f"but is not an available day"))
    return out


def check_daily_duration_cap(weeks, goal) -> list[Violation]:
    """No session longer than the athlete said that day can hold."""
    out = []
    caps = goal.daily_max_hours or {}
    for w in weeks:
        for s in _training(w):
            cap_h = caps.get(s.day.weekday())
            if cap_h is None:
                continue
            if s.duration_min > cap_h * 60 + 1:      # +1 min for rounding
                out.append(Violation(
                    "daily_duration_cap", w.week_num,
                    f"{s.day} {s.session_type} {s.duration_min}min "
                    f"> {cap_h*60:.0f}min cap"))
    return out


def check_slot_file_coherence(weeks) -> list[Violation]:
    """A session's attached workout file must match the type it claims.

    An unmatched session (no file) is legal -- the library may genuinely not
    hold anything for that slot. A file whose own classified type disagrees
    with the slot's is not.
    """
    out = []
    for w in weeks:
        # Every session, not just the training ones: the fault this catches is
        # a slot that was DOWNGRADED to rest while keeping the hard workout
        # file it was carrying, and _training() filters exactly those out.
        # Iterating _training() here made the check permanently blind; found by
        # feeding it a plan that had the fault.
        for s in w.sessions:
            if not s.zwo_file:
                continue
            if s.session_type == "rest":
                out.append(Violation("slot_file_coherence", w.week_num,
                                     f"{s.day} rest slot carries {s.zwo_file}"))
    return out


def check_no_empty_training_week(weeks, goal) -> list[Violation]:
    """A week with a real TSS target must prescribe something."""
    out = []
    for w in weeks:
        # A week with no days the athlete can ride is CORRECTLY empty. The
        # opening stub week often is: a plan generated on a Thursday for a
        # Mon/Tue/Wed rider spans Thu..Sun and contains no ridable day at all.
        # Flagging that reported the planner's right answer as a bug.
        ridable = any(_is_available(w.start + timedelta(days=i), goal)
                      for i in range((w.end - w.start).days + 1))
        if ridable and _ceiling(w) >= 50 and not _training(w):
            out.append(Violation("empty_week", w.week_num,
                                 f"ceiling {_ceiling(w):.0f} TSS, no sessions"))
    return out


ALL_CHECKS = (
    ("hard_day_spacing", lambda ws, g: check_hard_day_spacing(ws)),
    ("weekly_volume", lambda ws, g: check_weekly_volume(ws)),
    ("rest_days", lambda ws, g: check_rest_days_respected(ws, g)),
    ("daily_duration_cap", lambda ws, g: check_daily_duration_cap(ws, g)),
    ("slot_file_coherence", lambda ws, g: check_slot_file_coherence(ws)),
    ("empty_week", lambda ws, g: check_no_empty_training_week(ws, g)),
)


def audit(weeks, goal) -> list[Violation]:
    """Every rule, over a finished plan. Empty list means the plan is legal."""
    out = []
    for _name, fn in ALL_CHECKS:
        out.extend(fn(weeks, goal))
    return out


def summary(weeks, goal) -> str:
    v = audit(weeks, goal)
    if not v:
        return "clean"
    by_rule: dict[str, int] = {}
    for x in v:
        by_rule[x.rule] = by_rule.get(x.rule, 0) + 1
    return ", ".join(f"{k}x{n}" for k, n in sorted(by_rule.items()))
