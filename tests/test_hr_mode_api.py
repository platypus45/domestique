"""hr target_mode API surface (IP_HR_ONLY C15/C16/C17-detail).

Covers the gate on POST /api/settings and the per-segment hr payload on the
workout-detail endpoint. ProfileManager is stubbed — tests never touch the
real athlete.json.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_mod  # noqa: E402
import profile_manager as pm_mod  # noqa: E402


class _StubPM:
    """Minimal ProfileManager stand-in for target-mode reads. config.py proxies
    arbitrary athlete attrs through ProfileManager.get(), so unknown attribute
    reads fall back to a benign default instead of AttributeError-ing the app."""

    def __init__(self, athlete):
        self._athlete = dict(athlete)

    @property
    def ftp(self):
        return self._athlete.get("ftp", 250)

    @property
    def weight_kg(self):
        return self._athlete.get("weight_kg", 70.0)

    @property
    def lbm_kg(self):
        return self._athlete.get("lbm_kg", 56.0)

    def __getattr__(self, name):  # config proxies (weight_kg, rhr_baseline, …)
        if name.startswith(("get_", "record_", "_set")) or name in ("on_switch",):
            return lambda *a, **k: None
        return None

    @property
    def lthr(self):
        return self._athlete.get("lthr", 170)

    @property
    def lthr_is_set(self):
        return self._athlete.get("lthr") is not None

    @property
    def max_hr(self):
        return self._athlete.get("max_hr") or 190

    @property
    def target_mode(self):
        mode = self._athlete.get("target_mode", "power")
        if mode == "hr" and (not self.lthr_is_set or self.max_hr <= self.lthr):
            return "power"
        return mode

    def save_athlete(self, data):
        self._athlete.update(data)


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def _stub(monkeypatch, athlete):
    stub = _StubPM(athlete)
    monkeypatch.setattr(pm_mod.ProfileManager, "get", classmethod(lambda cls: stub))
    return stub


# ── C15: the gate is real (default-170 lthr must NOT satisfy it) ─────────────

def test_settings_rejects_hr_mode_without_explicit_lthr(client, monkeypatch):
    _stub(monkeypatch, {"max_hr": 190})  # no lthr key at all
    r = client.post("/api/settings", json={"target_mode": "hr"})
    assert r.status_code == 400
    assert "LTHR" in r.json()["detail"]


def test_settings_rejects_max_hr_below_lthr(client, monkeypatch):
    _stub(monkeypatch, {"lthr": 175, "max_hr": 170})
    r = client.post("/api/settings", json={"target_mode": "hr"})
    assert r.status_code == 400
    assert "max HR" in r.json()["detail"]


def test_settings_accepts_hr_mode_with_same_request_lthr(client, monkeypatch):
    stub = _stub(monkeypatch, {"max_hr": 190})
    r = client.post("/api/settings", json={"target_mode": "hr", "lthr": 160})
    assert r.status_code == 200
    assert stub._athlete["target_mode"] == "hr"


def test_settings_rejects_unknown_mode(client, monkeypatch):
    _stub(monkeypatch, {"lthr": 160, "max_hr": 190})
    r = client.post("/api/settings", json={"target_mode": "watts"})
    assert r.status_code == 400


# ── degraded read: broken invariant never reports hr mode ────────────────────

def test_target_mode_degrades_to_power_on_broken_invariant():
    assert _StubPM({"target_mode": "hr", "lthr": 175, "max_hr": 170}).target_mode == "power"
    assert _StubPM({"target_mode": "hr"}).target_mode == "power"
    assert _StubPM({"target_mode": "hr", "lthr": 160, "max_hr": 190}).target_mode == "hr"


# ── C17 (detail endpoint): hr payload present in hr mode, absent in power ────

ZWO = "threshold_steady_56min.zwo"


def test_detail_power_mode_has_no_hr_fields(client, monkeypatch):
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185})  # mode defaults to power
    d = client.get(f"/api/workout/all/{ZWO}").json()
    assert "target_mode" not in d
    assert all("hr" not in s and "hr_on" not in s for s in d["segments"])


def test_detail_hr_mode_attaches_segment_targets(client, monkeypatch):
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr"})
    d = client.get(f"/api/workout/all/{ZWO}").json()
    assert d["target_mode"] == "hr" and d["lthr"] == 160
    segs = d["segments"]
    # Warm-up ramp (600 s, 50→75% FTP) → hr_ramp 68%→83% LTHR.
    wu = next(s for s in segs if s["type"] == "Warmup")
    assert wu["hr"]["kind"] == "hr_ramp"
    assert wu["hr"]["bpm_start"] == round(0.68 * 160)
    assert wu["hr"]["bpm_end"] == round(0.83 * 160)
    # 180 s @ 95% → Z4 bpm range; 60 s @ 95% → RPE (short).
    s180 = next(s for s in segs if s["type"] == "SteadyState"
                and s["duration"] == 180 and s["pct"] == 95)
    assert s180["hr"]["kind"] == "hr" and s180["hr"]["zone"] == 4
    s60 = next(s for s in segs if s["type"] == "SteadyState"
               and s["duration"] == 60 and s["pct"] == 95)
    assert s60["hr"]["kind"] == "rpe" and s60["hr"]["reason"] == "short"
