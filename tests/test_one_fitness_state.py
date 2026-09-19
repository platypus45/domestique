"""CTL, ATL and TSB have one owner, and it is intervals.icu (week-view contract
P7, P10 and A8; the owner's decision, 2026-09-14: "CTL values from
intervals.icu").

Before: the readiness card merged live ICU values with a local EWMA over the
FIT-only ride list, per field, and could label the result "mixed"; the
composite readiness read TSB from the SQLite wellness table; the calendar's
actual CTL read the ICU wellness cache, then the local EWMA; and the calendar's
planned-CTL curve started from that local EWMA -- None for a rider whose rides
arrive from intervals.icu -- and then from a constant 37, so a rider generated
at CTL 29.6 was graded against a band built from 37 (the audit's S-2, S-3).

Now `fitness.state()`: intervals.icu live; else intervals.icu's last values as
the SQLite wellness table holds them, labelled with their date; else unknown.
The planned curve starts from the plan's own anchor, the ICU CTL stamped when
it was generated (plan["ctl_snapshot"]), on the day it was generated."""
from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cache
import clock
import db

TODAY = date(2026, 9, 16)


def _wellness_rows(ctl, atl, days=20, end=TODAY):
    return [{"id": (end - timedelta(days=i)).isoformat(), "ctl": ctl, "atl": atl,
             "sportInfo": []} for i in range(days - 1, -1, -1)]


def _seed_sqlite(ctl, atl, on):
    conn = db.get_db()
    conn.execute("DELETE FROM wellness")
    for i in range(35):                  # the composite scores from 30 days
        d = (on - timedelta(days=i)).isoformat()
        conn.execute("INSERT OR REPLACE INTO wellness (date, ctl, atl) VALUES (?, ?, ?)", (d, ctl, atl))
    conn.commit()


@pytest.fixture()
def env(tmp_path):
    clock.freeze(TODAY)
    plan_start = TODAY - timedelta(days=2)
    sessions = [{"day": (plan_start + timedelta(days=i)).isoformat(), "session_type": "rest",
                 "duration_min": 0, "tss_estimate": 0, "zwo_file": "", "status": "pending"}
                for i in range(7)]
    plan = {"goal": {"type": "general"}, "phases": [],
            "weeks": [{"week_num": 1, "start": plan_start.isoformat(),
                       "end": (plan_start + timedelta(days=6)).isoformat(), "phase": "base",
                       "tss_target": 0, "is_stepback": False, "sessions": sessions}],
            "ctl_snapshot": {"current_ctl": 55.0, "recent_weekly_tss": 300.0,
                             "generated_on": plan_start.isoformat()}}
    (tmp_path / "current_plan.json").write_text(json.dumps(plan))
    patches = [patch.object(app_module, "_plan_dir", return_value=tmp_path),
               patch.object(app_module, "_kick_lazy_icu_sync", return_value=False),
               patch("ride_storage.compute_local_ctl", return_value=99.0)]   # must never be read
    for p in patches:
        p.start()
    cache.clear_cache()
    yield TestClient(app_module.app)
    for p in reversed(patches):
        p.stop()
    db.get_db().execute("DELETE FROM wellness")
    db.get_db().commit()
    cache.clear_cache()
    clock.unfreeze()


def _readers(client):
    r = client.get("/api/readiness").json()["training"]
    summary = client.get("/api/calendar").json()["summary"]
    # /api/readiness/composite was a third reader of the same state; it was
    # deleted in the 2026-09-16 slimming (the dashboard stopped calling it in
    # v2.2.5). The point of this test -- every reader agrees -- is unchanged.
    return {"readiness": (r["ctl"], r["atl"], r["tsb"]),
            "readiness_source": r.get("source"),
            "calendar_ctl": summary.get("ctl_actual"),
            "planned_today": summary.get("ctl_planned_today")}


def _icu(ctl, atl, raise_=False):
    def fetch(days=42, **k):
        if raise_:
            from training import ICUNetworkError
            raise ICUNetworkError("down")
        return _wellness_rows(ctl, atl, days=min(days, 20))
    return [patch("training.fetch_wellness", side_effect=fetch),
            patch.object(app_module, "fetch_wellness", side_effect=fetch),
            patch("training._require_credentials", return_value=None),
            patch("training.fetch_activities", return_value=[])]


def _run(client, patches):
    for p in patches:
        p.start()
    try:
        cache.clear_cache()
        return _readers(client)
    finally:
        for p in reversed(patches):
            p.stop()


def test_icu_live_is_every_readers_answer(env):
    _seed_sqlite(40.0, 30.0, TODAY)                     # a stale store must not win
    got = _run(env, _icu(61.0, 70.0))
    assert got["readiness"] == (61.0, 70.0, -9.0)
    assert got["readiness_source"] == "icu"
    assert got["calendar_ctl"] == 61.0


def test_icu_down_reads_icus_last_values(env):
    _seed_sqlite(40.0, 30.0, TODAY - timedelta(days=1))
    got = _run(env, _icu(0, 0, raise_=True))
    assert got["readiness"] == (40.0, 30.0, 10.0)
    assert got["readiness_source"] == "icu_cached"
    assert got["calendar_ctl"] == 40.0


def test_nothing_known_is_unknown_not_a_guess(env):
    got = _run(env, _icu(0, 0, raise_=True))
    assert got["readiness"] == (None, None, None)
    assert got["readiness_source"] == "none"
    assert got["calendar_ctl"] is None


def test_the_planned_curve_starts_from_the_plans_anchor(env):
    """Seven rest days from a CTL of 55: two days in, the plan expects about
    52.5, never the 37 the curve used to start from."""
    got = _run(env, _icu(61.0, 70.0))
    assert got["planned_today"] is not None
    assert 50.0 < got["planned_today"] < 55.0, got["planned_today"]
