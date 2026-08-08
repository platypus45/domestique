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
        v = self._athlete.get("lthr")
        try:
            return v is not None and 100 <= float(v) <= 220
        except (TypeError, ValueError):
            return False

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

ZWO = "threshold_2x3min-3min_95pct_56min.zwo"


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


# ── red-team regressions (S1/S2/S4/D7) ───────────────────────────────────────

def test_settings_400_on_out_of_persist_range_max_hr(client, monkeypatch):
    """Red-team S1: max_hr 240 passed save_athlete's validator but the
    _set_max_hr persist path silently clamps to [140,220] — a 200 OK for an
    hr switch that never landed, leaving lthr>max_hr on disk. The endpoint
    must 400 outside the range that actually persists."""
    _stub(monkeypatch, {"max_hr": 190})
    r = client.post("/api/settings", json={"target_mode": "hr", "lthr": 220, "max_hr": 240})
    assert r.status_code == 400
    assert "out of range" in r.json()["detail"]


def test_settings_400_not_500_on_out_of_range_lthr(client, monkeypatch):
    """Red-team S2: out-of-range athlete input must be a 400, not an
    unhandled ValueError → 500."""
    _stub(monkeypatch, {"max_hr": 190})
    r = client.post("/api/settings", json={"lthr": 50})
    assert r.status_code == 400


def test_settings_self_heals_stale_hr_mode(client, monkeypatch):
    """Red-team S4: lowering max_hr below lthr WITHOUT target_mode in the
    body must also flip the stored mode to power — no stale raw 'hr' left
    for a future direct-dict reader."""
    stub = _stub(monkeypatch, {"lthr": 175, "max_hr": 190, "target_mode": "hr"})
    r = client.post("/api/settings", json={"max_hr": 160})
    assert r.status_code == 200
    assert stub._athlete["target_mode"] == "power"


def test_lthr_is_set_rejects_insane_values():
    """Red-team D7: a hand-edited lthr of 0 or 50 must not satisfy the gate
    (0 'is not None' — key-presence alone was bypassable)."""
    assert _StubPM({"lthr": 0, "max_hr": 190, "target_mode": "hr"}).target_mode == "power"
    assert _StubPM({"lthr": 50, "max_hr": 190, "target_mode": "hr"}).target_mode == "power"
    assert _StubPM({"lthr": 160, "max_hr": 190, "target_mode": "hr"}).target_mode == "hr"


# ── modal ⚡/❤ peek toggle (view override) ────────────────────────────────────

def test_detail_view_hr_in_power_mode(client, monkeypatch):
    """view=hr shows the HR conversion to a power-mode user (peek toggle)."""
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185})  # global mode: power
    d = client.get(f"/api/workout/all/{ZWO}?view=hr").json()
    assert d["target_mode"] == "hr" and d.get("hr_axis")
    assert any("hr" in s for s in d["segments"])


def test_detail_view_power_in_hr_mode(client, monkeypatch):
    """view=power shows the watt view to an hr-mode user."""
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr"})
    d = client.get(f"/api/workout/all/{ZWO}?view=power").json()
    assert "target_mode" not in d
    assert all("hr" not in s for s in d["segments"])


def test_detail_view_hr_ignored_without_sane_lthr(client, monkeypatch):
    """The peek honours the same gate as the settings toggle."""
    _stub(monkeypatch, {"max_hr": 190})  # no lthr
    d = client.get(f"/api/workout/all/{ZWO}?view=hr").json()
    assert "target_mode" not in d and d["hr_available"] is False


def test_fit_export_view_hr_in_power_mode(client, monkeypatch):
    """The FIT download follows the toggle (WYSIWYG)."""
    fitparse = pytest.importorskip("fitparse")
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185})  # global: power
    r = client.get(f"/api/export/fit-workout?session_type=z2&duration_min=56&name=t&zwo_file={ZWO}&view=hr")
    assert r.status_code == 200
    steps = [{f.name: f.value for f in m.fields}
             for m in fitparse.FitFile(r.content).get_messages("workout_step")]
    assert any(s.get("target_type") == "heart_rate" for s in steps)
    # and view=power forces watts even in hr mode
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr"})
    r2 = client.get(f"/api/export/fit-workout?session_type=z2&duration_min=56&name=t&zwo_file={ZWO}&view=power")
    steps2 = [{f.name: f.value for f in m.fields}
              for m in fitparse.FitFile(r2.content).get_messages("workout_step")]
    assert all(s.get("target_type") == "power" for s in steps2)


# ── W1 (v2.5.0): custom prescription rows end-to-end ─────────────────────────

OVR = {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]}


def test_settings_accepts_and_applies_custom_rows(client, monkeypatch):
    stub = _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr"})
    r = client.post("/api/settings", json={"hr_rows_custom": OVR})
    assert r.status_code == 200
    assert stub._athlete["hr_prescription_rows_custom"] == OVR
    # detail endpoint now speaks the custom numbers everywhere
    stub._athlete["hr_prescription_rows_custom"] = OVR
    d = client.get(f"/api/workout/all/{ZWO}").json()
    assert d["hr_axis"]["75"] == 138 and d["hr_axis"]["105"] == 172
    s180 = next(s for s in d["segments"] if s["type"] == "SteadyState"
                and s["duration"] == 180 and s["pct"] == 95)
    assert (s180["hr"]["bpm_low"], s180["hr"]["bpm_high"]) == (155, 172)
    wu = next(s for s in d["segments"] if s["type"] == "Warmup")
    assert wu["hr"]["bpm_end"] == 138


def test_settings_rejects_bad_custom_rows(client, monkeypatch):
    _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr"})
    for bad in (
        {"z1_high": 120, "z2": [138, 125], "z3": [140, 152], "z4": [155, 172]},  # low>high
        {"z1_high": 120, "z2": [125, 138], "z3": [140, 152], "z4": [155, 250]},  # > max_hr
        {"z1_high": 160, "z2": [125, 138], "z3": [140, 152], "z4": [155, 172]},  # descending tops
        {"z2": [125, 138]},                                                       # missing keys
    ):
        assert client.post("/api/settings", json={"hr_rows_custom": bad}).status_code == 400


def test_settings_reset_custom_rows(client, monkeypatch):
    stub = _stub(monkeypatch, {"lthr": 160, "max_hr": 185, "target_mode": "hr",
                               "hr_prescription_rows_custom": OVR})
    r = client.post("/api/settings", json={"hr_rows_custom": None})
    assert r.status_code == 200
    assert stub._athlete["hr_prescription_rows_custom"] is None
