"""v3.5.6 — optional session-RPE capture, and the scale bug it exposed.

Design is deliberately conservative, and the evidence is why:
  * Foster modified CR-10 (0-10) — the scale the whole session-RPE evidence
    base uses. Borg 6-20 and CR-100 are statistically interchangeable but give
    different ABSOLUTE numbers, so switching silently breaks every published
    threshold. The scale id is pinned on each record.
  * A single sRPE rating carries SEE ~1.2 CR-10 units (Herman 2006), so no rule
    may act on a 1-unit deviation — that is acting on noise.
  * No randomised trial shows that feeding RPE back into an endurance plan
    improves any outcome. So capture + surface, and let the ONE pre-existing
    consumer (the G7 3-day-mean fatigue gate) keep its published threshold
    rather than inventing new automatic plan changes.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import ride_storage as rs
import training_planner as tp

_LOCAL = {"Origin": "http://localhost:8080"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: tmp_path)
    rs.persist_icu_activity({
        "id": "i1", "type": "VirtualRide", "name": "Indoor",
        "start_date_local": "2026-07-26T10:00:00", "elapsed_time": 3600,
    })
    return TestClient(app_module.app)


# ── endpoint ────────────────────────────────────────────────────────────────

def test_rpe_is_stored_with_its_scale_and_timestamp(client, tmp_path):
    r = client.post("/api/ride/icu_i1/rpe", json={"rpe": 7}, headers=_LOCAL)
    assert r.status_code == 200 and r.json()["rpe"] == 7
    rec = json.loads((tmp_path / "i1.json").read_text())
    assert rec["rpe"] == 7
    assert rec["rpe_scale"] == "foster_cr10"   # pinned, never re-interpreted
    assert rec["rpe_at"]                       # staleness must be auditable


@pytest.mark.parametrize("bad", [11, -1, 99])
def test_out_of_range_is_rejected(client, bad):
    r = client.post("/api/ride/icu_i1/rpe", json={"rpe": bad}, headers=_LOCAL)
    assert r.status_code == 400


def test_non_numeric_is_rejected(client):
    r = client.post("/api/ride/icu_i1/rpe", json={"rpe": "hard"}, headers=_LOCAL)
    assert r.status_code == 400


def test_rating_can_be_cleared(client, tmp_path):
    client.post("/api/ride/icu_i1/rpe", json={"rpe": 5}, headers=_LOCAL)
    r = client.post("/api/ride/icu_i1/rpe", json={"rpe": None}, headers=_LOCAL)
    assert r.status_code == 200
    rec = json.loads((tmp_path / "i1.json").read_text())
    assert "rpe" not in rec and "rpe_scale" not in rec


def test_unknown_ride_is_404(client):
    r = client.post("/api/ride/icu_nope/rpe", json={"rpe": 5}, headers=_LOCAL)
    assert r.status_code == 404


def test_rpe_survives_both_sync_paths(client, tmp_path):
    """The rider's own input is the one thing ICU can never supply, so a
    re-sync must not erase it — the laps/streams lesson."""
    client.post("/api/ride/icu_i1/rpe", json={"rpe": 6}, headers=_LOCAL)
    base = {"id": "i1", "type": "VirtualRide", "name": "Indoor",
            "start_date_local": "2026-07-26T10:00:00", "elapsed_time": 3600}
    rs.persist_icu_activity(base)                              # hourly sync
    assert json.loads((tmp_path / "i1.json").read_text())["rpe"] == 6
    rs.persist_icu_activity(base, carry_hydrated=False)        # detail refresh
    assert json.loads((tmp_path / "i1.json").read_text())["rpe"] == 6


# ── the scale bug this surfaced ─────────────────────────────────────────────

def test_feel_and_rpe_are_never_averaged_together():
    """ICU's `feel` is 1-5 ("how did it go"); perceived_exertion is a 1-10
    CR-10 effort rating. The old code did mean(feel*2, rpe), mixing two
    scales in one number: feel=4 alone became 8.0 and tripped the G7 >=7
    auto-downgrade, while 4 on CR-10 means "somewhat hard" and must trip
    nothing. A real CR-10 rating now wins outright."""
    today = tp.date.today().isoformat()
    # A true RPE wins over feel, no averaging.
    both = tp._last_3d_mean_feel([{"date": today, "feel": 5,
                                   "perceived_exertion": 3}])
    assert both == 3.0, "feel must not pull a CR-10 rating upward"
    # feel alone still gives a usable fallback on the 1-10 axis…
    assert tp._last_3d_mean_feel([{"date": today, "feel": 4}]) == 8.0
    # …but a CR-10 4 stays a 4, well under the G7 threshold.
    assert tp._last_3d_mean_feel([{"date": today,
                                   "perceived_exertion": 4}]) == 4.0


def test_mean_is_over_rides_not_over_fields():
    today = tp.date.today().isoformat()
    v = tp._last_3d_mean_feel([
        {"date": today, "perceived_exertion": 8},
        {"date": today, "perceived_exertion": 4},
    ])
    assert v == 6.0


def test_no_signal_returns_none():
    today = tp.date.today().isoformat()
    assert tp._last_3d_mean_feel([{"date": today}]) is None
    assert tp._last_3d_mean_feel([]) is None


# ── UI contract ─────────────────────────────────────────────────────────────

def test_ui_uses_the_foster_anchors_and_posts_to_the_endpoint():
    from pathlib import Path
    src = (Path(app_module.__file__).parent / "templates" / "dashboard.html"
           ).read_text(encoding="utf-8")
    assert "RPE_ANCHORS" in src
    # Anchors are what make a rating reproducible — numbers alone are not.
    for anchor in ("Very easy", "Somewhat hard", "Very hard", "Maximal"):
        assert anchor in src, anchor
    assert "/rpe" in src and "setRideRpe" in src
    assert "How hard did that feel?" in src
    assert "(optional)" in src, "rating must read as optional"


# ── FIT-only riders (no intervals.icu account) ──────────────────────────────

def test_a_fit_import_can_be_rated_and_the_planner_sees_it(tmp_path, monkeypatch):
    """The RPE control renders for every ride, so it must work for a FIT
    import too — those riders have no ICU record to hold rider input, and no
    ICU-side rating field either, so this is the only way they can rate at all.
    It 404'd before v3.6.0."""
    fit = tmp_path / "20260726T100000.fit"
    fit.write_bytes(b"not-a-real-fit")
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    monkeypatch.setattr(rs, "compute_fit_load", lambda p: {"tss": 60,
                                                          "load_source": "hr",
                                                          "duration_s": 3600,
                                                          "started_at": "2026-07-26T10:00:00"})
    client = TestClient(app_module.app)
    r = client.post("/api/ride/fit_20260726T100000/rpe", json={"rpe": 8},
                    headers=_LOCAL)
    assert r.status_code == 200 and r.json()["rpe"] == 8
    side = json.loads((tmp_path / "20260726T100000.load.json").read_text())
    assert side["rpe"] == 8 and side["rpe_scale"] == "foster_cr10" and side["rpe_at"]
    # …and it reaches the gate's input, not just the disk.
    entry = [e for e in rs.load_all_rides()
             if e.get("ride_id") == "fit_20260726T100000"]
    assert entry and entry[0]["rpe"] == 8


def test_a_reimport_does_not_wipe_a_fit_rating(tmp_path, monkeypatch):
    """`write_fit_load_sidecar` recomputes the whole file and a re-import calls
    it eagerly — the same shape as the laps/streams loss."""
    fit = tmp_path / "20260726T100000.fit"
    fit.write_bytes(b"not-a-real-fit")
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    monkeypatch.setattr(rs, "compute_fit_load", lambda p: {"tss": 60,
                                                          "load_source": "hr"})
    assert rs.set_fit_rpe("20260726T100000", 7, "2026-07-26T12:00:00")
    rs.write_fit_load_sidecar(fit)                     # re-import
    side = json.loads((tmp_path / "20260726T100000.load.json").read_text())
    assert side["rpe"] == 7, "a recompute must not erase rider input"


def test_a_fit_rating_can_be_cleared(tmp_path, monkeypatch):
    fit = tmp_path / "20260726T100000.fit"
    fit.write_bytes(b"x")
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    monkeypatch.setattr(rs, "compute_fit_load", lambda p: {"tss": 60})
    rs.set_fit_rpe("20260726T100000", 7, "2026-07-26T12:00:00")
    client = TestClient(app_module.app)
    r = client.post("/api/ride/fit_20260726T100000/rpe", json={"rpe": None},
                    headers=_LOCAL)
    assert r.status_code == 200
    side = json.loads((tmp_path / "20260726T100000.load.json").read_text())
    assert "rpe" not in side and "rpe_scale" not in side


def test_an_unknown_fit_is_still_404(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    client = TestClient(app_module.app)
    r = client.post("/api/ride/fit_nope/rpe", json={"rpe": 5}, headers=_LOCAL)
    assert r.status_code == 404


def test_a_fit_rating_survives_the_ride_reaching_intervals_icu(tmp_path, monkeypatch):
    """A FIT-only rider rates a ride, then the importer relays that same file
    to intervals.icu. From then on the ICU twin wins the FIT/ICU dedupe — and
    the rating vanished from every reader, including the 3-day gate. Nothing
    else can regenerate it."""
    icu_dir = tmp_path / "icu"
    icu_dir.mkdir()
    fit = tmp_path / "20260726T200000.fit"
    fit.write_bytes(b"x")
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu_dir)
    monkeypatch.setattr(rs, "compute_fit_load", lambda p: {
        "tss": 60, "load_source": "hr", "duration_s": 3600,
        "started_at": "2026-07-26T20:00:00"})
    monkeypatch.setattr(rs, "_fit_iso_started_at",
                        lambda p: "2026-07-26T20:00:00")
    assert rs.set_fit_rpe("20260726T200000", 8, "2026-07-26T21:00:00")
    assert [e.get("rpe") for e in rs.load_all_rides()] == [8]
    # …the ICU twin appears for the same ride.
    (icu_dir / "999.json").write_text(json.dumps({
        "ride_id": "icu_999", "source": "icu", "external_id": "999",
        "started_at": "2026-07-26T20:00:00", "duration_s": 3600}))
    after = rs.load_all_rides()
    assert [e["ride_id"] for e in after] == ["icu_999"]      # deduped
    assert after[0].get("rpe") == 8, "the rating must survive the dedupe"


def test_an_icu_rating_wins_over_a_carried_fit_one(tmp_path, monkeypatch):
    """ICU is the newer edit surface, so its own value is not overwritten."""
    icu_dir = tmp_path / "icu"
    icu_dir.mkdir()
    fit = tmp_path / "20260726T200000.fit"
    fit.write_bytes(b"x")
    monkeypatch.setattr(rs, "_fit_rides_dir", lambda: tmp_path)
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu_dir)
    monkeypatch.setattr(rs, "compute_fit_load", lambda p: {
        "tss": 60, "duration_s": 3600, "started_at": "2026-07-26T20:00:00"})
    monkeypatch.setattr(rs, "_fit_iso_started_at",
                        lambda p: "2026-07-26T20:00:00")
    rs.set_fit_rpe("20260726T200000", 8, "2026-07-26T21:00:00")
    (icu_dir / "999.json").write_text(json.dumps({
        "ride_id": "icu_999", "source": "icu", "external_id": "999",
        "started_at": "2026-07-26T20:00:00", "duration_s": 3600, "rpe": 4}))
    assert rs.load_all_rides()[0]["rpe"] == 4
