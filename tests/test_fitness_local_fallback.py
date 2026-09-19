"""C13 (v3.12.0) — a rider without intervals.icu plans from their own rides.

fitness.state() answers intervals.icu live, then its cached values, then --
only for a profile with NO intervals.icu connection -- the local archive
(compute_local_ctl / compute_local_atl over imported rides), then unknown.
An ICU rider never gets the local number: their missing value is unknown.
"""
from datetime import date
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitness  # noqa: E402
import training  # noqa: E402
import ride_storage  # noqa: E402


def _raise_missing():
    raise training.ICUCredentialsMissing("no icu")


def _no_cache(monkeypatch):
    monkeypatch.setattr(fitness, "_cached_row", lambda today: None)


def test_no_icu_rider_gets_local_ctl_atl(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(training, "_require_credentials", _raise_missing)
    monkeypatch.setattr(ride_storage, "compute_local_ctl", lambda *a, **k: 48.4)
    monkeypatch.setattr(ride_storage, "compute_local_atl", lambda *a, **k: 61.0)
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda *a, **k: [])
    st = fitness.state(None, date(2026, 9, 16))
    assert (st["ctl"], st["atl"], st["tsb"], st["source"]) == (48.4, 61.0, -12.6, "local")
    assert st["as_of"] == "2026-09-16"


def test_icu_rider_with_nothing_cached_stays_unknown(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(training, "_require_credentials", lambda: None)          # connected
    monkeypatch.setattr(ride_storage, "compute_local_ctl", lambda *a, **k: 48.4)  # archive would answer...
    monkeypatch.setattr(ride_storage, "compute_local_atl", lambda *a, **k: 61.0)
    monkeypatch.setattr(ride_storage, "load_all_rides", lambda *a, **k: [])
    st = fitness.state(None, date(2026, 9, 16))
    assert st["source"] == "none" and st["ctl"] is None                            # ...but is not asked


def test_no_icu_rider_with_an_empty_archive_is_unknown(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(training, "_require_credentials", _raise_missing)
    monkeypatch.setattr(ride_storage, "compute_local_ctl", lambda *a, **k: None)
    st = fitness.state(None, date(2026, 9, 16))
    assert st["source"] == "none"


def test_icu_live_still_wins(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(training, "_require_credentials", _raise_missing)
    st = fitness.state({"ctl": 70, "atl": 65}, date(2026, 9, 16))
    assert (st["source"], st["tsb"]) == ("icu", 5.0)
