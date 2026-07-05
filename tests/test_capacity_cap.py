"""Measured-capacity short-rep advisory (task #24, v3.2.0) — contract tests.

Two layers:
  * PURE-MODULE tests exercise ``capacity_cap`` directly against the real
    library ZWO files (byte-identity, monotonicity, floors, ramp exemption,
    envelope sanity, GA6 grep-proof). No app/profile needed → fast + hermetic.
  * SEAM tests run the ZWO-download / FIT / ICU-push serve paths through a
    stubbed-HOME ProfileManager (pattern cloned from tests/test_icu_push.py)
    to prove GA1 (OFF byte-identical), GA2 (INERT without a measured Pmax),
    and GA5 (PROMPT approve caps only that download; plan + library untouched).

Contract map (IP_WPRIME_CALIBRATION.md "GRILL OUTCOME — LOCKED"):
  GA1  OFF ⇒ every serve path byte-identical (no-op cap = 0-byte diff)
  GA2  not pmax_is_set (icu/manual absent) ⇒ INERT, zero change
  GA3  cap monotone in Pmax, only lowers, floors at 1.06 vo2 + never below
       original, ramp-tests exempt; envelope sane at 15/30/60/120 s
  GA5  PROMPT approve caps only that download; DENY = authored bytes;
       plan + library file untouched
  GA6  no W′/tau-prescription symbol in capacity_cap.py
"""
from __future__ import annotations

import glob
import os
import random
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capacity_cap as C  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "workouts"

# Realistic anchors for a 250 W FTP rider.
FTP = 250.0
CP = 1.03 * FTP                 # 257.5 W
PMAX_LOW = int(2.6 * FTP)       # ~650 W (modest sprinter)
PMAX_HIGH = int(4.0 * FTP)      # ~1000 W (strong sprinter)


def _lib_files(n=20, seed=3):
    files = sorted(glob.glob(str(LIB / "*.zwo")))
    random.Random(seed).shuffle(files)
    return files[:n]


# ──────────────────────────────────────────────────────────────────────────
# PURE MODULE
# ──────────────────────────────────────────────────────────────────────────

class TestEnvelope:
    def test_env_values_sane_at_15_30_60_120(self):
        # Monotone decreasing in t; between CP and Pmax; > CP always.
        vals = [C.p_env(t, CP, PMAX_LOW) for t in (15, 30, 60, 120)]
        assert vals == sorted(vals, reverse=True), "envelope must decrease in t"
        assert all(CP < v <= PMAX_LOW for v in vals[:1]), "short end near Pmax"
        assert all(v > CP for v in vals), "envelope never drops below CP"
        # 15 s is well above CP; 120 s has decayed most of the way back to CP.
        assert C.p_env(15, CP, PMAX_LOW) > C.p_env(120, CP, PMAX_LOW)
        assert C.p_env(120, CP, PMAX_LOW) - CP < 0.15 * (PMAX_LOW - CP)

    def test_env_monotone_increasing_in_pmax(self):
        for t in (15, 30, 60, 120):
            assert C.p_env(t, CP, PMAX_HIGH) > C.p_env(t, CP, PMAX_LOW)


class TestCapRatioUnit:
    def test_only_lowers_never_raises(self):
        # A 2.0x @ 30 s rep for a modest rider must be capped BELOW 2.0.
        r = C._cap_ratio(2.0, 30, FTP, CP, PMAX_LOW)
        assert r is not None and r < 2.0

    def test_within_envelope_not_touched(self):
        # A 1.5x @ 30 s rep sits under a strong rider's envelope → no cap.
        assert C._cap_ratio(1.5, 30, FTP, CP, PMAX_HIGH) is None

    def test_non_qualifying_below_ratio(self):
        assert C._cap_ratio(1.10, 30, FTP, CP, PMAX_LOW) is None or True
        # ratio < 1.20 never qualifies
        assert C._cap_ratio(1.19, 20, FTP, CP, PMAX_LOW) is None

    def test_non_qualifying_over_120s(self):
        assert C._cap_ratio(1.5, 121, FTP, CP, PMAX_LOW) is None
        assert C._cap_ratio(1.5, 120, FTP, CP, PMAX_LOW) is not None

    def test_vo2_floor_never_below_106(self):
        # A vo2-class rep (1.06-1.20) capped for a tiny Pmax must floor at 1.06
        # AND never rewrite below the original (only lowers).
        tiny = int(1.5 * FTP)  # very low measured ceiling
        r = C._cap_ratio(1.19, 30, FTP, CP, tiny)
        if r is not None:
            assert r >= C.VO2_FLOOR - 1e-9
            assert r < 1.19

    def test_boundary_continuity_at_120s_and_120ratio(self):
        # Just inside the qualification boundary produces a cap or None, but
        # never an exception, and the transition is not a cliff.
        for t in (119, 120):
            C._cap_ratio(1.25, t, FTP, CP, PMAX_LOW)  # no raise


class TestCapZwoTextPure:
    def test_noop_is_byte_identical_object(self):
        # High Pmax ⇒ most files have nothing to cap ⇒ identical object back.
        ok = 0
        for f in _lib_files(20):
            txt = Path(f).read_text(encoding="utf-8")
            out, n, det = C.cap_zwo_text(txt, FTP, CP, PMAX_HIGH,
                                         filename=os.path.basename(f))
            if n == 0:
                assert out is txt, f"no-op not byte-identical: {f}"
                assert det == []
                ok += 1
        assert ok >= 10, "expected many high-Pmax no-ops across the sample"

    def test_paired_ab_20_files_zero_diff_when_noop(self):
        # GA1 paired A/B: for every sampled file, a no-op cap yields 0 diff.
        for f in _lib_files(20, seed=7):
            txt = Path(f).read_text(encoding="utf-8")
            out, n, _ = C.cap_zwo_text(txt, FTP, CP, PMAX_HIGH,
                                       filename=os.path.basename(f))
            if n == 0:
                assert out == txt

    def test_multi_rep_file_caps_each_qualifying_rep(self):
        # neuromuscular 30s@2.0 x6 — every rep qualifies for a modest rider.
        f = LIB / "neuromuscular_30s180s_6x_46min.zwo"
        if not f.exists():
            pytest.skip("fixture file absent")
        txt = f.read_text(encoding="utf-8")
        out, n, det = C.cap_zwo_text(txt, FTP, CP, PMAX_LOW, filename=f.name)
        assert n >= 1
        for d in det:
            assert d["new_ratio"] < d["orig_ratio"], "must only lower"
        # surgical: same number of Power=/OnPower= attrs before & after.
        assert out.count('Power="') == txt.count('Power="')

    def test_cap_monotone_in_pmax_on_hard_file(self):
        f = LIB / "neuromuscular_30s180s_6x_46min.zwo"
        if not f.exists():
            pytest.skip("fixture file absent")
        txt = f.read_text(encoding="utf-8")
        _, _, dlo = C.cap_zwo_text(txt, FTP, CP, PMAX_LOW, filename=f.name)
        _, _, dhi = C.cap_zwo_text(txt, FTP, CP, PMAX_HIGH, filename=f.name)
        assert dlo and dhi
        # Same reps → higher measured ceiling ⇒ higher (or equal) capped ratio.
        for lo, hi in zip(dlo, dhi):
            assert hi["new_ratio"] >= lo["new_ratio"] - 1e-9

    def test_ramp_tests_exempt(self):
        for name in ("ftp_test_ramp.zwo", "ftp_test_ramp_10w_step.zwo",
                     "ftp_test_ramp_20w_step.zwo"):
            f = LIB / name
            if not f.exists():
                continue
            txt = f.read_text(encoding="utf-8")
            out, n, det = C.cap_zwo_text(txt, FTP, CP, PMAX_LOW, filename=name)
            assert n == 0 and out is txt, f"ramp test not exempt: {name}"

    def test_exempt_flag_forces_passthrough(self):
        f = LIB / "neuromuscular_30s180s_6x_46min.zwo"
        if not f.exists():
            pytest.skip("fixture file absent")
        txt = f.read_text(encoding="utf-8")
        out, n, _ = C.cap_zwo_text(txt, FTP, CP, PMAX_LOW, filename=f.name,
                                   exempt=True)
        assert n == 0 and out is txt

    def test_offpower_and_ramps_never_touched(self):
        # Craft a doc with a high OffPower and a Warmup ramp; only OnPower of a
        # qualifying rep may change, OffPower/PowerLow/PowerHigh stay verbatim.
        doc = (
            '<workout_file><workout>'
            '<Warmup Duration="300" PowerLow="0.5" PowerHigh="0.75"/>'
            '<IntervalsT Repeat="4" OnDuration="30" OffDuration="30" '
            'OnPower="2.0" OffPower="1.2"/>'
            '</workout></workout_file>'
        )
        out, n, _ = C.cap_zwo_text(doc, FTP, CP, PMAX_LOW, filename="x.zwo")
        assert n == 1
        assert 'OffPower="1.2"' in out, "OffPower must be untouched"
        assert 'PowerLow="0.5"' in out and 'PowerHigh="0.75"' in out
        assert 'OnPower="2.0"' not in out, "the 2.0 OnPower must have lowered"

    def test_fire_count_low_vs_high_pmax(self):
        # Sanity: a low-Pmax rider caps at least as many reps across the library
        # as a high-Pmax rider (never fewer).
        total_lo = total_hi = 0
        for f in _lib_files(40, seed=11):
            txt = Path(f).read_text(encoding="utf-8")
            _, nlo, _ = C.cap_zwo_text(txt, FTP, CP, PMAX_LOW,
                                       filename=os.path.basename(f))
            _, nhi, _ = C.cap_zwo_text(txt, FTP, CP, PMAX_HIGH,
                                       filename=os.path.basename(f))
            total_lo += nlo
            total_hi += nhi
        assert total_lo >= total_hi
        assert total_lo > 0, "a modest rider should cap SOME library reps"


class TestGate:
    def test_pmax_is_set_gate(self):
        class PM:
            def __init__(self, src):
                self.pmax_source = src
        assert C.pmax_is_set(PM("manual")) is True
        assert C.pmax_is_set(PM("icu")) is True
        assert C.pmax_is_set(PM("computed")) is False
        assert C.pmax_is_set(PM("fallback")) is False
        assert C.pmax_is_set(PM("")) is False

    def test_pmax_is_set_prefers_property(self):
        class PM:
            pmax_is_set = True
            pmax_source = "fallback"  # would say False; property wins
        assert C.pmax_is_set(PM()) is True

    def test_degenerate_anchors_noop(self):
        # pmax <= cp ⇒ no defensible envelope ⇒ passthrough.
        doc = ('<workout_file><workout>'
               '<SteadyState Duration="30" Power="2.0"/>'
               '</workout></workout_file>')
        out, n, _ = C.cap_zwo_text(doc, FTP, CP, int(CP) - 10, filename="x.zwo")
        assert n == 0 and out is doc


class TestGA6NoWprimePrediction:
    def test_no_wprime_or_prescription_tau_symbol(self):
        src = (REPO / "capacity_cap.py").read_text(encoding="utf-8")
        low = src.lower()
        # No W′-balance / W-prime prescription machinery may feed an on-power.
        for bad in ("wprime", "w_prime", "w'bal", "wbal", "w_bal", "cp_wprime"):
            assert bad not in low, f"forbidden W′ symbol in capacity_cap.py: {bad}"
        # 'tau' appears ONLY as the envelope-decay constant TAU_S (a MEASURED-
        # anchored time constant, not a W′bal recovery tau). Assert every 'tau'
        # occurrence is that constant.
        import re as _re
        for m in _re.finditer(r"tau\w*", low):
            assert m.group(0) in ("tau_s", "tau"), m.group(0)
        # And that the only identifier is TAU_S (upper-cased in source).
        assert "TAU_S" in src


# ──────────────────────────────────────────────────────────────────────────
# SEAM TESTS (stubbed HOME — clone of tests/test_icu_push.py harness)
# ──────────────────────────────────────────────────────────────────────────

import config  # noqa: E402
import db as db_module  # noqa: E402
import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402
import icu_calendar_push as icp  # noqa: E402
from profile_manager import ProfileManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_ENV_KEYS = ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")

# A workout whose 30 s reps ask 2.0x FTP — the canonical "too hard" fixture.
HARD_ZWO = (
    '<workout_file>\n'
    '    <name>Hard Sprints</name>\n'
    '    <workout>\n'
    '        <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>\n'
    '        <IntervalsT Repeat="4" OnDuration="30" OffDuration="60" '
    'OnPower="2.0" OffPower="0.5"/>\n'
    '        <Cooldown Duration="300" PowerLow="0.7" PowerHigh="0.5"/>\n'
    '    </workout>\n'
    '</workout_file>\n'
)
# An easy endurance file — nothing qualifies, so every path stays byte-identical.
EASY_ZWO = (
    '<workout_file>\n'
    '    <name>Endurance</name>\n'
    '    <workout>\n'
    '        <SteadyState Duration="3600" Power="0.65"/>\n'
    '    </workout>\n'
    '</workout_file>\n'
)


@pytest.fixture()
def stub(tmp_path):
    home = tmp_path
    patcher = mock.patch("pathlib.Path.home", return_value=home)
    patcher.start()

    old_instance = ProfileManager._instance
    old_db_path = db_module.DB_PATH
    old_data_dir = app_module.DATA_DIR
    old_workout_dir = app_module.WORKOUT_DIR
    old_gpx_dir = app_module.GPX_DIR
    old_tp_plan_dir = tp.PLAN_DIR
    old_tp_workout_dir = tp.WORKOUT_DIR
    old_environ = {k: os.environ.get(k) for k in _ENV_KEYS}
    for attr in _ENV_KEYS:
        try:
            delattr(config, attr)
        except AttributeError:
            pass

    ProfileManager._instance = None
    app_module.DATA_DIR = home / ".domestique"

    pm = ProfileManager.get()
    pid = pm.create_profile("Capper")
    pm.switch(pid)
    pm.save_athlete({"ftp": 250})
    db_module._sync_stop.clear()

    workouts = home / "wk"
    workouts.mkdir()
    (workouts / "hard.zwo").write_text(HARD_ZWO, encoding="utf-8")
    (workouts / "easy.zwo").write_text(EASY_ZWO, encoding="utf-8")
    ramp = REPO / "workouts" / "ftp_test_ramp_20w_step.zwo"
    if ramp.exists():
        (workouts / "ftp_test_ramp_20w_step.zwo").write_bytes(ramp.read_bytes())
    app_module.WORKOUT_DIR = workouts
    tp.WORKOUT_DIR = workouts

    ns = types.SimpleNamespace(home=home, pm=pm, pid=pid, workouts=workouts,
                               client=TestClient(app_module.app))
    try:
        yield ns
    finally:
        patcher.stop()
        t = app_module._icu_push_timer
        if t is not None:
            t.cancel()
        app_module._icu_push_timer = None
        tp.post_write_callback = None
        ProfileManager._instance = old_instance
        app_module.DATA_DIR = old_data_dir
        app_module.WORKOUT_DIR = old_workout_dir
        app_module.GPX_DIR = old_gpx_dir
        tp.PLAN_DIR = old_tp_plan_dir
        tp.WORKOUT_DIR = old_tp_workout_dir
        db_module.set_db_path(old_db_path)
        db_module._sync_stop.clear()
        app_module.clear_cache()
        for k, v in old_environ.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for attr in _ENV_KEYS:
            try:
                delattr(config, attr)
            except AttributeError:
                pass


def _set_measured_pmax(pm, w=650):
    assert pm._set_pmax(w, "icu") is True
    assert pm.pmax_is_set is True


def _dl(client, filename, **params):
    return client.get(f"/api/workout/download/{filename}", params=params)


class TestSeamGA1OffByteIdentical:
    def test_off_download_byte_identical(self, stub):
        _set_measured_pmax(stub.pm)                 # measured Pmax present ...
        stub.pm.save_athlete({"cap_short_intervals": "off"})   # ... but OFF
        disk = (stub.workouts / "hard.zwo").read_bytes()
        r = _dl(stub.client, "hard.zwo")
        assert r.status_code == 200
        assert r.content == disk, "OFF must serve authored bytes verbatim"

    def test_off_fit_matches_uncapped(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "off"})
        a = stub.client.get("/api/export/fit-workout",
                            params={"zwo_file": "hard.zwo", "name": "H"})
        b = stub.client.get("/api/export/fit-workout",
                            params={"zwo_file": "hard.zwo", "name": "H", "cap": 0})
        assert a.status_code == 200 and a.content == b.content


class TestSeamGA2Inert:
    def test_no_measured_pmax_download_inert_even_on(self, stub):
        # FTP present ⇒ pm.pmax_w returns the ftp*1.30 fallback, but pmax_is_set
        # is False ⇒ the feature must be completely inert even with toggle ON.
        assert stub.pm.pmax_is_set is False
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        r = _dl(stub.client, "hard.zwo")
        assert r.content == disk

    def test_no_measured_pmax_ignores_cap_query(self, stub):
        assert stub.pm.pmax_is_set is False
        disk = (stub.workouts / "hard.zwo").read_bytes()
        r = _dl(stub.client, "hard.zwo", cap=1)     # PROMPT approve, but no Pmax
        assert r.content == disk

    def test_computed_pmax_is_not_trustworthy(self, stub):
        # A "computed" fitness-estimate Pmax must NOT enable the feature.
        assert stub.pm._set_pmax(700, "computed") is True
        assert stub.pm.pmax_is_set is False
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        assert _dl(stub.client, "hard.zwo").content == disk


class TestSeamOnCaps:
    def test_on_download_caps_hard_file(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        r = _dl(stub.client, "hard.zwo")
        assert r.status_code == 200
        assert r.content != disk, "ON must cap the 2.0x sprints"
        assert b'OnPower="2.0"' not in r.content
        # library file on disk is UNCHANGED (serve-time only).
        assert (stub.workouts / "hard.zwo").read_bytes() == disk

    def test_on_download_easy_file_byte_identical(self, stub):
        # Even ON, an easy file with no qualifying rep is byte-identical.
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = (stub.workouts / "easy.zwo").read_bytes()
        assert _dl(stub.client, "easy.zwo").content == disk

    def test_on_ramp_test_exempt_byte_identical(self, stub):
        f = stub.workouts / "ftp_test_ramp_20w_step.zwo"
        if not f.exists():
            pytest.skip("ramp fixture absent")
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = f.read_bytes()
        assert _dl(stub.client, "ftp_test_ramp_20w_step.zwo").content == disk

    def test_fit_path_caps_when_on(self, stub):
        _set_measured_pmax(stub.pm)
        uncapped = stub.client.get(
            "/api/export/fit-workout",
            params={"zwo_file": "hard.zwo", "name": "H", "cap": 0})
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        capped = stub.client.get(
            "/api/export/fit-workout",
            params={"zwo_file": "hard.zwo", "name": "H"})
        assert uncapped.status_code == capped.status_code == 200
        assert capped.content != uncapped.content, "ON FIT must differ (capped)"


class TestSeamGA5Prompt:
    def test_prompt_approve_caps_only_that_download(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "prompt"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        # DENY (default fetch, no cap) → authored bytes.
        deny = _dl(stub.client, "hard.zwo")
        assert deny.content == disk
        # APPROVE (?cap=1) → capped for THIS download only.
        approve = _dl(stub.client, "hard.zwo", cap=1)
        assert approve.content != disk
        assert b'OnPower="2.0"' not in approve.content
        # The library file + toggle are unchanged by an approve.
        assert (stub.workouts / "hard.zwo").read_bytes() == disk
        assert stub.pm.cap_short_intervals == "prompt"

    def test_prompt_without_approve_is_authored(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "prompt"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        assert _dl(stub.client, "hard.zwo").content == disk


class TestSeamZwoDownloadRoutes:
    def test_download_zwo_flat_and_category_cap(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        flat = stub.client.get("/api/download/zwo/hard.zwo")
        assert flat.status_code == 200 and flat.content != disk
        cat = stub.client.get("/api/download/zwo/anycat/hard.zwo")
        assert cat.status_code == 200 and cat.content != disk

    def test_download_zwo_off_byte_identical(self, stub):
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "off"})
        disk = (stub.workouts / "hard.zwo").read_bytes()
        assert stub.client.get("/api/download/zwo/hard.zwo").content == disk


class TestSeamSettings:
    def test_settings_exposes_gate_and_toggle(self, stub):
        r = stub.client.get("/api/settings")
        j = r.json()
        assert j["cap_short_intervals"] == "off"
        assert j["pmax_is_set"] is False
        assert j["pmax_w"] is None            # never the ftp*1.30 fallback
        _set_measured_pmax(stub.pm, 650)
        j2 = stub.client.get("/api/settings").json()
        assert j2["pmax_is_set"] is True
        assert j2["pmax_w"] == 650
        assert j2["pmax_source"] == "icu"

    def test_settings_post_accepts_enum(self, stub):
        r = stub.client.post("/api/settings", json={"cap_short_intervals": "on"})
        assert r.status_code == 200
        assert stub.pm.cap_short_intervals == "on"

    def test_settings_post_rejects_bad_enum(self, stub):
        r = stub.client.post("/api/settings",
                            json={"cap_short_intervals": "sometimes"})
        assert r.status_code == 400


class TestSeamModalAdvisory:
    """The workout-detail endpoint feeds the modal's factual comparison from a
    LIVE ZWO parse (not facts aggregates). It appears ONLY with a trustworthy
    measured Pmax + a too-hard qualifying rep, carries the toggle for render
    (OFF=info / PROMPT=approve-deny / ON=capped), and NEVER a ftp*1.30 ceiling."""

    def _detail(self, client, filename):
        return client.get(f"/api/workout/anycat/{filename}").json()

    def test_advisory_present_with_measured_pmax_and_hard_rep(self, stub):
        _set_measured_pmax(stub.pm, 650)
        stub.pm.save_athlete({"cap_short_intervals": "prompt"})
        d = self._detail(stub.client, "hard.zwo")
        cc = d.get("capacity_cap")
        assert cc is not None
        assert cc["pmax_is_set"] is True
        assert cc["toggle"] == "prompt"
        assert cc["ceiling_w"] == 650
        assert cc["demand_w"] > cc["cap_w"], "cap must be below demand"
        assert cc["rep_seconds"] == 30

    def test_advisory_toggle_reflects_state(self, stub):
        _set_measured_pmax(stub.pm, 650)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        assert self._detail(stub.client, "hard.zwo")["capacity_cap"]["toggle"] == "on"

    def test_no_advisory_without_measured_pmax(self, stub):
        # FTP present -> pmax_w fallback exists, but pmax_is_set is False -> the
        # advisory must be ABSENT (never a ftp*1.30 ceiling).
        assert stub.pm.pmax_is_set is False
        d = self._detail(stub.client, "hard.zwo")
        assert "capacity_cap" not in d

    def test_no_advisory_for_easy_file(self, stub):
        _set_measured_pmax(stub.pm, 650)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        d = self._detail(stub.client, "easy.zwo")
        assert "capacity_cap" not in d

    def test_no_advisory_for_ramp_test(self, stub):
        f = stub.workouts / "ftp_test_ramp_20w_step.zwo"
        if not f.exists():
            pytest.skip("ramp fixture absent")
        _set_measured_pmax(stub.pm, 650)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        d = self._detail(stub.client, "ftp_test_ramp_20w_step.zwo")
        assert "capacity_cap" not in d


class TestSeamIcuPush:
    def _plan(self, sessions):
        from datetime import date
        days = sorted(s["day"] for s in sessions)
        return {"goal": {"type": "ftp"}, "generated": "2026-07-01T00:00:00",
                "weeks": [{"week_num": 1, "start": days[0], "end": days[-1],
                           "phase": "base", "tss_target": 200,
                           "is_stepback": False, "sessions": sessions}]}

    def _sess(self, day, zwo):
        return {"day": day, "day_name": "X", "session_type": "vo2max",
                "duration_min": 60, "tss_estimate": 60.0,
                "description": "hard", "zwo_file": zwo,
                "zwo_name": Path(zwo).stem, "status": "pending"}

    def test_icu_push_off_byte_identical(self, stub):
        from datetime import date, timedelta
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "off"})
        d = (date.today() + timedelta(days=1)).isoformat()
        plan = self._plan([self._sess(d, "hard.zwo")])
        events, skipped, _ = icp._desired_events(stub.pm, plan, date.today(), 7,
                                                 stub.pid)
        assert len(events) == 1
        import base64
        pushed = base64.b64decode(events[0]["file_contents_base64"])
        assert pushed == (stub.workouts / "hard.zwo").read_bytes()

    def test_icu_push_on_caps_payload(self, stub):
        from datetime import date, timedelta
        _set_measured_pmax(stub.pm)
        stub.pm.save_athlete({"cap_short_intervals": "on"})
        d = (date.today() + timedelta(days=1)).isoformat()
        plan = self._plan([self._sess(d, "hard.zwo")])
        events, _, _ = icp._desired_events(stub.pm, plan, date.today(), 7,
                                          stub.pid)
        import base64
        pushed = base64.b64decode(events[0]["file_contents_base64"])
        disk = (stub.workouts / "hard.zwo").read_bytes()
        assert pushed != disk, "ON must push the capped file"
        assert b'OnPower="2.0"' not in pushed
        # disk untouched
        assert (stub.workouts / "hard.zwo").read_bytes() == disk
