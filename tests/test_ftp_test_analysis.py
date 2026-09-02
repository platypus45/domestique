"""v3.11.3 — the FTP-test analysis tunnel: calculators report WHERE the
number came from, and the analysis endpoint serves trace + windows."""
from __future__ import annotations

import json

import fitness_estimation as fe


def _coggan_ride(block_w=280, warm_w=120):
    s = [warm_w] * 600 + ([int(block_w * 0.95)] * 60 + [warm_w] * 60) * 3
    s += [warm_w] * 240 + [int(block_w * 1.12)] * 300 + [warm_w] * 540
    s += [block_w] * 1200 + [int(warm_w * 0.8)] * 300
    return s


def test_coggan_reports_window_and_blowout_location():
    r = fe.coggan_20min_ftp(_coggan_ride())
    assert r["window_end_s"] - r["window_start_s"] == 1200
    assert r["factor"] == 0.95
    # the 20-min block sits after 600+360+240+300+540 = 2040 s
    assert abs(r["window_start_s"] - 2040) <= 30
    assert "blowout_missing" not in r
    assert r["blowout_start_s"] < r["window_start_s"]


def test_ramp_reports_best_minute_location():
    s = [120] * 300
    for step in range(100, 420, 20):
        s += [step] * 60
    s += [30] * 120
    r = fe.ramp_test_ftp(s)
    assert r["best_60s"] == 400 and r["factor"] == 0.75
    # the 400 W step is the last one before the cooldown
    assert r["window_start_s"] == 300 + 15 * 60


def test_sixty_reports_plateau_bounds():
    s = [120] * 900 + [250] * 3600 + [100] * 300
    r = fe.sixty_min_ftp(s)
    assert r["window_start_s"] <= 900 and r["window_end_s"] >= 900 + 3600 - 60
    assert r["factor"] == 1.0


def test_evaluate_carries_window_keys():
    out = fe.evaluate_ftp_test(_coggan_ride(), prior_ftp=250)
    sug = out["ftp_test_suggestion"]
    for k in ("window_start_s", "window_end_s", "factor", "blowout_start_s"):
        assert k in sug, k


def test_analysis_endpoint_icu_ride(tmp_path, monkeypatch):
    import ride_storage as rs
    import app as app_module
    from fastapi.testclient import TestClient
    icu = tmp_path / "icu"; icu.mkdir()
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    series = _coggan_ride()
    out = fe.evaluate_ftp_test(series, prior_ftp=250)
    env = {"external_id": "r1", "started_at": "2026-09-02T09:00:00",
           "streams": {"watts": series}, **out}
    (icu / "r1.json").write_text(json.dumps(env))
    client = TestClient(app_module.app)
    r = client.get("/api/ride/icu_r1/ftp-test-analysis")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["suggestion"]["ftp"] == out["ftp_test_suggestion"]["ftp"]
    assert d["total_s"] == len(series)
    assert len(d["series"]) <= 1800 and len(d["series"]) > 100
    kinds = {w["kind"] for w in d["windows"]}
    assert "scored" in kinds and "blowout" in kinds


def test_analysis_endpoint_404_without_suggestion(tmp_path, monkeypatch):
    import ride_storage as rs
    import app as app_module
    from fastapi.testclient import TestClient
    icu = tmp_path / "icu"; icu.mkdir()
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    (icu / "r2.json").write_text(json.dumps({"external_id": "r2", "streams": {"watts": [100] * 100}}))
    r = TestClient(app_module.app).get("/api/ride/icu_r2/ftp-test-analysis")
    assert r.status_code == 404
