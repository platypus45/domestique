"""v1.6.3 — ride_storage.list_rides survives 'id' missing from a Strava export.

The user's boot log showed two WARN entries:
    Failed to load ride .../ride_2026-04-18T08-42-44.strava.json: 'id'
    Failed to load ride .../ride_2026-04-18T08-39-34.strava.json: 'id'

Cause: ``data["id"]`` raised KeyError for old Strava export records that
predate the post-v1.0 'id' field. Pre-v1.6.3 the broad except logged a
WARN per record (deduped per filename) and skipped the ride. v1.6.3
falls back to ``f.stem`` as the id so the ride loads cleanly, and only
skips records that lack the calendar-essential ``started_at`` /
``finished_at`` timestamps -- those become a single INFO log per file,
not a WARN.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import ride_storage


def _make_ride_file(rides_dir: Path, name: str, data: dict) -> Path:
    p = rides_dir / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_list_rides_uses_filename_when_id_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ride_storage, "_rides_dir", lambda: tmp_path)
    ride_storage._RIDE_LOAD_WARNED.clear()

    _make_ride_file(tmp_path, "ride_2026-04-18T08-42-44.strava", {
        # no 'id' field
        "started_at": "2026-04-18T08:42:44Z",
        "finished_at": "2026-04-18T09:42:44Z",
        "ride_type": "free",
    })

    rides = ride_storage.list_rides()

    assert len(rides) == 1
    assert rides[0]["id"] == "ride_2026-04-18T08-42-44.strava"
    assert rides[0]["started_at"] == "2026-04-18T08:42:44Z"


def test_list_rides_skips_ride_without_timestamps(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ride_storage, "_rides_dir", lambda: tmp_path)
    ride_storage._RIDE_LOAD_WARNED.clear()

    _make_ride_file(tmp_path, "ride_broken", {
        # no id, no timestamps -> calendar can't place it
        "ride_type": "free",
    })
    _make_ride_file(tmp_path, "ride_good", {
        "id": "good",
        "started_at": "2026-05-13T10:00:00Z",
        "finished_at": "2026-05-13T11:00:00Z",
    })

    import logging
    with caplog.at_level(logging.WARNING, logger="domestique.rides"):
        rides = ride_storage.list_rides()

    ids = [r["id"] for r in rides]
    assert "good" in ids
    assert "ride_broken" not in ids
    # The broken record must NOT produce a WARN -- it gets an INFO-level
    # skip so we don't spam the diag ring on every dashboard refresh.
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert all("ride_broken" not in r.getMessage() for r in warns)


def test_list_rides_no_keyerror_in_warn_log(tmp_path, monkeypatch, caplog):
    """Regression: the literal `'id'` KeyError must not reach the WARN log."""
    monkeypatch.setattr(ride_storage, "_rides_dir", lambda: tmp_path)
    ride_storage._RIDE_LOAD_WARNED.clear()

    _make_ride_file(tmp_path, "ride_2026-04-18T08-42-44.strava", {
        "started_at": "2026-04-18T08:42:44Z",
        "finished_at": "2026-04-18T09:42:44Z",
    })

    import logging
    with caplog.at_level(logging.WARNING, logger="domestique.rides"):
        ride_storage.list_rides()

    for rec in caplog.records:
        assert "'id'" not in rec.getMessage(), \
            f"KeyError('id') leaked into WARN log: {rec.getMessage()}"
