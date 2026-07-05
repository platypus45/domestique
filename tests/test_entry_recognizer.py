"""MODE 2 (IP_PLAN_CONTINUITY B-D3 / B-LOCKED-3) — entry recognizer engine tests.

GB3 fixture matrix: full compliance (credits all) / half volume (stops
streak) / volume-without-shape (QUALIFIES — shape is advisory) / nothing
(0 credit → start fresh) / hr-only rides (tss from the hrTSS cascade) /
compliant-with-stepbacks (week-LEVEL targets incl. ×0.72 discount pass) /
illness gap (1-in-4 tolerated; 2 consecutive misses end the streak) /
archive shorter than runway (capped) / partial current week excluded.

Pinned env (W8 pattern): frozen today = 2026-01-05 (Monday), current_ctl=50.
Hermetic — synthetic rides only, no HOME reads, no network, zero writes.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR


@pytest.fixture(scope="module", autouse=True)
def _env(planner_pinned_env):
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _rides(weekly: dict[int, float], zones: dict | None = None,
           extra_fields: dict | None = None) -> list[dict]:
    """Synthetic archive: window w (whole 7-day windows counted back from the
    frozen today) carries ``weekly[w]`` TSS split over two rides, both dated
    strictly inside [today-7w, today-7(w-1)-1]."""
    out = []
    for w, load in weekly.items():
        for days_back in (7 * w, 7 * w - 3):
            r = {
                "started_at": (ANCHOR - timedelta(days=days_back)).isoformat()
                              + "T09:00:00",
                "tss": load / 2.0,
            }
            if zones:
                r["time_in_zone"] = dict(zones)
            if extra_fields:
                r.update(extra_fields)
            out.append(r)
    return out


def _goal(**kw) -> "tp.Goal":
    base = dict(goal_type="general", plan_weeks=12, hours_per_week=8.0)
    base.update(kw)
    return tp.Goal(**base)


def _scan(goal, rides):
    return tp.recognize_entry(goal, rides, current_ctl=50.0)


EASY_ZONES = {"z1": 1200, "z2": 4000, "z3": 500, "z4": 200, "z5": 100}


# ── full compliance — credits every archive week ─────────────────────────────

def test_full_compliance_credits_all_archive_weeks():
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 7)}, zones=EASY_ZONES))
    assert res["proposal_weeks"] == 6
    assert res["equivalent_start_date"] == (ANCHOR - timedelta(days=42)).isoformat()
    assert len(res["weeks"]) == 6
    assert all(r["qualifies"] for r in res["weeks"])


# ── half volume — the streak stops (and stays anchored at the recent week) ──

def test_recent_half_volume_zeroes_credit():
    """Streak must END at the most recent whole week: 4 compliant weeks
    followed by 2 recent low-volume weeks yield ZERO credit (the two recent
    misses are consecutive for every candidate that could reach the
    compliant block)."""
    weekly = {6: 500, 5: 500, 4: 500, 3: 500, 2: 120, 1: 120}
    res = _scan(_goal(), _rides(weekly))
    assert res["proposal_weeks"] == 0
    assert res["equivalent_start_date"] is None
    # Evidence rows still shown (widest candidate) with the misses flagged.
    assert res["weeks"] and not res["weeks"][-1]["qualifies"]


def test_older_half_volume_stops_backward_extension():
    """Low-volume weeks BEFORE a compliant block stop the backward growth of
    the streak: 3 recent compliant weeks + the 1-in-4 tolerance eating the
    single boundary miss ⇒ credit 4, never the full 6."""
    weekly = {6: 100, 5: 100, 4: 100, 3: 500, 2: 500, 1: 500}
    res = _scan(_goal(), _rides(weekly))
    assert res["proposal_weeks"] == 4
    assert res["equivalent_start_date"] == (ANCHOR - timedelta(days=28)).isoformat()


# ── volume without shape — QUALIFIES (zone shape is advisory only) ──────────

def test_volume_without_shape_qualifies():
    all_intensity = {"z1": 0, "z2": 0, "z3": 0, "z4": 600, "z5": 3000}
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 5)}, zones=all_intensity))
    assert res["proposal_weeks"] == 4
    assert all(r["qualifies"] for r in res["weeks"])
    # ... but the advisory annotation surfaces the shape mismatch.
    assert all(r["shape_note"] == "0% easy riding" for r in res["weeks"])


# ── nothing — zero credit, start fresh ───────────────────────────────────────

def test_no_rides_zero_credit_start_fresh():
    res = _scan(_goal(), [])
    assert res["proposal_weeks"] == 0
    assert res["equivalent_start_date"] is None
    assert res["weeks"] == []


# ── hr-only rides — first-class via the tss cascade ─────────────────────────

def test_hr_only_rides_credit_via_tss_cascade():
    """FIT-sidecar hrTSS rides carry only started_at + tss (+load_source=hr):
    no power, no zones. They must credit exactly like power rides; the shape
    note is simply absent."""
    rides = _rides({w: 500 for w in range(1, 5)},
                   extra_fields={"load_source": "hr"})
    res = _scan(_goal(), rides)
    assert res["proposal_weeks"] == 4
    assert all(r["qualifies"] for r in res["weeks"])
    assert all(r["shape_note"] is None for r in res["weeks"])


def test_hr_zone_dict_drives_advisory_note():
    rides = _rides({1: 500, 2: 500},
                   extra_fields={"hr_time_in_zone": {"z1": 3000, "z2": 3000,
                                                     "z3": 1000, "z4": 500}})
    res = _scan(_goal(), rides)
    assert res["proposal_weeks"] == 2
    assert all(r["shape_note"] == "80% easy riding" for r in res["weeks"])


# ── compliant-with-stepbacks — week-LEVEL targets (×0.72 discount) pass ──────

def test_stepback_weeks_scored_against_discounted_targets():
    """Rider at 0.65× each week's OWN target — including the ×0.72 stepback
    discount — qualifies everywhere. An impl scoring stepback weeks against
    the UNdiscounted phase target would fail them (0.65×0.72 = 0.468 < 0.6)."""
    c = 8
    hyp = _goal(start_date=ANCHOR - timedelta(days=7 * c))
    # Expected week-level targets from the DOCUMENTED contract (public Phase
    # API + the generate_plan stepback cadence), not from the helper under test.
    targets = []
    gw = 0
    for p in tp.generate_phases(hyp, 50.0):
        cur = p.start
        while cur <= p.end:
            gw += 1
            t = float(p.weekly_tss_target)
            if gw % tp.STEP_BACK_EVERY == 0 and p.name != "taper":
                t = float(round(t * 0.72))
            targets.append(t)
            cur += timedelta(weeks=1)
    assert min(targets[:c]) < max(targets[:c]), "no stepback landed in-window"

    weekly = {c - k + 1: 0.65 * targets[k - 1] for k in range(1, c + 1)}
    res = _scan(_goal(), _rides(weekly))
    assert res["proposal_weeks"] == c
    assert all(r["qualifies"] for r in res["weeks"])
    assert [r["target_tss"] for r in res["weeks"]] == [round(t, 1) for t in targets[:c]]


# ── illness gap — 1-in-4 tolerated; 2 consecutive misses end the streak ─────

def test_illness_single_gap_tolerated():
    weekly = {w: 500 for w in range(1, 9)}
    del weekly[5]  # claimed week 4 of 8 — one empty week (illness)
    res = _scan(_goal(), _rides(weekly))
    assert res["proposal_weeks"] == 8
    flags = [r["qualifies"] for r in res["weeks"]]
    assert flags == [True, True, True, False, True, True, True, True]


def test_two_consecutive_misses_end_streak():
    weekly = {w: 500 for w in range(1, 9)}
    del weekly[4]
    del weekly[5]  # two consecutive empty weeks
    res = _scan(_goal(), _rides(weekly))
    # Every candidate containing BOTH empty weeks rejects (consecutive misses
    # are never tolerated) — unlike the single-gap case above, the full 8
    # can't be credited. The streak restarts after the pair: 3 clean recent
    # weeks + the standard 1-in-4 tolerance eating the single boundary miss.
    assert res["proposal_weeks"] == 4
    assert res["equivalent_start_date"] == (ANCHOR - timedelta(days=28)).isoformat()
    assert [r["qualifies"] for r in res["weeks"]] == [False, True, True, True]


# ── archive shorter than runway — capped (and the inverse) ──────────────────

def test_archive_shorter_than_runway_capped():
    res = _scan(_goal(plan_weeks=16), _rides({w: 500 for w in range(1, 5)}))
    assert res["proposal_weeks"] == 4
    assert res["capped"] is True


def test_archive_covering_runway_not_capped():
    res = _scan(_goal(plan_weeks=5), _rides({w: 500 for w in range(1, 9)}))
    assert res["capped"] is False
    # Credit still leaves ≥1 schedulable week: c ≤ runway−1.
    assert res["proposal_weeks"] == 4


# ── partial current week — rides today never count toward a week ────────────

def test_partial_current_week_excluded():
    today_ride = [{"started_at": ANCHOR.isoformat() + "T07:00:00", "tss": 1000}]
    # Today-only archive: no whole week exists → zero credit.
    res = _scan(_goal(), today_ride)
    assert res["proposal_weeks"] == 0
    assert res["weeks"] == []

    # With real past weeks, today's ride still counts toward NO window.
    res = _scan(_goal(), _rides({1: 500, 2: 500}) + today_ride)
    assert res["proposal_weeks"] == 2
    assert [r["actual_tss"] for r in res["weeks"]] == [500.0, 500.0]


# ── response shape + window math ─────────────────────────────────────────────

def test_response_shape_and_window_math():
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 4)}))
    assert set(res.keys()) == {"proposal_weeks", "equivalent_start_date",
                               "capped", "weeks"}
    c = res["proposal_weeks"]
    assert c == 3
    for k, row in enumerate(res["weeks"], start=1):
        assert set(row.keys()) == {"index", "window_start", "actual_tss",
                                   "target_tss", "qualifies", "shape_note"}
        assert row["index"] == k
        # Whole 7-day windows counted back from today — never ISO weeks.
        assert row["window_start"] == \
            (ANCHOR - timedelta(days=7 * (c - k + 1))).isoformat()


# ── event goal — hypothesis rebuilds the FULL runway week budget (H1 parity) ─

def test_event_goal_hypothesis_uses_full_runway():
    goal = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=56),
                   event_name="TestFondo", event_km=150.0, event_climb_m=1500.0,
                   plan_weeks=8, hours_per_week=8.0)  # today-anchored form value
    res = _scan(goal, _rides({w: 500 for w in range(1, 5)}))
    assert res["proposal_weeks"] == 4
    assert res["equivalent_start_date"] == (ANCHOR - timedelta(days=28)).isoformat()
    assert res["capped"] is True  # archive 4 < runway−1 = 7
    assert all(r["target_tss"] > 0 for r in res["weeks"])


# ── read-only guarantee — zero RNG side effects ──────────────────────────────

def test_scan_is_rng_pure():
    random.seed(123456)
    state = random.getstate()
    _scan(_goal(), _rides({w: 500 for w in range(1, 7)}))
    assert random.getstate() == state
