"""FTP-tests overhaul (IP W1/W2) — the load-bearing invariants:

* 60-min protocol: detection (ordered BEFORE coggan), plateau-extent
  calculator, early-quit fallback to the validated 20-min method.
* Hint normalization: the FIT's embedded workout name / ICU activity name
  (the ZWO's display <name>) recognises the family, not just filenames.
* evaluate_ftp_test: the single recognition+calculation entry point shared
  by the FIT-import and sync paths.
* 1 Hz resampling: smart-recording FITs must not stretch test windows.
* execution_score: a perfect hour of power is not "over"; a ramp is silent.
* match_zwo family gate: test slots only serve scorable protocols.
* persist_icu_activity: suggestion + review verdict survive a re-sync.
* fresh-legs guard: never rewrites ridden/user-placed/past days.
"""
from __future__ import annotations

import json

import fitness_estimation as fe


# ── synthetic rides ──────────────────────────────────────────────────────────

def _hour_of_power(test_w=250, test_min=60, warm_w=120):
    """86-min protocol shape: 15min warmup w/ openers, Nmin steady, cooldown."""
    s = [warm_w] * 600                                   # 10min easy ramp-ish
    s += ([int(test_w * 0.95)] * 60 + [warm_w] * 60) * 3  # 3x1min openers
    s += [warm_w] * 240
    s += [test_w] * (test_min * 60)
    s += [int(warm_w * 0.8)] * 360
    return s


def _steady_z2_hour(w=180):
    return [w] * 3600


# ── W1a: sixty_min detection ─────────────────────────────────────────────────

def test_sixty_min_detects_by_shape_before_coggan():
    # A full hour plateau contains an 18-min one — ordering is load-bearing.
    assert fe.detect_ftp_test_shape(_hour_of_power()) == "sixty_min"


def test_sixty_min_filename_token():
    assert fe.detect_ftp_test_shape(
        [100] * 300, "ftp_test_60min_100pct_86min.zwo") == "sixty_min"


def test_workout_name_hint_normalized():
    # Zwift stamps the ZWO's display <name> into the FIT; ICU uses it as the
    # activity name. Neither contains underscore tokens.
    assert fe.detect_ftp_test_shape(
        [100] * 300, "FTP Test — 60min hour of power (86min)") == "sixty_min"
    assert fe.detect_ftp_test_shape(
        [100] * 300, "FTP Test — Coggan 20min protocol (59min)") == "coggan_20min"
    assert fe.detect_ftp_test_shape(
        [100] * 300, "FTP Test Ramp (35min)") == "ramp"


def test_steady_z2_hour_is_not_a_test():
    # Plateau ≈ ride mean → the 1.10× guard keeps an ordinary endurance
    # hour out.
    assert fe.detect_ftp_test_shape(_steady_z2_hour()) is None


def test_long_ride_containing_hard_hour_is_not_auto_scored():
    # 3 h ride with one maximal hour: non-plateau share > 40% → silent.
    s = [150] * 3600 + [260] * 3600 + [150] * 3600
    assert fe.detect_ftp_test_shape(s) != "sixty_min"


# ── W1a: sixty_min calculator ────────────────────────────────────────────────

def test_sixty_min_value_is_plateau_mean_factor_one():
    r = fe.sixty_min_ftp(_hour_of_power(test_w=250))
    assert r is not None
    assert r["type"] == "sixty_min"
    # Factor 1.0 by definition; plateau extraction may pull in an adjacent
    # opener minute at most, so tolerate ±2 W around 250.
    assert 248 <= r["value"] <= 252
    assert r["plateau_min"] >= 50


def test_sixty_min_early_quit_returns_none():
    # 41 min of effort is not an hour — never average a blind 60-min window
    # across the quit point.
    assert fe.sixty_min_ftp(_hour_of_power(test_min=41)) is None


def test_evaluate_sixty_min_early_quit_falls_back_to_coggan():
    s = _hour_of_power(test_w=250, test_min=41)
    out = fe.evaluate_ftp_test(
        s, filename_hint="ftp_test_60min_100pct_86min.zwo", prior_ftp=240)
    assert out is not None
    sug = out["ftp_test_suggestion"]
    assert out["ftp_test_type"] == "sixty_min"
    assert sug["fallback_from"] == "sixty_min"
    assert sug["method"] == "coggan_20min"
    # 0.95 × best-20 of a 41-min steady effort ≈ 0.95 × 250.
    assert 232 <= sug["ftp"] <= 240


# ── W2b: evaluate_ftp_test single entry point ────────────────────────────────

def test_evaluate_matches_individual_calculators():
    s = _hour_of_power(test_w=250)
    out = fe.evaluate_ftp_test(s, prior_ftp=240)
    assert out["is_ftp_test"] is True
    assert out["ftp_test_type"] == "sixty_min"
    assert out["ftp_test_suggestion"]["value"] == fe.sixty_min_ftp(s)["value"]
    assert out["ftp_test_suggestion"]["prior_ftp"] == 240
    assert out["ftp_test_suggestion"]["pct_delta"] != 0.0


def test_evaluate_none_for_plain_ride():
    assert fe.evaluate_ftp_test(_steady_z2_hour()) is None


# ── 1 Hz resampling (smart recording) ────────────────────────────────────────

def test_resample_sparse_series_stretches_to_wall_time():
    from app import _resample_series_1hz
    # 3-second smart recording: 400 samples over 1197 s.
    ts = [1000.0 + 3 * i for i in range(400)]
    vals = [250] * 400
    out = _resample_series_1hz(ts, vals)
    assert len(out) == 1198
    # Hold-last across ≤3 s gaps → the grid stays at 250 throughout.
    assert out[0] == 250 and out[600] == 250 and out[-1] == 250


def test_resample_1hz_passthrough_untouched():
    from app import _resample_series_1hz
    ts = [float(i) for i in range(300)]
    vals = list(range(300))
    assert _resample_series_1hz(ts, vals) is vals


def test_resample_missing_timestamps_passthrough():
    from app import _resample_series_1hz
    vals = [1, 2, 3]
    assert _resample_series_1hz([None, None, None], vals) is vals


# ── W1a: execution_score protocol-awareness ──────────────────────────────────

def test_perfect_hour_of_power_not_graded_over():
    import execution_score as es
    planned = {"session_type": "ftp_test", "duration_min": 86,
               "tss_estimate": 100,
               "zwo_file": "ftp_test_60min_100pct_86min.zwo"}
    ride = {"duration_min": 86.0, "tss": 100,
            "time_in_zone": {"z1": 1500, "z2": 60, "z3": 0,
                             "z4": 3600, "z5": 0, "z6": 0, "z7": 0}}
    r = es.score_ride(planned, ride, "power")
    assert r["verdict"] == "on_target", r


def test_ramp_test_execution_is_silent():
    import execution_score as es
    planned = {"session_type": "ftp_test", "duration_min": 60,
               "tss_estimate": 60, "zwo_file": "ftp_test_ramp.zwo"}
    ride = {"duration_min": 22.0, "tss": 40,
            "time_in_zone": {"z1": 600, "z2": 200, "z3": 100, "z4": 120,
                             "z5": 100, "z6": 100, "z7": 100}}
    r = es.score_ride(planned, ride, "power")
    assert r["score"] is None


def test_coggan_test_grading_unchanged():
    import execution_score as es
    planned = {"session_type": "ftp_test", "duration_min": 59,
               "tss_estimate": 72,
               "zwo_file": "ftp_test_coggan_20min.zwo"}
    ride = {"duration_min": 59.0, "tss": 72,
            "time_in_zone": {"z1": 1900, "z2": 200, "z3": 0,
                             "z4": 1200, "z5": 240, "z6": 0, "z7": 0}}
    r = es.score_ride(planned, ride, "power")
    assert r["score"] is not None
    assert r["verdict"] in ("on_target", "over")  # pre-existing 0.35 row


# ── W1d: match_zwo family gate ───────────────────────────────────────────────

def test_ftp_test_family_tokens():
    import training_planner as tp
    assert tp._ftp_test_family("ftp_test_coggan_20min.zwo")
    assert tp._ftp_test_family("ftp_test_ramp_10w_step.zwo")
    assert tp._ftp_test_family("ftp_test_60min_100pct_86min.zwo")
    assert tp._ftp_test_family("ftp_test_cts_2x8min_54min.zwo") is None
    assert tp._ftp_test_family("ftp_test_mixed_90min.zwo") is None
    assert tp._ftp_test_family("ftp_test_protocol_53min.zwo") is None


def test_test_slot_never_serves_unscorable_protocol():
    """A test slot must only serve files the calculators can score."""
    import training_planner as tp
    lib = tp.load_workout_library()
    served = [w for w in lib
              if "ftp_test" in {t.lower() for t in (w.get("Tags") or [])}]
    scorable = [w["File"] for w in served if tp._ftp_test_family(w["File"])]
    # The canonical three families exist in the shipped library.
    assert any("ftp_test_coggan" in f for f in scorable)
    assert any("ftp_test_ramp" in f for f in scorable)
    assert any("ftp_test_60min" in f for f in scorable)


# ── W2a: persistence carry ───────────────────────────────────────────────────

def test_ftp_suggestion_and_review_survive_resync(tmp_path, monkeypatch):
    import ride_storage as rs
    icu = tmp_path / "icu"
    icu.mkdir()
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    monkeypatch.setattr(rs, "_maybe_attach_prs", lambda *a, **k: None,
                        raising=False)
    payload = {"id": "i9", "type": "Ride", "name": "FTP Test — Coggan",
               "start_date_local": "2026-08-29T10:00:00", "duration": 3540}
    p = rs.persist_icu_activity(payload)
    data = json.loads(p.read_text())
    data["is_ftp_test"] = True
    data["ftp_test_type"] = "coggan_20min"
    data["ftp_test_suggestion"] = {"ftp": 260, "method": "coggan_20min"}
    data["ftp_test_review"] = {"action": "declined", "at": "2026-08-29T20:00"}
    p.write_text(json.dumps(data))
    # 30-min re-sync re-persists the bare ICU payload.
    p2 = rs.persist_icu_activity(payload)
    fresh = json.loads(p2.read_text())
    assert fresh.get("ftp_test_suggestion", {}).get("ftp") == 260
    assert fresh.get("ftp_test_review", {}).get("action") == "declined"
    assert fresh.get("is_ftp_test") is True
    # And the review ALSO survives a forced detail refresh (rider input).
    p3 = rs.persist_icu_activity(payload, carry_hydrated=False)
    assert json.loads(p3.read_text()).get(
        "ftp_test_review", {}).get("action") == "declined"


# ── W1e: fresh-legs guard safety ─────────────────────────────────────────────

def test_fresh_legs_guard_respects_ridden_and_pinned_days():
    from datetime import date, timedelta
    import training_planner as tp
    today = date.today()

    def _sess(day, stype, status="pending", user_moved=False):
        s = tp.PlannedSession(day=day, day_name="d", session_type=stype,
                              duration_min=60, tss_estimate=60.0,
                              description="")
        s.status = status
        s.user_moved = user_moved
        return s

    done_prev = _sess(today + timedelta(days=1), "vo2max", status="done")
    test1 = _sess(today + timedelta(days=2), "ftp_test")
    pinned_prev = _sess(today + timedelta(days=3), "threshold",
                        user_moved=True)
    test2 = _sess(today + timedelta(days=4), "ftp_test")
    hard_prev = _sess(today + timedelta(days=5), "threshold")
    test3 = _sess(today + timedelta(days=6), "ftp_test")

    class W:
        sessions = [done_prev, test1, pinned_prev, test2, hard_prev, test3]

    tp._ensure_fresh_legs_before_ftp_tests([W()])
    assert done_prev.session_type == "vo2max"      # ridden — untouched
    assert pinned_prev.session_type == "threshold"  # user-placed — untouched
    assert hard_prev.session_type == "recovery"     # plain hard — eased
