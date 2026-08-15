"""First session back after a short break: taper-shaped, not the lightest file.

The rule replaced a blanket "recovery first" that squandered freshness: a
rider with 4 days of complete rest and TSB +2.5 was handed a 30-TSS recovery
spin, rode 101 TSS instead, and was fine. The science (docs/SCIENCE.md,
"Returning after a break"): complete rest is the one taper variant that does
NOT supercompensate (Shepley 1992), what a short break costs is plasma volume
that one INTENSE session restores within 24h (Gillen 1991), and a taper cuts
volume while holding intensity (Bosquet 2007). So 4-7 days off puts the
week's planned quality first at ~70% volume; 8-14 days holds quality but caps
the ceiling at threshold; under 4 days nothing changes; 15+ belongs to the
gap-regen recovery ramp; and a rider whose TSB is still clearly negative
after days of rest gets the conservative layout left alone.
"""
from __future__ import annotations

import datetime

import training_planner as tp


def _week(types_durs):
    monday = datetime.date(2026, 8, 17)
    sessions = []
    for i, (stype, dur) in enumerate(types_durs):
        day = monday + datetime.timedelta(days=i)
        sessions.append(tp.PlannedSession(
            day=day, day_name=day.strftime("%A"), session_type=stype,
            duration_min=dur, tss_estimate=round(
                dur / 60 * tp.TSS_PER_HOUR.get(stype, 45)) if dur else 0,
            description=stype))
    return [tp.PlannedWeek(week_num=1, start=monday,
                           end=monday + datetime.timedelta(days=6),
                           phase="continuous", tss_target=200,
                           is_stepback=False, sessions=sessions)]


_SHAPE = [("recovery", 60), ("tempo", 60), ("z2", 60), ("rest", 0),
          ("vo2max", 60), ("rest", 0), ("vo2max", 60)]


def test_short_gap_puts_quality_first_at_reduced_volume():
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=5, tsb=2.5, library=[])
    first = weeks[0].sessions[0]
    assert first.session_type == "tempo", (
        f"first day back is {first.session_type} — the freshness a rest block "
        f"buys is being spent on the lightest session in the library")
    assert first.duration_min == 40, (
        f"quality moved forward but at {first.duration_min}min — the volume "
        f"cut (x0.70 of 60) is what makes it taper-shaped")
    # The displaced easy session took the vacated day, so the week's session
    # count and spacing survive.
    assert weeks[0].sessions[1].session_type == "recovery"


def test_tiny_gap_changes_nothing():
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=2, tsb=5.0, library=[])
    assert weeks[0].sessions[0].session_type == "recovery"
    assert weeks[0].sessions[0].duration_min == 60


def test_unknown_gap_changes_nothing():
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=None, tsb=None, library=[])
    assert weeks[0].sessions[0].session_type == "recovery"


def test_negative_tsb_blocks_the_reshape():
    """TSB still clearly negative after days off means the break was not
    plain rest — illness is the case the planner cannot see. Conservative
    layout stands."""
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=5, tsb=-12.0, library=[])
    assert weeks[0].sessions[0].session_type == "recovery"


def test_mid_gap_caps_the_ceiling_at_threshold():
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=10, tsb=8.0, library=[])
    types = [s.session_type for s in weeks[0].sessions]
    assert "vo2max" not in types, (
        "8-14 days out, time-to-exhaustion is measurably down (Houmard 1992) "
        "— the max-aerobic top end waits a week; threshold stands in")
    assert types.count("threshold") >= 2


def test_long_gap_is_the_ramps_territory():
    weeks = _week(_SHAPE)
    tp._apply_reentry_shape(weeks, gap_days=21, tsb=15.0, library=[])
    assert weeks[0].sessions[0].session_type == "recovery"


def test_all_easy_week_is_not_hardened():
    """A stepback/deload week has no quality to move forward — inventing one
    would override a deliberate easy week."""
    weeks = _week([("recovery", 60), ("z2", 60), ("z2", 60), ("rest", 0),
                   ("z2", 60), ("rest", 0), ("z2", 90)])
    tp._apply_reentry_shape(weeks, gap_days=5, tsb=3.0, library=[])
    assert all(s.session_type in ("recovery", "z2", "rest")
               for s in weeks[0].sessions)
