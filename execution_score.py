"""Execution scoring — "did you ride what was prescribed?" (P2.1, G10).

Pure module: :func:`score_ride` takes the planned session dict, a full ride
record and the athlete's target_mode and returns a 0-100 execution score with
per-axis components and a verdict. NO file/DB access in here — callers fetch
(app.py re-fetches the full ride by activity_id at completion-match time,
because the week-activities collector strips time-in-zone).

Model v1 (deliberately coarse, contract G10):
  duration_ratio  = ride_duration / planned_duration
  load_ratio      = ride_tss / planned_tss
  intensity_ratio = (fraction of ride TiZ in the session's PRESCRIBED band)
                    / (expected in-band fraction for that session type)
  score = round(100 * (0.3*dur + 0.3*load + 0.4*intensity))  with each
          component CAPPED at 1 for the score; the VERDICT derives from the
          UNCAPPED ratios (thresholds locked in tests). Missing axes drop out
          and the remaining weights renormalize (load_only ⇒ 0.5/0.5).

Zone-bucket mapping (test-locked table below):
  * The ride's power ``time_in_zone`` is the canonical {z1..z7} dict
    (ride_storage normalized shape). The overlapping ``ss`` accumulator is
    EXCLUDED everywhere — it double-counts z3/z4 time and would corrupt both
    the numerator and the denominator.
  * sweetspot straddle: SS work (84-97 %FTP) spans the Coggan z3/z4 boundary
    at 90 %, so the band is {z3, z4} — a rider sitting at either side of the
    90 % line is not split-penalized.
  * overunder straddle: unders (~90-95 %) are z4, overs (~105-110 %) poke into
    z5, so the band is {z4, z5}.
  * Expected fractions are the plausible in-band share of a well-executed
    session of that type (intervals + recovery valleys + warmup/cooldown),
    calibrated against the library's typical structures. Locked by tests.
  * hr mode maps prescription rows onto the ICU HR-zone frame by bucket
    INDEX with a declared ±1-bucket tolerance (HR lag + the Coggan-power vs
    athlete-HR-zone frame mismatch). Types whose power band starts at z5+
    (vo2max/anaerobic) carry RPE cues in hr mode (hr_targets returns RPE for
    zone >= 5), so with HR-only data they score duration+load only.
  * sprint/neuromuscular is ALWAYS load_only: sub-30 s max efforts are too
    short for TiZ to register a meaningful band fraction in either frame.

Basis selection (G3 — basis follows DATA present, prescription follows mode):
  power TiZ present  -> basis "power" (even when the app is in hr mode);
  else hr TiZ present AND the type is HR-guidable -> basis "hr";
  else -> basis "load_only" (duration + load axes only).

Structure fidelity (advisory axis, additive — see structure_fidelity.py):
  score_ride's result carries a "fidelity" key comparing the prescribed
  .zwo segment timeline against the delivered 1 Hz watts trace (per-rep
  completion + on-target accounting). It is computed ONLY when the caller
  provides planned_segments (parse_zwo_text output — this module stays
  I/O-free) plus a watts trace and FTP, either as keyword args or embedded
  in the ride record (ride["streams"]["watts"|"power"], ride["ftp_at_ride"]
  / ["eftp_at_ride"]); otherwise it is None. It NEVER contributes to
  score/verdict — those semantics stay locked.
"""
from __future__ import annotations

from structure_fidelity import score_structure

__all__ = [
    "score_ride",
    "POWER_BANDS", "RPE_ONLY_TYPES", "TYPE_ALIASES",
    "WEIGHTS",
    "VERDICT_OFF_PLAN_BELOW", "VERDICT_UNDER_BELOW", "VERDICT_OVER_ABOVE",
    "HR_BUCKET_TOLERANCE",
]

# ── Locked scoring constants ─────────────────────────────────────────────────
WEIGHTS = {"duration": 0.3, "load": 0.3, "intensity": 0.4}

# Verdict thresholds over the UNCAPPED ratios (locked in tests):
#   any ratio < 0.40           -> off_plan   (ride bears little resemblance)
#   else any ratio < 0.80      -> under
#   else any ratio > 1.25      -> over
#   else                       -> on_target
VERDICT_OFF_PLAN_BELOW = 0.40
VERDICT_UNDER_BELOW = 0.80
VERDICT_OVER_ABOVE = 1.25

# hr mode: prescription row k accepts HR-frame buckets k-1..k+1 (declared).
HR_BUCKET_TOLERANCE = 1

# session_type -> (power zone buckets, expected in-band fraction).
# Zone buckets are Coggan indices 1..7 over the ride's time_in_zone z1..z7.
POWER_BANDS: dict[str, tuple[tuple[int, ...], float]] = {
    "recovery":  ((1, 2), 0.90),
    "z2":        ((1, 2), 0.80),
    "long_z2":   ((1, 2), 0.80),
    "tempo":     ((3,), 0.45),
    "sweetspot": ((3, 4), 0.50),   # straddle: SS spans the z3/z4 90 % line
    "threshold": ((4,), 0.50),
    "overunder": ((4, 5), 0.50),   # straddle: unders z4, overs poke into z5
    "ftp_test":  ((4, 5), 0.35),
    "vo2max":    ((5, 6), 0.28),
    "vo2_short": ((5, 6), 0.25),
    "anaerobic": ((5, 6, 7), 0.12),
}

# Types scored on duration+load only, in EVERY basis (see module docstring).
RPE_ONLY_TYPES = frozenset({"sprint"})

# Normalization aliases -> canonical POWER_BANDS / RPE_ONLY_TYPES keys.
TYPE_ALIASES = {
    "sweet_spot": "sweetspot",
    "over_under": "overunder",
    "endurance": "z2",
    "neuromuscular": "sprint",
    "vo2": "vo2max",
}


def _canon_type(session_type) -> str:
    st = str(session_type or "").strip().lower()
    return TYPE_ALIASES.get(st, st)


def _tiz_seconds(tiz) -> "dict[int, float] | None":
    """{zone_index: seconds} from a ride time-in-zone dict; None if unusable.

    Accepts the canonical ride_storage shape {"z1"..."z7"[, "ss"]}. The "ss"
    key is dropped (overlapping accumulator, not a partition bucket).
    """
    if not isinstance(tiz, dict):
        return None
    out: dict[int, float] = {}
    for i in range(1, 8):
        v = tiz.get(f"z{i}")
        try:
            out[i] = max(0.0, float(v or 0))
        except (TypeError, ValueError):
            out[i] = 0.0
    if sum(out.values()) <= 0:
        return None
    return out


def _ride_duration_min(ride: dict) -> "float | None":
    for key, scale in (("duration_min", 1.0), ("moving_s", 1 / 60.0),
                       ("duration_s", 1 / 60.0)):
        v = ride.get(key)
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f * scale
    return None


def _ratio_axis(actual, planned) -> "dict | None":
    """One duration/load axis: uncapped ratio + capped score, or None."""
    try:
        a = float(actual)
        p = float(planned)
    except (TypeError, ValueError):
        return None
    if p <= 0 or a < 0:
        return None
    r = a / p
    return {"ratio": round(r, 3), "score": round(min(r, 1.0), 3)}


def _hr_band(power_zones: "tuple[int, ...]") -> "tuple[int, ...]":
    """ICU HR-frame buckets for a power band: same indices ±1, clamped 1..7."""
    keep: set[int] = set()
    for z in power_zones:
        for k in range(z - HR_BUCKET_TOLERANCE, z + HR_BUCKET_TOLERANCE + 1):
            if 1 <= k <= 7:
                keep.add(k)
    return tuple(sorted(keep))


def _intensity_axis(tiz: "dict[int, float]", zones: "tuple[int, ...]",
                    expected: float, frame: str) -> dict:
    total = sum(tiz.values())
    in_band = sum(tiz.get(z, 0.0) for z in zones)
    fraction = in_band / total if total > 0 else 0.0
    ratio = (fraction / expected) if expected > 0 else 0.0
    return {
        "ratio": round(ratio, 3),
        "score": round(min(ratio, 1.0), 3),
        "band": [f"z{z}" for z in zones],
        "band_frame": frame,
        "band_fraction": round(fraction, 3),
        "target_fraction": expected,
    }


def _verdict(ratios: "list[float]") -> str:
    if not ratios:
        return "off_plan"
    lo, hi = min(ratios), max(ratios)
    if lo < VERDICT_OFF_PLAN_BELOW:
        return "off_plan"
    if lo < VERDICT_UNDER_BELOW:
        return "under"
    if hi > VERDICT_OVER_ABOVE:
        return "over"
    return "on_target"


def _embedded_watts(ride: dict):
    """1 Hz watts list from the v1.0.6 streams envelope, or None."""
    streams = ride.get("streams")
    if not isinstance(streams, dict):
        return None
    w = streams.get("watts") or streams.get("power")
    return w if isinstance(w, list) and w else None


def _ride_ftp(ride: dict) -> "float | None":
    for key in ("ftp_at_ride", "eftp_at_ride"):
        try:
            f = float(ride.get(key))
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def score_ride(planned: dict, ride: dict, mode: str, *,
               planned_segments=None, watts=None, ftp=None) -> dict:
    """Score a completed ride against its planned session.

    Args:
        planned: session dict — reads ``session_type``, ``duration_min``,
            ``tss_estimate``.
        ride: full ride record (ride_storage shape) — reads ``duration_min``
            / ``moving_s`` / ``duration_s``, ``tss``, ``time_in_zone``,
            ``hr_time_in_zone``; plus ``streams``/``ftp_at_ride``/
            ``eftp_at_ride`` as fidelity fallbacks (see below).
        mode: athlete target_mode, "power" or "hr" (prescription frame; the
            intensity BASIS still follows the data present, per G3).
        planned_segments: optional prescribed timeline for the advisory
            fidelity axis (structure_fidelity.parse_zwo_text output). This
            module is I/O-free, so the CALLER parses the .zwo.
        watts: optional 1 Hz power trace; falls back to
            ``ride["streams"]["watts"|"power"]`` when omitted.
        ftp: optional FTP watts; falls back to ``ride["ftp_at_ride"]`` then
            ``ride["eftp_at_ride"]`` when omitted.

    Returns:
        {"score": int|None, "basis": "power"|"hr"|"load_only",
         "components": {"duration": {...}|None, "load": {...}|None,
                        "intensity": {...}|None},
         "verdict": "on_target"|"under"|"over"|"off_plan",
         "fidelity": dict|None}
        ``score`` is None only when NO axis is computable (callers skip
        persisting in that case). ``fidelity`` (structure_fidelity result
        shape) is ADVISORY: present only when planned_segments + a watts
        trace + FTP were all resolvable, and never affects score/verdict.

    Pure + deterministic (G3). Components are capped at 1 for the score;
    verdicts derive from the uncapped ratios.
    """
    if not isinstance(planned, dict):
        planned = {}
    if not isinstance(ride, dict):
        ride = {}
    stype = _canon_type(planned.get("session_type"))

    # FTP-tests IP W1a: protocol-aware test grading. A ramp is ridden TO
    # FAILURE — its finish time and zone mix are unknowable a priori, so
    # every axis grades noise; stay silent (score None → callers skip
    # persisting; the test is judged by its FTP suggestion instead). The
    # steady protocols (coggan 20-min, 60-min hour) grade against their OWN
    # planned hard share — the static 0.35 row reads a perfectly executed
    # hour of power as verdict "over" (hard share ≈ 0.70).
    _ftp_expected_override = None
    if stype == "ftp_test":
        _zwo_l = str(planned.get("zwo_file") or "").lower()
        if "ftp_test_ramp" in _zwo_l:
            return {"score": None, "basis": "load_only",
                    "components": {"duration": None, "load": None,
                                   "intensity": None},
                    "verdict": "off_plan", "fidelity": None}
        if "ftp_test_60min" in _zwo_l:
            _ftp_expected_override = 0.70
        # coggan (and unknown files) keep the static POWER_BANDS row.

    duration = _ratio_axis(_ride_duration_min(ride), planned.get("duration_min"))
    load = _ratio_axis(ride.get("tss"), planned.get("tss_estimate"))

    # ── Intensity basis resolution (G3: basis follows DATA present) ─────────
    intensity = None
    basis = "load_only"
    if stype not in RPE_ONLY_TYPES and stype in POWER_BANDS:
        zones, expected = POWER_BANDS[stype]
        if _ftp_expected_override is not None:
            expected = _ftp_expected_override
        power_tiz = _tiz_seconds(ride.get("time_in_zone"))
        hr_tiz = _tiz_seconds(ride.get("hr_time_in_zone"))
        if power_tiz is not None:
            basis = "power"
            intensity = _intensity_axis(power_tiz, zones, expected, "power")
        elif hr_tiz is not None and min(zones) <= 4:
            # HR-guidable band only — z5+ prescriptions are RPE in hr mode
            # (hr_targets returns RPE for zone >= 5), so HR TiZ can't grade
            # them; they fall through to load_only.
            basis = "hr"
            intensity = _intensity_axis(hr_tiz, _hr_band(zones), expected, "hr")

    components = {"duration": duration, "load": load, "intensity": intensity}

    # ── Advisory structure-fidelity axis (never touches score/verdict) ──────
    fidelity = None
    fid_watts = watts if watts is not None else _embedded_watts(ride)
    fid_ftp = ftp if ftp is not None else _ride_ftp(ride)
    if planned_segments and fid_watts and fid_ftp:
        fidelity = score_structure(planned_segments, fid_watts, fid_ftp)

    # ── Weighted score over present axes (renormalized) ─────────────────────
    present = {k: v for k, v in components.items() if v is not None}
    if not present:
        return {"score": None, "basis": basis, "components": components,
                "verdict": "off_plan", "fidelity": fidelity}
    wsum = sum(WEIGHTS[k] for k in present)
    score = round(100 * sum(WEIGHTS[k] * present[k]["score"] for k in present)
                  / wsum)

    verdict = _verdict([v["ratio"] for v in present.values()])
    return {"score": score, "basis": basis, "components": components,
            "verdict": verdict, "fidelity": fidelity}
