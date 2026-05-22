"""v1.3.0 — aggregated power-curve computation + per-ride PRs + ICU history backfill.

Single canonical source of truth for the rider mean-max curve, the per-ride
PR list, and the one-shot detail/streams pull that hydrates the local cache
when it's missing efforts. ALL editorial flags are gone — an effort = an
effort. The only filter that survives is a 1-s sensor-glitch drop (1-s peak
with HR < 50 % HR_max, a wireless-dropout phantom spike).

Read both decision docs before editing:
  /tmp/MASTER_DECISIONS_v130.md          (original plan)
  /tmp/MASTER_DECISIONS_v130_PATCH.md    (overrides on conflict)

Locked invariants:
  G1   — `_aggregate_best_efforts_90d()` becomes a thin shim around this
         module (live in app.py).
  G16  — drop position-based (first-60s / last-30s) filters; offset data is
         not in the cached ICU envelope.
  G9   — sub-HR sensor-glitch filter applies ONLY to 1-s peaks.
  G10  — per-ride `weight_kg` + `ftp_at_ride` drive each point's W/kg + %FTP.
         P&G overlay uses CURRENT profile FTP + weight.
  G2   — atomic writes (tempfile + rename) for backfilled JSONs.
  G3   — single-flight lock at ~/.domestique/cache/.backfill.lock.
  G7   — `compute_ride_prs` returns the FULL list; UI does the cap.
  G15  — backfill idempotency uses `set(e.secs) ⊇ STANDARD_DURATIONS`,
         not just file-presence.
  G4   — caller-side cache key is `(profile, window, latest_ride_id)`;
         this module does not cache.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("domestique.power_curve")


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

# Pinot & Grappe 2011, Table 2 — "elite-amateur" reference power-duration curve
# in W/kg at standard durations. Plotted as a dashed reference line behind the
# rider's curve so the user can see where they sit relative to the population.
# Values: 5s/15s/30s/1m/2m/5m/8m/10m/20m/30m/60m. (We keep all
# STANDARD_DURATIONS keys — durations not in P&G are interpolated linearly in
# log-time.)
_PG_2011_W_PER_KG: dict[int, float] = {
    1:    16.0,   # extrapolation to 1 s — anchored to the 5 s value (P&G
                  # didn't measure < 5 s; a flat extension is conservative).
    5:    16.0,
    15:   13.5,
    30:   11.5,
    60:    9.5,
    120:   7.6,
    300:   5.27,
    480:   4.85,
    600:   4.65,
    1200:  4.30,
    1800:  4.10,
    3600:  3.75,
}


def _profile_dir() -> Path:
    return Path.home() / ".domestique" / "cache"


def _backfill_lock_path() -> Path:
    p = _profile_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p / ".backfill.lock"


def _icu_rides_dir() -> Path:
    base = Path.home() / ".domestique" / "rides" / "icu"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _ride_started_iso_date(ride: dict) -> str:
    s = (ride.get("started_at") or "")[:10]
    return s


def _load_cached_rides() -> list[dict]:
    """Read every cached ICU envelope from disk.

    Returns the list as-is — no filtering, no modification. Callers apply
    window filters.
    """
    out: list[dict] = []
    for f in sorted(_icu_rides_dir().glob("*.json")):
        # Skip dotfiles like .last_sync_at — they're not ride records.
        if f.name.startswith("."):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"power_curve: failed to load {f}: {e}")
            continue
        if isinstance(data, dict) and data.get("ride_id"):
            out.append(data)
    return out


def _filter_rides_by_window(rides: list[dict], window_days: int) -> list[dict]:
    """Filter rides to those started within ``window_days`` of today."""
    if not rides:
        return []
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    return [r for r in rides if _ride_started_iso_date(r) >= cutoff]


def _profile_ftp_weight(profile_id: str = "default") -> tuple[int, float]:
    """Best-effort current FTP + weight for the active profile."""
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        return int(pm.ftp), float(pm.weight_kg)
    except Exception:
        return 200, 70.0


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` via tempfile + rename (G2 atomic-write)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the tempfile if rename failed.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ══════════════════════════════════════════════════════════════════════════════
# SENSOR-GLITCH FILTER (G16 + G9)
# ══════════════════════════════════════════════════════════════════════════════

def is_sensor_glitch(effort: dict, ride: dict, profile: dict) -> bool:
    """Return True iff ``effort`` is a 1-s phantom spike — a sensor glitch.

    Per PATCH G16 + G9, this is the ONLY recording-artifact filter that
    survives v1.3.0:

      effort.secs == 1  AND  HR < 50 % HR_max during the 1-s window.

    HR not rising at all on a real 1-s sprint is physiologically impossible;
    such a reading is a wireless-dropout reporting a phantom 1500 W spike.

    A 30-s sprint with low HR is genuine (HR-lag is real); we keep it.
    """
    if not isinstance(effort, dict):
        return False
    try:
        secs = int(effort.get("secs") or 0)
    except (TypeError, ValueError):
        return False
    if secs != 1:
        return False

    # Pull the effort's contemporaneous HR. The cached envelope shape doesn't
    # carry per-effort HR; the raw streams do. We accept either:
    #   effort.hr        (set when streams were re-extracted by backfill)
    #   effort.hr_at_peak
    eff_hr = effort.get("hr") or effort.get("hr_at_peak")
    try:
        eff_hr = int(eff_hr) if eff_hr is not None else None
    except (TypeError, ValueError):
        eff_hr = None
    if eff_hr is None:
        # No HR data at the effort → cannot apply the glitch filter; counts
        # as-recorded (per "an effort = an effort").
        return False

    # HR_max — first ride.hr_max, then profile (accept BOTH `max_hr` and
    # `hr_max` for robustness — ProfileManager exposes `max_hr`, but earlier
    # callers used `hr_max`; GRILL-WAVE2A W2A-G4 found this mismatch made
    # the filter near-inert on real envelopes).
    hr_max = (ride.get("hr_max")
              or profile.get("max_hr")
              or profile.get("hr_max"))
    try:
        hr_max_i = int(hr_max) if hr_max is not None else None
    except (TypeError, ValueError):
        hr_max_i = None
    if hr_max_i is None or hr_max_i <= 0:
        return False

    return eff_hr < (0.5 * hr_max_i)


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

def aggregate_power_curve(profile_id: str = "default",
                           window_days: int = 90) -> dict:
    """Aggregate the rider's mean-max curve across every cached ride in window.

    Walks ride.efforts (already extracted ICU best-efforts). Each duration
    point is the maximum watts for that duration across all rides in window,
    annotated with the source ride_id + date and per-ride watts_per_kg /
    pct_ftp (G10: uses ride.weight_kg + ride.ftp_at_ride at compute time).

    Output (locked):
      {
        "window_days": 90,
        "n_rides": 53,
        "weight_kg": 71.5,                # current profile weight
        "current_ftp": 248,                # current profile FTP
        "rider_curve": [
          {"duration_s": 300, "watts": 295,
           "watts_per_kg": 4.13, "pct_ftp": 119.0,
           "ride_id": "icu_iXXX", "date": "..."},
          ...
        ],
        "pg_2011_baseline": [
          {"duration_s": 300, "watts_per_kg": 5.27,
           "watts_at_current_weight": 376}, ...
        ],
        "cp_w": 248,
        "wprime_j": 20695,
        "pmax_w": 1115
      }
    """
    profile_ftp, profile_weight = _profile_ftp_weight(profile_id)
    # GRILL-WAVE2A W2A-G4 fix: pass max_hr in the profile dict so
    # is_sensor_glitch can apply the 1-s phantom-spike filter when ride.hr_max
    # is absent. Best-effort fetch via ProfileManager; falls back to None.
    try:
        from profile_manager import ProfileManager
        _pm = ProfileManager.get()
        profile_max_hr = int(_pm.max_hr) if _pm.max_hr else None
    except Exception:
        profile_max_hr = None
    profile = {"ftp": profile_ftp, "weight_kg": profile_weight,
               "max_hr": profile_max_hr}

    all_rides = _load_cached_rides()
    rides = _filter_rides_by_window(all_rides, window_days)

    # Best per duration: {duration_s: (watts, ride_id, date, weight_kg, ftp_at_ride)}
    best: dict[int, tuple[int, str, str, Optional[float], Optional[int]]] = {}
    for r in rides:
        efforts = r.get("efforts") or []
        if not isinstance(efforts, list):
            continue
        ride_id = r.get("ride_id") or ""
        ride_date = _ride_started_iso_date(r)
        ride_weight = r.get("weight_kg")
        ride_ftp = r.get("ftp_at_ride")
        for eff in efforts:
            if not isinstance(eff, dict):
                continue
            try:
                secs_i = int(eff.get("secs") or 0)
                watts_i = int(eff.get("watts") or 0)
            except (TypeError, ValueError):
                continue
            if secs_i <= 0 or watts_i <= 0:
                continue
            # G16 + G9: drop only the 1-s sensor glitch.
            if is_sensor_glitch(eff, r, profile):
                continue
            cur = best.get(secs_i)
            if cur is None or watts_i > cur[0]:
                best[secs_i] = (watts_i, ride_id, ride_date,
                                ride_weight, ride_ftp)

    # Build the rider_curve sorted by duration.
    rider_curve: list[dict] = []
    for secs_i in sorted(best.keys()):
        watts, ride_id, ride_date, ride_weight, ride_ftp = best[secs_i]
        # G10: W/kg uses the ride's weight at the time, falling back to the
        # current profile weight when the ride didn't carry one.
        weight_for_pt = ride_weight if (ride_weight and ride_weight > 0) \
            else profile_weight
        watts_per_kg = round(watts / float(weight_for_pt), 2) \
            if weight_for_pt and weight_for_pt > 0 else None
        # G10: %FTP uses the ride's FTP at the time, falling back to current.
        # v1.8.9 Bug 1 (master §1): pct_ftp MUST be a positive number for
        # every point whose watts > 0. When per-ride ftp is missing/zero,
        # fall back to profile_ftp (which itself defaults to 200 via
        # _profile_ftp_weight). Never emit None when watts > 0.
        ftp_for_pt = ride_ftp if (ride_ftp and ride_ftp > 0) else profile_ftp
        if not (ftp_for_pt and ftp_for_pt > 0):
            ftp_for_pt = profile_ftp if profile_ftp and profile_ftp > 0 else 200
        pct_ftp = round(100.0 * watts / float(ftp_for_pt), 1)
        rider_curve.append({
            "duration_s": secs_i,
            "watts": int(watts),
            "watts_per_kg": watts_per_kg,
            "pct_ftp": pct_ftp,
            "ride_id": ride_id,
            "date": ride_date,
        })

    # P&G 2011 baseline rendered at every STANDARD_DURATIONS tier (G11 scaling
    # to current FTP / weight is documented in the dashboard agent's brief).
    try:
        from fitness_estimation import STANDARD_DURATIONS as _SD
    except Exception:
        _SD = [1, 5, 15, 30, 60, 120, 300, 480, 600, 1200, 1800, 3600]
    pg_baseline: list[dict] = []
    for d in sorted(_SD):
        wpkg = _pg_w_per_kg(d)
        if wpkg is None:
            continue
        watts_at_current = int(round(wpkg * profile_weight))
        pg_baseline.append({
            "duration_s": d,
            "watts_per_kg": round(wpkg, 2),
            "watts_at_current_weight": watts_at_current,
        })

    # CP / W' / Pmax — Monod 2-param fit reusing fitness_estimation.
    cp_w: Optional[int] = None
    wprime_j: Optional[int] = None
    pmax_w: Optional[int] = None
    try:
        from fitness_estimation import compute_cp_wprime, MONOD_DURATIONS_S
        be_dict = {pt["duration_s"]: pt["watts"] for pt in rider_curve
                   if pt["duration_s"] in MONOD_DURATIONS_S}
        if len(be_dict) >= 2:
            res = compute_cp_wprime(be_dict)
            if res:
                cp_w = int(round(res[0]))
                wprime_j = int(round(res[1]))
        # Pmax = the best 1- or 5-s watts in the curve.
        for short_d in (1, 5):
            for pt in rider_curve:
                if pt["duration_s"] == short_d:
                    pmax_w = int(pt["watts"])
                    break
            if pmax_w is not None:
                break
    except Exception as e:
        log.debug(f"power_curve CP/W'/Pmax compute skipped: {e}")

    return {
        "window_days": int(window_days),
        "n_rides": len(rides),
        "weight_kg": float(profile_weight),
        "current_ftp": int(profile_ftp),
        "rider_curve": rider_curve,
        "pg_2011_baseline": pg_baseline,
        "cp_w": cp_w,
        "wprime_j": wprime_j,
        "pmax_w": pmax_w,
    }


def _pg_w_per_kg(duration_s: int) -> Optional[float]:
    """Return the P&G 2011 baseline W/kg at ``duration_s``.

    Uses table values directly when the duration is a measured anchor;
    otherwise log-interpolates between the two surrounding anchors.
    """
    if duration_s in _PG_2011_W_PER_KG:
        return _PG_2011_W_PER_KG[duration_s]
    if duration_s <= 0:
        return None
    anchors = sorted(_PG_2011_W_PER_KG.keys())
    if duration_s < anchors[0] or duration_s > anchors[-1]:
        return None
    # Find bracket.
    import math
    for i in range(len(anchors) - 1):
        lo, hi = anchors[i], anchors[i + 1]
        if lo <= duration_s <= hi:
            ylo = _PG_2011_W_PER_KG[lo]
            yhi = _PG_2011_W_PER_KG[hi]
            t = (math.log(duration_s) - math.log(lo)) / \
                (math.log(hi) - math.log(lo))
            return ylo + t * (yhi - ylo)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PER-RIDE PRs
# ══════════════════════════════════════════════════════════════════════════════

def compute_ride_prs(ride_id: str, window_days: int = 90) -> list[dict]:
    """Return today-vs-rolling-prior-best PRs for a single ride.

    Compares the named ride's effort at each standard duration against the
    maximum across rides in the same ``window_days`` window THAT STARTED
    BEFORE this ride. Only durations where today exceeds prior by ≥1 W
    surface as a PR. Durations with NO prior best in window emit
    ``tier='first'`` (PATCH G6 + GRILL W2A-G11).

    Output (locked, G7 — UI cap is the dashboard's job):
      [{duration_s, today_w, previous_w, previous_date, previous_ride_id,
        exceedance_w, exceedance_pct, tier:'major'|'minor'|'first'}, ...]

    Tiering:
      'major' = exceedance_w ≥ 5 W OR exceedance_pct ≥ 2 %
      'minor' = otherwise (1-5 W exceedance below 2 %).
      'first' = no prior best at this duration in the window.
                previous_* fields are None; exceedance_pct is None.
    """
    if not isinstance(ride_id, str) or not ride_id:
        return []
    rides = _load_cached_rides()
    target = next((r for r in rides if r.get("ride_id") == ride_id), None)
    if target is None:
        return []
    target_date = _ride_started_iso_date(target)
    target_efforts = target.get("efforts") or []
    if not isinstance(target_efforts, list) or not target_efforts:
        return []

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    prior_rides = [
        r for r in rides
        if _ride_started_iso_date(r) >= cutoff
        and _ride_started_iso_date(r) < target_date
        and r.get("ride_id") != ride_id
    ]

    # Build the prior best per duration.
    prior_best: dict[int, tuple[int, str, str]] = {}
    for r in prior_rides:
        for eff in r.get("efforts") or []:
            if not isinstance(eff, dict):
                continue
            try:
                secs_i = int(eff.get("secs") or 0)
                watts_i = int(eff.get("watts") or 0)
            except (TypeError, ValueError):
                continue
            if secs_i <= 0 or watts_i <= 0:
                continue
            cur = prior_best.get(secs_i)
            if cur is None or watts_i > cur[0]:
                prior_best[secs_i] = (
                    watts_i,
                    _ride_started_iso_date(r),
                    r.get("ride_id") or "",
                )

    out: list[dict] = []
    for eff in target_efforts:
        if not isinstance(eff, dict):
            continue
        try:
            secs_i = int(eff.get("secs") or 0)
            today_w = int(eff.get("watts") or 0)
        except (TypeError, ValueError):
            continue
        if secs_i <= 0 or today_w <= 0:
            continue
        prior = prior_best.get(secs_i)
        if prior is None:
            # GRILL-WAVE2A W2A-G11 + PATCH G6: rides 1-N with no prior best at
            # this duration emit a tier='first' entry instead of being silently
            # dropped. Storage carries the FULL list (G7) — the dashboard caps
            # at the top-3 highest watts when rendering tier='first' badges.
            out.append({
                "duration_s": secs_i,
                "today_w": today_w,
                "previous_w": None,
                "previous_date": None,
                "previous_ride_id": None,
                "exceedance_w": today_w,   # vs nothing
                "exceedance_pct": None,    # undefined
                "tier": "first",
            })
            continue
        prev_w, prev_date, prev_ride_id = prior
        exceedance_w = today_w - prev_w
        if exceedance_w < 1:
            continue
        exceedance_pct = round(100.0 * exceedance_w / prev_w, 2) \
            if prev_w > 0 else 0.0
        tier = "major" if (exceedance_w >= 5 or exceedance_pct >= 2.0) \
            else "minor"
        out.append({
            "duration_s": secs_i,
            "today_w": today_w,
            "previous_w": prev_w,
            "previous_date": prev_date,
            "previous_ride_id": prev_ride_id,
            "exceedance_w": exceedance_w,
            "exceedance_pct": exceedance_pct,
            "tier": tier,
        })

    # Sort by duration so the dashboard renders short→long predictably; the
    # cap-by-exceedance is a UI concern (G7).
    out.sort(key=lambda p: p["duration_s"])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# BACKFILL
# ══════════════════════════════════════════════════════════════════════════════

def _needs_refetch(ride_path: Path) -> bool:
    """G15 — return True iff the cached envelope is missing the v1.3.0
    full STANDARD_DURATIONS coverage.

    Re-reads the file each call (cheap; one stat + one parse per ride).
    """
    try:
        from fitness_estimation import STANDARD_DURATIONS as _SD
    except Exception:
        _SD = [1, 5, 15, 30, 60, 120, 300, 480, 600, 1200, 1800, 3600]
    if not ride_path.exists():
        return True
    try:
        data = json.loads(ride_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    efforts = data.get("efforts") or []
    cached_secs = {e.get("secs") for e in efforts if isinstance(e, dict)}
    return not cached_secs.issuperset(set(_SD))


def _extract_efforts_from_streams(streams: dict) -> list[dict]:
    """Compute the v1.3.0 STANDARD_DURATIONS best-efforts from raw streams.

    Sliding-window max-mean over the watts channel. HR pulled from the same
    index so the sensor-glitch filter has data when the dashboard later
    computes the curve. Skips silently on missing power.
    """
    try:
        from fitness_estimation import STANDARD_DURATIONS as _SD
    except Exception:
        _SD = [1, 5, 15, 30, 60, 120, 300, 480, 600, 1200, 1800, 3600]

    pwr = streams.get("watts") or streams.get("power") or []
    hr = streams.get("heartrate") or streams.get("hr") or []
    if not isinstance(pwr, list) or not pwr:
        return []
    powers = [int(p or 0) for p in pwr]
    hrs = [int(h or 0) for h in hr] if isinstance(hr, list) else []
    n = len(powers)
    out: list[dict] = []
    for d in _SD:
        if d > n:
            continue
        # initial sum
        wsum = sum(powers[:d])
        best_sum = wsum
        best_i = 0
        for i in range(1, n - d + 1):
            wsum += powers[i + d - 1] - powers[i - 1]
            if wsum > best_sum:
                best_sum = wsum
                best_i = i
        watts_avg = round(best_sum / d)
        # HR at the start of the window — what we use for the 1-s glitch
        # filter. (For d > 1 the HR field is informational; the filter
        # only fires on d == 1 per G9.)
        hr_at = hrs[best_i] if best_i < len(hrs) else 0
        out.append({
            "label": f"{d}s",
            "watts": int(watts_avg),
            "secs": int(d),
            "hr": int(hr_at) if hr_at else None,
            "offset_s": int(best_i),  # informational only — NO position-
                                       # based filter uses this (G16).
        })
    return out


def acquire_backfill_lock() -> tuple[bool, dict]:
    """G3 — single-flight lock. Returns (acquired, lock_info).

    When already-running, returns (False, {existing_lock_data}). When stale
    (>10 min), reclaims the lock. On acquire, writes the new lock file and
    returns (True, lock_info).
    """
    lock_path = _backfill_lock_path()
    if lock_path.exists():
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        started = float(existing.get("started_at") or 0)
        if started and (time.time() - started) > 600:
            try:
                lock_path.unlink()
            except OSError:
                pass
        else:
            return False, existing or {}
    info = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "task_id": uuid.uuid4().hex,
    }
    _atomic_write_json(lock_path, info)
    return True, info


def release_backfill_lock() -> None:
    """Best-effort lock release."""
    lock_path = _backfill_lock_path()
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass


def backfill_icu_history(profile_id: str = "default",
                          max_per_second: int = 1,
                          _skip_lock: bool = False) -> dict:
    """One-shot detail+streams pull for cached-list rides missing efforts.

    For each ride file in ``~/.domestique/rides/icu/`` that fails the G15
    coverage check, fetches /activity/<id>/streams from ICU, derives
    efforts at every STANDARD_DURATIONS tier, and persists the augmented
    envelope back to disk via atomic write (G2).

    Single-flight lock at ``~/.domestique/cache/.backfill.lock`` (G3); a
    second concurrent call returns ``{"status": "already_running",
    "task_id": <existing>}``.

    Rate-limited at ``max_per_second`` requests / sec. Default 1 to respect
    ICU's published rate limits.

    GRILL-WAVE2A W2A-G2: ``_skip_lock=True`` is for callers that already hold
    the lock at a higher level (the FastAPI endpoint at app.py:1124 holds the
    lock for the worker thread's full lifetime to close the TOCTOU window).

    Returns:
      {"status": "ok" | "already_running",
       "task_id": "...", "backfilled": N, "already_cached": M,
       "failed": K, "elapsed_s": float}
    """
    if _skip_lock:
        # Caller (the FastAPI worker thread) already holds the lock.
        # Build a lock dict locally for task_id continuity.
        lock_path = _backfill_lock_path()
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            lock = {"task_id": uuid.uuid4().hex, "started_at": time.time()}
        acquired = True
    else:
        acquired, lock = acquire_backfill_lock()
    if not acquired:
        return {
            "status": "already_running",
            "task_id": lock.get("task_id"),
            "backfilled": 0,
            "already_cached": 0,
            "failed": 0,
            "elapsed_s": 0.0,
        }

    started_at = time.time()
    task_id = lock.get("task_id") or uuid.uuid4().hex
    backfilled = 0
    already_cached = 0
    failed = 0

    try:
        # ICU fetcher — patched in tests.
        try:
            from training import fetch_activity_streams
        except Exception:
            fetch_activity_streams = None  # type: ignore[assignment]

        delay = 1.0 / max(1, int(max_per_second))
        last_call = 0.0

        for ride_path in sorted(_icu_rides_dir().glob("*.json")):
            if ride_path.name.startswith("."):
                continue
            if not _needs_refetch(ride_path):
                already_cached += 1
                continue
            # Read the existing envelope; we only mutate efforts.
            try:
                data = json.loads(ride_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                failed += 1
                continue
            ext = data.get("external_id") or ""
            if not ext:
                failed += 1
                continue

            # Rate limit.
            now = time.time()
            wait = delay - (now - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.time()

            streams = None
            if fetch_activity_streams is not None:
                try:
                    streams = fetch_activity_streams(str(ext))
                except Exception as e:
                    log.warning(f"backfill: streams fetch {ext} failed: {e}")
                    failed += 1
                    continue
            if not isinstance(streams, dict) or not streams:
                failed += 1
                continue

            efforts = _extract_efforts_from_streams(streams)
            if not efforts:
                failed += 1
                continue

            data["efforts"] = efforts
            try:
                _atomic_write_json(ride_path, data)
            except OSError as e:
                log.warning(f"backfill: write {ride_path} failed: {e}")
                failed += 1
                continue
            backfilled += 1
    finally:
        # GRILL-WAVE2A W2A-G2: when caller passed _skip_lock=True the worker
        # thread owns the lock and releases in its own finally. Don't double-
        # release here.
        if not _skip_lock:
            release_backfill_lock()

    return {
        "status": "ok",
        "task_id": task_id,
        "backfilled": backfilled,
        "already_cached": already_cached,
        "failed": failed,
        "elapsed_s": round(time.time() - started_at, 2),
    }


def latest_ride_id_in_window(profile_id: str = "default",
                              window_days: int = 90) -> str:
    """Return the ride_id of the most recent ride within the window.

    Used for cache invalidation per G4 — when a new ride imports, this
    value changes, so the cache key changes, so the next request
    recomputes. Returns "" when no rides in window.
    """
    rides = _filter_rides_by_window(_load_cached_rides(), window_days)
    if not rides:
        return ""
    rides.sort(key=lambda r: _ride_started_iso_date(r), reverse=True)
    return rides[0].get("ride_id") or ""


# ══════════════════════════════════════════════════════════════════════════════
# FATIGUE RESISTANCE (Pinot 2014 robustness index)
# ══════════════════════════════════════════════════════════════════════════════

# Duration tiers used for the FR index per audit §2 (1/5/15/30/60 min). The
# headline robustness score averages 1/5/15/30 min only — 60-min windows
# rarely surface past 2000 kJ and would skew the mean.
_FR_DURATIONS_S: list[int] = [60, 300, 900, 1800, 3600]
_FR_HEADLINE_DURATIONS_S: set[int] = {60, 300, 900, 1800}

# Allowed kj_threshold values per PATCH G5 — both literature-grounded
# (1500: van Erp 2021 / Mateo-March 2022; 2000: Pinot & Grappe 2014).
_FR_VALID_KJ_THRESHOLDS: set[int] = {1500, 2000}

# A "fresh" peak window starts before the rider has accumulated this much
# kJ. Audit §2 — 0..500 kJ counts as fresh-leg state.
_FR_FRESH_KJ_CEILING: int = 500


def _ride_power_stream(ride: dict) -> list[int]:
    """Return the per-second watts list for ``ride`` if streams are cached.

    The v1.0.6 envelope at ``ride.streams.watts`` carries 1Hz data after
    backfill has hydrated it. Rides without streams contribute NO sliding
    windows but their summary ``ride.kj`` still counts toward the long-
    ride gate (so insufficient_data reasons stay honest).
    """
    streams = ride.get("streams") or {}
    if not isinstance(streams, dict):
        return []
    pwr = streams.get("watts") or streams.get("power") or []
    if not isinstance(pwr, list) or not pwr:
        return []
    out: list[int] = []
    for p in pwr:
        try:
            out.append(int(p) if p is not None else 0)
        except (TypeError, ValueError):
            out.append(0)
    return out


def _fr_per_ride_peaks(power_w: list[int],
                        durations: list[int],
                        kj_threshold: int,
                        fresh_kj_ceiling: int = _FR_FRESH_KJ_CEILING,
                        ) -> dict[int, dict]:
    """Compute fresh + tired peaks per duration for a single ride.

    Walks ``power_w`` (1 Hz). For each duration, slides a window and tracks
    two peak watts:
      • ``fresh_w`` — best window whose kJ-at-start is < ``fresh_kj_ceiling``.
      • ``tired_w`` — best window whose kJ-at-start is ≥ ``kj_threshold``.

    Uses NumPy when present (sub-second on 14400-sample rides). Returns
    ``{duration_s: {"fresh_w", "fresh_kj_at_start",
                    "tired_w", "tired_kj_at_start"}}`` — values are None
    when no window in that bucket exists for the ride.

    The kJ axis ALWAYS resets per ride per audit §6 + brief checklist.
    Cumulative-across-day kJ stacking is explicitly out of scope.
    """
    n = len(power_w)
    out: dict[int, dict] = {}
    if n == 0:
        return out

    # NumPy fast path — vectorise both cumulative kJ and sliding-window mean.
    try:
        import numpy as np
        arr = np.asarray(power_w, dtype=np.float64)
        # cum kJ in kJ (1 W·s == 0.001 kJ).
        cum_kj = np.cumsum(arr) / 1000.0
        # Sliding-window sum via cumsum trick — compute once, reuse per d.
        csum = np.concatenate(([0.0], np.cumsum(arr)))
        for d in durations:
            if d > n:
                out[d] = {"fresh_w": None, "fresh_kj_at_start": None,
                          "tired_w": None, "tired_kj_at_start": None}
                continue
            window_sums = csum[d:] - csum[:-d]
            window_means = window_sums / float(d)
            # kJ-at-start of each window. Window i starts at index i, so
            # the kJ accumulated BEFORE that window is cum_kj[i-1] (or 0
            # when i==0). n_windows == n - d + 1.
            kj_before = np.concatenate(([0.0], cum_kj[:-1]))[: n - d + 1]
            fresh_mask = kj_before < float(fresh_kj_ceiling)
            tired_mask = kj_before >= float(kj_threshold)
            fresh_w = None
            fresh_kj = None
            tired_w = None
            tired_kj = None
            if fresh_mask.any():
                idx = int(np.argmax(np.where(fresh_mask, window_means,
                                              -np.inf)))
                fresh_w = float(window_means[idx])
                fresh_kj = float(kj_before[idx])
            if tired_mask.any():
                idx = int(np.argmax(np.where(tired_mask, window_means,
                                              -np.inf)))
                tired_w = float(window_means[idx])
                tired_kj = float(kj_before[idx])
            out[d] = {
                "fresh_w": fresh_w,
                "fresh_kj_at_start": fresh_kj,
                "tired_w": tired_w,
                "tired_kj_at_start": tired_kj,
            }
        return out
    except ImportError:
        pass

    # Pure-Python fallback (no NumPy) — same algorithm, slower.
    cum_kj = [0.0] * n
    s = 0.0
    for i, p in enumerate(power_w):
        s += float(p) / 1000.0
        cum_kj[i] = s
    for d in durations:
        if d > n:
            out[d] = {"fresh_w": None, "fresh_kj_at_start": None,
                      "tired_w": None, "tired_kj_at_start": None}
            continue
        wsum = sum(power_w[:d])
        best_fresh_w = None
        best_fresh_kj = None
        best_tired_w = None
        best_tired_kj = None
        for i in range(0, n - d + 1):
            if i > 0:
                wsum += power_w[i + d - 1] - power_w[i - 1]
            mean_w = wsum / d
            kj_at_start = cum_kj[i - 1] if i > 0 else 0.0
            if kj_at_start < fresh_kj_ceiling:
                if best_fresh_w is None or mean_w > best_fresh_w:
                    best_fresh_w = mean_w
                    best_fresh_kj = kj_at_start
            if kj_at_start >= kj_threshold:
                if best_tired_w is None or mean_w > best_tired_w:
                    best_tired_w = mean_w
                    best_tired_kj = kj_at_start
        out[d] = {
            "fresh_w": best_fresh_w,
            "fresh_kj_at_start": best_fresh_kj,
            "tired_w": best_tired_w,
            "tired_kj_at_start": best_tired_kj,
        }
    return out


def compute_fatigue_resistance(profile_id: str = "default",
                                window_days: int = 365,
                                kj_threshold: int = 1500) -> dict:
    """Pinot 2014 robustness index — peak power on tired vs fresh legs.

    Per PATCH G5 the kj_threshold is a 2-button toggle; only {1500, 2000}
    are accepted. Determines BOTH the minimum kJ a ride must reach to count
    as a "long ride" AND the kJ accumulation point at which the "tired
    peak" is measured.

    Algorithm (audit §2 / §3):
      1. Walk every cached ride within ``window_days``.
      2. For each ride, compute per-duration sliding-window peaks split by
         the kJ axis: a "fresh" peak (kJ-at-start < 500) and a "tired" peak
         (kJ-at-start >= kj_threshold). Per-ride kJ axis resets to 0
         (a PM ride does NOT carry over the AM ride's kJ).
      3. Long-ride gate: a ride counts toward ``n_long_rides`` iff its
         max kJ reaches kj_threshold. Falls back to ``ride.kj`` summary
         when the streams aren't cached (ride.kj is the canonical total).
      4. Robustness score = mean of FR indices across the headline
         durations (60 / 300 / 900 / 1800 s) — the 60-min window is
         shown in scatter only, not in the headline mean.

    Returns (locked, post W2B-G2 fix-forward):
      {"window_days": 365,
       "n_long_rides": 7,                # rides whose summary kJ >= threshold
       "n_long_rides_with_streams": 6,   # subset that have streams cached
       "fit_status": "success" | "insufficient_data",
       "reason": null | "no_rides_in_window" |
                 "fewer_than_4_long_rides" |
                 "streams_not_hydrated_run_backfill" |
                 "no_fresh_tired_overlap" | "compute_failed",
       "kj_threshold": 1500,
       "robustness_score": 88.4,
       "by_duration": [{"duration_s":300,"fr_index_pct":92.1,
                        "n_data_points":12}, ...],
       "scatter": [{"duration_s":300,"kj":1850,"watts":295,
                    "ride_id":"icu_iXXX","date":"..."}, ...]}

    ``fit_status='insufficient_data'`` when fewer than 4 long rides have
    cached power streams (W2B-G2 fix: was previously firing on summary-
    only rides too, silently misleading users with cached envelopes that
    lack streams). The ``reason`` field tells the dashboard which
    insufficient-data path triggered so it can surface a useful message
    instead of "Need 4 long rides".

    When ``kj_threshold`` is not in {1500, 2000} we coerce to 1500 here
    (graceful — the API endpoint at /api/profile/fatigue-resistance
    rejects with 422 upstream so callers see the explicit error).

    Bonk inclusion (per audit §6 + brief checklist): rides whose power
    drops to 0 in the last hour STILL count — Pinot 2014 includes them
    because fatigue is signal, not noise.
    """
    # Coerce invalid kj_threshold to default 1500 — endpoint validates first.
    if kj_threshold not in _FR_VALID_KJ_THRESHOLDS:
        kj_threshold = 1500

    insufficient_dict = {
        "window_days": int(window_days),
        "n_long_rides": 0,
        "n_long_rides_with_streams": 0,
        "fit_status": "insufficient_data",
        "reason": None,
        "kj_threshold": int(kj_threshold),
        "robustness_score": None,
        "by_duration": [],
        "scatter": [],
    }

    all_rides = _load_cached_rides()
    rides = _filter_rides_by_window(all_rides, window_days)
    if not rides:
        out = dict(insufficient_dict)
        out["reason"] = "no_rides_in_window"
        return out

    # Per-duration aggregates across rides.
    by_duration: dict[int, dict] = {
        d: {"fresh_best_w": 0.0, "tired_best_w": 0.0,
            "n_data_points": 0}
        for d in _FR_DURATIONS_S
    }
    scatter: list[dict] = []
    n_long_rides = 0  # rides whose summary kJ ≥ threshold (any source)
    n_long_rides_with_streams = 0  # rides we can ACTUALLY compute peaks for

    for r in rides:
        ride_id = r.get("ride_id") or ""
        ride_date = _ride_started_iso_date(r)
        # Long-ride gate. Use ride.kj summary as the cheapest signal — it's
        # always present in the v1.0.6 envelope. Fall back to streams if
        # the summary kj is missing.
        ride_kj = r.get("kj")
        try:
            ride_kj_f = float(ride_kj) if ride_kj is not None else 0.0
        except (TypeError, ValueError):
            ride_kj_f = 0.0
        powers = _ride_power_stream(r)
        # When stream is present but envelope kj missing, derive from stream.
        if ride_kj_f == 0.0 and powers:
            ride_kj_f = sum(powers) / 1000.0
        is_long = ride_kj_f >= float(kj_threshold)
        if is_long:
            n_long_rides += 1

        if not powers:
            # No streams cached — can't compute sliding-window peaks. The
            # ride STILL counts toward the diagnostic n_long_rides so the
            # response can explain to the user *why* the score is missing
            # (W2B-G2: avoid silently misleading insufficient_data).
            continue

        if is_long:
            n_long_rides_with_streams += 1

        peaks = _fr_per_ride_peaks(powers, _FR_DURATIONS_S, kj_threshold)
        for d, pk in peaks.items():
            agg = by_duration[d]
            fresh_w = pk.get("fresh_w")
            tired_w = pk.get("tired_w")
            if fresh_w is not None and fresh_w > agg["fresh_best_w"]:
                agg["fresh_best_w"] = float(fresh_w)
            if tired_w is not None:
                if tired_w > agg["tired_best_w"]:
                    agg["tired_best_w"] = float(tired_w)
                # Each ride contributes one tired data point per duration.
                agg["n_data_points"] += 1
                # Scatter row: one point per (ride, duration) tired peak.
                kj_at_start = pk.get("tired_kj_at_start") or 0.0
                scatter.append({
                    "duration_s": int(d),
                    "kj": round(float(kj_at_start), 1),
                    "watts": int(round(float(tired_w))),
                    "ride_id": ride_id,
                    "date": ride_date,
                })

    if n_long_rides < 4:
        out = dict(insufficient_dict)
        out["n_long_rides"] = n_long_rides
        out["n_long_rides_with_streams"] = n_long_rides_with_streams
        out["reason"] = "fewer_than_4_long_rides"
        return out

    if n_long_rides_with_streams < 4:
        # User has enough long rides on the calendar BUT the per-second
        # power streams aren't cached for them — can't compute peaks.
        # W2B-G2: explain instead of silently saying insufficient_data.
        out = dict(insufficient_dict)
        out["n_long_rides"] = n_long_rides
        out["n_long_rides_with_streams"] = n_long_rides_with_streams
        out["reason"] = "streams_not_hydrated_run_backfill"
        return out

    # Build by_duration response + headline robustness mean.
    by_duration_out: list[dict] = []
    headline_indices: list[float] = []
    for d in _FR_DURATIONS_S:
        agg = by_duration[d]
        fresh = agg["fresh_best_w"]
        tired = agg["tired_best_w"]
        n_pts = agg["n_data_points"]
        fr_index = None
        if fresh > 0 and tired > 0:
            fr_index = round(100.0 * tired / fresh, 1)
            if d in _FR_HEADLINE_DURATIONS_S:
                headline_indices.append(float(fr_index))
        by_duration_out.append({
            "duration_s": int(d),
            "fr_index_pct": fr_index,
            "n_data_points": int(n_pts),
        })

    if not headline_indices:
        # We have ≥4 long rides BUT no overlap of fresh + tired peaks at
        # any of the headline durations. Honest insufficient.
        out = dict(insufficient_dict)
        out["n_long_rides"] = n_long_rides
        out["n_long_rides_with_streams"] = n_long_rides_with_streams
        out["reason"] = "no_fresh_tired_overlap"
        return out

    robustness = round(sum(headline_indices) / len(headline_indices), 1)

    return {
        "window_days": int(window_days),
        "n_long_rides": int(n_long_rides),
        "n_long_rides_with_streams": int(n_long_rides_with_streams),
        "fit_status": "success",
        "reason": None,
        "kj_threshold": int(kj_threshold),
        "robustness_score": robustness,
        "by_duration": by_duration_out,
        "scatter": scatter,
    }
