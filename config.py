"""Configuration — health tracker.

Global constants stay here. Per-profile values (athlete data, ICU creds,
paths) are resolved dynamically via __getattr__ which proxies to
ProfileManager.
"""
import os
from pathlib import Path

# ── Intervals.icu base URL (truly global) ────────────────────────────────────
ICU_BASE = "https://intervals.icu/api/v1"

# ── Weekly mesocycle planner — Seiler (2010), Stöggl & Sperlich (2014) ───────
WEEKLY_LIT_PCT = 0.80
WEEKLY_HIT_PCT = 0.20
MAX_HIT_PER_WEEK = 2
MIN_HIT_GAP_HOURS = 48
LONG_RIDE_DAY = 6
FTP_TEST_INTERVAL_WEEKS = 6
TAPER_AUTO_LOCK = True
PLAN_RECALC_INTERVAL_DAYS = 7

# ── Training load thresholds ─────────────────────────────────────────────────
ACWR_GREEN_LOW  = 0.85
ACWR_GREEN_HIGH = 1.15
ACWR_ORANGE_HIGH = 1.25
RAMP_RATE_GREEN  = 7
RAMP_RATE_ORANGE = 9
MONOTONY_GREEN   = 1.5
MONOTONY_RED     = 2.0
TSB_RED          = -30

# ── Sleep thresholds (hours) ─────────────────────────────────────────────────
SLEEP_GREEN  = 7.5
SLEEP_ORANGE = 6.5

# ── EA thresholds (kcal/kg LBM/day) — IOC consensus (Mountjoy et al., 2018) ─
EA_OPTIMAL  = 45
EA_SAFE     = 35
EA_DANGER   = 30


# ── Dynamic proxy for per-profile values ─────────────────────────────────────

def __getattr__(name: str):
    """Resolve per-profile values on access via ProfileManager.

    Only evaluates the REQUESTED attribute (not all 16) for performance.
    """
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    # Map attribute names to property accessors (lazy — only requested one evaluates)
    _props = {
        "ATHLETE_FTP_W": "ftp", "ATHLETE_WEIGHT_KG": "weight_kg",
        "ATHLETE_LBM_KG": "lbm_kg", "ATHLETE_LTHR": "lthr",
        "ATHLETE_MAX_HR": "max_hr",
        "HRV_BASELINE_MEAN": "hrv_baseline_mean", "HRV_BASELINE_SD": "hrv_baseline_sd",
        "RHR_BASELINE": "rhr_baseline",
        "ICU_ATHLETE_ID": "icu_athlete_id", "ICU_API_KEY": "icu_api_key",
    }
    if name in _props:
        return getattr(pm, _props[name])
    raise AttributeError(f"module 'config' has no attribute {name!r}")
