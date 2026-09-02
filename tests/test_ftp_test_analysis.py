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


# ── dashboard wiring pins (text-scan, same style as the other UI pins) ───────

_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        from pathlib import Path
        _HTML = (Path(__file__).resolve().parent.parent / "src" / "templates"
                 / "dashboard.html").read_text(encoding="utf-8")
    return _HTML


def test_modal_opens_the_tunnel_when_ride_known():
    h = _html()
    assert 'id="ftp-analysis-host"' in h
    assert "if (opts.rideId) { try { renderFtpTestAnalysis(opts.rideId, fs); } catch(_) {} }" in h
    assert "/ftp-test-analysis`" in h
    for label in ("1 · RECOGNISE", "2 · MEASURE", "3 · COMPUTE", "4 · APPLY"):
        assert label in h, label


def test_banner_review_button_and_ride_id_on_actions():
    h = _html()
    assert "Review the evidence" in h
    assert "rideId: window._lastFtpSuggestion.rideId" in h
    # all three actions from the banner carry the ride id → review persisted.
    assert h.count("{rideId: '${escJs(String(rideId))}'}") == 3
    assert "haltStep: s.ftp_test_halt_step, rideId: rideId };" in h


# ── FIT branch of the analysis endpoint (synthetic activity FIT) ─────────────

def _activity_fit_bytes(series, start_ts=1_700_000_000):
    """A minimal activity FIT with 1 Hz record messages carrying power."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer, Sport
    b = FitFileBuilder()
    fid = FileIdMessage(); fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value; fid.product = 0
    fid.serial_number = 1; fid.time_created = start_ts * 1000
    b.add(fid)
    w = WorkoutMessage(); w.workout_name = "FTP Test — Coggan 20min protocol (59min)"
    b.add(w)
    for i, p in enumerate(series):
        r = RecordMessage(); r.timestamp = (start_ts + i) * 1000; r.power = int(p)
        b.add(r)
    s = SessionMessage(); s.total_timer_time = float(len(series)); s.sport = Sport.CYCLING
    s.start_time = start_ts * 1000
    b.add(s)
    return b.build().to_bytes()


def test_analysis_endpoint_fit_ride(tmp_path, monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    fitdir = tmp_path / "fits"; fitdir.mkdir()
    monkeypatch.setattr(app_module, "_rides_fit_dir", lambda: fitdir)
    series = _coggan_ride()
    (fitdir / "ride_synth.fit").write_bytes(_activity_fit_bytes(series))
    r = TestClient(app_module.app).get("/api/ride/ride_synth/ftp-test-analysis")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "fit"
    assert d["suggestion"]["type"] == "coggan_20min"      # recognised from the FIT workout name
    assert abs(d["total_s"] - len(series)) <= 2
    assert any(w["kind"] == "scored" for w in d["windows"])
    expect = fe.evaluate_ftp_test(series, prior_ftp=d["suggestion"]["prior_ftp"])["ftp_test_suggestion"]["ftp"]
    assert d["suggestion"]["ftp"] == expect


# ── v3.11.3: FIT import actually READS the records (fit-tool compat) ────────

def test_parse_fit_stats_reads_power_hr_cadence(tmp_path):
    """Regression: every field went through msg.get_value(), which does not
    exist in fit-tool 0.9.15/0.9.16 — all FIT imports read as zero power."""
    import app as app_module
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer, Sport
    b = FitFileBuilder()
    fid = FileIdMessage(); fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value; fid.product = 0
    fid.serial_number = 1; fid.time_created = 1_700_000_000_000
    b.add(fid)
    for i in range(120):
        r = RecordMessage(); r.timestamp = (1_700_000_000 + i) * 1000
        r.power = 200 + (i % 5); r.heart_rate = 140; r.cadence = 90
        b.add(r)
    s = SessionMessage(); s.total_timer_time = 120.0; s.sport = Sport.CYCLING
    s.start_time = 1_700_000_000_000; b.add(s)
    p = tmp_path / "r.fit"; p.write_bytes(b.build().to_bytes())
    st = app_module._parse_fit_stats(p)
    assert st.get("sample_count") == 120
    assert 200 <= st.get("avg_power", 0) <= 205, st.get("avg_power")
    assert st.get("avg_hr") == 140
    assert st.get("avg_cadence") == 90
    assert st.get("duration_sec") == 120
