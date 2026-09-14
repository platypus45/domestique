"""One view of a week, read from the stored plan once and served to every card.

Until 2026-09-14 each reader derived the week for itself: /api/weekly-plan ran
a second planner on every GET and served its target as the home badge, the
rollup and the "Last week" card; the Today card read a session's label while
the This Week cell read its file, and the stored record carried both,
disagreeing; the calendar's history rows said a past week was planned at 0
while the rollup invented a target for it (notes/review/architecture-audit-
2026-09-14.md, notes/week-view-contract.md). This module is the contract's
P1, P3 and P9 on the read side: one boundary, one planned load, one budget,
one identity (the served file), one band map, and "no plan on record" said as
None, never as 0 or as a number nobody prescribed.

The write side of the week (the builders, the passes, what a session's label
means to the sampler) is Step 6 of notes/overhaul-plan.md; the stored label
stays there as the slot's, and is exposed here as ``slot_type``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

import clock

BANDS = ("low_aerobic", "mid_aerobic", "high_aerobic", "anaerobic")

# Coggan zones to the four exposure bands, the same table the actual side
# (app._exposure_split_from_tiz) uses for a ride's measured time in zone.
ZONE_BAND = {"z1": "low_aerobic", "z2": "low_aerobic", "z3": "mid_aerobic",
             "z4": "high_aerobic", "z5": "anaerobic", "z6": "anaerobic", "z7": "anaerobic"}

# A session with no served file is banded by its type. One table, replacing
# app._SESSION_TYPE_TO_BAND, tp.SESSION_TYPE_TO_BAND and the dashboard's copy.
TYPE_BAND = {
    "recovery": "low_aerobic", "z2": "low_aerobic", "long_z2": "low_aerobic",
    "endurance": "low_aerobic", "tempo": "mid_aerobic", "sweetspot": "high_aerobic",
    "threshold": "high_aerobic", "ftp_test": "high_aerobic", "overunder": "anaerobic",
    "vo2max": "anaerobic", "anaerobic": "anaerobic", "sprint": "anaerobic",
    "neuromuscular": "anaerobic",
}


def iso_week(today: Optional[date] = None, offset: int = 0) -> tuple[date, date]:
    """Monday..Sunday of the ISO week ``offset`` weeks from today's."""
    t = today or clock.today()
    start = t - timedelta(days=t.weekday()) + timedelta(weeks=offset)
    return start, start + timedelta(days=6)


def library_by_file(rows) -> dict:
    return {r.get("File"): r for r in (rows or []) if r.get("File")}


def derived_type(session: dict, lib: dict, type_of_row: Callable[[dict], str]) -> str:
    """The session's identity: the type its served file's content carries;
    the stored label only when no file is served. Rest is rest."""
    slot = (session.get("session_type") or "rest").lower()
    if slot == "rest":
        return "rest"
    row = lib.get(session.get("zwo_file") or "")
    if row is None:
        return slot
    try:
        return type_of_row(row) or slot
    except Exception:  # noqa: BLE001 - a row the classifier cannot read keeps its slot
        return slot


def zone_minutes(session: dict, lib: dict, session_type: str) -> dict:
    """Minutes per exposure band: from the served file's zone shares when it
    has one, else the type's band for the whole duration."""
    out = {b: 0.0 for b in BANDS}
    dur = float(session.get("duration_min") or 0)
    if session_type == "rest" or dur <= 0:
        return out
    row = lib.get(session.get("zwo_file") or "")
    shares = {}
    if row:
        for i in range(1, 8):
            v = row.get(f"Z{i}%")
            if v not in (None, ""):
                try:
                    shares[f"z{i}"] = float(v)
                except (TypeError, ValueError):
                    pass
    if sum(shares.values()) > 0:
        total = sum(shares.values())
        for z, pct in shares.items():
            out[ZONE_BAND.get(z, "low_aerobic")] += dur * pct / total
        return out
    out[TYPE_BAND.get(session_type, "low_aerobic")] = dur
    return out


@dataclass
class WeekView:
    start: date
    end: date
    offset: int
    on_record: bool                       # a stored row covers any day of it
    week_num: int
    phase: str
    is_stepback: bool
    budget: Optional[float]               # the ramp's tss_target, when one row owns the week
    planned_tss: Optional[float]          # what the rider was given: sum of stored sessions
    sessions: list = field(default_factory=list)   # one per day, Monday first
    planned_minutes: int = 0
    exposure_minutes_planned: dict = field(default_factory=dict)

    def as_weekly_plan(self) -> dict:
        """The shape /api/weekly-plan has always served, plus budget and
        on_record. ``tss_target`` is the planned load: what the week
        prescribes, which is what every card compares the ridden load to."""
        return {
            "week_num": self.week_num,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "phase": self.phase,
            "tss_target": self.planned_tss,
            "budget": self.budget,
            "planned_tss": self.planned_tss,
            "on_record": self.on_record,
            "is_stepback": self.is_stepback,
            "sessions": [dict(s) for s in self.sessions],
            "exposure_minutes_planned": {k: int(round(v)) for k, v in self.exposure_minutes_planned.items()},
        }


def _placeholder(day: date) -> dict:
    return {"day": day.isoformat(), "day_name": day.strftime("%a"), "session_type": "rest",
            "slot_type": "rest", "duration_min": 0, "tss_estimate": 0, "description": "",
            "zwo_file": "", "zwo_name": "", "display_name": "", "zwo_duration_min": 0,
            "zone_dist": None, "score": None, "zwo_tss": None,
            "zone_minutes": {b: 0 for b in BANDS}, "status": "pending", "on_record": False}


def build(plan: Optional[dict], start: date, lib: dict, type_of_row: Callable[[dict], str],
          naming: Optional[Callable[[str], tuple]] = None, offset: int = 0) -> WeekView:
    """The week beginning ``start`` (a Monday) as the stored ``plan`` has it."""
    end = start + timedelta(days=6)
    s_iso, e_iso = start.isoformat(), end.isoformat()
    rows = [w for w in ((plan or {}).get("weeks") or [])
            if (w.get("start") or "") <= e_iso and (w.get("end") or "") >= s_iso]
    by_day: dict[str, dict] = {}
    for w in rows:
        for s in w.get("sessions") or []:
            d = s.get("day") or ""
            if s_iso <= d <= e_iso:
                by_day[d] = s
    on_record = bool(rows)
    sessions = []
    planned = 0.0
    minutes = 0
    exposure = {b: 0.0 for b in BANDS}
    for i in range(7):
        day = start + timedelta(days=i)
        stored = by_day.get(day.isoformat())
        if stored is None:
            sessions.append(_placeholder(day))
            continue
        out = dict(stored)
        out.setdefault("day_name", day.strftime("%a"))
        out["slot_type"] = stored.get("session_type") or "rest"
        st = derived_type(stored, lib, type_of_row)
        out["session_type"] = st
        out["on_record"] = True
        zf = stored.get("zwo_file") or ""
        row = lib.get(zf) if zf else None
        if naming is not None:
            try:
                dn, zdur = naming(zf)
                out["display_name"], out["zwo_duration_min"] = dn, zdur
            except Exception:  # noqa: BLE001
                pass
        if row:
            out["zone_dist"] = {f"z{i}": row.get(f"Z{i}%", 0) for i in range(1, 7)}
            out["score"] = row.get("Score")
            out["protocol"] = row.get("Protocol")
            out["zwo_tss"] = row.get("TSS")
        else:
            out.setdefault("zone_dist", None)
            out.setdefault("score", None)
            out.setdefault("zwo_tss", None)
        zm = zone_minutes(stored, lib, st)
        out["zone_minutes"] = {k: int(round(v)) for k, v in zm.items()}
        for b, v in zm.items():
            exposure[b] += v
        if st != "rest":
            planned += float(stored.get("tss_estimate") or 0)
            minutes += int(round(float(stored.get("duration_min") or 0)))
        sessions.append(out)
    owner = None
    if rows:
        # The row that owns the week: the one covering Monday, else the first.
        owner = next((w for w in rows if (w.get("start") or "") <= s_iso), rows[0])
    budget = None
    if owner is not None and len(rows) == 1 and owner.get("tss_target") not in (None, ""):
        budget = float(owner["tss_target"])
    return WeekView(
        start=start, end=end, offset=offset, on_record=on_record,
        week_num=int((owner or {}).get("week_num") or 0),
        phase=str((owner or {}).get("phase") or ""),
        is_stepback=bool((owner or {}).get("is_stepback")),
        budget=budget,
        planned_tss=planned if on_record else None,
        sessions=sessions, planned_minutes=minutes, exposure_minutes_planned=exposure,
    )
