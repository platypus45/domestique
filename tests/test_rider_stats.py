"""P2.4 (v3.0.0, G14-G20) — /api/rider-stats aggregation endpoint.

Locks:
  * payload shape: provenance TRIPLE {value, source, source_date} per field,
    source ∈ {icu, manual, derived, fallback} — never a bare "est." tag;
  * empty-states: fallback-valued fields (wprime/pmax source ∈ {"",fallback},
    cp with no persisted key + power-curve cp_w None, lbm default) emit
    value=None; VO2max row is OMITTED when ICU never supplied one;
  * partial payload: a failing subsystem never 500s the endpoint;
  * peaks come from aggregate_power_curve("default", 90);
  * season totals label distance "(ICU rides only)" and count hrTSS loads;
  * UI: stats card at the TOP of the Analysis tab, lazy with the tab.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import profile_manager as pm_mod  # noqa: E402

LOCKED_SOURCES = {"icu", "manual", "derived", "fallback"}


class _StubPM:
    def __init__(self, athlete=None):
        self._athlete = dict(athlete or {})

    @property
    def max_hr(self):
        return self._athlete.get("max_hr") or 190

    def get_ftp_source(self):
        return self._athlete.get("ftp_source")

    def get_ftp_source_date(self):
        return self._athlete.get("ftp_source_date")

    def __getattr__(self, name):
        if name.startswith(("get_", "record_", "_set")) or name == "on_switch":
            return lambda *a, **k: None
        return None


FULL_ATHLETE = {
    "ftp": 250, "ftp_source": "manual", "ftp_source_date": "2026-06-01",
    "weight_kg": 70.0, "lbm_kg": 58.0,
    "lthr": 165, "lthr_source": "manual", "lthr_source_date": "2026-06-01",
    "max_hr": 185, "max_hr_source": "manual",
    "rhr_baseline": 46,
    "cp": 255,
    "wprime_j": 20500, "wprime_source": "manual",
    "pmax_w": 1100, "pmax_source": "icu",
}

CURVE = {
    "window_days": 90, "n_rides": 10, "weight_kg": 70.0, "current_ftp": 250,
    "rider_curve": [
        {"duration_s": 5, "watts": 1050, "watts_per_kg": 15.0,
         "pct_ftp": 420.0, "ride_id": "icu_1", "date": "2026-06-20"},
        {"duration_s": 60, "watts": 520, "watts_per_kg": 7.4,
         "pct_ftp": 208.0, "ride_id": "icu_1", "date": "2026-06-20"},
        {"duration_s": 300, "watts": 330, "watts_per_kg": 4.7,
         "pct_ftp": 132.0, "ride_id": "icu_2", "date": "2026-06-10"},
        {"duration_s": 1200, "watts": 270, "watts_per_kg": 3.9,
         "pct_ftp": 108.0, "ride_id": "icu_3", "date": "2026-05-30"},
    ],
    "pg_2011_baseline": [], "cp_w": 252, "wprime_j": 19800, "pmax_w": 1050,
}


def _ride(d, tss=60, np_w=200, avg_hr=140, dec=3.0, source="icu",
          dist=40.0, dur=3600, load_source="icu"):
    return {"ride_id": f"icu_{d}", "source": source, "external_id": d,
            "started_at": f"{d}T10:00:00", "duration_s": dur, "tss": tss,
            "np_w": np_w, "avg_hr": avg_hr, "decoupling_pct": dec,
            "distance_km": dist, "load_source": load_source}


def _rides_fixture():
    today = date.today()
    out = []
    # 3 recent (EF higher) + 3 prior (EF lower) + 1 old HR-load ride.
    for i in range(3):
        d = (today - timedelta(days=3 + i * 5)).isoformat()
        out.append(_ride(d, np_w=210, avg_hr=140))          # EF 1.5
    for i in range(3):
        d = (today - timedelta(days=25 + i * 5)).isoformat()
        out.append(_ride(d, np_w=196, avg_hr=140))          # EF 1.4
    out.append(_ride((today - timedelta(days=100)).isoformat(),
                     np_w=None, avg_hr=150, dec=None, load_source="hr_icu"))
    return out


@pytest.fixture
def stats_env(monkeypatch):
    """Full happy-path environment: every subsystem stubbed with data."""
    pm = _StubPM(FULL_ATHLETE)
    monkeypatch.setattr(pm_mod.ProfileManager, "get",
                        classmethod(lambda cls: pm))
    import power_curve
    monkeypatch.setattr(power_curve, "aggregate_power_curve",
                        lambda profile_id="default", window_days=90: CURVE)
    monkeypatch.setattr(app_module.db, "query_metric_history",
                        lambda metric, days=365: (
                            [{"date": "2026-06-15", "value": 57.0,
                              "source": "intervals.icu", "notes": None}]
                            if metric == "vo2max" else []))
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_recent_wellness",
                        lambda days=90: [
                            {"id": "2026-06-27", "restingHR": 45,
                             "sportInfo": [{"eftp": 258.4}]},
                            {"id": "2026-06-20", "restingHR": 47,
                             "sportInfo": []},
                        ])
    monkeypatch.setattr(app_module, "api_profile_dfa_rides", lambda: {
        "aggregate": {"hrvt1": {"hr": 142.0, "n_hr": 6,
                                "r2_hr_median": 0.8, "power": 205.0,
                                "n_power": 4, "r2_power_median": 0.75,
                                "date_span": ["2026-05-25", "2026-06-28"]}}})
    monkeypatch.setattr(app_module, "_load_all_rides_safe",
                        lambda: _rides_fixture())
    monkeypatch.setattr(app_module, "_merge_training_load",
                        lambda t: {"ctl": 52.3, "atl": 44.0, "tsb": 8.3,
                                   "source": "icu"})
    monkeypatch.setattr(app_module, "cached", lambda key, fn, ttl=300: {})
    return pm


def _walk_triples(node, path=""):
    """Yield every provenance triple in the payload."""
    if isinstance(node, dict):
        if set(node) >= {"value", "source", "source_date"}:
            yield path, node
        else:
            for k, v in node.items():
                yield from _walk_triples(v, f"{path}.{k}")


# ── payload shape + provenance ───────────────────────────────────────────────

def test_payload_shape_and_provenance(stats_env):
    out = app_module.api_rider_stats()
    assert out["errors"] == []
    p = out["power"]
    assert p["ftp"] == {"value": 250, "source": "manual",
                        "source_date": "2026-06-01"}
    assert p["eftp"] == {"value": 258, "source": "icu",
                         "source_date": "2026-06-27"}
    assert p["w_per_kg"]["value"] == 3.57
    assert p["cp"] == {"value": 255, "source": "manual", "source_date": None}
    assert p["wprime_kj"] == {"value": 20.5, "source": "manual",
                              "source_date": None}
    assert p["pmax"] == {"value": 1100, "source": "icu", "source_date": None}
    # Peaks: 90d window from aggregate_power_curve.
    pk = out["peaks"]
    assert pk["window_days"] == 90
    assert pk["5s"]["watts"] == 1050 and pk["20m"]["watts"] == 270
    assert pk["1m"]["date"] == "2026-06-20"
    # VO2max: ICU-only, labeled.
    assert out["vo2max"]["value"] == 57.0
    assert out["vo2max"]["source"] == "icu"
    assert out["vo2max"]["label"] == "via Intervals.icu"
    # Heart.
    h = out["heart"]
    assert h["lthr"]["value"] == 165 and h["lthr"]["source"] == "manual"
    assert h["max_hr"]["value"] == 185
    assert h["rhr"] == {"value": 46, "source": "manual", "source_date": None}
    assert h["dfa_aet"]["value"] == 142.0
    assert h["dfa_aet"]["watts"] == 205.0
    assert h["dfa_aet"]["source_date"] == "2026-06-28"
    # Efficiency: EF latest + 42d trend up (1.5 vs 1.4 ≈ +7%).
    ef = out["efficiency"]["ef"]
    assert ef["value"] == 1.5
    assert ef["trend"] == "up" and ef["trend_pct"] > 2
    assert out["efficiency"]["decoupling"]["value"] == 3.0
    # Load triples via merged (icu) source.
    assert out["load"]["ctl"] == {"value": 52.3, "source": "icu",
                                  "source_date": date.today().isoformat()}
    # Body.
    assert out["body"]["weight_kg"]["value"] == 70.0
    assert out["body"]["lbm_kg"]["value"] == 58.0
    # Every triple speaks the locked vocabulary.
    for path, t in _walk_triples(out):
        assert t["source"] in LOCKED_SOURCES, f"{path}: {t['source']}"
    # Never a bare "est." tag anywhere in the payload.
    assert '"est."' not in json.dumps(out)


def test_season_totals(stats_env):
    out = app_module.api_rider_stats()
    se = out["season"]
    assert se["distance_label"] == "(ICU rides only)"
    y = se["rolling_365"]
    assert y["rides"] == 7
    assert y["hours"] == 7.0          # 7 × 3600 s
    assert y["tss"] == 420            # 7 × 60
    assert y["distance_km"] == 280    # 7 ICU rides × 40 km
    assert y["hr_loads"] == 1         # the hrTSS-sourced ride is counted+labeled
    assert se["year"]["rides"] >= 6   # calendar-year window


# ── empty-states ─────────────────────────────────────────────────────────────

def test_empty_states_never_fabricate_numbers(stats_env, monkeypatch):
    """Fresh-ish profile: fallback-valued fields render value=None (G-locked)
    and the VO2max row is omitted entirely."""
    pm = _StubPM({"ftp": 250, "weight_kg": 70.0, "lbm_kg": 56.0,  # lbm default
                  "wprime_j": 20000, "wprime_source": "fallback",
                  "pmax_w": 325, "pmax_source": ""})
    monkeypatch.setattr(pm_mod.ProfileManager, "get",
                        classmethod(lambda cls: pm))
    import power_curve
    monkeypatch.setattr(power_curve, "aggregate_power_curve",
                        lambda profile_id="default", window_days=90:
                        {**CURVE, "cp_w": None, "rider_curve": []})
    monkeypatch.setattr(app_module.db, "query_metric_history",
                        lambda metric, days=365: [])
    import ride_storage
    monkeypatch.setattr(ride_storage, "load_recent_wellness", lambda days=90: [])
    monkeypatch.setattr(app_module, "api_profile_dfa_rides",
                        lambda: {"aggregate": {"hrvt1": None}})
    monkeypatch.setattr(app_module, "_load_all_rides_safe", lambda: [])

    out = app_module.api_rider_stats()
    p = out["power"]
    # wprime/pmax fallback-sourced → EMPTY, never the property fallbacks.
    assert p["wprime_kj"]["value"] is None
    assert p["pmax"]["value"] is None
    # cp: no persisted key AND curve cp_w None → EMPTY (keys on cp_w).
    assert p["cp"]["value"] is None and p["cp"]["source"] == "fallback"
    # lbm default → EMPTY.
    assert out["body"]["lbm_kg"]["value"] is None
    # VO2max omitted, not nulled.
    assert "vo2max" not in out
    # No provenance-less numbers: peaks all None.
    assert out["peaks"]["5s"] is None
    assert out["heart"]["dfa_aet"]["value"] is None
    assert out["efficiency"]["ef"]["value"] is None


def test_cp_falls_back_to_power_curve_monod(stats_env, monkeypatch):
    pm = _StubPM({k: v for k, v in FULL_ATHLETE.items() if k != "cp"})
    monkeypatch.setattr(pm_mod.ProfileManager, "get",
                        classmethod(lambda cls: pm))
    out = app_module.api_rider_stats()
    assert out["power"]["cp"] == {"value": 252, "source": "derived",
                                  "source_date": None}


# ── partial payload on subsystem failure (never a 500) ──────────────────────

def test_partial_payload_on_subsystem_failure(stats_env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("subsystem down")
    import power_curve
    monkeypatch.setattr(power_curve, "aggregate_power_curve", _boom)
    monkeypatch.setattr(app_module.db, "query_metric_history", _boom)
    monkeypatch.setattr(app_module, "api_profile_dfa_rides", _boom)
    monkeypatch.setattr(app_module, "_merge_training_load", _boom)

    client = TestClient(app_module.app)
    r = client.get("/api/rider-stats")
    assert r.status_code == 200          # partial payload, never a 500
    out = r.json()
    for section in ("power_curve", "vo2max", "dfa_aet", "load"):
        assert section in out["errors"]
    # Healthy subsystems still delivered.
    assert out["power"]["ftp"]["value"] == 250
    assert out["heart"]["lthr"]["value"] == 165
    assert out["season"]["rolling_365"]["rides"] == 7
    assert "vo2max" not in out
    assert out["load"] == {}
    assert out["heart"]["dfa_aet"]["value"] is None


def test_profile_failure_still_returns(monkeypatch, stats_env):
    monkeypatch.setattr(pm_mod.ProfileManager, "get",
                        classmethod(lambda cls: (_ for _ in ()).throw(
                            RuntimeError("no profile"))))
    out = app_module.api_rider_stats()
    assert "profile" in out["errors"]
    assert out["power"]["ftp"]["value"] is None   # empty-state, no crash


# ── UI structural: card at the TOP of the Analysis tab, lazy ────────────────

def test_stats_card_top_of_analysis_tab_and_lazy():
    html = (ROOT / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    sec = html.index('id="sec-analysis"')
    card = html.index('id="rider-stats-card"')
    fitness = html.index('id="fitness-chart"')
    assert sec < card < fitness, "stats card must sit at the TOP of Analysis"
    # Lazy with the tab: fetched from loadAnalysisTab, not at boot.
    loader = html.index("function loadAnalysisTab()")
    assert "loadRiderStats" in html[loader:loader + 2000]
    assert "/api/rider-stats" in html
    # Greyed staleness per source_date.
    assert "RIDER_STATS_STALE_DAYS" in html
