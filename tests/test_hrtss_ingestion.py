"""W4 (v2.5.0) — hrTSS/TRIMP ingestion for locally imported FITs.

POST /api/ride/import used to persist raw FIT bytes only, so imported rides
contributed 0 to CTL/ATL/weekly-actual. W4 computes a load summary at
ingestion (power TSS / local hrTSS) and persists it as a ``<stem>.load.json``
sidecar; ``load_all_rides`` attaches ``tss`` + ``load_source`` so the
existing readers (``compute_local_atl``, ``recent_mean_weekly_tss``) pick
imported FITs up without any reader edits.

Synthetic FITs are built with the bundled ``fit_tool`` (same dependency the
app writes/reads FITs with). ProfileManager is stubbed (pattern:
tests/test_hr_mode_api.py::_StubPM) — tests never touch the real athlete.json.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import profile_manager as pm_mod  # noqa: E402
import ride_storage  # noqa: E402

FTP = 250
LTHR = 160
MAX_HR = 190


class _StubPM:
    """Minimal ProfileManager stand-in (see tests/test_hr_mode_api.py)."""

    def __init__(self, athlete):
        self._athlete = dict(athlete)

    @property
    def ftp(self):
        return self._athlete.get("ftp", FTP)

    @property
    def lthr(self):
        return self._athlete.get("lthr", LTHR)

    @property
    def max_hr(self):
        return self._athlete.get("max_hr", MAX_HR)

    def __getattr__(self, name):  # config proxies arbitrary athlete attrs
        return None


def _stub_pm(monkeypatch, athlete=None):
    stub = _StubPM(athlete or {"ftp": FTP, "lthr": LTHR, "max_hr": MAX_HR})
    monkeypatch.setattr(
        pm_mod.ProfileManager, "get", classmethod(lambda cls: stub)
    )
    return stub


@pytest.fixture
def stub_pm(monkeypatch):
    return _stub_pm(monkeypatch)


@pytest.fixture
def fit_dir(tmp_path, monkeypatch):
    """Redirect the FIT + ICU ride dirs into tmp and empty the legacy
    archive (pattern: tests/test_calendar_icu_sync.py::_IcuSyncBase)."""
    d = tmp_path / "rides"
    icu = d / "icu"
    icu.mkdir(parents=True)
    monkeypatch.setattr(ride_storage, "_fit_rides_dir", lambda: d)
    monkeypatch.setattr(ride_storage, "_icu_rides_dir", lambda: icu)
    monkeypatch.setattr(ride_storage, "list_rides", lambda: [])
    return d


def _build_fit(path, *, n, power=None, hr=None, start_ms=None, file_tss=None):
    """Craft a minimal FIT activity: FileId + n RecordMessages + Session.

    ``power`` / ``hr`` may be a constant or a per-sample list; None omits
    the field entirely (a true no-power / no-HR file, not zeros).
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    b = FitFileBuilder()
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.serial_number = 1
    b.add(fid)

    base_ms = start_ms if start_ms is not None else 1_750_000_000_000
    for i in range(n):
        rec = RecordMessage()
        rec.timestamp = base_ms + i * 1000
        if power is not None:
            rec.power = int(power[i]) if isinstance(power, (list, tuple)) else int(power)
        if hr is not None:
            rec.heart_rate = int(hr[i]) if isinstance(hr, (list, tuple)) else int(hr)
        b.add(rec)

    s = SessionMessage()
    s.start_time = base_ms
    s.timestamp = base_ms + n * 1000
    s.total_timer_time = float(n)
    s.total_elapsed_time = float(n)
    if file_tss is not None:
        s.training_stress_score = float(file_tss)
    b.add(s)

    path.write_bytes(b.build().to_bytes())
    return path


def _fit_entries(rides):
    return [r for r in rides if r.get("source") == "fit"]


# ── hrTSS: no power, HR present ──────────────────────────────────────────────

def test_hr_only_fit_gets_hrtss(fit_dir, stub_pm):
    """No-power + HR ride gets hrTSS within ±1 of the hand-computed formula."""
    n = 600
    hr_series = [140] * 300 + [170] * 300
    _build_fit(fit_dir / "hr_only.fit", n=n, hr=hr_series)

    rides = ride_storage.load_all_rides()
    (entry,) = _fit_entries(rides)

    # Hand-computed, independently of compute_hr_tss:
    # hrTSS = Σ((hr/lthr)²)/3600 × 100 over samples with hr>0.
    expected = sum((h / LTHR) ** 2 for h in hr_series) / 3600.0 * 100.0
    assert entry.get("tss") is not None
    assert abs(entry["tss"] - expected) <= 1.0
    assert entry["load_source"] == "hr_local"
    # Sidecar persisted next to the FIT (compute-once).
    side = fit_dir / "hr_only.load.json"
    assert side.exists()
    assert json.loads(side.read_text())["load_source"] == "hr_local"


def test_hr_clamped_to_max_hr(fit_dir, stub_pm):
    """Samples above max_hr are clamped before entering the formula."""
    n = 600
    _build_fit(fit_dir / "hr_spike.fit", n=n, hr=200)  # 200 > max_hr 190

    rides = ride_storage.load_all_rides()
    (entry,) = _fit_entries(rides)

    expected = n * (MAX_HR / LTHR) ** 2 / 3600.0 * 100.0  # clamped at 190
    assert abs(entry["tss"] - expected) <= 1.0
    unclamped = n * (200 / LTHR) ** 2 / 3600.0 * 100.0
    assert abs(entry["tss"] - unclamped) > 1.0  # clamp actually engaged


# ── power TSS ────────────────────────────────────────────────────────────────

def test_power_fit_gets_np_tss(fit_dir, stub_pm):
    """Power ride gets NP-based TSS = dur_h × (NP/FTP)² × 100 (constant power
    → NP == power exactly, so the expectation is hand-computable)."""
    n = 600
    _build_fit(fit_dir / "power.fit", n=n, power=200, hr=150)

    rides = ride_storage.load_all_rides()
    (entry,) = _fit_entries(rides)

    expected = (n / 3600.0) * (200 / FTP) ** 2 * 100.0  # 10.67
    assert abs(entry["tss"] - expected) <= 1.0
    # Power wins over HR when both are present.
    assert entry["load_source"] == "power"


def test_power_fit_file_tss_unchanged(fit_dir, stub_pm):
    """A FIT whose producing app stored training_stress_score keeps that
    number (pre-existing behavior: _parse_fit_stats/_build_fit_normalized
    already display the file value — W4 must not introduce a second one)."""
    _build_fit(fit_dir / "power_filetss.fit", n=600, power=200, file_tss=80.0)

    rides = ride_storage.load_all_rides()
    (entry,) = _fit_entries(rides)

    assert entry["tss"] == 80.0  # file value, NOT the NP-computed 10.7
    assert entry["load_source"] == "power"


def test_fit_entry_keeps_preexisting_shape(fit_dir, stub_pm):
    """W4 only ADDS keys to the load_all_rides FIT entry — every pre-W4 key
    is still present (readers untouched)."""
    _build_fit(fit_dir / "shape.fit", n=60, power=200)

    (entry,) = _fit_entries(ride_storage.load_all_rides())
    for key in ("ride_id", "source", "external_id", "name", "started_at",
                "duration_s", "size_bytes", "_fit_path"):
        assert key in entry, f"pre-existing key {key} missing"
    assert entry["ride_id"] == "fit_shape"
    assert entry["source"] == "fit"


# ── neither power nor HR ─────────────────────────────────────────────────────

def test_no_power_no_hr_gets_no_tss(fit_dir, stub_pm):
    """No load signal → no tss key (unchanged), definitive sidecar marker."""
    _build_fit(fit_dir / "bare.fit", n=60)  # records carry timestamps only

    (entry,) = _fit_entries(ride_storage.load_all_rides())
    assert "tss" not in entry
    assert "load_source" not in entry
    # Definitive marker persisted so the file is never re-parsed.
    marker = json.loads((fit_dir / "bare.load.json").read_text())
    assert marker["tss"] is None


# ── sidecar lifecycle ────────────────────────────────────────────────────────

def test_sidecar_computed_once_then_reused(fit_dir, stub_pm, monkeypatch):
    """Second listing reads the sidecar — no FIT re-parse."""
    _build_fit(fit_dir / "once.fit", n=600, hr=150)
    (first,) = _fit_entries(ride_storage.load_all_rides())
    assert first["tss"] is not None

    import fit_activity

    def _boom(_path):
        raise AssertionError("FIT re-parsed despite existing sidecar")

    monkeypatch.setattr(fit_activity, "parse_record_streams", _boom)
    (second,) = _fit_entries(ride_storage.load_all_rides())
    assert second["tss"] == first["tss"]
    assert second["load_source"] == first["load_source"]


def test_missing_profile_retries_later(fit_dir, monkeypatch):
    """FTP not set → nothing persisted (no frozen zero); once the profile is
    complete the next listing computes + persists."""
    _stub_pm(monkeypatch, {"ftp": 0, "lthr": 0, "max_hr": 0})
    _build_fit(fit_dir / "retry.fit", n=600, power=200)

    (entry,) = _fit_entries(ride_storage.load_all_rides())
    assert "tss" not in entry
    assert not (fit_dir / "retry.load.json").exists()

    _stub_pm(monkeypatch)  # profile now configured
    (entry,) = _fit_entries(ride_storage.load_all_rides())
    assert entry["tss"] is not None
    assert (fit_dir / "retry.load.json").exists()


# ── ICU normalization tag (pure tag, zero numeric change) ───────────────────

def _icu_activity(**extra):
    a = {
        "id": "t1",
        "name": "ride",
        "start_date_local": "2026-07-01T10:00:00",
        "elapsed_time": 3600,
        "moving_time": 3600,
    }
    a.update(extra)
    return a


def test_icu_power_ride_tagged_icu():
    norm = ride_storage._normalize_icu_activity(
        _icu_activity(icu_training_load=65, icu_pm_p_avg=180)
    )
    assert norm["tss"] == 65.0  # numeric value untouched
    assert norm["load_source"] == "icu"


def test_icu_no_power_ride_tagged_hr_icu():
    norm = ride_storage._normalize_icu_activity(
        _icu_activity(icu_training_load=65, average_heartrate=150)
    )
    assert norm["tss"] == 65.0
    assert norm["avg_power_w"] is None
    assert norm["load_source"] == "hr_icu"


def test_icu_no_load_no_tag():
    norm = ride_storage._normalize_icu_activity(_icu_activity())
    assert norm["tss"] is None
    assert norm["load_source"] is None


# ── the actual readers pick imported FITs up (no reader edits) ──────────────

def test_readers_pick_up_fit_tss(fit_dir, stub_pm):
    """compute_local_atl + recent_mean_weekly_tss (the real reader functions)
    now see the imported FIT's tss via load_all_rides."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    start_local = dt.datetime.combine(yesterday, dt.time(12, 0)).astimezone()
    n = 600
    _build_fit(
        fit_dir / "reader.fit", n=n, hr=150,
        start_ms=int(start_local.timestamp() * 1000),
    )

    rides = ride_storage.load_all_rides()
    (entry,) = _fit_entries(rides)
    tss = entry["tss"]
    assert tss is not None
    # Enrichment for the existing FIT-vs-ICU dedupe: true ride date + duration.
    assert entry["started_at"][:10] == yesterday.isoformat()
    assert entry["duration_s"] == n

    atl = ride_storage.compute_local_atl(rides, today=dt.date.today())
    # Ride yesterday: EWMA τ=7 → tss/7 yesterday, ×6/7 today.
    assert atl == pytest.approx(round((tss / 7.0) * (6.0 / 7.0), 1), abs=0.11)

    weekly = ride_storage.recent_mean_weekly_tss(extra_rides=rides)
    assert weekly == pytest.approx(tss, abs=0.11)


def test_fit_dedupes_against_icu_copy(fit_dir, stub_pm):
    """Once the FIT entry carries its true date + duration, the EXISTING
    dedupe drops it in favor of the ICU copy — the relayed ride's TSS is
    counted once, not twice."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    start_local = dt.datetime.combine(yesterday, dt.time(12, 0)).astimezone()
    n = 600
    _build_fit(
        fit_dir / "relayed.fit", n=n, hr=150,
        start_ms=int(start_local.timestamp() * 1000),
    )
    icu_rec = {
        "ride_id": "icu_r1",
        "source": "icu",
        "external_id": "r1",
        "started_at": start_local.isoformat(),
        "duration_s": n,
        "tss": 15.0,
        "load_source": "hr_icu",
    }
    (fit_dir / "icu" / "r1.json").write_text(json.dumps(icu_rec))

    rides = ride_storage.load_all_rides()
    assert [r["ride_id"] for r in rides] == ["icu_r1"]
    total_tss = sum(r.get("tss") or 0 for r in rides)
    assert total_tss == 15.0  # not double-counted
