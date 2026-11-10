"""Setup wizard: a weight filled in from intervals.icu follows intervals.icu.

Owner decision 2026-11-10. The wizard's auto-fill marks the weight as coming
from intervals.icu; saved unedited, it is stamped "icu" (like the LTHR hint)
and later syncs keep it in step with the scale. A typed weight stays the
rider's. The auto-fill uses the same "intervals.icu weight" as the follow
(3.14.0 grill M5: the newest scale reading of 30 days, else the athlete
setting), so the first sync does not move the number the rider confirmed.
The filled-in weight is not logged as the rider's own history row either:
that row would keep today's scale reading out of the history, and the follow
would then land on an older reading.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module                      # noqa: E402
import db                                     # noqa: E402
import training as training_module            # noqa: E402
from test_icu_push import stub, _apikey       # noqa: E402,F401  (fixture)

DAY = date(2026, 11, 10)
SETUP = ROOT / "src" / "templates" / "setup.html"


def _iso(days_ago=0):
    return (DAY - timedelta(days=days_ago)).isoformat()


def _rec(days_ago=0, **fields):
    return {"id": _iso(days_ago), "ctl": 40.0, "atl": 38.0, **fields}


def _sync(rows):
    with mock.patch.object(db, "fetch_wellness", return_value=rows):
        return db.sync_wellness(days=60)


def _athlete(stub):
    return json.loads((stub.pm.active_dir / "athlete.json").read_text(encoding="utf-8"))


def _setup_save(rider, **body):
    r = rider.client.post("/api/setup/save", json=body)
    assert r.status_code == 200, r.text
    return r


def _settings_rows():
    return [r for r in db.query_metric_history("weight", 365) if r["source"] == "settings"]


@pytest.fixture()
def rider(stub, freeze_clock):
    freeze_clock(DAY)
    return stub


# ── the save ─────────────────────────────────────────────────────────────────

def test_a_filled_in_weight_follows_intervals_icu(rider):
    _setup_save(rider, weight=72.4, weight_source_hint="icu")
    a = _athlete(rider)
    assert (a["weight_kg"], a["weight_source"]) == (72.4, "icu")
    assert rider.client.get("/api/settings").json()["weight_follow_icu"] is True
    _sync([_rec(0, weight=71.6)])
    assert rider.pm._athlete["weight_kg"] == 71.6, "the next scale reading is followed"


def test_the_first_sync_keeps_the_number_the_rider_confirmed(rider):
    # The wizard filled in today's scale reading, 72.4; last week's was 73.4.
    _setup_save(rider, weight=72.4, weight_source_hint="icu")
    assert _settings_rows() == [], "an intervals.icu weight was logged as the rider's row"
    _sync([_rec(7, weight=73.4), _rec(0, weight=72.4)])
    rows = {r["date"]: r for r in db.query_metric_history("weight", 365)}
    assert rows[_iso(0)]["source"] == "intervals.icu", "today's reading was kept out"
    assert rider.pm._athlete["weight_kg"] == 72.4, "the follow landed on an older reading"


def test_a_typed_weight_stays_the_riders(rider):
    _setup_save(rider, weight=68.0)
    assert _athlete(rider)["weight_source"] == "manual"
    assert [r["value"] for r in _settings_rows()] == [68.0], "a typed weight is still logged"
    _sync([_rec(0, weight=72.4)])
    assert rider.pm._athlete["weight_kg"] == 68.0


def test_a_hint_without_a_weight_moves_nothing(rider):
    before = _athlete(rider).get("weight_source")
    _setup_save(rider, ftp=250, weight_source_hint="icu")
    assert _athlete(rider).get("weight_source") == before


def test_an_untick_in_the_same_body_wins(rider):
    r = rider.client.post("/api/settings", json={
        "weight": 68.0, "weight_follow_icu": False, "weight_source_hint": "icu"})
    assert r.status_code == 200, r.text
    assert _athlete(rider)["weight_source"] == "manual"


@pytest.mark.parametrize("hint", ["manual", "intervals.icu", "yes"])
def test_only_the_icu_hint_counts(rider, hint):
    r = rider.client.post("/api/settings", json={"weight": 68.0, "weight_source_hint": hint})
    assert r.status_code == 200, r.text
    assert _athlete(rider)["weight_source"] == "manual"


# ── the auto-fill ────────────────────────────────────────────────────────────

def _numbers(rider, monkeypatch, wellness, setting=75.0):
    _apikey(rider)
    monkeypatch.setattr(training_module, "_get", lambda path, *a, **k: {
        "weight": setting,
        "sportSettings": [{"types": ["Ride"], "ftp": 250, "lthr": 160, "max_hr": 190}]})
    asked = []

    def fake_wellness(days):
        asked.append(days)
        return wellness
    monkeypatch.setattr(app_module, "fetch_wellness", fake_wellness)
    app_module.clear_cache()
    out = rider.client.get("/api/icu/athlete-numbers").json()
    return out, asked


def test_the_auto_fill_uses_the_weight_the_follow_uses(rider, monkeypatch):
    out, asked = _numbers(rider, monkeypatch, [_rec(40, weight=70.1), _rec(3, weight=72.4)])
    assert out["ok"] is True and out["weight"] == 72.4
    assert asked == [db.ICU_WEIGHT_RECENT_DAYS]
    out, _ = _numbers(rider, monkeypatch, [_rec(40, weight=70.1)])
    assert out["weight"] == 75.0, "no reading in the window: the athlete setting"
    out, _ = _numbers(rider, monkeypatch, [])
    assert out["weight"] == 75.0


def test_the_auto_fill_survives_a_failed_wellness_fetch(rider, monkeypatch):
    _numbers(rider, monkeypatch, [])

    def boom(days):
        raise training_module.ICUNetworkError("offline")
    monkeypatch.setattr(app_module, "fetch_wellness", boom)
    out = rider.client.get("/api/icu/athlete-numbers").json()
    assert out["ok"] is True and out["weight"] == 75.0 and out["ftp"] == 250


# ── the wizard page ──────────────────────────────────────────────────────────

def test_the_wizard_sends_the_hint_only_for_an_unedited_filled_in_weight():
    src = SETUP.read_text(encoding="utf-8")
    assert ("if ('weight' in payload && _icuOrigin.has('s-weight')) "
            "payload.weight_source_hint = 'icu';") in src
    assert "Your weight will keep following intervals.icu" in src
