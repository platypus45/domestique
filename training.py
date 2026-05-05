"""Fetch training load metrics from Intervals.icu."""
import json
import logging
import math
import random
import statistics
import time
import urllib.parse
import urllib.request
import urllib.error
import base64
from datetime import date, timedelta
import config

_log = logging.getLogger("domestique.training")


# TRIMP → TSS scaling factor (Banister 1980; also see Morton 1990).
# Used as a cross-sport "effective load" proxy when TSS is unavailable.
# Imported by app.py for the same scaling — single source of truth.
TRIMP_TO_TSS_FACTOR = 0.7

# v4.5.4 FIX-CREDS-PROFILE: Cloudflare in front of intervals.icu returns
# HTTP 403 (error code 1010) for the default ``Python-urllib/3.x`` User-Agent.
# Every ICU call MUST advertise a non-default UA or both sync AND athlete-id
# discovery silently fail. Read at module-import time so it's a constant on
# the request path. Symptom user reported: paste API key in Settings →
# discover_athlete_id() 403s → athlete_id stays empty in .env →
# _icu_credentials_present() returns False → "Connect Intervals.icu in
# Settings" toast on every Sync click despite valid key on disk.
ICU_USER_AGENT = "Domestique/4.5.4 (+https://github.com/platypus45/health_tracker)"

# Classic CTL/ATL impulse-response time constants (days).
# Coggan/Banister defaults for cycling. This app is cycling-focused.
CTL_TAU = 42
ATL_TAU = 7

# v1.0.6 — three-dimensional Banister τ defaults from Kontro 2026 Fig S2.
# These are SINGLE-ATHLETE illustrative examples, not population-validated.
# Profile-overridable. The existing CTL_TAU/ATL_TAU stays as the
# one-dimensional primary curve; the 3D set is additive.
CP_TAU1 = 52.0   # CP fitness time constant (days)
CP_TAU2 = 10.0   # CP fatigue time constant (days)
WPRIME_TAU1 = 5.0
WPRIME_TAU2 = 5.0
PMAX_TAU1 = 10.0
PMAX_TAU2 = 4.0


class ICUCredentialsMissing(Exception):
    """Raised when ICU_ATHLETE_ID or ICU_API_KEY is not configured."""
    pass


class ICUAuthError(Exception):
    """HTTP 401/403 from Intervals.icu — credentials invalid or revoked.

    Bubbles up to db._sync_loop where 5 consecutive hits flip auth_disabled
    and stop retrying until the user fixes their key.
    """
    pass


class ICURateLimitError(Exception):
    """HTTP 429 from Intervals.icu. Carries Retry-After (seconds) if present."""
    def __init__(self, retry_after: float | None = None, *args):
        super().__init__(*args)
        self.retry_after = retry_after


class ICUServerError(Exception):
    """HTTP 5xx from Intervals.icu (after retry budget exhausted) or other 4xx."""
    pass


class ICUNetworkError(Exception):
    """urllib URLError / timeout / OSError after retry budget exhausted."""
    pass


def _require_credentials() -> None:
    """Raise ICUCredentialsMissing if either credential is empty/None."""
    athlete_id = getattr(config, "ICU_ATHLETE_ID", None)
    api_key = getattr(config, "ICU_API_KEY", None)
    if not athlete_id or not api_key:
        raise ICUCredentialsMissing(
            "ICU_ATHLETE_ID and ICU_API_KEY must be configured"
        )


def _auth_header() -> dict:
    _require_credentials()
    token = base64.b64encode(f"API_KEY:{config.ICU_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def discover_athlete_id(api_key: str) -> dict | None:
    """v4.5.3 — auto-detect athlete ID from API key.

    GETs /api/v1/athlete/0 which ICU resolves to the authenticated athlete.
    Returns the parsed JSON dict (with 'id' and 'name' fields) on success,
    or None on auth failure / network error / parse error. Caller pulls
    out whichever fields it needs.

    Eliminates a class of typo-induced HTTP 403 errors where the user
    pastes a valid API key but mistypes the 7-character athlete ID.
    """
    if not api_key:
        return None
    token = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    req = urllib.request.Request(
        "https://intervals.icu/api/v1/athlete/0",
        headers={
            "Authorization": f"Basic {token}",
            "User-Agent": ICU_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and data.get("id"):
                return data
            return None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def upload_fit_to_icu(fit_path: "str | pathlib.Path", retry: int = 1) -> dict:
    """F7 (v4.1.0) — fire-and-forget FIT upload to Intervals.icu.

    POSTs the FIT file bytes to
    ``/athlete/{id}/activities`` as a multipart/form-data ``file`` field.
    Retries once on network/5xx failures, then gives up silently (the user
    can always re-upload later — we don't want the dashboard UI to stall
    on a flaky ICU uplink).

    Returns {ok: bool, status: int, detail: str}. Caller logs the result.
    """
    import pathlib as _pathlib
    import uuid as _uuid
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return {"ok": False, "status": 0, "detail": "no_credentials"}
    path = _pathlib.Path(fit_path)
    if not path.exists():
        return {"ok": False, "status": 0, "detail": "file_missing"}
    try:
        data = path.read_bytes()
    except OSError as e:
        return {"ok": False, "status": 0, "detail": f"read_failed:{e}"}

    # Minimal multipart/form-data body. Avoid requests dependency; urllib
    # is already the HTTP layer for every other ICU call in this module.
    boundary = f"----domestique-{_uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    url = f"{config.ICU_BASE}/athlete/{config.ICU_ATHLETE_ID}/activities"
    for attempt in range(retry + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                **_auth_header(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": ICU_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.getcode()
                if 200 <= status < 300:
                    _log.info(f"EVENT=icu_fit_upload_ok status={status} file={path.name}")
                    return {"ok": True, "status": status, "detail": "uploaded"}
                if 500 <= status < 600 and attempt < retry:
                    time.sleep(2.0)
                    continue
                return {"ok": False, "status": status, "detail": "http_error"}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                _log.warning(f"EVENT=icu_fit_upload_auth status={e.code}")
                return {"ok": False, "status": e.code, "detail": "auth_failed"}
            if 500 <= e.code < 600 and attempt < retry:
                time.sleep(2.0)
                continue
            return {"ok": False, "status": e.code, "detail": str(e)[:120]}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < retry:
                time.sleep(2.0)
                continue
            _log.warning(f"EVENT=icu_fit_upload_network err={e}")
            return {"ok": False, "status": 0, "detail": f"network:{e}"}
    return {"ok": False, "status": 0, "detail": "exhausted"}


def _get(path: str, params: dict | None = None) -> list | dict | None:
    """GET a JSON resource from Intervals.icu.

    Returns the parsed JSON on 2xx. Raises typed errors on persistent failures
    so the background sync loop (db._sync_loop) can react:
      - 401/403                    → ICUAuthError (5-strike disable)
      - 429                        → ICURateLimitError (respect Retry-After)
      - 5xx (after 3 attempts)     → ICUServerError
      - other 4xx                  → ICUServerError
      - URL/timeout/OSError (×3)   → ICUNetworkError

    Retry policy: up to 3 attempts, exponential backoff with jitter for 5xx
    and network errors. 429 honours Retry-After (capped at 60s). 401/403
    short-circuit — no point retrying a bad key.
    """
    url = f"{config.ICU_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = _auth_header()
    headers["User-Agent"] = ICU_USER_AGENT
    req = urllib.request.Request(url, headers=headers)

    last_network_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                try:
                    return json.loads(resp.read())
                except json.JSONDecodeError as e:
                    # Bad JSON from a 2xx is a server-side bug — surface it.
                    _log.warning(f"ICU JSON decode error on {path}: {e}")
                    raise ICUServerError(f"Invalid JSON from {path}: {e}") from e
        except urllib.error.HTTPError as e:
            # 401/403: auth failure — bubble immediately, no retry.
            if e.code in (401, 403):
                _log.warning(f"ICU HTTP {e.code} on {path}: {e.reason}")
                raise ICUAuthError(f"HTTP {e.code} on {path}: {e.reason}") from e
            # 429: rate-limited. If Retry-After header set and we have budget, sleep + retry.
            if e.code == 429:
                retry_after_raw = e.headers.get("Retry-After", "0") if e.headers else "0"
                try:
                    retry_after = float(retry_after_raw or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                if retry_after > 0 and attempt < 2:
                    time.sleep(min(retry_after, 60))
                    continue
                _log.warning(f"ICU HTTP 429 on {path} (Retry-After={retry_after})")
                raise ICURateLimitError(retry_after=retry_after if retry_after > 0 else None)
            # 5xx: server blip — retry with exp backoff + jitter.
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt + random.random())
                continue
            _log.warning(f"ICU HTTP {e.code} on {path}: {e.reason}")
            raise ICUServerError(f"HTTP {e.code} on {path}: {e.reason}") from e
        except urllib.error.URLError as e:
            last_network_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt + random.random())
                continue
            _log.warning(f"ICU URL error on {path}: {e.reason}")
            raise ICUNetworkError(f"URL error on {path}: {e.reason}") from e
        except (TimeoutError, OSError) as e:
            last_network_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt + random.random())
                continue
            _log.warning(f"ICU connection error on {path}: {e}")
            raise ICUNetworkError(f"Connection error on {path}: {e}") from e

    # Defensive: should not reach here.
    if last_network_exc is not None:
        raise ICUNetworkError(f"Retries exhausted for {path}: {last_network_exc}")
    return None


# ── wellness (CTL / ATL / TSB / HRV / RHR / sleep) ──────────────────────────

def fetch_wellness(days: int = 42) -> list[dict]:
    """Return wellness records for the past `days` days, newest last.

    Returns [] if ICU credentials are not configured (graceful degradation).
    Raises ICUAuthError / ICURateLimitError / ICUServerError / ICUNetworkError
    on persistent HTTP / network failures — the background sync loop needs
    these signals to trip the 5-strike auth-disable counter and to respect
    Retry-After. FastAPI handlers call this via app.cached(), which already
    falls back to stale cache / {} on any exception.
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return []
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    data = _get(
        f"athlete/{config.ICU_ATHLETE_ID}/wellness",
        {"oldest": oldest, "newest": newest},
    )
    if not data:
        return []
    return sorted(data, key=lambda r: r.get("id", ""))


# ── activities ────────────────────────────────────────────────────────────────

def fetch_activities(days: int = 14) -> list[dict]:
    """Return activities for the past `days` days.

    Default is 14 days — used by dashboards / recent-activity widgets.
    Callers pass different values for different purposes:
      - db.run_sync(days=90)      — backfill for local SQLite persistence
      - get_today_metrics(days=7) — 7-day rolling window for today's metrics
      - app.py (14 default)       — recent-activity widget / fallback

    Returns [] if ICU credentials are not configured (graceful degradation).
    Raises ICU{Auth,RateLimit,Server,Network}Error on persistent failures so
    the background sync loop can react. FastAPI handlers call this via
    app.cached(), which swallows exceptions into stale cache.
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return []
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    data = _get(
        f"athlete/{config.ICU_ATHLETE_ID}/activities",
        {"oldest": oldest, "newest": newest},
    )
    if not data:
        return []
    return sorted(data, key=lambda r: r.get("start_date_local", ""))


def fetch_recent_activities(
    athlete_id: str | None = None,
    api_key: str | None = None,
    days: int = 90,
) -> list[dict]:
    """v4.4.0 — pull the last `days` of activities for the local rides store.

    Thin wrapper around :func:`fetch_activities` with explicit (athlete_id,
    api_key) overrides so the calendar-sync code in app.py can use credentials
    coming from a profile config file rather than the module-level constants
    set at boot. When called with no overrides falls back to the configured
    credentials.

    Returns [] when credentials are missing (graceful degradation, same as
    fetch_activities). Raises ICU{Auth,RateLimit,Server,Network}Error on
    persistent failures so the caller can decide whether to disable sync.
    """
    if athlete_id and api_key:
        # Temporarily swap config so _get builds the right URL + auth header.
        # v4.5.2 FIX-CREDS-HOTRELOAD: ``del`` in finally instead of restoring
        # ``prev_id`` — config.ICU_* are normally resolved dynamically via
        # ``config.__getattr__`` against ProfileManager._env, but assigning a
        # module-level attribute here would shadow __getattr__ forever after,
        # making subsequent in-memory cred refreshes invisible to readers.
        config.ICU_ATHLETE_ID = athlete_id
        config.ICU_API_KEY = api_key
        try:
            return fetch_activities(days=days)
        finally:
            try:
                del config.ICU_ATHLETE_ID
            except AttributeError:
                pass
            try:
                del config.ICU_API_KEY
            except AttributeError:
                pass
    return fetch_activities(days=days)


def fetch_recent_wellness(
    athlete_id: str | None = None,
    api_key: str | None = None,
    days: int = 90,
) -> list[dict]:
    """v4.5.0 — pull the last `days` of wellness records for the local store.

    Mirrors :func:`fetch_recent_activities` for the wellness endpoint.
    Returns daily records with keys: id (date YYYY-MM-DD), hrv, hrv_baseline,
    restingHR, sleepSecs, sleepScore, weight, fatigue, stress, soreness,
    sportInfo, ctl, atl. Returns [] when credentials are missing (graceful
    degradation, same as fetch_wellness). Raises ICU{Auth,RateLimit,Server,
    Network}Error on persistent failures so the caller can decide whether to
    disable sync.
    """
    if athlete_id and api_key:
        # See fetch_recent_activities above for the ``del``-instead-of-restore
        # rationale (v4.5.2 FIX-CREDS-HOTRELOAD).
        config.ICU_ATHLETE_ID = athlete_id
        config.ICU_API_KEY = api_key
        try:
            return fetch_wellness(days=days)
        finally:
            try:
                del config.ICU_ATHLETE_ID
            except AttributeError:
                pass
            try:
                del config.ICU_API_KEY
            except AttributeError:
                pass
    return fetch_wellness(days=days)


def fetch_activity_detail(activity_id: str) -> dict | None:
    """v4.4.0 — fetch a single activity's full record from ICU.

    Used by GET /api/ride/<id>/detail when the locally-cached normalized
    record lacks fields (e.g. intervals, time-in-zone arrays). Returns None
    on credentials-missing or any failure other than auth (so the calendar
    can still serve the cached version).
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return None
    try:
        data = _get(f"activity/{activity_id}")
    except (ICUAuthError, ICURateLimitError, ICUServerError, ICUNetworkError) as e:
        _log.warning(f"fetch_activity_detail({activity_id}) failed: {e}")
        return None
    return data if isinstance(data, dict) else None


def fetch_activity_intervals(activity_id: str) -> list | None:
    """v4.5.5 — fetch the per-interval breakdown for one activity.

    The bare ``/api/v1/activity/<id>`` endpoint usually returns
    ``icu_intervals=null``; the actual list lives at
    ``/api/v1/activity/<id>/intervals``. Returns a list of interval dicts
    (start_index, moving_time, average_watts, average_heartrate, label,
    type, zone, etc.) or None on failure.
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return None
    try:
        data = _get(f"activity/{activity_id}/intervals")
    except (ICUAuthError, ICURateLimitError, ICUServerError, ICUNetworkError) as e:
        _log.warning(f"fetch_activity_intervals({activity_id}) failed: {e}")
        return None
    if isinstance(data, dict):
        iv = data.get("icu_intervals")
        if isinstance(iv, list):
            return iv
    if isinstance(data, list):
        return data
    return None


def fetch_activity_full(activity_id: str) -> dict | None:
    """v4.5.5 — fetch /activity/<id> and merge the /intervals subpath.

    Returns the activity dict augmented with ``icu_intervals`` populated from
    the /intervals subpath when the bare GET returned null. Caller can then
    feed this straight into ride_storage._normalize_icu_activity().
    """
    detail = fetch_activity_detail(activity_id)
    if not isinstance(detail, dict):
        return None
    if not detail.get("icu_intervals"):
        intervals = fetch_activity_intervals(activity_id)
        if isinstance(intervals, list):
            detail["icu_intervals"] = intervals
    return detail


def fetch_activity_fit_file(activity_id: str) -> bytes | None:
    """v1.0.7 — fetch the raw FIT-file blob for an activity.

    ICU exposes the rider's raw FIT at ``/api/v1/activity/<id>/fit-file``.
    Returns the raw bytes on 200, None on 4xx / 5xx / network failures so
    the caller can degrade gracefully (no DFA α1 cached, ride still imports).
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return None

    url = f"{config.ICU_BASE}/activity/{activity_id}/fit-file"
    headers = _auth_header()
    headers["User-Agent"] = ICU_USER_AGENT
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            if 200 <= status < 300:
                return resp.read()
            _log.warning(f"fetch_activity_fit_file({activity_id}) status={status}")
            return None
    except urllib.error.HTTPError as e:
        _log.warning(f"fetch_activity_fit_file({activity_id}) HTTP {e.code}: {e.reason}")
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log.warning(f"fetch_activity_fit_file({activity_id}) network: {e}")
        return None


def fetch_activity_streams(activity_id: str) -> dict | None:
    """v4.4.0 — fetch raw 1Hz streams for an activity.

    ICU's /api/v1/activity/<id>/streams endpoint returns a list of stream
    dicts (one per channel: power, heart_rate, cadence, …). Returns None on
    failure so the detail endpoint can fall back to whatever local FIT it has
    (or omit samples entirely).
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return None
    try:
        data = _get(f"activity/{activity_id}/streams")
    except (ICUAuthError, ICURateLimitError, ICUServerError, ICUNetworkError) as e:
        _log.warning(f"fetch_activity_streams({activity_id}) failed: {e}")
        return None
    # ICU returns a list of {type, data, ...}; normalize to a dict by type.
    if isinstance(data, list):
        out: dict = {}
        for s in data:
            if not isinstance(s, dict):
                continue
            t = s.get("type")
            d = s.get("data")
            if t and isinstance(d, list):
                out[t] = d
        return out
    if isinstance(data, dict):
        return data
    return None


# ── derived metrics ───────────────────────────────────────────────────────────

def compute_acwr(wellness: list[dict]) -> float | None:
    """ATL / CTL (EWMA-based, from Intervals.icu values)."""
    today = next(
        (w for w in reversed(wellness) if w.get("atl") and w.get("ctl")),
        None,
    )
    if today is None:
        return None
    atl = today["atl"]
    ctl = today["ctl"]
    if ctl == 0:
        return None
    return round(atl / ctl, 3)


def compute_ramp_rate(wellness: list[dict]) -> float | None:
    """CTL change over the last 7 days."""
    records = [w for w in wellness if w.get("ctl")]
    if len(records) < 8:
        return None
    ctl_now = records[-1]["ctl"]
    ctl_7d = records[-8]["ctl"]
    return round(ctl_now - ctl_7d, 1)


def compute_monotony_strain(wellness: list[dict]) -> tuple[float | None, float | None]:
    """Foster (1998) Training Monotony and Strain using daily TSS.

    Monotony = mean(daily_load) / stdev(daily_load) over 7 days.
    Strain   = weekly_load × monotony.

    Uses actual daily TSS from activities (via Intervals.icu wellness data),
    NOT ATL proxy — ATL is an EWMA that smooths out day-to-day variation,
    which is exactly what monotony tries to measure.
    Rest days count as load = 0 (critical for correct SD calculation).
    """
    from collections import defaultdict
    from datetime import date as dt_date, timedelta

    # Build a 7-day load map from wellness data (date → daily TSS)
    today = dt_date.today()
    daily_loads = {}
    for i in range(7):
        d = (today - timedelta(days=i)).isoformat()
        daily_loads[d] = 0.0  # default: rest day = 0

    # Try to get actual daily TSS from activities stored in db
    try:
        import db
        activities = db.query_activities(days=7)
        for a in activities:
            d = a.get("date", "")
            if d in daily_loads:
                daily_loads[d] += (a.get("tss") or 0)
    except Exception:
        # Fallback: estimate daily load from ATL differences (imperfect but better than ATL direct)
        for w in wellness[-7:]:
            d = w.get("id", "")
            if d in daily_loads and w.get("atl") is not None:
                # Rough daily TSS estimate from ATL: load ≈ ATL × 7 (since ATL ≈ 7-day EMA)
                # This is still a proxy, but at least includes rest-day zeros
                pass  # keep as 0 if no activities found

    loads = list(daily_loads.values())
    if len(loads) < 7:
        return None, None
    weekly_load = sum(loads)
    if weekly_load == 0:
        return None, None
    mean_load = statistics.mean(loads)
    try:
        sd_load = statistics.stdev(loads)
    except statistics.StatisticsError:
        return None, None
    if sd_load == 0:
        return 7.0, round(weekly_load * 7.0, 1)  # perfectly monotonous = 7.0 (mean/stdev when all equal)
    monotony = round(mean_load / sd_load, 2)
    strain = round(weekly_load * monotony, 1)
    return monotony, strain


def get_today_metrics() -> dict:
    """Return all training load metrics for today.

    Returns {} gracefully if ICU credentials are missing, or on any ICU API
    failure — FastAPI callers want a no-data result, not a 500.
    """
    try:
        _require_credentials()
    except ICUCredentialsMissing:
        return {}
    try:
        wellness = fetch_wellness(days=42)
    except (ICUAuthError, ICURateLimitError, ICUServerError, ICUNetworkError) as e:
        _log.warning("get_today_metrics: ICU fetch failed (%s) — returning empty", e)
        return {}
    if not wellness:
        return {}

    today_rec = next(
        (w for w in reversed(wellness)
         if w["id"] == date.today().isoformat()),
        wellness[-1],
    )

    ctl  = today_rec.get("ctl")
    atl  = today_rec.get("atl")
    tsb  = round(ctl - atl, 1) if (ctl is not None and atl is not None) else None
    acwr = compute_acwr(wellness)
    ramp = compute_ramp_rate(wellness)
    mono, strain = compute_monotony_strain(wellness)

    # recent activities (last 7 days)
    try:
        activities = fetch_activities(days=7)
    except (ICUAuthError, ICURateLimitError, ICUServerError, ICUNetworkError) as e:
        _log.warning("get_today_metrics: activities fetch failed (%s)", e)
        activities = []
    recent = []
    for a in activities:
        tss = a.get("icu_training_load") or a.get("training_load") or 0
        trimp = a.get("trimp") or 0
        # Effective load: max(TSS, TRIMP × TRIMP_TO_TSS_FACTOR)
        # — best estimate across all sports (Banister 1980).
        effective_load = round(max(tss, trimp * TRIMP_TO_TSS_FACTOR), 1) if trimp else tss
        # ICU returns `raw_json` as a JSON string (not a parsed dict), so the
        # legacy `isinstance(..., dict)` check was always False and the
        # fallback lookup below silently skipped attrs that only exist in
        # the raw payload. Parse the string first, then fall back to {}.
        _raw_val = a.get("raw_json")
        raw: dict = {}
        if isinstance(_raw_val, dict):
            raw = _raw_val
        elif isinstance(_raw_val, str) and _raw_val:
            try:
                _decoded = json.loads(_raw_val)
                if isinstance(_decoded, dict):
                    raw = _decoded
            except (json.JSONDecodeError, ValueError):
                raw = {}
        # Widened projection — needed for kcal estimator and downstream consumers.
        kilojoules = a.get("kilojoules") or raw.get("kilojoules")
        calories = a.get("calories") or raw.get("calories")
        weighted_watts = (
            a.get("weighted_average_watts")
            or a.get("icu_weighted_avg_watts")
            or raw.get("weighted_average_watts")
            or raw.get("icu_weighted_avg_watts")
        )
        moving_time_sec = a.get("moving_time") or a.get("elapsed_time") or raw.get("moving_time")
        distance_km = None
        if a.get("distance") is not None:
            try:
                distance_km = round(float(a.get("distance")) / 1000.0, 2)
            except (TypeError, ValueError):
                distance_km = None
        elevation_gain = a.get("total_elevation_gain") or raw.get("total_elevation_gain")
        recent.append({
            "id": a.get("id"),
            "date": a.get("start_date_local", "")[:10],
            "name": a.get("name", ""),
            "sport": a.get("sport_type", a.get("type", "")),
            "duration_min": round((moving_time_sec or 0) / 60),
            "tss": tss,
            "trimp": round(trimp, 1) if trimp else None,
            "effective_load": effective_load,
            "avg_power": a.get("average_watts"),
            "avg_hr": a.get("average_heartrate"),
            # Widened projection for kcal estimator (see master_bug_report.md §5).
            "distance_km": distance_km,
            "kilojoules": kilojoules,
            "calories": calories,
            "weighted_average_watts": weighted_watts,
            "moving_time_sec": moving_time_sec,
            "elevation_gain": elevation_gain,
        })

    return {
        "date": today_rec["id"],
        "ctl": round(ctl, 1) if ctl is not None else None,
        "atl": round(atl, 1) if atl is not None else None,
        "tsb": tsb,
        "acwr": acwr,
        "ramp_rate": ramp,
        "monotony": mono,
        "strain": strain,
        "recent_activities": recent,
    }


def classify_acwr(acwr: float | None) -> str:
    """Gabbett (2016): 0.8-1.3 sweet spot. Low ACWR = undertraining risk."""
    from config import ACWR_GREEN_LOW, ACWR_GREEN_HIGH, ACWR_ORANGE_HIGH
    if acwr is None:
        return "?"
    if ACWR_GREEN_LOW <= acwr <= ACWR_GREEN_HIGH:
        return "GREEN"
    if acwr < ACWR_GREEN_LOW:
        return "ORANGE"   # undertraining / detraining risk
    if acwr <= ACWR_ORANGE_HIGH:
        return "ORANGE"   # overreaching risk
    return "RED"


def classify_tsb(tsb: float | None) -> str:
    from config import TSB_RED
    if tsb is None:
        return "?"
    if tsb >= -10:
        return "GREEN"
    if tsb >= TSB_RED:
        return "ORANGE"
    return "RED"


def classify_ramp(ramp: float | None) -> str:
    from config import RAMP_RATE_GREEN, RAMP_RATE_ORANGE
    if ramp is None:
        return "?"
    if ramp <= RAMP_RATE_GREEN:
        return "GREEN"
    if ramp <= RAMP_RATE_ORANGE:
        return "ORANGE"
    return "RED"


def classify_monotony(mono: float | None) -> str:
    from config import MONOTONY_GREEN, MONOTONY_RED
    if mono is None:
        return "?"
    if mono < MONOTONY_GREEN:
        return "GREEN"
    if mono < MONOTONY_RED:
        return "ORANGE"
    return "RED"
