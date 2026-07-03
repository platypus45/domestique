"""Configuration — health tracker.

Global constants stay here. Per-profile values (athlete data, ICU creds,
paths) are resolved dynamically via __getattr__ which proxies to
ProfileManager.
"""
import os
from pathlib import Path

# ── Intervals.icu base URL (truly global) ────────────────────────────────────
ICU_BASE = "https://intervals.icu/api/v1"

# ── Intervals.icu OAuth (per-profile "Connect") — global app credentials ──────
# Issued by ICU after app registration (email support@intervals.icu). BLANK until
# provisioned → the OAuth flow returns "oauth_not_configured" and the API-key path
# stays the only option (zero behaviour change until creds land). Read from env so
# the build can inject them without hard-coding. NOTE: the client_secret ships in
# the distributed binary (installed-app OAuth, no PKCE here) — it is NOT truly
# secret; ICU can rotate it. The per-user access token is what protects ride data,
# and it is no more exposed than today's API key.
def _load_oauth_env() -> None:
    """Populate ICU_OAUTH_* from a gitignored ``.oauth.env`` (KEY=VALUE) if present
    so the client_secret never enters this PUBLIC repo. Dev drops the file in the
    repo root; the build bundles it into the frozen app (see domestique.spec) so
    the shipped binary has it. A real environment variable still wins (setdefault)."""
    import sys
    candidates = [Path(__file__).resolve().parent / ".oauth.env"]
    _mei = getattr(sys, "_MEIPASS", None)
    if _mei:
        candidates.append(Path(_mei) / ".oauth.env")
    for _p in candidates:
        try:
            if not _p.is_file():
                continue
            for _line in _p.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
        except Exception:
            pass


_load_oauth_env()
# client_id is NOT secret (it travels in the browser authorize URL) → safe default.
ICU_OAUTH_CLIENT_ID = os.environ.get("ICU_OAUTH_CLIENT_ID", "511")
# client_secret comes ONLY from .oauth.env / the environment — never hard-coded here.
ICU_OAUTH_CLIENT_SECRET = os.environ.get("ICU_OAUTH_CLIENT_SECRET", "")
ICU_OAUTH_AUTHORIZE_URL = "https://intervals.icu/oauth/authorize"
ICU_OAUTH_TOKEN_URL = "https://intervals.icu/api/oauth/token"
ICU_OAUTH_REDIRECT_URI = "http://localhost:8080/oauth/icu/callback"  # port pinned (launcher.py)
# CALENDAR:WRITE (v3.0.1, IP_ICU_PUSH): lets the push engine upsert planned
# workouts onto the athlete's ICU calendar. Pre-existing connections granted
# only the READ set keep working read-only; the UI offers a one-click
# reconnect to pick up the new scope (granted scopes are stamped per profile).
ICU_OAUTH_SCOPES = "ACTIVITY:READ,WELLNESS:READ,CALENDAR:READ,LIBRARY:READ,CALENDAR:WRITE"

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
        "ICU_ACCESS_TOKEN": "icu_access_token",
    }
    if name in _props:
        return getattr(pm, _props[name])
    raise AttributeError(f"module 'config' has no attribute {name!r}")
