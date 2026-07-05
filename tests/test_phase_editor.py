"""Phase-split editor (v3.2.0, IP_PHASE_EDITOR) — engine tests.

GP1  phase_weeks=None ⇒ byte-identical planner output + untouched global RNG
     stream (same-process paired A/B — GB1 pattern).
GP2  Valid custom split moves LENGTHS only (A4): labels == requested for all
     phases except the last pre-taper phase, which absorbs the runway
     remainder (span = req×7 ±6d); taper spans to the target; per-phase TSS
     formulas unchanged; progressive-overload clamp holds. Event AND
     non-event (locked consolidation).
GP3  Every invalid case → recommendation + "fallback:<reason>", never raises.
GP4  Race-week micro-plan (its OWN runway-from-today trigger) and B/C
     mini-tapers ignore overrides.
GP6  Refit paths (regenerate_from_today tp:8995 / recalculate_plan tp:9491)
     re-validate against THAT call's runway; status stamped into the info
     dict; goal.phase_weeks never mutated by auto paths.
A3   custom == recommendation ⇒ (None, "") — stored None, no badge path.
Also: validator purity (zero RNG draws), build2=0 FTP-test retarget.

Pinned env (W8 pattern): frozen today = 2026-01-05 (Monday), current_ctl=50,
recent_weekly_tss=650, seed_salt=0. Hermetic — no HOME writes, no network.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, PLANNER_PIN_ARGS


@pytest.fixture(scope="module", autouse=True)
def _env(planner_pinned_env):
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _serialize_weeks(weeks) -> bytes:
    """Canonical byte serialization (mirrors test_plan_entry_continuity)."""
    out = []
    for w in weeks:
        out.append({
            "week_num": w.week_num,
            "start": w.start.isoformat(), "end": w.end.isoformat(),
            "phase": w.phase, "tss_target": w.tss_target,
            "is_stepback": w.is_stepback,
            "sessions": [
                {
                    "day": s.day.isoformat(), "day_name": s.day_name,
                    "session_type": s.session_type,
                    "duration_min": s.duration_min,
                    "tss_estimate": s.tss_estimate,
                    "description": s.description,
                    "zwo_file": s.zwo_file, "zwo_name": s.zwo_name,
                }
                for s in w.sessions
            ],
        })
    return json.dumps(out, sort_keys=True).encode()


def _gen(goal, seed_salt=0):
    return tp.generate_plan(goal, seed_salt=seed_salt, **PLANNER_PIN_ARGS)


def _phase_tuples(phases):
    return [(p.name, p.weeks, p.start, p.end, p.weekly_tss_target) for p in phases]


def _event_goal_16w(**kw):
    return tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=112),
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0, **kw)


EVENT_CUSTOM_16 = {"base": 5, "build1": 3, "build2": 3, "peak": 2, "taper": 3}


# ── GP1 — None ⇒ byte-identical + zero extra RNG draws (GB1 pattern) ─────────

def test_gp1_paired_none_field_byte_equal_weeks():
    goal_a = tp.Goal(goal_type="general", plan_weeks=8, hours_per_week=8.0)
    goal_b = tp.Goal(goal_type="general", plan_weeks=8, hours_per_week=8.0,
                     phase_weeks=None)

    random.seed(24681357)
    phases_a, weeks_a = _gen(goal_a)
    rng_state_a = random.getstate()

    random.seed(24681357)
    phases_b, weeks_b = _gen(goal_b)
    rng_state_b = random.getstate()

    assert _serialize_weeks(weeks_a) == _serialize_weeks(weeks_b)
    assert _phase_tuples(phases_a) == _phase_tuples(phases_b)
    # Zero extra draws from the global RNG stream on the None path.
    assert rng_state_a == rng_state_b


# ── recommendation vector ────────────────────────────────────────────────────

def test_recommended_vector_matches_splitter_labels():
    g = _event_goal_16w()
    rec, reason = tp._recommended_phase_weeks(g)
    assert reason == ""
    assert rec == {"base": 4, "build1": 4, "build2": 4, "peak": 2, "taper": 2}
    assert sum(rec.values()) == g.weeks_available() == 16
    # The splitter's own labels agree with the vector on the rec path.
    labels = {p.name: p.weeks for p in tp.generate_phases(g, 50.0)}
    assert labels == rec


def test_recommended_vector_nonevent_excludes_locked_consolidation():
    g = tp.Goal(goal_type="general", plan_weeks=12)
    rec, _ = tp._recommended_phase_weeks(g)
    assert "consolidation" not in rec and "taper" not in rec
    assert sum(rec.values()) == 11  # M − 1: consolidation week is locked


def test_recommended_vector_ctl_goal_has_taper():
    g = tp.Goal(goal_type="ctl", target_ctl=70.0, plan_weeks=12)
    rec, _ = tp._recommended_phase_weeks(g)
    assert rec.get("taper") == 2  # "event" language includes ctl goals (A5)
    assert sum(rec.values()) == 12


# ── GP2 — valid custom split: lengths only ───────────────────────────────────

def test_gp2_event_custom_labels_dates_tss():
    g_rec = _event_goal_16w()
    rec_phases = tp.generate_phases(g_rec, 50.0)

    g = _event_goal_16w(phase_weeks=dict(EVENT_CUSTOM_16))
    phases = tp.generate_phases(g, 50.0)
    assert g._phase_weeks_status == "applied"

    by_name = {p.name: p for p in phases}
    assert [p.name for p in phases] == ["base", "build1", "build2", "peak", "taper"]
    # Labels == requested for every phase except the last pre-taper one (A4).
    for name in ("base", "build1", "build2", "taper"):
        assert by_name[name].weeks == EVENT_CUSTOM_16[name], name
    # Last pre-taper phase absorbs the remainder: span = req×7 ± 6 days.
    peak_span = (by_name["peak"].end - by_name["peak"].start).days + 1
    assert abs(peak_span - 7 * EVENT_CUSTOM_16["peak"]) <= 6
    # Taper spans to the target: 7×3 days ending ON race day.
    assert by_name["taper"].end == g.target_date
    assert (by_name["taper"].end - by_name["taper"].start).days + 1 == 21
    # Contiguous start→target, split starts at the anchor.
    assert phases[0].start == ANCHOR
    for a, b in zip(phases, phases[1:]):
        assert (b.start - a.end).days == 1
    # TSS formulas per phase unchanged (lengths only) + progressive overload.
    tss_rec = {p.name: p.weekly_tss_target for p in rec_phases}
    tss_cus = {p.name: p.weekly_tss_target for p in phases}
    assert tss_rec == tss_cus
    assert (tss_cus["base"] <= tss_cus["build1"] <= tss_cus["build2"]
            <= tss_cus["peak"])


def test_gp2_nonevent_custom_exact_and_consolidation_locked():
    custom = {"base": 4, "build1": 2, "build2": 3, "peak": 2}  # sums to 11
    g = tp.Goal(goal_type="general", plan_weeks=12, phase_weeks=dict(custom))
    phases = tp.generate_phases(g, 50.0)
    assert g._phase_weeks_status == "applied"
    assert [(p.name, p.weeks) for p in phases] == [
        ("base", 4), ("build1", 2), ("build2", 3), ("peak", 2),
        ("consolidation", 1),
    ]
    for a, b in zip(phases, phases[1:]):
        assert (b.start - a.end).days == 1


def test_gp2_ctl_target_custom_taper_relaid():
    g = tp.Goal(goal_type="ctl", target_ctl=75.0,
                target_date=ANCHOR + timedelta(days=84),
                phase_weeks={"base": 3, "build1": 3, "build2": 3,
                             "peak": 2, "taper": 1})
    phases = tp.generate_phases(g, 50.0)
    assert g._phase_weeks_status == "applied"
    taper = phases[-1]
    assert taper.name == "taper" and taper.end == g.target_date
    assert (taper.end - taper.start).days + 1 == 7  # 1 week, to the day


# ── GP3 — invalid ⇒ fallback to recommendation + reason, never raises ───────

INVALID_VECTORS = [
    ({"base": 9, "build1": 3, "build2": 3, "peak": 2, "taper": 3},
     "split totals"),                                          # bad sum
    ({"base": 6, "build1": 3, "build2": 3, "peak": 2, "taper": 0},
     "taper needs at least 1"),                                # taper 0
    ({"base": 4, "build1": 3, "build2": 3, "peak": 2, "taper": 4},
     "taper is capped at 3"),                                  # taper 4
    ({"base": 7, "build1": 3, "build2": 3, "peak": 0, "taper": 3},
     "peak needs at least 1"),                                 # peak 0
    ({"base": 3, "build1": 3, "build2": 3, "peak": 4, "taper": 3},
     "peak is capped at 3"),                                   # peak 4
    ({"base": 3, "build1": 5, "build2": 3, "peak": 2, "taper": 3},
     "build1 is capped at 4"),                                 # build1 5
    ({"base": 3, "build1": 3, "build2": 5, "peak": 2, "taper": 3},
     "build2 is capped at 4"),                                 # build2 5 (eval gap)
    ({"base": 0, "build1": 0, "build2": 4, "peak": 3, "taper": 3},
     "must open with base or build1"),                         # first = build2
    ({"base": 3.5, "build1": 3, "build2": 3, "peak": 2, "taper": 3},
     "whole numbers"),                                         # float
    ({"base": True, "build1": 7, "build2": 3, "peak": 2, "taper": 3},
     "whole numbers"),                                         # bool
    ({"base": -1, "build1": 4, "build2": 4, "peak": 3, "taper": 3},
     "cannot be negative"),                                    # negative
    ({"base": 5, "build1": 3, "build2": 3, "peak": 2, "taper": 3, "yoga": 0},
     "not an adjustable phase"),                               # junk key
    ({"base": 6, "build1": 3, "build2": 3, "peak": 2},
     "missing a week count"),                                  # missing taper
    ("6,3,3,2,2", "mapping"),                                  # not a dict
]


@pytest.mark.parametrize("bad,frag", INVALID_VECTORS,
                         ids=[str(i) for i in range(len(INVALID_VECTORS))])
def test_gp3_invalid_falls_back_with_reason(bad, frag):
    g_rec = _event_goal_16w()
    rec_phases = tp.generate_phases(g_rec, 50.0)

    g = _event_goal_16w()
    vec, reason = tp.validate_phase_weeks(g, bad)
    assert vec is None and frag in reason

    g.phase_weeks = bad
    phases = tp.generate_phases(g, 50.0)  # must not raise
    assert _phase_tuples(phases) == _phase_tuples(rec_phases)
    assert g._phase_weeks_status == f"fallback:{reason}"


def test_short_runway_editor_disabled_high1():
    """Evaluator HIGH-1: 14-27d real runway + the app-side max(4,·) week
    floor ⇒ M inflated beyond the real span ⇒ a validating vector's phases
    silently dropped by the reconcile. The editor must be DISABLED there."""
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=20),
                plan_weeks=4)  # inflated M, exactly the H1-floor shape
    rec, reason = tp._recommended_phase_weeks(g)
    assert rec is None and "under four weeks" in reason
    vec, vreason = tp.validate_phase_weeks(
        g, {"build1": 2, "peak": 1, "taper": 1})
    assert vec is None and "under four weeks" in vreason
    # And the engine never stamps "applied" for it.
    g.phase_weeks = {"build1": 2, "peak": 1, "taper": 1}
    tp.generate_phases(g, 50.0)
    assert str(g._phase_weeks_status).startswith("fallback:")


def test_short_runway_backdated_keeps_editor():
    """Backdated plan, event 20d out but anchor 6 weeks back: the real
    anchor→target span is ~9 weeks, M is NOT inflated, phases (incl. the
    elapsed ones) all materialize — the editor stays available."""
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=20),
                start_date=ANCHOR - timedelta(days=42))
    rec, reason = tp._recommended_phase_weeks(g)
    assert rec is not None and reason == ""


def test_gp3_phase_absent_from_recommendation_crisis_tier():
    # 5-week event runway ⇒ crisis tier: build1+peak(+taper) only — no base.
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=35))
    rec, _ = tp._recommended_phase_weeks(g)
    assert set(rec) == {"build1", "peak", "taper"}
    vec, reason = tp.validate_phase_weeks(
        g, {"base": 1, "build1": 1, "peak": 1, "taper": 2})
    assert vec is None and "'base' is not an adjustable phase" in reason


def test_gp3_consolidation_key_locked_nonevent():
    g = tp.Goal(goal_type="general", plan_weeks=12)
    vec, reason = tp.validate_phase_weeks(
        g, {"base": 3, "build1": 3, "build2": 3, "peak": 2, "consolidation": 1})
    assert vec is None and "consolidation week is fixed" in reason


# ── A3 — custom == recommendation ⇒ None, no badge ──────────────────────────

def test_a3_phantom_custom_is_not_custom():
    g_rec = _event_goal_16w()
    rec_phases = tp.generate_phases(g_rec, 50.0)
    rec, _ = tp._recommended_phase_weeks(g_rec)

    vec, reason = tp.validate_phase_weeks(g_rec, dict(rec))
    assert vec is None and reason == ""  # valid but NOT custom

    g = _event_goal_16w(phase_weeks=dict(rec))
    phases = tp.generate_phases(g, 50.0)
    assert getattr(g, "_phase_weeks_status", None) is None  # no badge path
    assert _phase_tuples(phases) == _phase_tuples(rec_phases)


# ── validator purity — zero global-RNG draws ─────────────────────────────────

def test_validator_pure_no_rng_draws():
    g = _event_goal_16w()
    random.seed(1337)
    state = random.getstate()
    tp._recommended_phase_weeks(g)
    tp.validate_phase_weeks(g, dict(EVENT_CUSTOM_16))
    tp.validate_phase_weeks(g, {"base": 99})
    tp.validate_phase_weeks(
        tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=10)),
        {"taper": 1})
    assert random.getstate() == state


# ── GP4 — race-week micro-plan + B/C mini-taper ignore overrides ─────────────

def test_gp4_micro_plan_ignores_override():
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=10),
                event_name="SoonRace", phase_weeks={"taper": 1})
    phases = tp.generate_phases(g, 50.0)
    assert len(phases) == 1 and phases[0].name == "taper"
    assert phases[0].end == g.target_date
    assert g._phase_weeks_status == f"fallback:{tp._PW_REASON_MICRO}"


def test_gp4_micro_trigger_is_runway_from_today_not_total():
    # Backdated: total runway 12 weeks, but the race is 10 days out — the
    # micro-plan's OWN trigger wins and the editor is disabled.
    g = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=10),
                start_date=ANCHOR - timedelta(days=74))
    rec, reason = tp._recommended_phase_weeks(g)
    assert rec is None and reason == tp._PW_REASON_MICRO


def test_gp4_bc_mini_taper_survives_custom_split():
    b_date = ANCHOR + timedelta(days=42)
    g = _event_goal_16w(phase_weeks=dict(EVENT_CUSTOM_16),
                        events=[tp.TargetEvent(date=b_date, priority="B",
                                               name="Local crit")])
    phases, weeks = _gen(g)
    all_sessions = [s for w in weeks for s in w.sessions]
    # The B race day is still marked by the existing guard passes.
    assert any(getattr(s, "is_race", False) and s.day == b_date
               for s in all_sessions)
    # And the custom split still applied.
    assert {p.name: p.weeks for p in phases if p.name == "base"} == {"base": 5}


# ── GP6 — refit paths: validity-gated, stamped, goal never mutated ──────────

@pytest.fixture(scope="module")
def nonevent_custom_plan():
    custom = {"base": 4, "build1": 2, "build2": 3, "peak": 2}
    goal = tp.Goal(goal_type="general", plan_weeks=12,
                   phase_weeks=dict(custom))
    phases, weeks = _gen(goal)
    return goal, custom, phases, weeks


def test_gp6_nonevent_refit_auto_applies(nonevent_custom_plan):
    goal, custom, _, weeks = nonevent_custom_plan
    before = dict(goal.phase_weeks)
    new_phases, all_weeks, regen_info = tp.regenerate_from_today(
        goal, weeks, current_ctl=50.0, activities=[])
    # plan_weeks short-circuit ⇒ same runway ⇒ auto-apply (A1).
    assert regen_info["phase_weeks_status"] == "applied"
    labeled = [(p.name, p.weeks) for p in new_phases]
    assert labeled == [("base", 4), ("build1", 2), ("build2", 3),
                       ("peak", 2), ("consolidation", 1)]
    assert goal.phase_weeks == before  # never mutated by the auto path


def test_gp6_event_refit_runway_moved_falls_back():
    # Vector made against a 15-week runway; today's runway is 14 weeks ⇒ the
    # refit re-validates against THAT call's runway and falls back (A1).
    stale = {"base": 5, "build1": 4, "build2": 3, "peak": 1, "taper": 2}  # 15
    goal = tp.Goal(goal_type="event",
                   target_date=ANCHOR + timedelta(days=100),
                   event_km=150.0, event_climb_m=1000.0)
    assert goal.weeks_available() == 14
    phases, weeks = _gen(goal)          # rec plan (no custom at generate)
    goal.phase_weeks = dict(stale)

    new_phases, all_weeks, regen_info = tp.regenerate_from_today(
        goal, weeks, current_ctl=50.0, activities=[])
    assert str(regen_info["phase_weeks_status"]).startswith("fallback:")
    assert "split totals 15" in regen_info["phase_weeks_status"]
    assert goal.phase_weeks == stale    # never mutated

    # Weekly recalc (tp:9491) stamps the same fallback.
    new_phases2, _, recalc_info = tp.recalculate_plan(
        goal, weeks, current_ctl=20.0)  # low CTL ⇒ deviation ⇒ rebuild
    assert recalc_info["action"] == "recalculated"
    assert str(recalc_info["phase_weeks_status"]).startswith("fallback:")
    assert goal.phase_weeks == stale


def test_gp6_nonevent_recalc_auto_applies(nonevent_custom_plan):
    goal, custom, _, weeks = nonevent_custom_plan
    g2 = tp.Goal(goal_type="general", plan_weeks=12,
                 target_date=ANCHOR + timedelta(days=84),
                 phase_weeks=dict(custom))
    _, weeks2 = _gen(g2)
    new_phases, _, recalc_info = tp.recalculate_plan(
        g2, weeks2, current_ctl=20.0)  # low CTL ⇒ structural rebuild
    assert recalc_info["action"] == "recalculated"
    assert recalc_info["phase_weeks_status"] == "applied"
    assert g2.phase_weeks == custom


# ── build2=0 — mid-cycle FTP test retargets to peak start ────────────────────

def test_build2_zero_ftp_test_retargets_to_peak():
    custom = {"base": 8, "build1": 4, "build2": 0, "peak": 2, "taper": 2}
    g = _event_goal_16w(phase_weeks=dict(custom))
    phases, weeks = _gen(g)
    assert g._phase_weeks_status == "applied"
    names = [p.name for p in phases]
    assert "build2" not in names
    peak = next(p for p in phases if p.name == "peak")
    tests = [s for w in weeks for s in w.sessions
             if s.session_type == "ftp_test"]
    assert tests, "build2=0 must not lose the mid-cycle FTP test"
    assert all(s.day >= peak.start for s in tests)
    assert min((s.day - peak.start).days for s in tests) < 7
