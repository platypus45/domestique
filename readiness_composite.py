"""v1.1.0 — Bayesian HRV-readiness composite (0-10 score).

This module is intentionally NEW and SEPARATE from the existing readiness.py
(which produces a 0-100 composite via fixed weights and is wired to live home-page
endpoints). The two modules coexist. callers of the legacy compute_readiness()
remain untouched.

Locked contracts (per /tmp/MASTER_DECISIONS_v107_v110_v120_PATCH.md):
  - Module name: readiness_composite (NOT readiness — that exists in production)
  - Function:    compute_readiness_composite(profile_id, date) -> dict
  - Score range: 0-10 (NOT 0-100)
  - Status:      'insufficient_data' | 'static_weights' | 'dynamic_weights'
  - Weights:     hrv_z 0.30 / ln_rmssd_z 0.15 / tsb 0.20 / hooper_z 0.15 /
                 dfa_alpha1_y 0.15 / feel 0.05 (sum = 1.0)
  - Re-norm:     when components missing, sum of available weights must be
                 >= 0.5; otherwise score=None. Available weights re-normalised
                 to 1.0; confidence = sum of available weights pre-renorm.

Component z-scores are computed against the rider's own 60-day rolling
mean ± SD (Plews 2018 / Buchheit 2017 — individual baselines, not population
means). Bayesian update fires when ≥ 60 days of wellness data is available:
once per week, the rolling-correlation matrix between component z-scores and
next-day eFTP-on-ride proxy is collapsed into ridge-regression weights, then
clipped per-component to [0.05, 0.50] and re-normalised to sum to 1.0.
Persisted as athlete_metrics rows metric='readiness_weights' (JSON in notes).
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date as _date, datetime as _datetime, timedelta as _td
from typing import Any

import db

_log = logging.getLogger("domestique.readiness_composite")


# ── Locked weight contract (PATCH G13) ──────────────────────────────────────
W_INITIAL: dict[str, float] = {
    "hrv_z":         0.30,
    "ln_rmssd_z":    0.15,
    "tsb":           0.20,
    "hooper_z":      0.15,
    "dfa_alpha1_y":  0.15,
    "feel":          0.05,
}
# Sum is 1.0; lock invariant.
assert abs(sum(W_INITIAL.values()) - 1.0) < 1e-9

_WEIGHT_FLOOR = 0.05
_WEIGHT_CEIL = 0.50
_MIN_AVAILABLE_WEIGHT = 0.5   # PATCH G13 — < 0.5 returns score=None
_DAYS_FOR_STATIC = 30          # PATCH G7
_DAYS_FOR_DYNAMIC = 60         # PATCH G7
_BASELINE_WINDOW_DAYS = 60     # rolling mean / SD window
_BAYESIAN_UPDATE_INTERVAL_DAYS = 7
_DFA_RECENT_WINDOW_DAYS = 2    # "yesterday's" α1 — accept up to 2 days back


# ── Status tokens ───────────────────────────────────────────────────────────
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_STATIC = "static_weights"
STATUS_DYNAMIC = "dynamic_weights"


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _zscore(value: float | None, series: list[float]) -> float | None:
    """Z-score `value` against the distribution of `series` (sample SD).

    Returns None when value is None or series is too short / has zero SD.
    Clipped to [-3, 3] to bound contribution from rare extreme readings.
    """
    if value is None:
        return None
    vals = [v for v in series if isinstance(v, (int, float)) and not math.isnan(v)]
    if len(vals) < 14:
        # need at least ~2 weeks of data for a meaningful z-score
        return None
    mean = statistics.fmean(vals)
    try:
        sd = statistics.stdev(vals)
    except statistics.StatisticsError:
        return None
    if sd <= 1e-9:
        return None
    z = (value - mean) / sd
    # clip
    return max(-3.0, min(3.0, z))


def _query_wellness_window(end_date: _date, days: int) -> list[dict]:
    """Return wellness rows for the [end_date - days, end_date] window.

    Uses the local SQLite store (db.py:wellness table). Each row has
    keys: date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp.
    """
    conn = db.get_db()
    start = (end_date - _td(days=days)).isoformat()
    end = end_date.isoformat()
    rows = conn.execute(
        "SELECT date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp "
        "FROM wellness WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_daily_log_window(end_date: _date, days: int) -> dict[str, dict]:
    """Return {date_iso: row} for daily_log window."""
    conn = db.get_db()
    start = (end_date - _td(days=days)).isoformat()
    end = end_date.isoformat()
    rows = conn.execute(
        "SELECT date, sleep_quality, fatigue, soreness, stress, mood, hooper_index "
        "FROM daily_log WHERE date >= ? AND date <= ?",
        (start, end),
    ).fetchall()
    return {r["date"]: dict(r) for r in rows}


def _query_dfa_alpha1_recent(end_date: _date) -> float | None:
    """Read yesterday's (or up to _DFA_RECENT_WINDOW_DAYS back) DFA α1 value.

    Source: athlete_metrics rows metric='dfa_alpha1_avg' (v1.0.7 contract).
    Returns None when no recent value or when v1.0.7 hasn't landed yet.
    """
    conn = db.get_db()
    start = (end_date - _td(days=_DFA_RECENT_WINDOW_DAYS)).isoformat()
    end = (end_date - _td(days=1)).isoformat()  # NOT today; "yesterday" semantics
    try:
        row = conn.execute(
            "SELECT value FROM athlete_metrics "
            "WHERE metric = 'dfa_alpha1_avg' AND date >= ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (start, end),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return _safe_float(row["value"] if "value" in row.keys() else row[0])


def _ln_rmssd_7d_for_date(wellness_rows: list[dict], target_date: str) -> float | None:
    """Compute the 7-day mean ln(rmssd) ending at `target_date`.

    Mirrors sleep.py's rolling-7d computation but operates on the wellness
    rows we already have in memory. Returns None if < 4 valid samples in
    the trailing 7 days.
    """
    target = _date.fromisoformat(target_date)
    start = (target - _td(days=6)).isoformat()
    samples: list[float] = []
    for r in wellness_rows:
        if r["date"] < start or r["date"] > target_date:
            continue
        hrv = _safe_float(r.get("hrv"))
        if hrv is not None and hrv > 0:
            samples.append(math.log(hrv))
    if len(samples) < 4:
        return None
    return statistics.fmean(samples)


def _build_baseline_series(
    wellness_rows: list[dict],
    daily_log_map: dict[str, dict],
    target_date: str,
) -> dict[str, list[float]]:
    """Build 60-day series for each z-score-able component."""
    target = _date.fromisoformat(target_date)
    start = (target - _td(days=_BASELINE_WINDOW_DAYS)).isoformat()
    series: dict[str, list[float]] = {
        "hrv": [],
        "ln_rmssd_7d": [],
        "tsb": [],
        "hooper_index": [],
    }
    for r in wellness_rows:
        if r["date"] < start or r["date"] >= target_date:
            continue
        hrv = _safe_float(r.get("hrv"))
        if hrv is not None and hrv > 0:
            series["hrv"].append(hrv)
        ctl = _safe_float(r.get("ctl"))
        atl = _safe_float(r.get("atl"))
        if ctl is not None and atl is not None:
            series["tsb"].append(ctl - atl)
        # 7-day ln_rmssd at this date
        ln7 = _ln_rmssd_7d_for_date(wellness_rows, r["date"])
        if ln7 is not None:
            series["ln_rmssd_7d"].append(ln7)
    for d_iso, row in daily_log_map.items():
        if d_iso < start or d_iso >= target_date:
            continue
        h = _safe_float(row.get("hooper_index"))
        if h is not None:
            series["hooper_index"].append(h)
    return series


def _components_for_date(
    wellness_rows: list[dict],
    daily_log_map: dict[str, dict],
    series: dict[str, list[float]],
    target_date: str,
    dfa_alpha1_y: float | None,
) -> dict[str, float | None]:
    """Compute today's component values + z-scores.

    Returns dict shape:
      {
        "hrv_z":         float | None,
        "ln_rmssd_z":    float | None,
        "tsb":           float | None,        # NOT z-scored — already a balance
        "hooper_z":      float | None,        # inverted so high Hooper = neg
        "dfa_alpha1_y":  float | None,        # raw α1 (yesterday); see scoring
        "feel":          float | None,        # 1-10 → mapped to z via -1..+1
        # raw values for transparency:
        "_raw": {...},
      }
    """
    today_w = next((r for r in wellness_rows if r["date"] == target_date), None)
    today_log = daily_log_map.get(target_date)

    raw_hrv = _safe_float(today_w.get("hrv")) if today_w else None
    raw_ctl = _safe_float(today_w.get("ctl")) if today_w else None
    raw_atl = _safe_float(today_w.get("atl")) if today_w else None
    raw_tsb = (raw_ctl - raw_atl) if (raw_ctl is not None and raw_atl is not None) else None
    raw_ln7 = _ln_rmssd_7d_for_date(wellness_rows, target_date) if wellness_rows else None
    raw_hooper = _safe_float(today_log.get("hooper_index")) if today_log else None
    raw_feel = None
    if today_log:
        # Subjective "feel" — proxy from inverse mood: mood is 1..7 (high=good).
        # Map mood ∈ [1,7] → feel z-ish ∈ [-1.5, +1.5].
        mood_v = _safe_float(today_log.get("mood"))
        if mood_v is not None:
            raw_feel = (mood_v - 4.0) / 2.0  # 1→-1.5, 4→0, 7→+1.5

    components: dict[str, float | None] = {
        "hrv_z":        _zscore(raw_hrv, series.get("hrv", [])),
        "ln_rmssd_z":   _zscore(raw_ln7, series.get("ln_rmssd_7d", [])),
        # TSB is used directly (-30 .. +30); z-scoring it would double-normalise
        # against itself. We map it linearly inside _component_score().
        "tsb":          raw_tsb,
        # Hooper is inverted (high Hooper = high stress = bad), so flip sign.
        "hooper_z":     None,
        "dfa_alpha1_y": dfa_alpha1_y,
        "feel":         raw_feel,
    }
    h_z = _zscore(raw_hooper, series.get("hooper_index", []))
    if h_z is not None:
        components["hooper_z"] = -h_z   # inversion: high Hooper → negative
    components["_raw"] = {
        "hrv": raw_hrv,
        "ln_rmssd_7d": raw_ln7,
        "tsb": raw_tsb,
        "hooper_index": raw_hooper,
        "dfa_alpha1_y": dfa_alpha1_y,
        "mood": today_log.get("mood") if today_log else None,
    }
    return components


def _component_score(key: str, value: float) -> float:
    """Map a raw component value to a 0-10 contribution.

    Each path is calibrated so a "neutral" reading lands at 5.0.
      - z-score components (hrv_z, ln_rmssd_z, hooper_z): z=0 → 5.0,
        z=+2 → ~9.0, z=-2 → ~1.0. Linear: 5 + 2 * z, clipped to [0, 10].
      - TSB: -10 → 4, 0 → 5.5, +10 → 7. Roughly: 5.5 + 0.15 * tsb, clipped.
      - DFA α1 (yesterday): 0.75 is the Rogers 2021 LT1 boundary. < 0.5
        is severe stress (~0), 0.75 = neutral (5.0), > 1.0 = green (~9).
      - feel: already in z-ish space [-1.5, +1.5]; 5 + 2 * feel.
    """
    if key in ("hrv_z", "ln_rmssd_z", "hooper_z"):
        return max(0.0, min(10.0, 5.0 + 2.0 * value))
    if key == "tsb":
        # Linear ramp; +10 ≈ 7.0 (peaked but not detrained), -30 → 1.0.
        s = 5.5 + 0.15 * value
        return max(0.0, min(10.0, s))
    if key == "dfa_alpha1_y":
        # Piecewise: < 0.5 → 0..2 (depressed), 0.75 → 5.0, > 1.0 → 9.0
        if value < 0.5:
            return max(0.0, min(2.0, value * 4.0))
        if value < 0.75:
            return 2.0 + (value - 0.5) / 0.25 * 3.0   # 0.5→2.0, 0.75→5.0
        if value < 1.0:
            return 5.0 + (value - 0.75) / 0.25 * 4.0  # 0.75→5.0, 1.0→9.0
        # Cap upside at α1 = 1.5
        return max(0.0, min(10.0, 9.0 + (value - 1.0) * 2.0))
    if key == "feel":
        return max(0.0, min(10.0, 5.0 + 2.0 * value))
    raise KeyError(f"unknown component key: {key!r}")


def compute_score(components: dict[str, float | None],
                  weights: dict[str, float]) -> tuple[float | None, float]:
    """Locked formula from PATCH G13.

    Returns (score, confidence). When sum of available weights < 0.5,
    returns (None, that_sum). Otherwise re-normalises available weights
    to 1.0 and computes the weighted-mean score.
    """
    available_keys = [k for k in weights
                      if components.get(k) is not None and k in W_INITIAL]
    available_weights = {k: weights[k] for k in available_keys}
    total_weight = sum(available_weights.values())
    if total_weight < _MIN_AVAILABLE_WEIGHT:
        return None, round(total_weight, 3)
    # re-normalise weights so the available subset sums to 1.0
    score = 0.0
    for k in available_keys:
        contrib = _component_score(k, components[k])  # type: ignore[arg-type]
        score += (available_weights[k] / total_weight) * contrib
    return round(score, 2), round(total_weight, 3)


def _load_dynamic_weights(profile_id: str, on_or_before: str) -> dict[str, float] | None:
    """Read the most recent persisted readiness_weights row.

    Persisted as athlete_metrics rows with metric='readiness_weights' and
    notes=<json> blob holding the per-component weights. Returns None when
    no rows exist or JSON malformed.
    """
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT date, notes FROM athlete_metrics "
            "WHERE metric = 'readiness_weights' AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (on_or_before,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        notes = row["notes"] if "notes" in row.keys() else row[1]
        blob = json.loads(notes or "{}")
    except Exception:
        return None
    weights = blob.get("weights")
    if not isinstance(weights, dict):
        return None
    # validate keys
    out: dict[str, float] = {}
    for k in W_INITIAL:
        v = _safe_float(weights.get(k))
        out[k] = v if v is not None else W_INITIAL[k]
    # clip + re-normalise
    out = _clip_and_normalise(out)
    return out


def _clip_and_normalise(weights: dict[str, float]) -> dict[str, float]:
    clipped = {k: max(_WEIGHT_FLOOR, min(_WEIGHT_CEIL, v)) for k, v in weights.items()}
    total = sum(clipped.values())
    if total <= 0:
        return dict(W_INITIAL)
    return {k: round(v / total, 4) for k, v in clipped.items()}


def _next_day_eftp_proxy(wellness_rows: list[dict]) -> dict[str, float]:
    """Build {date_iso: eftp_value} map for ridge regression target."""
    out: dict[str, float] = {}
    for r in wellness_rows:
        eftp = _safe_float(r.get("eftp"))
        if eftp is not None and eftp > 0:
            out[r["date"]] = eftp
    return out


def _bayesian_weight_update(
    profile_id: str,
    target_date: str,
    wellness_rows: list[dict],
    daily_log_map: dict[str, dict],
    series: dict[str, list[float]],
    current_weights: dict[str, float],
) -> dict[str, float]:
    """Simple ridge-regression-style update.

    Walks the past 28-60 days; for each day we have BOTH a component vector
    and a next-day eFTP value, compute the Pearson correlation between each
    component's contribution-score and the next-day eFTP. Then update each
    weight as a convex combination of the prior weight and the magnitude of
    the correlation, clip to [0.05, 0.50], re-normalise to sum to 1.0.

    This is intentionally simple — no scipy dependency, no matrix inversion.
    The aim is to nudge weights toward components that empirically predict
    next-day performance for this rider, not to fit a perfect linear model.
    """
    eftp_map = _next_day_eftp_proxy(wellness_rows)
    if not eftp_map:
        return current_weights

    target = _date.fromisoformat(target_date)
    earliest = (target - _td(days=_BASELINE_WINDOW_DAYS)).isoformat()

    # Per-component sample lists: (component_score, next_day_eftp)
    pairs: dict[str, list[tuple[float, float]]] = {k: [] for k in W_INITIAL}

    # Walk every day in the baseline window; reconstruct components with
    # the same routine we use for "today".
    cur = _date.fromisoformat(earliest)
    while cur < target:
        cur_iso = cur.isoformat()
        cur += _td(days=1)
        next_iso = cur.isoformat()
        if next_iso not in eftp_map:
            continue
        # Need a small backward-looking series to z-score against — use the
        # current 60-day window we already built. For non-z components
        # (tsb, dfa_alpha1_y, feel), we use the raw value.
        comps = _components_for_date(
            wellness_rows, daily_log_map, series, cur_iso,
            dfa_alpha1_y=None,  # no historical α1 for retroactive baseline
        )
        next_eftp = eftp_map[next_iso]
        for k in W_INITIAL:
            v = comps.get(k)
            if v is None:
                continue
            pairs[k].append((_component_score(k, v), next_eftp))

    # Pearson correlation per component → magnitude weight.
    new_weights: dict[str, float] = dict(current_weights)
    for k, samples in pairs.items():
        if len(samples) < 8:   # need at least ~1 week of paired data
            continue
        xs = [s[0] for s in samples]
        ys = [s[1] for s in samples]
        try:
            mx = statistics.fmean(xs)
            my = statistics.fmean(ys)
            sx = statistics.stdev(xs)
            sy = statistics.stdev(ys)
            if sx <= 1e-9 or sy <= 1e-9:
                continue
            cov = sum((x - mx) * (y - my) for x, y in samples) / (len(samples) - 1)
            corr = cov / (sx * sy)
        except statistics.StatisticsError:
            continue
        # corr ∈ [-1, +1]. We want weight to grow with |corr|, but bounded.
        # Convex blend: 0.7 * prior + 0.3 * (|corr| scaled into prior magnitude).
        magnitude = abs(corr)
        scale_target = max(_WEIGHT_FLOOR, min(_WEIGHT_CEIL, 0.5 * magnitude + 0.05))
        new_weights[k] = 0.7 * current_weights[k] + 0.3 * scale_target

    return _clip_and_normalise(new_weights)


def _persist_weights(profile_id: str, target_date: str,
                     weights: dict[str, float]) -> None:
    """Persist weights as athlete_metrics row metric='readiness_weights'.

    value = 0 (sentinel — actual values live in notes JSON blob).
    """
    blob = json.dumps({"profile_id": profile_id, "weights": weights})
    try:
        db.log_metric(target_date, "readiness_weights", 0.0,
                      source="composite_bayesian", notes=blob)
    except Exception as exc:
        _log.warning("readiness_weights persist failed: %s", exc)


def _should_run_bayesian_update(target_date: str) -> bool:
    """Fire at most once per week (Mondays + first call after 7-day gap).

    Looks at the most recent persisted readiness_weights row. If its date
    is more than _BAYESIAN_UPDATE_INTERVAL_DAYS old, we update.
    """
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT date FROM athlete_metrics "
            "WHERE metric = 'readiness_weights' "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return True
    if row is None:
        return True
    last_iso = row["date"] if "date" in row.keys() else row[0]
    try:
        last = _date.fromisoformat(last_iso)
        target = _date.fromisoformat(target_date)
    except Exception:
        return True
    return (target - last).days >= _BAYESIAN_UPDATE_INTERVAL_DAYS


# ── Public entry point ─────────────────────────────────────────────────────

def compute_readiness_composite(profile_id: str, date: str) -> dict:
    """Compute the v1.1.0 readiness-composite score for `profile_id` on `date`.

    Args:
        profile_id: rider identifier (used only to scope the persisted weight
            blob; the wellness/daily_log tables are single-rider per DB).
        date: ISO YYYY-MM-DD.

    Returns:
        {
          "score":      float | None,           # 0-10 or None when insufficient
          "status":     "insufficient_data" | "static_weights" | "dynamic_weights",
          "components": {hrv_z: float|None, ...},
          "weights":    {hrv_z: float, ...},    # the actual weights used
          "confidence": float,                  # sum of available weight pre-renorm
          "advice":     str,
        }
    """
    try:
        target = _date.fromisoformat(date)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"date must be ISO YYYY-MM-DD, got {date!r}") from exc

    # 1. pull wellness window and check minimum-history floor (PATCH G7)
    wellness_rows = _query_wellness_window(target, days=_BASELINE_WINDOW_DAYS + 7)
    n_days = sum(1 for r in wellness_rows
                 if r.get("hrv") is not None or r.get("ctl") is not None)
    if n_days < _DAYS_FOR_STATIC:
        return {
            "score": None,
            "status": STATUS_INSUFFICIENT,
            "components": {},
            "weights": dict(W_INITIAL),
            "confidence": 0.0,
            "advice": (
                f"Need ≥{_DAYS_FOR_STATIC} days of wellness sync to enable "
                f"readiness scoring. Currently have {n_days}."
            ),
        }

    # 2. assemble baselines + today's components
    daily_log_map = _query_daily_log_window(target, days=_BASELINE_WINDOW_DAYS + 7)
    series = _build_baseline_series(wellness_rows, daily_log_map, date)
    dfa_y = _query_dfa_alpha1_recent(target)
    components = _components_for_date(wellness_rows, daily_log_map, series,
                                      date, dfa_alpha1_y=dfa_y)

    # 3. choose weights (static vs dynamic, PATCH G7)
    if n_days < _DAYS_FOR_DYNAMIC:
        weights = dict(W_INITIAL)
        status = STATUS_STATIC
    else:
        # Dynamic path: load persisted weights or initial; maybe update.
        persisted = _load_dynamic_weights(profile_id, on_or_before=date)
        weights = persisted or dict(W_INITIAL)
        if _should_run_bayesian_update(date):
            updated = _bayesian_weight_update(
                profile_id, date, wellness_rows, daily_log_map, series, weights,
            )
            if updated != weights:
                _persist_weights(profile_id, date, updated)
                weights = updated
        status = STATUS_DYNAMIC

    # 4. compute score with re-normalisation (PATCH G13)
    score, confidence = compute_score(components, weights)

    # 5. advice copy
    advice = _advice_for_score(score, status)

    # Strip the internal _raw key from components before returning
    public_components = {k: v for k, v in components.items() if not k.startswith("_")}

    return {
        "score": score,
        "status": status,
        "components": public_components,
        "weights": weights,
        "confidence": confidence,
        "advice": advice,
    }


def _advice_for_score(score: float | None, status: str) -> str:
    if status == STATUS_INSUFFICIENT or score is None:
        return "Need ≥30 days of wellness sync to enable readiness scoring."
    if score >= 8.0:
        return "Green — fully ready for hard work."
    if score >= 5.0:
        return "Normal day. Your prescribed session is appropriate."
    if score >= 3.0:
        return "Soft tier-down recommended. Drop today's hard session by one tier."
    return "Advisory rest day. Recovery takes priority over training."
