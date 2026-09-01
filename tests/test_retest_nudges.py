"""P2.3 (v3.0.0, G11) — FTP/LTHR retest nudges.

Locks:
  * None-semantics: no source_date → NO nudge inside the 28d profile grace,
    then the unknown-source rule fires ("never_tested");
  * staleness thresholds: 56d manual/tested, 28d icu_estimate/unknown;
  * mode priority: hr target_mode → LTHR nudge (instructions action),
    power → FTP nudge (ftp_test insert action);
  * snooze 14d round-trips through athlete.json (sanctioned save_athlete
    path — survives webview storage clears);
  * no nudge when fresh;
  * the insert path: ftp_test is a sanctioned /api/plan/swap-type target and
    the suggested slot lands in the NEXT week, skipping rest/races/pins.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import profile_manager as pm_mod  # noqa: E402

TODAY = date(2026, 7, 2)


class _StubPM:
    def __init__(self, athlete=None, created=None, ftp_date=None):
        self._athlete = dict(athlete or {})
        self._created = created
        self._ftp_date = ftp_date
        self.active_id = "tester"

    @property
    def target_mode(self):
        mode = self._athlete.get("target_mode", "power")
        if mode == "hr" and not self._athlete.get("lthr"):
            return "power"
        return mode

    def get_ftp_source(self):
        return self._athlete.get("ftp_source")

    def get_ftp_source_date(self):
        return self._ftp_date

    def list_profiles(self):
        if not self._created:
            return []
        return [{"id": "tester", "created": self._created}]

    def save_athlete(self, data):
        self._athlete.update(data)

    def __getattr__(self, name):
        if name.startswith(("get_", "record_", "_set")) or name == "on_switch":
            return lambda *a, **k: None
        return None


def _iso(d: date) -> str:
    return d.isoformat()


# ── pure staleness rule ──────────────────────────────────────────────────────

def test_locked_constants():
    assert app_module.RETEST_STALE_DAYS_TESTED == 56
    assert app_module.RETEST_STALE_DAYS_ESTIMATE == 28
    assert app_module.RETEST_PROFILE_GRACE_DAYS == 28
    assert app_module.RETEST_SNOOZE_DAYS == 14


def test_none_date_grace_then_never_tested():
    """source_date None → quiet inside created+28d, nudge after (G11)."""
    fresh_profile = _iso(TODAY - timedelta(days=10))
    stale, days, reason = app_module._retest_staleness(
        None, None, fresh_profile, TODAY)
    assert (stale, reason) == (False, "profile_grace")
    old_profile = _iso(TODAY - timedelta(days=29))
    stale, days, reason = app_module._retest_staleness(
        None, None, old_profile, TODAY)
    assert (stale, days, reason) == (True, None, "never_tested")
    # Grace boundary: exactly 28 days old → nudge fires.
    boundary = _iso(TODAY - timedelta(days=28))
    stale, _d, _r = app_module._retest_staleness(None, None, boundary, TODAY)
    assert stale is True
    # Legacy profile with no creation stamp → past the grace by definition.
    stale, _d, reason = app_module._retest_staleness(None, None, None, TODAY)
    assert stale is True and reason == "never_tested"


@pytest.mark.parametrize("source,limit", [
    ("manual", 56), ("tested_ramp", 56),
    ("icu_estimate", 28), ("unknown", 28), ("eftp_icu", 28),
])
def test_staleness_thresholds(source, limit):
    fresh = _iso(TODAY - timedelta(days=limit - 1))
    stale = _iso(TODAY - timedelta(days=limit))
    assert app_module._retest_staleness(source, fresh, None, TODAY)[0] is False
    got = app_module._retest_staleness(source, stale, None, TODAY)
    assert got[0] is True and got[1] == limit and got[2] == "stale"


def test_no_nudge_when_fresh():
    pm = _StubPM({"ftp": 250, "ftp_source": "manual"},
                 ftp_date=_iso(TODAY - timedelta(days=5)))
    assert app_module._retest_nudge_payload(pm, today=TODAY) is None


# ── mode priority ────────────────────────────────────────────────────────────

def test_power_mode_nudges_ftp_with_insert_action(monkeypatch):
    monkeypatch.setattr(app_module, "_next_ftp_test_slot", lambda: "2026-07-06")
    pm = _StubPM({"ftp": 250, "ftp_source": "manual",
                  "lthr": 165, "lthr_source": "manual",
                  "lthr_source_date": _iso(TODAY - timedelta(days=200))},
                 ftp_date=_iso(TODAY - timedelta(days=200)))
    n = app_module._retest_nudge_payload(pm, today=TODAY)
    assert n["metric"] == "ftp"
    assert n["action"] == "insert_ftp_test"
    assert n["suggested_date"] == "2026-07-06"
    assert n["days_since"] == 200


def test_hr_mode_nudges_lthr_with_instructions_action():
    pm = _StubPM({"target_mode": "hr", "ftp": 250, "ftp_source": "manual",
                  "lthr": 165, "lthr_source": "manual",
                  "lthr_source_date": _iso(TODAY - timedelta(days=200))},
                 ftp_date=_iso(TODAY - timedelta(days=200)))
    n = app_module._retest_nudge_payload(pm, today=TODAY)
    assert n["metric"] == "lthr"
    assert n["action"] == "lthr_instructions"
    assert "suggested_date" not in n


def test_hr_mode_fresh_lthr_no_nudge_even_if_ftp_stale():
    pm = _StubPM({"target_mode": "hr", "ftp": 250, "ftp_source": "manual",
                  "lthr": 165, "lthr_source": "manual",
                  "lthr_source_date": _iso(TODAY - timedelta(days=3))},
                 ftp_date=_iso(TODAY - timedelta(days=300)))
    assert app_module._retest_nudge_payload(pm, today=TODAY) is None


# ── snooze round-trip ────────────────────────────────────────────────────────

def test_snooze_round_trip(monkeypatch):
    pm = _StubPM({"ftp": 250, "ftp_source": "manual"},
                 ftp_date=_iso(TODAY - timedelta(days=200)))
    monkeypatch.setattr(pm_mod.ProfileManager, "get",
                        classmethod(lambda cls: pm))
    assert app_module._retest_nudge_payload(pm, today=TODAY) is not None

    client = TestClient(app_module.app)
    r = client.post("/api/retest-nudge/snooze")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Persisted via the sanctioned save_athlete path.
    assert pm._athlete["retest_snooze_until"] == body["until"]
    until = date.fromisoformat(body["until"])
    assert (until - date.today()).days == app_module.RETEST_SNOOZE_DAYS

    # Suppressed through the snooze window (inclusive), back after it.
    assert app_module._retest_nudge_payload(pm, today=until) is None
    assert app_module._retest_nudge_payload(
        pm, today=until + timedelta(days=1)) is not None


def test_today_session_payload_carries_flag():
    """/api/today-session exposes retest_nudge (source-level check — the
    full impl path needs live plan/readiness fixtures)."""
    src = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
    assert '"retest_nudge": _retest_nudge_payload(_PM.get())' in src


# ── FULL INSERT v1 plumbing ──────────────────────────────────────────────────

def test_ftp_test_is_a_sanctioned_swap_type():
    assert "ftp_test" in app_module._SWAP_TYPES


def test_next_slot_lands_in_next_week_and_skips_pins(monkeypatch, tmp_path):
    today = date.today()
    monday_next = today + timedelta(days=7 - today.weekday())
    sessions = []
    # Next week: Mon race, Tue user-swapped, Wed rest, Thu z2, Fri threshold.
    rows = [("rest", {}), ("z2", {"user_swapped": True}), ("rest", {}),
            ("z2", {}), ("threshold", {}), ("rest", {}), ("rest", {})]
    rows[0] = ("threshold", {"is_race": True, "race": {"name": "R"}})
    for off, (stype, extra) in enumerate(rows):
        d = monday_next + timedelta(days=off)
        sessions.append({"day": d.isoformat(), "day_name": "D",
                         "session_type": stype, "duration_min": 60,
                         "tss_estimate": 60, "description": "", **extra})
    plan = {"weeks": [{"week_num": 2, "start": monday_next.isoformat(),
                       "end": (monday_next + timedelta(days=6)).isoformat(),
                       "phase": "build1", "tss_target": 300,
                       "sessions": sessions}]}
    import json as _json
    (tmp_path / "current_plan.json").write_text(_json.dumps(plan))
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    got = app_module._next_ftp_test_slot()
    # Hard slot preferred; race + user-swapped + rest skipped → Friday.
    assert got == (monday_next + timedelta(days=4)).isoformat()


def test_next_slot_none_without_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    assert app_module._next_ftp_test_slot() is None


# ── UI structural ────────────────────────────────────────────────────────────

def test_dashboard_banner_and_instructions_modal():
    html = (ROOT / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="retest-nudge-host"' in html
    assert "renderRetestNudge(d.retest_nudge)" in html
    assert "/api/retest-nudge/snooze" in html
    assert "showLthrTestInstructions" in html
    assert "average HR of the final 20 minutes" in html  # 20-min TT protocol
    # The insert button rides the EXISTING swap-type machinery.
    assert "session_type: 'ftp_test'" in html
