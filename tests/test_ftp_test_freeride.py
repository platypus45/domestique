"""v3.11.3 — FTP-test blocks are FREE RIDES, end to end.

A test measures FTP; it must never prescribe one. The measured blocks in the
20-min (test + 5-min all-out clearing effort) and 60-min files are ZWO
<FreeRide> elements (no target — in erg mode a 100% SteadyState held the
rider at the OLD FTP during the very effort meant to replace it), and the FIT
download exports them as OPEN steps (the old transcode turned a FreeRide into
a 50%-of-FTP power target).
"""
from __future__ import annotations

import io
from pathlib import Path

import fitparse

import app

W = Path(__file__).resolve().parent.parent / "src" / "workouts"
COGGAN = "ftp_test_coggan_3x1min-1min_95pct_59min.zwo"
SIXTY = "ftp_test_60min_steady_86min.zwo"


def _steps(data: bytes):
    f = fitparse.FitFile(io.BytesIO(data))
    return [{d.name: d.value for d in m} for m in f.get_messages("workout_step")]


def test_zwo_measured_blocks_are_freeride():
    cog = (W / COGGAN).read_text(encoding="utf-8")
    assert '<FreeRide Duration="1200"' in cog          # the 20-min test
    assert '<FreeRide Duration="300"' in cog           # the 5-min all-out clearing effort
    assert 'Power="1.00" pace="test"' not in cog
    sixty = (W / SIXTY).read_text(encoding="utf-8")
    assert '<FreeRide Duration="3600"' in sixty
    assert 'Power="1.00"' not in sixty


def _open_step(steps, secs):
    ms = secs * 1000
    hits = [s for s in steps if s.get("duration_time") in (secs, float(secs)) or s.get("duration_value") == ms]
    assert hits, f"no {secs}s step in {[s.get('duration_time') or s.get('duration_value') for s in steps]}"
    return hits[0]


def test_fit_export_20min_block_is_open_target():
    steps = _steps(app.build_fit_workout_bytes("ftp_test", 59, "t", COGGAN))
    for secs in (1200, 300):          # the test block + the 5-min all-out effort
        s = _open_step(steps, secs)
        assert str(s.get("target_type")).lower() == "open", s
        # An open step carries no power prescription — the old transcode
        # stamped 50% FTP here. (Genuine 50% REST steps elsewhere are fine.)
        assert not s.get("custom_target_power_low"), s


def test_fit_export_60min_block_is_open_target():
    steps = _steps(app.build_fit_workout_bytes("ftp_test", 86, "t", SIXTY))
    s = _open_step(steps, 3600)
    assert str(s.get("target_type")).lower() == "open", s


def test_fit_export_prescribed_blocks_keep_targets():
    # Openers at 95% stay real power targets — only the measured blocks are open.
    steps = _steps(app.build_fit_workout_bytes("ftp_test", 59, "t", COGGAN))
    powered = [st for st in steps if str(st.get("target_type")).lower() == "power"]
    assert powered, "openers/warmup must still carry power targets"
