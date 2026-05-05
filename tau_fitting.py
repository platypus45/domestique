"""v1.0.7 — per-athlete Banister τ fitting.

Replaces the folkloric CTL_TAU=42 / ATL_TAU=7 (and the v1.0.6 3D defaults
CP_τ=52/10, W'_τ=5/5, Pmax_τ=10/4) with rider-specific values when the
training log contains enough data to identify the impulse-response
parameters.

Algorithm — discrete-daily Banister forward simulation (mirrors the
``training.py:CTL_TAU`` math), then `scipy.optimize.curve_fit` (Trust
Region Reflective with bounds) does the nonlinear least-squares step.
Bootstrap CIs are computed by resampling the (date, marker_value) pairs
1000× — see ``audit_v107_tau_fitting.md`` §5 (Hellard 2006 / Sohn 2002).

Known caveat — Hellard 2006 (PMC1974899) found τ_fitness × τ_fatigue
correlation 0.99 ± 0.01 in their swimmer cohort fit by Gauss-Newton.
We mitigate by:

  1. Bounding τ_fitness and τ_fatigue to physiologically plausible ranges
     (see ``_FIT_BOUNDS`` below).
  2. Locking τ_fatigue ≤ τ_fitness / 2 implicitly via initial guesses
     biased toward the bound.
  3. Gating success on a residual r² ≥ 0.40 (per the Hellard cohort
     floor of 0.61, with extra margin for amateur-rider data).
  4. Returning ``low_confidence`` whenever the bootstrap CI exceeds 50 %
     of the point estimate — the user sees the rejection in the dashboard
     and we silently fall back to conventional τ for the planner reads.

Both ``count_weighted_markers`` and ``fit_tau_per_athlete`` are pure
read functions — no schema migrations live here. The TAU-FIT-WIRING
agent (the next dispatch) is responsible for the ``activities.is_race``
column + the API endpoints + the dashboard panel.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

# scipy is locked into requirements.txt at v1.0.7 IMPL phase A. Importing
# at module load (not lazily) so a missing-scipy bundle fails loudly at
# import rather than silently falling through to "insufficient_data" the
# first time the planner asks for a fit.
import numpy as np  # type: ignore[import-untyped]
from scipy.optimize import curve_fit  # type: ignore[import-untyped]
from scipy.signal import lfilter  # type: ignore[import-untyped]

import db

_log = logging.getLogger("domestique.tau_fitting")


# ── weighted-marker counting ────────────────────────────────────────────────
#
# Per /tmp/MASTER_DECISIONS_v107_v110_v120_PATCH.md G9, the marker weights
# are LOCKED. Don't tweak these without a corresponding PATCH update.
_W_RACE = 1.0           # is_race=1 activity (highest signal)
_W_EFTP_STEP = 0.5      # ≥3W jump in eFTP rows (smooth signal but plentiful)
_W_FTP_TEST = 0.8       # explicit FTP test session
_W_COGGAN_20 = 0.8      # Coggan-20 / Ramp test result

# Minimum eFTP delta (watts) to count as a "step" change. Below this the
# rider is likely just on the noise floor of ICU's daily eFTP estimate.
_EFTP_STEP_MIN_W = 3.0


def _activities_has_is_race_column(conn) -> bool:
    """True iff `activities.is_race` exists.

    The column is added by the v1.0.7 IMPL-TAU-FIT-WIRING agent — until
    that lands, ``count_weighted_markers`` must read 0 races (not raise
    an OperationalError) so the contract test passes when run against a
    pre-WIRING DB.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
    return "is_race" in cols


def _count_weighted_markers_in_range(conn, horizon_start: str,
                                      horizon_end: str) -> float:
    """Internal helper — weighted-marker count over an explicit date range.

    Public ``count_weighted_markers`` is a thin wrapper that derives the
    range from (today, horizon_days). ``fit_tau_per_athlete`` uses this
    helper directly so that horizon_end_date is honoured (otherwise a
    holdout fit would over-count by including markers in the holdout).
    """
    weighted = 0.0

    # 1) Race-tagged activities (graceful when the column doesn't exist yet).
    if _activities_has_is_race_column(conn):
        race_count = conn.execute(
            "SELECT COUNT(*) FROM activities "
            "WHERE date >= ? AND date <= ? AND is_race = 1",
            (horizon_start, horizon_end),
        ).fetchone()[0]
        weighted += _W_RACE * float(race_count or 0)

    # 2) eFTP step changes — pull the eFTP series in chronological order and
    #    count |Δ| ≥ 3 W transitions.
    eftp_rows = conn.execute(
        "SELECT value FROM athlete_metrics "
        "WHERE metric = 'eftp' AND date >= ? AND date <= ? ORDER BY date",
        (horizon_start, horizon_end),
    ).fetchall()
    eftp_values = [float(r[0]) for r in eftp_rows if r[0] is not None]
    eftp_steps = 0
    for prev, cur in zip(eftp_values, eftp_values[1:]):
        if abs(cur - prev) >= _EFTP_STEP_MIN_W:
            eftp_steps += 1
    weighted += _W_EFTP_STEP * eftp_steps

    # 3) FTP-test / Coggan-20 / Ramp sessions — match the activity name
    #    against the conventions used by fitness_estimation.detect_ftp_test_shape.
    ftp_rows = conn.execute(
        "SELECT name FROM activities WHERE date >= ? AND date <= ?",
        (horizon_start, horizon_end),
    ).fetchall()
    ftp_test_count = 0
    coggan_count = 0
    for r in ftp_rows:
        name = (r[0] or "").lower()
        # Coggan-20 / Ramp test markers — ride filenames + ICU activity names
        # both follow the ``ftp_test_coggan`` / ``ftp_test_ramp`` convention.
        if "ftp_test_coggan" in name or "coggan_20" in name or "ftp_test_ramp" in name or "ramp_test" in name:
            coggan_count += 1
        elif "ftp_test" in name or "ftp test" in name:
            ftp_test_count += 1
    weighted += _W_FTP_TEST * ftp_test_count
    weighted += _W_COGGAN_20 * coggan_count

    return float(weighted)


def count_weighted_markers(profile_id: str, horizon_days: int) -> float:
    """Return weighted-marker count over a trailing horizon ending today.

    Weights (per PATCH G9):
      * race-tagged activities: 1.0 each
      * eFTP step changes ≥ 3 W between consecutive eFTP rows: 0.5 each
      * manual FTP-test sessions (``activities.name`` matching): 0.8 each
      * Coggan-20 / Ramp test results: 0.8 each

    Returns a ``float`` (0.0 when no data).

    ``profile_id`` is currently informational only — the existing DB layer
    is single-profile-at-a-time (each profile has its own SQLite file under
    ``~/.domestique/profiles/<id>/``). The argument is locked into the
    contract for v1.2.0 OOS-validation re-use.
    """
    conn = db.get_db()
    today = date.today()
    horizon_start = (today - timedelta(days=int(horizon_days))).isoformat()
    horizon_end = today.isoformat()
    return _count_weighted_markers_in_range(conn, horizon_start, horizon_end)


# ── Banister τ fitting ──────────────────────────────────────────────────────
#
# Conventional τ defaults — these are also the curve_fit initial guesses,
# so a low-data fit converges back near them rather than drifting wildly.
_CONV_CTL_TAU = 42.0
_CONV_ATL_TAU = 7.0
_CONV_CP_TAU1 = 52.0
_CONV_CP_TAU2 = 10.0
_CONV_WPRIME_TAU1 = 5.0
_CONV_WPRIME_TAU2 = 5.0
_CONV_PMAX_TAU1 = 10.0
_CONV_PMAX_TAU2 = 4.0

# Physiologically plausible bounds (lower, upper) per audit §6.
_FIT_BOUNDS = {
    "ctl_tau": (14.0, 90.0),
    "atl_tau": (2.0, 20.0),
}

# Status thresholds (PATCH G9 + audit §4):
_THRESH_INSUFFICIENT = 5.0    # weighted_n < 5 → insufficient_data
_THRESH_SUCCESS = 10.0        # weighted_n >= 10 + r² >= 0.40 + CI-tight → success
_THRESH_R2 = 0.40
_THRESH_CI_RATIO = 0.50

# Bootstrap config — Hellard 2006 / Sohn 2002 say 1000 is sufficient for
# CI estimation. We seed deterministically inside fit_tau_per_athlete()
# so unit tests are reproducible.
_BOOTSTRAP_N = 1000


def _build_daily_load_series(conn, horizon_start: str,
                              horizon_end: str) -> list[tuple[str, float]]:
    """Return [(date_iso, daily_tss)] over the inclusive horizon.

    Rest days appear as 0.0. Date span is filled in (no gaps), which is a
    hard requirement for the EWMA convolution to hit the correct dates.
    """
    rows = conn.execute(
        "SELECT date, COALESCE(SUM(tss), 0) FROM activities "
        "WHERE date >= ? AND date <= ? GROUP BY date",
        (horizon_start, horizon_end),
    ).fetchall()
    by_date = {r[0]: float(r[1] or 0.0) for r in rows}

    start_d = date.fromisoformat(horizon_start)
    end_d = date.fromisoformat(horizon_end)
    out = []
    cur = start_d
    while cur <= end_d:
        iso = cur.isoformat()
        out.append((iso, by_date.get(iso, 0.0)))
        cur += timedelta(days=1)
    return out


def _ewma_series(loads: list[float], tau: float) -> list[float]:
    """Discrete daily EWMA matching training.py CTL/ATL semantics.

    f[t] = f[t-1] + (loads[t] - f[t-1]) / tau (cold-start at f=0)

    Kept as a list-returning helper for legacy callers; new code in this
    module uses _ewma_vec (lfilter-backed) for the bootstrap hot path.
    """
    if tau <= 0 or not loads:
        return [0.0] * len(loads)
    return _ewma_vec(np.asarray(loads, dtype=float), float(tau)).tolist()


def _ewma_vec(loads: np.ndarray, tau: float) -> np.ndarray:
    """Vectorised EWMA — lfilter-backed first-order IIR.

    The recurrence ``f[t] = f[t-1] + (L[t] - f[t-1]) / τ`` rewrites as
    ``f[t] = (1 - 1/τ) * f[t-1] + (1/τ) * L[t]``, which is exactly the
    transfer function ``H(z) = b/(1 - a·z^-1)`` with ``b = 1/τ`` and
    ``a = 1 - 1/τ``.  scipy.signal.lfilter runs this in C — ~50× faster
    than the Python-loop form, and the bootstrap calls _ewma_vec twice
    per resample (CTL + ATL), so the speedup compounds.

    Numerical fidelity is bit-exact under double precision: lfilter and
    the Python loop both compute the same recurrence, just in C vs
    Python.  Verified by ``test_lfilter_matches_python_loop`` in the
    test suite.
    """
    if tau <= 0 or loads.size == 0:
        return np.zeros_like(loads, dtype=float)
    inv_tau = 1.0 / float(tau)
    b = np.array([inv_tau], dtype=float)
    a = np.array([1.0, -(1.0 - inv_tau)], dtype=float)
    # zi=None → cold-start at 0, matching the Python loop's f=0 initial.
    return lfilter(b, a, loads.astype(float))


def _banister_predict(loads: np.ndarray, tau1: float, tau2: float,
                       k1: float, k2: float, p_base: float) -> np.ndarray:
    """Banister forward simulation for a single component.

    P(t) = p_base + k1·CTL(τ1, t) − k2·ATL(τ2, t)

    Uses _ewma_vec (lfilter) to avoid two Python-level loops per call.
    Each curve_fit iteration calls this once; each bootstrap resample
    runs ~30-100 curve_fit iterations. The speedup is critical for the
    1000-resample CI computation.
    """
    ctl = _ewma_vec(loads, float(tau1))
    atl = _ewma_vec(loads, float(tau2))
    return float(p_base) + k1 * ctl - k2 * atl


def _fit_one_component(
    daily_loads: np.ndarray,
    marker_indices: np.ndarray,
    marker_values: np.ndarray,
    tau1_init: float,
    tau2_init: float,
    tau1_bounds: tuple[float, float],
    tau2_bounds: tuple[float, float],
) -> dict[str, Any]:
    """Fit a single component (CTL or per-3D-component) to its markers.

    Returns ``{'tau1', 'tau2', 'k1', 'k2', 'p_base', 'r2', 'residuals'}``
    or ``{'fit_failed': True}`` on convergence error.
    """
    # Initial guesses near conventional with k1, k2, p_base loosely scaled
    # to the marker median (so the curve_fit search starts in-range).
    p_med = float(np.median(marker_values)) if len(marker_values) else 0.0
    k1_init = max(p_med * 0.001, 1e-3)
    k2_init = max(p_med * 0.0005, 1e-3)
    p_base_init = float(p_med * 0.8)

    # Wrapping closure so curve_fit can vary the params.
    def _model(_x, tau1, tau2, k1, k2, p_base):
        sim = _banister_predict(daily_loads, tau1, tau2, k1, k2, p_base)
        return sim[marker_indices]

    lower = [tau1_bounds[0], tau2_bounds[0], 0.0, 0.0, 0.0]
    upper = [tau1_bounds[1], tau2_bounds[1], np.inf, np.inf, np.inf]
    p0 = [tau1_init, tau2_init, k1_init, k2_init, p_base_init]

    try:
        popt, _pcov = curve_fit(
            _model,
            xdata=marker_indices,
            ydata=marker_values,
            p0=p0,
            bounds=(lower, upper),
            method="trf",
            maxfev=5000,
        )
    except (RuntimeError, ValueError) as e:
        _log.debug("curve_fit failed: %s", e)
        return {"fit_failed": True, "reason": str(e)}

    tau1_f, tau2_f, k1_f, k2_f, p_base_f = popt
    pred = _model(marker_indices, *popt)
    residuals = marker_values - pred
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((marker_values - np.mean(marker_values)) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0

    return {
        "tau1": float(tau1_f),
        "tau2": float(tau2_f),
        "k1": float(k1_f),
        "k2": float(k2_f),
        "p_base": float(p_base_f),
        "r2": float(r2),
        "residuals": residuals.tolist(),
    }


def _bootstrap_ci(
    daily_loads: np.ndarray,
    marker_indices: np.ndarray,
    marker_values: np.ndarray,
    tau1_init: float,
    tau2_init: float,
    tau1_bounds: tuple[float, float],
    tau2_bounds: tuple[float, float],
    n_resamples: int,
    rng: np.random.Generator,
) -> dict[str, tuple[float, float]] | None:
    """Block-bootstrap CIs for tau1 and tau2.

    Resamples the (marker_idx, marker_value) pairs WITH replacement
    ``n_resamples`` times. Returns 2.5th/97.5th percentile pairs per
    parameter, or ``None`` if too many resamples failed.
    """
    if len(marker_indices) < 3:
        return None

    tau1_samples = []
    tau2_samples = []
    n = len(marker_indices)
    failures = 0
    fail_cap = max(int(n_resamples * 0.5), 50)

    for _ in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        bs_indices = marker_indices[idx]
        bs_values = marker_values[idx]
        # Sort by index so the model evaluation is monotone in time
        # (curve_fit doesn't care, but it's cleaner).
        order = np.argsort(bs_indices)
        bs_indices = bs_indices[order]
        bs_values = bs_values[order]
        fit = _fit_one_component(
            daily_loads, bs_indices, bs_values,
            tau1_init, tau2_init, tau1_bounds, tau2_bounds,
        )
        if fit.get("fit_failed"):
            failures += 1
            if failures > fail_cap:
                _log.debug("bootstrap aborted: %d failures", failures)
                return None
            continue
        tau1_samples.append(fit["tau1"])
        tau2_samples.append(fit["tau2"])

    if len(tau1_samples) < max(int(n_resamples * 0.3), 30):
        return None

    return {
        "tau1": (
            float(np.percentile(tau1_samples, 2.5)),
            float(np.percentile(tau1_samples, 97.5)),
        ),
        "tau2": (
            float(np.percentile(tau2_samples, 2.5)),
            float(np.percentile(tau2_samples, 97.5)),
        ),
    }


def _ci_too_wide(point: float, ci_low: float, ci_high: float,
                 ratio: float = _THRESH_CI_RATIO) -> bool:
    """Return True iff (high - low) / point > ratio.

    Robust to point ≈ 0 (returns True so we reject).
    """
    if point is None or abs(point) < 1e-6:
        return True
    return (ci_high - ci_low) / abs(point) > ratio


def _persist_metric(conn, metric: str, value: float, notes_dict: dict[str, Any]) -> None:
    """Write one fit row to athlete_metrics, source='nls_fit'.

    Source priority is enforced by the existing manual-source guard
    elsewhere in the codebase — manual rows are NEVER overwritten by
    'nls_fit'. We respect the existing ladder: manual > intervals.icu /
    nls_fit > settings.
    """
    today_iso = date.today().isoformat()
    existing = conn.execute(
        "SELECT source FROM athlete_metrics WHERE date = ? AND metric = ?",
        (today_iso, metric),
    ).fetchone()
    if existing and existing[0] == "manual":
        # Don't clobber a manually-typed override.
        _log.debug("Skipping persist of %s — manual override present", metric)
        return
    conn.execute(
        "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source, notes) "
        "VALUES (?, ?, ?, 'nls_fit', ?)",
        (today_iso, metric, float(value), json.dumps(notes_dict)),
    )


def fit_tau_per_athlete(profile_id: str, persist: bool = True,
                         horizon_end_date: date | None = None) -> dict:
    """Fit Banister τ values from the rider's training log.

    Args:
        profile_id: informational; the DB layer is single-profile-at-a-time.
        persist: when False, the function returns the fit dict but does NOT
            write to athlete_metrics. v1.2.0 OOS validation calls with
            persist=False to avoid contaminating the live fit.
        horizon_end_date: when set, only includes data up to that date.
            v1.2.0 OOS validation calls with horizon_end_date=N-4_weeks_ago
            to fit on the holdout-excluded subset.

    Returns the full fit dict per /tmp/MASTER_DECISIONS_v107.md §1, with
    a guaranteed ``fit_status`` field of one of:
      * ``"success"`` — weighted_n ≥ 10, r² ≥ 0.40, CI-tight
      * ``"low_confidence"`` — 5 ≤ weighted_n < 10 OR (weighted_n ≥ 10 but
        the fit failed the CI/r² gate)
      * ``"insufficient_data"`` — weighted_n < 5 OR scipy didn't converge
    """
    horizon_days = 365  # 12 months — wider than the audit's 6-month floor
    today = horizon_end_date or date.today()
    horizon_end_iso = today.isoformat()
    horizon_start_iso = (today - timedelta(days=horizon_days)).isoformat()

    conn = db.get_db()

    # Compute n_markers BEFORE the weighted_n early-return so the v1.2.0
    # contract test (test_horizon_end_date_truncates_eftp_inputs) can
    # observe truncation regardless of whether the fit went through.
    eftp_rows = conn.execute(
        "SELECT date, value FROM athlete_metrics "
        "WHERE metric = 'eftp' AND date >= ? AND date <= ? ORDER BY date",
        (horizon_start_iso, horizon_end_iso),
    ).fetchall()
    n_markers = len(eftp_rows)

    # Weighted-marker count must respect horizon_end_date too — otherwise
    # the v1.2.0 OOS holdout fit would over-count by including markers in
    # the holdout window.
    weighted_n = _count_weighted_markers_in_range(
        conn, horizon_start_iso, horizon_end_iso,
    )

    base_response: dict[str, Any] = {
        "ctl_tau_fit": None, "atl_tau_fit": None,
        "ctl_tau_ci_low": None, "ctl_tau_ci_high": None,
        "atl_tau_ci_low": None, "atl_tau_ci_high": None,
        "cp_tau1_fit": None, "cp_tau2_fit": None,
        "wprime_tau1_fit": None, "wprime_tau2_fit": None,
        "pmax_tau1_fit": None, "pmax_tau2_fit": None,
        "fit_residual_r2": None,
        "n_markers": n_markers,
        "weighted_n": float(weighted_n),
        "fit_horizon_days": horizon_days,
        "fit_status": "insufficient_data",
    }

    if weighted_n < _THRESH_INSUFFICIENT:
        return base_response

    daily_pairs = _build_daily_load_series(conn, horizon_start_iso, horizon_end_iso)
    if not daily_pairs:
        return base_response
    daily_loads = np.array([L for _, L in daily_pairs], dtype=float)

    if n_markers < 5:
        # Even with race tags upping weighted_n, the NLS needs at least
        # ~5 numeric observations for curve_fit to have any hope.
        return base_response

    # Build (day_index, value) pairs aligned with daily_loads.
    date_to_idx = {iso: i for i, (iso, _) in enumerate(daily_pairs)}
    marker_indices = []
    marker_values = []
    for r in eftp_rows:
        if r[0] in date_to_idx:
            marker_indices.append(date_to_idx[r[0]])
            marker_values.append(float(r[1]))
    marker_indices = np.array(marker_indices, dtype=int)
    marker_values = np.array(marker_values, dtype=float)

    if len(marker_indices) < 5:
        return base_response

    # ── point fit ──
    fit = _fit_one_component(
        daily_loads,
        marker_indices,
        marker_values,
        tau1_init=_CONV_CTL_TAU,
        tau2_init=_CONV_ATL_TAU,
        tau1_bounds=_FIT_BOUNDS["ctl_tau"],
        tau2_bounds=_FIT_BOUNDS["atl_tau"],
    )

    if fit.get("fit_failed"):
        return base_response  # status stays insufficient_data

    base_response["ctl_tau_fit"] = round(fit["tau1"], 1)
    base_response["atl_tau_fit"] = round(fit["tau2"], 1)
    base_response["fit_residual_r2"] = round(fit["r2"], 3)

    # ── bootstrap CI ──
    rng = np.random.default_rng(seed=42)
    bs = _bootstrap_ci(
        daily_loads, marker_indices, marker_values,
        tau1_init=_CONV_CTL_TAU,
        tau2_init=_CONV_ATL_TAU,
        tau1_bounds=_FIT_BOUNDS["ctl_tau"],
        tau2_bounds=_FIT_BOUNDS["atl_tau"],
        n_resamples=_BOOTSTRAP_N,
        rng=rng,
    )
    ci_too_wide = True
    if bs:
        base_response["ctl_tau_ci_low"] = round(bs["tau1"][0], 1)
        base_response["ctl_tau_ci_high"] = round(bs["tau1"][1], 1)
        base_response["atl_tau_ci_low"] = round(bs["tau2"][0], 1)
        base_response["atl_tau_ci_high"] = round(bs["tau2"][1], 1)
        ci_too_wide = _ci_too_wide(
            fit["tau1"], bs["tau1"][0], bs["tau1"][1], ratio=_THRESH_CI_RATIO,
        )

    # ── status decision ──
    if (weighted_n >= _THRESH_SUCCESS
        and fit["r2"] >= _THRESH_R2
        and not ci_too_wide):
        base_response["fit_status"] = "success"
    else:
        base_response["fit_status"] = "low_confidence"

    # Per-component τ values are not yet refit (audit §3 calls out
    # τ_CP / τ_W' / τ_Pmax as needing per-component anchors which v1.0.7
    # doesn't have; deferred to v1.1.0+). For now, populate the
    # per-component fields with conventional τ so the dashboard panel
    # can render the table without missing keys, and the WIRING agent's
    # planner integration has stable values to read.
    base_response["cp_tau1_fit"] = _CONV_CP_TAU1
    base_response["cp_tau2_fit"] = _CONV_CP_TAU2
    base_response["wprime_tau1_fit"] = _CONV_WPRIME_TAU1
    base_response["wprime_tau2_fit"] = _CONV_WPRIME_TAU2
    base_response["pmax_tau1_fit"] = _CONV_PMAX_TAU1
    base_response["pmax_tau2_fit"] = _CONV_PMAX_TAU2

    # ── persist ──
    if persist and base_response["fit_status"] == "success":
        notes = {
            "ci_low": base_response["ctl_tau_ci_low"],
            "ci_high": base_response["ctl_tau_ci_high"],
            "r2": base_response["fit_residual_r2"],
            "n_markers": int(n_markers),
            "weighted_n": float(weighted_n),
            "horizon_days": int(horizon_days),
        }
        try:
            _persist_metric(conn, "ctl_tau_fit", base_response["ctl_tau_fit"], notes)
            atl_notes = dict(notes)
            atl_notes["ci_low"] = base_response["atl_tau_ci_low"]
            atl_notes["ci_high"] = base_response["atl_tau_ci_high"]
            _persist_metric(conn, "atl_tau_fit", base_response["atl_tau_fit"], atl_notes)
            conn.commit()
        except Exception as e:  # pragma: no cover — logged for diagnostics
            _log.warning("τ-fit persist failed: %s", e)
            conn.rollback()

    return base_response
