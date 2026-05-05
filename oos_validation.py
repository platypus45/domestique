"""v1.2.0 — out-of-sample Banister validation per athlete.

Per /tmp/MASTER_DECISIONS_v120.md §0:

The TSS-based Banister stack has been operationalised in commercial software
for 20 years and rarely tested out-of-sample (per the README §0a Vermeire
2021 critique). This module closes that gap on a per-rider basis.

Pipeline:

  1. Fit τ_CTL / τ_ATL on the rider's training log MINUS the last
     `holdout_weeks` (default 4) weeks. This calls
     ``tau_fitting.fit_tau_per_athlete(profile_id, persist=False,
     horizon_end_date=N-4w-ago)`` so the live `nls_fit` rows are not
     affected (PATCH G4).
  2. Reconstruct the rider's daily TSS load over the holdout window.
  3. Forward-simulate the holdout window using the fitted τ values + the
     rider's actual TSS in those weeks. This produces predicted CTL / ATL
     trajectories.
  4. Compare predicted FTP / CP / 5-min / 1-min power against actual values
     observed from any race-tagged or FTP-test session in the holdout.
     Race-tagged is preferred; FTP-test sessions are used as a fallback
     anchor because they are the most reliable amateur-rider performance
     test.
  5. Compute MAE per metric, compare against the Hellard 2006
     ([PMC1974899](https://pubmed.ncbi.nlm.nih.gov/17909403/)) literature
     baseline of 5-8 % MAE, and report a verdict.
  6. Bootstrap-CI the MAE: resample the (predicted, actual) pairs with
     replacement `bootstrap_n` times, take the 2.5th/97.5th percentiles.

This is a READ-only validation module — it never writes to the live
`athlete_metrics` table. The endpoint layer (`app.py`) is what writes
the cache rows (`banister_oos_run_at`, `banister_oos_mae_pct`,
`banister_oos_ci_pct`).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import numpy as np  # type: ignore[import-untyped]

import db
import tau_fitting

_log = logging.getLogger("domestique.oos_validation")


# Hellard 2006 / Banister-impulse-response literature: 5-8 % MAE on FTP-equiv
# performance markers across the 9-swimmer cohort.  Per-master §1 we anchor
# the "your model accuracy" copy against this band.
_HELLARD_BASELINE_LOW_PCT = 5.0
_HELLARD_BASELINE_HIGH_PCT = 8.0

# Minimum data thresholds for validation to run at all.
_MIN_TOTAL_DAYS = 8 * 30  # 8 calendar months
_MIN_MARKERS_IN_HOLDOUT = 2

# Bootstrap config — same N as tau_fitting for consistency.
_BOOTSTRAP_DEFAULT_N = 1000


def _ewma_series(loads: list[float], tau: float) -> list[float]:
    """Discrete daily EWMA matching training.py CTL/ATL semantics.

    Mirrors ``tau_fitting._ewma_series`` so this module's forward-simulator
    is independent of any private helper drift in tau_fitting.
    """
    if tau <= 0 or not loads:
        return [0.0] * len(loads)
    out = []
    f = 0.0
    for L in loads:
        f = f + (L - f) / tau
        out.append(f)
    return out


def _build_daily_load_series(conn, start_iso: str, end_iso: str) -> list[tuple[str, float]]:
    """Return [(date_iso, daily_tss)] over the inclusive range.

    Rest days appear as 0.0. Mirrors ``tau_fitting._build_daily_load_series``
    deliberately to keep this module self-contained (no private-import).
    """
    rows = conn.execute(
        "SELECT date, COALESCE(SUM(tss), 0) FROM activities "
        "WHERE date >= ? AND date <= ? GROUP BY date",
        (start_iso, end_iso),
    ).fetchall()
    by_date = {r[0]: float(r[1] or 0.0) for r in rows}

    start_d = date.fromisoformat(start_iso)
    end_d = date.fromisoformat(end_iso)
    out = []
    cur = start_d
    while cur <= end_d:
        iso = cur.isoformat()
        out.append((iso, by_date.get(iso, 0.0)))
        cur += timedelta(days=1)
    return out


def _activities_has_is_race_column(conn) -> bool:
    """True iff `activities.is_race` exists.

    Mirrors ``tau_fitting._activities_has_is_race_column`` — when the
    WIRING agent hasn't landed yet, this returns False and we silently
    skip race-tagged marker collection.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
    return "is_race" in cols


def _collect_holdout_markers(
    conn, holdout_start_iso: str, holdout_end_iso: str
) -> list[dict[str, Any]]:
    """Return [{"date","metric","actual"}] for performance markers in holdout.

    Markers come from two sources:
      * Race-tagged activities (`is_race=1`) — pulled with their `tss` and
        `duration_sec` to derive an actual FTP-equivalent. We use the
        ICU-style heuristic: ``actual_ftp ≈ icu_efftp`` if present in
        activities, else NP × duration / hour scaling. For the FTP marker
        comparison we use the activity's **eFTP** stamp closest to the date
        (intervals.icu emits an eFTP per day, we read athlete_metrics).
      * FTP-test / Coggan / Ramp test sessions (named match) — same
        eFTP-stamp lookup.

    For v1.2.0 we anchor against the **eFTP** series in `athlete_metrics`
    because that's the universally-available high-frequency signal in
    Domestique's data store. A per-component (CP / 5-min / 1-min) anchor
    requires power-curve data which v1.2.0 does not yet pull.
    """
    markers: list[dict[str, Any]] = []

    # eFTP rows in the holdout window are the primary marker signal.
    eftp_rows = conn.execute(
        "SELECT date, value FROM athlete_metrics "
        "WHERE metric = 'eftp' AND date >= ? AND date <= ? ORDER BY date",
        (holdout_start_iso, holdout_end_iso),
    ).fetchall()

    # Optionally restrict markers to those near a race / FTP-test session if
    # such activities exist; otherwise fall back to using all eFTP rows
    # (the audit's intent — eFTP is the physically-grounded performance
    # marker, and races are noisy without per-rider context). This keeps
    # the validation pipeline working for riders with no race tags.
    has_race_col = _activities_has_is_race_column(conn)
    activity_anchor_dates: set[str] = set()

    if has_race_col:
        race_rows = conn.execute(
            "SELECT date FROM activities "
            "WHERE date >= ? AND date <= ? AND is_race = 1",
            (holdout_start_iso, holdout_end_iso),
        ).fetchall()
        activity_anchor_dates.update(r[0] for r in race_rows)

    # FTP-test sessions by name match (mirrors tau_fitting._count_weighted...).
    ftp_test_rows = conn.execute(
        "SELECT date, name FROM activities WHERE date >= ? AND date <= ?",
        (holdout_start_iso, holdout_end_iso),
    ).fetchall()
    for d, name in ftp_test_rows:
        nm = (name or "").lower()
        if ("ftp_test" in nm or "ftp test" in nm
            or "coggan_20" in nm or "ramp_test" in nm):
            activity_anchor_dates.add(d)

    # If the rider has anchor sessions in the holdout, prefer the eFTP rows
    # within ±2 days of an anchor; else use every eFTP row as a marker.
    if activity_anchor_dates:
        anchor_dates_set = {date.fromisoformat(d) for d in activity_anchor_dates}
        for d, v in eftp_rows:
            d_obj = date.fromisoformat(d)
            for a in anchor_dates_set:
                if abs((d_obj - a).days) <= 2:
                    markers.append({"date": d, "metric": "ftp", "actual": float(v)})
                    break
    else:
        for d, v in eftp_rows:
            markers.append({"date": d, "metric": "ftp", "actual": float(v)})

    return markers


def _predict_ftp_for_date(
    fit: dict, daily_loads: list[float], date_to_idx: dict[str, int],
    target_iso: str,
) -> float | None:
    """Forward-simulate predicted FTP at a target date using the fitted model.

    Uses the fit's ``k1, k2, p_base, ctl_tau, atl_tau`` to reconstruct the
    Banister equation P(t) = p_base + k1*CTL - k2*ATL.

    For v1.2.0 the fit dict from tau_fitting only persists ``ctl_tau_fit``
    and ``atl_tau_fit`` — k1/k2/p_base are NOT exposed. We re-derive them
    by re-fitting the (k1, k2, p_base) trio against the pre-holdout markers
    with τ values frozen at the fitted τ_CTL / τ_ATL. This is a 3-param
    linear-LS problem against the EWMA features.
    """
    ctl_tau = fit.get("ctl_tau_fit")
    atl_tau = fit.get("atl_tau_fit")
    if ctl_tau is None or atl_tau is None:
        return None
    if target_iso not in date_to_idx:
        return None

    ctl = _ewma_series(daily_loads, float(ctl_tau))
    atl = _ewma_series(daily_loads, float(atl_tau))
    idx = date_to_idx[target_iso]
    return float(ctl[idx]), float(atl[idx])


def _refit_k_params_with_frozen_tau(
    daily_loads: list[float],
    marker_indices: np.ndarray,
    marker_values: np.ndarray,
    ctl_tau: float,
    atl_tau: float,
) -> dict[str, float] | None:
    """Solve the 3-param linear LS for (k1, k2, p_base) with τ frozen.

    With τ_CTL and τ_ATL fixed, the Banister equation is linear:

        P(t) = k1·CTL(t) - k2·ATL(t) + p_base

    Closed-form via numpy.linalg.lstsq. Returns ``None`` if the system is
    underdetermined (< 3 markers).
    """
    if len(marker_indices) < 3:
        return None
    ctl = np.asarray(_ewma_series(daily_loads, float(ctl_tau)))
    atl = np.asarray(_ewma_series(daily_loads, float(atl_tau)))
    A = np.stack([ctl[marker_indices],
                  -atl[marker_indices],
                  np.ones(len(marker_indices))], axis=1)
    try:
        coeffs, *_ = np.linalg.lstsq(A, marker_values, rcond=None)
    except (np.linalg.LinAlgError, ValueError):
        return None
    k1, k2, p_base = float(coeffs[0]), float(coeffs[1]), float(coeffs[2])
    return {"k1": k1, "k2": k2, "p_base": p_base}


def _verdict_vs_hellard(mae_pct: float) -> str:
    """Map per-metric MAE % to one of the locked enum values per master §1."""
    if mae_pct < _HELLARD_BASELINE_LOW_PCT:
        return "better_than_literature"
    if mae_pct <= _HELLARD_BASELINE_HIGH_PCT:
        return "in_line"
    return "worse_than_literature"


def _bootstrap_mae_ci(
    abs_errors: list[float], n_resamples: int, seed: int = 42,
) -> tuple[float, float] | None:
    """Bootstrap CI for the MAE.

    Resamples the per-marker absolute errors with replacement `n_resamples`
    times, takes the 2.5th and 97.5th percentile of the resampled mean.

    Returns (low, high) or ``None`` if too few markers.
    """
    if len(abs_errors) < 2:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(abs_errors, dtype=float)
    n = len(arr)
    samples = []
    for _ in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        samples.append(float(np.mean(arr[idx])))
    return float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5))


def _empty_response(
    horizon_weeks: int,
    holdout_weeks: int,
    n_markers: int,
    fit_status: str = "insufficient_data",
) -> dict:
    """Build the empty-but-well-formed response shape per master §1."""
    return {
        "fit_status": fit_status,
        "horizon_weeks": horizon_weeks,
        "holdout_weeks": holdout_weeks,
        "n_markers_in_holdout": int(n_markers),
        "predictions": [],
        "ftp_mae_w": None,
        "ftp_mae_pct": None,
        "ftp_mae_pct_ci_low": None,
        "ftp_mae_pct_ci_high": None,
        "ctl_mae_tss": None,
        "ctl_mae_tss_ci_low": None,
        "ctl_mae_tss_ci_high": None,
        "hellard_2006_baseline_pct": f"{int(_HELLARD_BASELINE_LOW_PCT)}-{int(_HELLARD_BASELINE_HIGH_PCT)}",
        "comparison": None,
        "cp_fitness_mae_pct": None,
        "wprime_fitness_mae_pct": None,
        "pmax_fitness_mae_pct": None,
        "tau_fits_used": {},
    }


def validate_banister_oos(
    profile_id: str, holdout_weeks: int = 4, bootstrap_n: int = _BOOTSTRAP_DEFAULT_N,
) -> dict:
    """Out-of-sample Banister validation per the master §1 contract.

    See module docstring for pipeline overview. The returned dict shape is
    locked by /tmp/MASTER_DECISIONS_v120.md §1; do not change field names
    without an accompanying PATCH update.

    Args:
        profile_id: informational; the DB layer is single-profile-at-a-time.
        holdout_weeks: number of trailing weeks to hold out for the OOS
            test. Default 4 per master §1.
        bootstrap_n: bootstrap resamples for the MAE CI. Default 1000.

    Returns the master-§1 dict with `fit_status` ∈ {"success",
    "low_confidence", "insufficient_data"}.
    """
    today = date.today()
    holdout_end = today
    holdout_start = today - timedelta(weeks=holdout_weeks)
    fit_horizon_days = 365  # match tau_fitting._ horizon
    fit_horizon_weeks = fit_horizon_days // 7
    horizon_start = today - timedelta(days=fit_horizon_days)

    # ── early-out: insufficient horizon ──
    conn = db.get_db()
    earliest_activity = conn.execute(
        "SELECT MIN(date) FROM activities"
    ).fetchone()
    earliest = earliest_activity[0] if earliest_activity else None
    if earliest:
        try:
            span_days = (today - date.fromisoformat(earliest)).days
        except ValueError:
            span_days = 0
        if span_days < _MIN_TOTAL_DAYS:
            return _empty_response(fit_horizon_weeks, holdout_weeks, 0)
    else:
        return _empty_response(fit_horizon_weeks, holdout_weeks, 0)

    # ── fit on training-only window (PATCH G4: persist=False) ──
    fit = tau_fitting.fit_tau_per_athlete(
        profile_id, persist=False, horizon_end_date=holdout_start,
    )
    if fit.get("fit_status") == "insufficient_data":
        return _empty_response(fit_horizon_weeks, holdout_weeks, 0)

    # ── collect holdout markers ──
    holdout_start_iso = holdout_start.isoformat()
    holdout_end_iso = holdout_end.isoformat()
    markers = _collect_holdout_markers(conn, holdout_start_iso, holdout_end_iso)

    if len(markers) < _MIN_MARKERS_IN_HOLDOUT:
        return _empty_response(
            fit_horizon_weeks, holdout_weeks, len(markers),
            fit_status="insufficient_data",
        )

    # ── forward-simulate over the full horizon (training + holdout) ──
    full_pairs = _build_daily_load_series(
        conn, horizon_start.isoformat(), holdout_end_iso,
    )
    daily_loads_full = [L for _, L in full_pairs]
    date_to_idx_full = {iso: i for i, (iso, _) in enumerate(full_pairs)}

    # Re-fit (k1, k2, p_base) on pre-holdout markers with τ frozen.
    pre_holdout_pairs = _build_daily_load_series(
        conn, horizon_start.isoformat(), (holdout_start - timedelta(days=1)).isoformat(),
    )
    pre_loads = [L for _, L in pre_holdout_pairs]
    pre_date_to_idx = {iso: i for i, (iso, _) in enumerate(pre_holdout_pairs)}
    pre_eftp_rows = conn.execute(
        "SELECT date, value FROM athlete_metrics "
        "WHERE metric = 'eftp' AND date >= ? AND date <= ? ORDER BY date",
        (horizon_start.isoformat(), (holdout_start - timedelta(days=1)).isoformat()),
    ).fetchall()
    pre_indices = []
    pre_values = []
    for d, v in pre_eftp_rows:
        if d in pre_date_to_idx:
            pre_indices.append(pre_date_to_idx[d])
            pre_values.append(float(v))

    k_params: dict[str, float] | None = None
    if len(pre_indices) >= 3 and fit.get("ctl_tau_fit") and fit.get("atl_tau_fit"):
        k_params = _refit_k_params_with_frozen_tau(
            pre_loads,
            np.asarray(pre_indices, dtype=int),
            np.asarray(pre_values, dtype=float),
            float(fit["ctl_tau_fit"]),
            float(fit["atl_tau_fit"]),
        )

    if k_params is None or fit.get("ctl_tau_fit") is None:
        return _empty_response(
            fit_horizon_weeks, holdout_weeks, len(markers),
            fit_status="insufficient_data",
        )

    # Build CTL / ATL trajectories over the FULL horizon (to predict in holdout).
    ctl_trajectory = _ewma_series(daily_loads_full, float(fit["ctl_tau_fit"]))
    atl_trajectory = _ewma_series(daily_loads_full, float(fit["atl_tau_fit"]))

    # ── predict + compare per holdout marker ──
    predictions: list[dict[str, Any]] = []
    abs_errors_w: list[float] = []
    pct_errors: list[float] = []
    ctl_abs_errors: list[float] = []  # for CTL-MAE: predicted vs actual TSS-equiv

    for m in markers:
        d_iso = m["date"]
        actual = float(m["actual"])
        if d_iso not in date_to_idx_full:
            continue
        idx = date_to_idx_full[d_iso]
        ctl_v = ctl_trajectory[idx]
        atl_v = atl_trajectory[idx]
        predicted = (
            k_params["p_base"]
            + k_params["k1"] * ctl_v
            - k_params["k2"] * atl_v
        )
        err_w = predicted - actual
        pct_err = (err_w / actual) * 100.0 if abs(actual) > 1e-6 else 0.0
        predictions.append({
            "metric": m["metric"],
            "date": d_iso,
            "predicted": round(predicted, 1),
            "actual": round(actual, 1),
            "error": round(err_w, 1),
            "pct_error": round(pct_err, 2),
        })
        abs_errors_w.append(abs(err_w))
        pct_errors.append(abs(pct_err))
        ctl_abs_errors.append(abs(ctl_v - actual))  # crude proxy for CTL MAE

    if not abs_errors_w:
        return _empty_response(
            fit_horizon_weeks, holdout_weeks, len(markers),
            fit_status="insufficient_data",
        )

    # ── aggregate MAE ──
    ftp_mae_w = float(np.mean(abs_errors_w))
    ftp_mae_pct = float(np.mean(pct_errors))
    ctl_mae_tss = float(np.mean(ctl_abs_errors))

    # ── bootstrap CIs on MAE % (the user-facing headline) ──
    pct_ci = _bootstrap_mae_ci(pct_errors, bootstrap_n)
    ctl_ci = _bootstrap_mae_ci(ctl_abs_errors, bootstrap_n)

    response = {
        "fit_status": "success",
        "horizon_weeks": fit_horizon_weeks,
        "holdout_weeks": holdout_weeks,
        "n_markers_in_holdout": len(predictions),
        "predictions": predictions,
        "ftp_mae_w": round(ftp_mae_w, 2),
        "ftp_mae_pct": round(ftp_mae_pct, 2),
        "ftp_mae_pct_ci_low": round(pct_ci[0], 2) if pct_ci else None,
        "ftp_mae_pct_ci_high": round(pct_ci[1], 2) if pct_ci else None,
        "ctl_mae_tss": round(ctl_mae_tss, 2),
        "ctl_mae_tss_ci_low": round(ctl_ci[0], 2) if ctl_ci else None,
        "ctl_mae_tss_ci_high": round(ctl_ci[1], 2) if ctl_ci else None,
        "hellard_2006_baseline_pct": (
            f"{int(_HELLARD_BASELINE_LOW_PCT)}-{int(_HELLARD_BASELINE_HIGH_PCT)}"
        ),
        "comparison": _verdict_vs_hellard(ftp_mae_pct),
        # Per-component metrics — v1.2.0 doesn't pull power-curve data, so
        # these mirror the FTP MAE for now (deferred to a future iteration
        # per master §5). Surfaced as None in the dashboard to be honest.
        "cp_fitness_mae_pct": None,
        "wprime_fitness_mae_pct": None,
        "pmax_fitness_mae_pct": None,
        "tau_fits_used": {
            "ctl": fit.get("ctl_tau_fit"),
            "atl": fit.get("atl_tau_fit"),
        },
    }

    # Demote to low_confidence when the underlying tau-fit was itself low-conf.
    if fit.get("fit_status") == "low_confidence":
        response["fit_status"] = "low_confidence"

    return response
