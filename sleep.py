"""HRV, RHR and sleep metrics from Intervals.icu wellness data."""
import math
import statistics
from datetime import date, timedelta
from training import fetch_wellness
import config


def _ln_rmssd(hrv_ms: float | None) -> float | None:
    if hrv_ms is not None and hrv_ms > 0:
        return round(math.log(hrv_ms), 4)
    return None


def compute_sleep_score(sleep_h: float | None) -> dict:
    """Classify a single night's sleep duration against config thresholds.

    Uses the English tokens (GREEN / ORANGE / RED / ?) so that the
    ``_EMOJI`` lookup in main.py and the dashboard ``statusClass()``
    frontend both resolve. See sleep-status block below for context.

    Args:
        sleep_h: sleep duration in hours (may be None when no data).

    Returns:
        Dict with ``sleep_h`` and ``sleep_status`` keys.
    """
    status = "?"
    if sleep_h is not None:
        if sleep_h >= config.SLEEP_GREEN:
            status = "GREEN"
        elif sleep_h >= config.SLEEP_ORANGE:
            status = "ORANGE"
        else:
            status = "RED"
    return {"sleep_h": sleep_h, "sleep_status": status}


def get_sleep_metrics() -> dict:
    """
    Return today's sleep + HRV + RHR data with trend context.
    Uses last 42 days of wellness records.
    """
    wellness = fetch_wellness(days=42)
    return compute_sleep_metrics_from_wellness(wellness)


def compute_sleep_metrics_from_wellness(wellness: list[dict]) -> dict:
    """v4.5.0 — derive sleep + HRV + RHR metrics from any wellness list.

    Extracted from :func:`get_sleep_metrics` so the local-wellness fallback
    path in app.py can reuse the exact same HRV baseline / RHR delta logic
    without re-fetching from ICU. Returns the same shape as
    :func:`get_sleep_metrics`; returns {} when ``wellness`` is falsy.
    """
    if not wellness:
        return {}

    # build daily records with LnRMSSD
    records = []
    for w in wellness:
        hrv = w.get("hrv")
        ln = _ln_rmssd(hrv)
        rhr = w.get("restingHR")
        sleep_secs = w.get("sleepSecs")
        sleep_h = round(sleep_secs / 3600, 2) if sleep_secs else None
        records.append({
            "date": w["id"],
            "hrv_ms": hrv,
            "ln_rmssd": ln,
            "rhr": rhr,
            "sleep_h": sleep_h,
            "sleep_score": w.get("sleepScore"),
        })

    # Rolling LnRMSSD average over the last 4–7 valid samples.
    # Name keeps `ln_7d` for downstream consumers; slicing takes up to 7 records
    # and requires ≥4 to produce an estimate. Gaps in HRV data are tolerated
    # because the slice is drawn from *non-null* ln_rmssd entries only.
    ln_values = [(r["date"], r["ln_rmssd"]) for r in records if r["ln_rmssd"]]
    ln_7d = None
    if len(ln_values) >= 4:
        recent_ln_rmssd = [v for _, v in ln_values[-7:]]
        ln_7d = round(statistics.mean(recent_ln_rmssd), 4)

    # English status tokens (GREEN/ORANGE/RED) are matched in
    # dashboard.html statusClass() alongside the legacy Dutch labels.

    # HRV baseline (from config or auto-computed from last 28 days)
    baseline_mean = config.HRV_BASELINE_MEAN
    baseline_sd = config.HRV_BASELINE_SD

    if baseline_mean is None and len(ln_values) >= 14:
        base_vals = [v for _, v in ln_values[-28:]]
        baseline_mean = round(statistics.mean(base_vals), 4)
        # Floor the SD fallback at 1.0: the old 0.1 default for a single-sample
        # window produced a SWC band so tight (±0.05 on LnRMSSD) that almost
        # every real reading landed outside the "normal" range, flipping the
        # HRV status RED for stable athletes. 1.0 matches the empirical SD
        # observed across multi-week wellness pulls.
        baseline_sd = round(statistics.stdev(base_vals), 4) if len(base_vals) > 1 else 1.0

    swc_upper = round(baseline_mean + 0.5 * baseline_sd, 4) if (baseline_mean is not None and baseline_sd is not None) else None
    swc_lower = round(baseline_mean - 0.5 * baseline_sd, 4) if (baseline_mean is not None and baseline_sd is not None) else None

    # HRV status
    hrv_status = "?"
    if ln_7d and swc_upper and swc_lower:
        if ln_7d >= swc_upper:
            hrv_status = "GREEN"
        elif ln_7d >= swc_lower:
            hrv_status = "ORANGE"
        else:
            hrv_status = "RED"

    # Rolling RHR baseline over the last 4–7 valid samples (excluding today to
    # avoid self-inclusion bias). Naming keeps `rhr_7d_avg` for downstream
    # consumers; the window is whatever is available in [4, 7] non-null samples.
    rhr_values = [r["rhr"] for r in records if r["rhr"]]
    today_rhr = next((r["rhr"] for r in reversed(records) if r["rhr"]), None)
    # Use preceding records (exclude last entry which may be today)
    rhr_baseline = rhr_values[:-1] if len(rhr_values) > 1 else rhr_values
    rhr_7d_avg = round(statistics.mean(rhr_baseline[-7:]), 1) if len(rhr_baseline) >= 4 else None
    rhr_delta = round(today_rhr - rhr_7d_avg, 1) if (today_rhr is not None and rhr_7d_avg is not None) else None

    rhr_status = "?"
    if rhr_delta is not None:
        if rhr_delta <= 3:
            rhr_status = "GREEN"
        elif rhr_delta <= 7:
            rhr_status = "ORANGE"
        else:
            rhr_status = "RED"

    # today's sleep
    today_rec = next(
        (r for r in reversed(records)
         if r["date"] == date.today().isoformat()),
        records[-1] if records else {},
    )

    # Sleep + HRV last-known fallback — mirror the RHR chain above. Today's
    # overnight metrics reach intervals.icu hours late (Garmin→ICU sync lag),
    # so a not-yet-synced night should show the last real reading + the date it
    # came from, not a bare "—". `*_asof` lets the UI tag a stale value.
    def _last_nonnull(field):
        return next(((r[field], r["date"]) for r in reversed(records)
                     if r.get(field) is not None), (None, None))
    if today_rec.get("hrv_ms") is not None:
        hrv_ms, hrv_asof, ln_rmssd_today = (
            today_rec.get("hrv_ms"), today_rec.get("date"), today_rec.get("ln_rmssd"))
    else:
        hrv_ms, hrv_asof = _last_nonnull("hrv_ms")
        ln_rmssd_today = _ln_rmssd(hrv_ms)
    if today_rec.get("sleep_h") is not None or today_rec.get("sleep_score") is not None:
        sleep_rec = today_rec
    else:
        sleep_rec = next(
            (r for r in reversed(records)
             if r.get("sleep_h") is not None or r.get("sleep_score") is not None),
            today_rec)
    sleep_h = sleep_rec.get("sleep_h")
    sleep_score = sleep_rec.get("sleep_score")
    sleep_asof = sleep_rec.get("date")
    sleep_status = "?"
    if sleep_h is not None:
        if sleep_h >= config.SLEEP_GREEN:
            sleep_status = "GREEN"
        elif sleep_h >= config.SLEEP_ORANGE:
            sleep_status = "ORANGE"
        else:
            sleep_status = "RED"

    # count consecutive red HRV days (skip days with no data, don't break streak)
    red_streak = 0
    if swc_lower:
        for r in reversed(records):
            if r.get("ln_rmssd") is None:
                continue  # skip days without HRV data — don't reset streak
            if r["ln_rmssd"] < swc_lower:
                red_streak += 1
            else:
                break

    return {
        "date": today_rec.get("date"),
        "sleep_h": sleep_h,
        "sleep_score": sleep_score,
        "sleep_asof": sleep_asof,
        "sleep_status": sleep_status,
        "hrv_ms": hrv_ms,
        "hrv_asof": hrv_asof,
        "ln_rmssd_today": ln_rmssd_today,
        "ln_rmssd_7d": ln_7d,
        "hrv_baseline_mean": baseline_mean,
        "hrv_baseline_sd": baseline_sd,
        "swc_upper": swc_upper,
        "swc_lower": swc_lower,
        "hrv_status": hrv_status,
        "rhr_today": today_rhr,
        "rhr_7d_avg": rhr_7d_avg,
        "rhr_delta": rhr_delta,
        "rhr_status": rhr_status,
        "red_hrv_streak": red_streak,
    }
