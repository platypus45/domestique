"""The rider's CTL, ATL and TSB: one owner, intervals.icu.

The owner's decision (2026-09-14): fitness values come from intervals.icu.
Until then five derivations answered (notes/review/audit-2026-09-14/state.md,
S-2 and S-3): live ICU, the ICU wellness cache, a local EWMA over the FIT-only
ride list (None for a rider whose rides arrive from intervals.icu), the SQLite
wellness table, and the constants 30, 37 and 50, merged per field.

`state()` answers, in order:
  1. intervals.icu now (the caller's `get_today_metrics` result);
  2. intervals.icu's last values as the SQLite wellness table holds them
     (filled by the 30-min sync loop), with the date they are from;
  3. unknown: every value None, source "none". Never a guess.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import clock

# How old a stored ICU value may be and still stand for today.
MAX_CACHED_AGE_DAYS = 7


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _state(ctl: float, atl: float, source: str, as_of: str) -> dict:
    return {"ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1),
            "source": source, "as_of": as_of}


UNKNOWN = {"ctl": None, "atl": None, "tsb": None, "source": "none", "as_of": None}


def _cached_row(today: date) -> Optional[dict]:
    try:
        import db
        conn = db.get_db()
        row = conn.execute(
            "SELECT date, ctl, atl FROM wellness WHERE date <= ? AND date >= ? "
            "AND ctl IS NOT NULL AND atl IS NOT NULL ORDER BY date DESC LIMIT 1",
            (today.isoformat(), (today - timedelta(days=MAX_CACHED_AGE_DAYS)).isoformat()),
        ).fetchone()
    except Exception:  # noqa: BLE001 - no store is "not cached", not an error
        return None
    return dict(row) if row is not None else None


def stored_ctl_on(day_iso: str) -> Optional[float]:
    """intervals.icu's CTL for a past day as the wellness table holds it (the
    nearest stored day at or before it, within MAX_CACHED_AGE_DAYS)."""
    try:
        row = _cached_row(date.fromisoformat(str(day_iso)[:10]))
    except ValueError:
        return None
    return round(float(row["ctl"]), 1) if row else None


def state(icu_metrics: Optional[dict], today: Optional[date] = None) -> dict:
    """{ctl, atl, tsb, source: "icu" | "icu_cached" | "none", as_of}."""
    today = today or clock.today()
    icu = icu_metrics or {}
    ctl, atl = _num(icu.get("ctl")), _num(icu.get("atl"))
    if ctl is not None and atl is not None:
        return _state(ctl, atl, "icu", today.isoformat())
    row = _cached_row(today)
    if row is not None:
        return _state(float(row["ctl"]), float(row["atl"]), "icu_cached", row["date"])
    return dict(UNKNOWN)
