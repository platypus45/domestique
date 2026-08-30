"""Deleting a ride on intervals.icu deletes it here too — carefully.

Sync only ever upserted, so an activity removed upstream lived on locally
forever. The reported case: one ride uploaded twice (Karoo head unit and
Garmin), synced as two activities, one copy deleted on intervals.icu — and
the calendar kept showing both after a resync.

The pruner's safety invariants matter more than the pruning:
  * only ICU records — a rider's own FIT imports are never candidates;
  * only inside the just-fetched window, shrunk a day at the old edge, so a
    fetch says nothing about rides it could not have returned;
  * a mass disappearance is treated as a broken fetch, not as truth — no
    rider deletes 20+ activities between syncs (the v3.3.0 lesson: infra
    failure must never be applied per-record).
"""
from __future__ import annotations

import datetime
import json

import pytest

import ride_storage as rs


@pytest.fixture()
def icu_dir(tmp_path, monkeypatch):
    d = tmp_path / "rides" / "icu"
    d.mkdir(parents=True)
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: d)
    return d


def _rec(icu_dir, rid, days_ago):
    day = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    (icu_dir / f"{rid}.json").write_text(json.dumps({
        "ride_id": f"icu_{rid}", "source": "icu", "name": "Ride",
        "started_at": f"{day}T10:00:00", "tss": 60, "duration_s": 3600,
    }), encoding="utf-8")


def test_upstream_deleted_ride_is_pruned(icu_dir):
    _rec(icu_dir, "i111", days_ago=2)   # still on ICU
    _rec(icu_dir, "i222", days_ago=2)   # the deleted double upload
    n = rs.prune_deleted_icu_records({"i111"}, window_days=90)
    assert n == 1
    assert (icu_dir / "i111.json").exists()
    assert not (icu_dir / "i222.json").exists()


def test_rides_outside_the_window_are_never_touched(icu_dir):
    """A 7-day incremental fetch says nothing about a 30-day-old ride."""
    _rec(icu_dir, "i333", days_ago=30)
    n = rs.prune_deleted_icu_records({"i999"}, window_days=7)
    assert n == 0
    assert (icu_dir / "i333.json").exists()


def test_window_edge_gets_a_day_of_slack(icu_dir):
    """A ride exactly window_days old straddles timezones — never delete it."""
    _rec(icu_dir, "i444", days_ago=90)
    n = rs.prune_deleted_icu_records(set(), window_days=90)
    assert n == 0
    assert (icu_dir / "i444.json").exists()


def test_mass_disappearance_is_treated_as_a_broken_fetch(icu_dir):
    """25 in-window records all missing from the fetch = a payload that parsed
    to nothing, not a rider clearing their archive. Prune NOTHING."""
    for i in range(25):
        _rec(icu_dir, f"i5{i:02d}", days_ago=3)
    n = rs.prune_deleted_icu_records(set(), window_days=90)
    assert n == 0
    assert len(list(icu_dir.glob("*.json"))) == 25


def test_empty_fetch_prunes_a_small_inwindow_remainder(icu_dir):
    """An empty SUCCESSFUL fetch is valid: the rider deleted their recent
    rides upstream. A couple of records is a plausible deletion, not infra."""
    _rec(icu_dir, "i601", days_ago=2)
    _rec(icu_dir, "i602", days_ago=4)
    n = rs.prune_deleted_icu_records(set(), window_days=90)
    assert n == 2


def test_unreadable_record_is_skipped_not_deleted(icu_dir):
    (icu_dir / "i700.json").write_text("{not json", encoding="utf-8")
    n = rs.prune_deleted_icu_records(set(), window_days=90)
    assert n == 0
    assert (icu_dir / "i700.json").exists()
