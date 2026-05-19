"""Build a FIT activity file from a saved Domestique ride.

Produces a `.fit` blob for download / third-party upload. The shape
follows the standard ACTIVITY file layout:

    FileIdMessage  (type=ACTIVITY, manufacturer=DEVELOPMENT, product_name="Domestique")
    RecordMessage  × N   (per-second power / hr / cadence / speed / distance)
    SessionMessage       (cycling, sub_sport=virtual_activity, trainer=true)
    ActivityMessage

Note on attribution
-------------------
We use `manufacturer=DEVELOPMENT(255)` and `product=0` — third-party
uploaders (TrainingPeaks, Intervals.icu, etc.) accept these without
a registered device ID, so the file is useful for analysis even
though no platform renders a "via Domestique" source label.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("domestique.fit_activity")


# v1.0.7 IMPL-HRV-PROMPT — friendly-name lookup for FIT FileIdMessage.garmin_product
# Per master decisions G8 (dfa_alpha1_status='no_rr_data'), the dashboard surfaces
# a one-time-per-version toast educating the rider how to enable HRV recording on
# THEIR specific Garmin device. Auto-detection reads file_id.garmin_product.
#
# Numeric IDs verified against fit_tool.profile.profile_type.GarminProduct enum
# (where present) and Garmin's public ProductTable for newer devices not yet in
# fit_tool's bundled enum (e.g. Fēnix 8 = 4426). IDs not in this table fall back
# to "Unknown Garmin product ID <n>".
_GARMIN_PRODUCT_NAMES: dict[int, str] = {
    # Edge head units — full HRV-recording support (per README §"DFA Alpha1").
    3121: "Edge 530",
    3122: "Edge 830",
    2713: "Edge 1030",
    3570: "Edge 1030 Plus",
    4061: "Edge 1040",
    # Fēnix multisport watches.
    3290: "Fēnix 6",
    3291: "Fēnix 6X",
    3288: "Fēnix 6S",
    3905: "Fēnix 7",
    3906: "Fēnix 7S",
    3907: "Fēnix 7X",
    4426: "Fēnix 8",
    # Epix watches.
    3943: "Epix 2",
    # Forerunner — newer firmware exposes HRV recording.
    3589: "Forerunner 745",
    3113: "Forerunner 945",
    4024: "Forerunner 255",
    4025: "Forerunner 255S",
    4063: "Forerunner 265",
    4068: "Forerunner 265S",
    3858: "Forerunner 955",
    3989: "Forerunner 965",
}
# NOTE: where fit_tool.profile_type.GarminProduct disagrees with our table
# (Garmin reuses some product IDs across regions / "Asia" variants), we trust
# our table — it's keyed by the model name the rider sees in the README's
# device-by-device path table.


def parse_device_info(fit_path: Path) -> dict | None:
    """v1.0.7 IMPL-HRV-PROMPT — extract recording device info from a FIT file.

    Walks the ``file_id`` (FileIdMessage, global mesg id 0) record and returns
    ``{manufacturer, garmin_product, garmin_product_id, garmin_product_name}``
    for use by the home-page HRV-recording-prompt toast.

    The toast fires when a synced ride lands with HR data but
    ``dfa_alpha1_status == 'no_rr_data'``. To show the rider an actionable
    "Settings → Activity Profiles → … → HRV = On" path, we need to know
    which Garmin head unit they're using. ``manufacturer`` (e.g. "garmin")
    plus ``garmin_product`` (numeric) plus the friendly-name lookup
    (``_GARMIN_PRODUCT_NAMES``) supply that.

    Returns:
        dict with keys ``manufacturer`` (str | None: "garmin" / "unknown"),
        ``garmin_product`` (int | None: raw numeric ID),
        ``garmin_product_id`` (int | None: alias for ``garmin_product`` —
        kept for v1.0.7 master-decisions field-name compatibility),
        ``garmin_product_name`` (str: friendly name or
        "Unknown Garmin product ID <n>").
        Returns ``None`` only when the FIT itself fails to parse.
    """
    try:
        from fit_tool.fit_file import FitFile
    except Exception as e:
        log.warning(f"parse_device_info: fit_tool import failed: {e}")
        return None

    try:
        ff = FitFile.from_file(str(fit_path))
    except Exception as e:
        log.warning(f"parse_device_info({fit_path}) FIT parse failed: {e}")
        return None

    manufacturer: str | None = None
    garmin_product_id: int | None = None
    try:
        for rec in ff.records:
            msg = rec.message
            if type(msg).__name__ != "FileIdMessage":
                continue
            # ── manufacturer (FIT spec: numeric enum; 1 = Garmin) ─────
            try:
                m_raw = msg.manufacturer
            except Exception:
                m_raw = None
            if m_raw is None:
                try:
                    m_raw = msg.get_value("manufacturer")
                except Exception:
                    m_raw = None
            if m_raw is not None:
                # FitFile may surface the enum object or the int.
                try:
                    m_int = int(getattr(m_raw, "value", m_raw))
                    manufacturer = "garmin" if m_int == 1 else f"id_{m_int}"
                except (TypeError, ValueError):
                    manufacturer = str(m_raw).lower()
            # ── garmin_product (numeric; resolved to friendly name below) ─
            try:
                gp_raw = msg.garmin_product
            except Exception:
                gp_raw = None
            if gp_raw is None:
                try:
                    gp_raw = msg.get_value("garmin_product")
                except Exception:
                    gp_raw = None
            if gp_raw is not None:
                try:
                    garmin_product_id = int(getattr(gp_raw, "value", gp_raw))
                except (TypeError, ValueError):
                    garmin_product_id = None
            # The first FileIdMessage is the activity's authoritative one.
            break
    except Exception as e:
        log.warning(f"parse_device_info({fit_path}) walk failed: {e}")
        return {
            "manufacturer": manufacturer or "unknown",
            "garmin_product": garmin_product_id,
            "garmin_product_id": garmin_product_id,
            "garmin_product_name": "unknown",
        }

    if manufacturer is None:
        manufacturer = "unknown"

    if garmin_product_id is None:
        product_name = "unknown"
    else:
        product_name = _GARMIN_PRODUCT_NAMES.get(
            garmin_product_id,
            f"Unknown Garmin product ID {garmin_product_id}",
        )

    return {
        "manufacturer": manufacturer,
        "garmin_product": garmin_product_id,
        "garmin_product_id": garmin_product_id,
        "garmin_product_name": product_name,
    }


def parse_rr_intervals(fit_path: Path) -> list[float]:
    """v1.0.7 — extract RR-intervals (in seconds) from a FIT file's HrvMessage records.

    FIT spec: each ``hrv`` mesg (global id 78) carries a ``time`` array of up to
    5 RR-intervals in seconds, 0-padded. A chest-strap pairing emits one
    ``HrvMessage`` per second; optical-wrist HR emits none. Returns a flat
    chronological list of RR durations with zero-padding stripped. Empty list
    when the FIT has no HrvMessage records or the parse fails.
    """
    try:
        from fit_tool.fit_file import FitFile
    except Exception as e:
        log.warning(f"parse_rr_intervals: fit_tool import failed: {e}")
        return []

    try:
        ff = FitFile.from_file(str(fit_path))
    except Exception as e:
        log.warning(f"parse_rr_intervals({fit_path}) FIT parse failed: {e}")
        return []

    rrs: list[float] = []
    try:
        for rec in ff.records:
            msg = rec.message
            if type(msg).__name__ != "HrvMessage":
                continue
            t = None
            try:
                t = msg.time
            except Exception:
                # Some fit_tool versions only expose via get_value.
                try:
                    t = msg.get_value("time")
                except Exception:
                    t = None
            if t is None:
                continue
            if not isinstance(t, (list, tuple)):
                t = [t]
            for x in t:
                try:
                    v = float(x)
                except (TypeError, ValueError):
                    continue
                # v1.8.1 — FIT encodes "no value" for unused RR slots in
                # an HrvMessage as 0xFFFF / 1000 = 65.535 seconds. Each
                # HrvMessage holds up to 5 RR slots; when the chest
                # strap doesn't deliver 5 beats in the message window
                # the trailing slots get the sentinel. Pre-v1.8.1 the
                # filter was ``v > 0`` which let sentinels through — DFA
                # then saw an array dominated by 65.535 and bailed with
                # ``no_rr_data``. Realistic RR range: 0.3 s (200 bpm)
                # → 2.0 s (30 bpm). Tolerate a wider 0.25-3.0 band for
                # edge-case beats; anything above 3 s is either a sensor
                # glitch or the 65.535 sentinel.
                if 0.25 < v < 3.0:
                    rrs.append(v)
    except Exception as e:
        log.warning(f"parse_rr_intervals({fit_path}) walk failed: {e}")
        return rrs
    return rrs


def _serial_for_profile(profile_id: str) -> int:
    """Stable 32-bit serial derived from profile_id (deterministic)."""
    h = hashlib.sha256(profile_id.encode("utf-8")).digest()
    # Take the first 4 bytes as an unsigned int. Mask to 31 bits to stay
    # within int32 range that older FIT tools occasionally clamp to.
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def _ride_start_dt(ride: dict) -> datetime:
    """Parse the ride's started_at into an aware UTC datetime.

    Falls back to "now - duration" if started_at is missing/malformed."""
    s = ride.get("started_at")
    if s:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    summary = ride.get("summary", {}) or {}
    dur = int(summary.get("duration_sec", 0))
    # Fallback per docstring: "now - duration" so record timestamps don't run
    # into the future when started_at is missing/malformed.
    return (datetime.now(timezone.utc) - timedelta(seconds=dur)).replace(microsecond=0)


def build_activity_fit(ride: dict, profile_id: str) -> bytes:
    """Render a saved-ride dict to a FIT activity binary blob.

    `ride` has the schema produced by `ride_storage.save_ride`:
      {samples: {elapsed, power, hr, cadence, speed, distance}, summary: …}
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.activity_message import ActivityMessage
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.lap_message import LapMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import (
        Event,
        EventType,
        FileType,
        Manufacturer,
        Sport,
        SubSport,
    )

    builder = FitFileBuilder()

    samples = ride.get("samples", {}) or {}
    summary = ride.get("summary", {}) or {}
    n = len(samples.get("power", []) or [])
    if n == 0:
        raise ValueError("Cannot build FIT activity: ride has no samples")

    start_dt = _ride_start_dt(ride)
    start_unix_ms = int(start_dt.timestamp() * 1000)

    # ── FileIdMessage ────────────────────────────────────────────────────
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = _serial_for_profile(profile_id)
    try:
        file_id.time_created = start_unix_ms
    except Exception as e:
        # Some fit_tool versions reject the millis; log and move on.
        log.debug("FIT field write failed (file_id.time_created): %s", e)
    # Defensive product_name hint — some viewers surface this string
    # in their activity details even when manufacturer is DEVELOPMENT.
    try:
        file_id.product_name = "Domestique"
    except Exception as e:
        log.debug("FIT field write failed (file_id.product_name): %s", e)
    builder.add(file_id)

    # ── RecordMessage per-sample ─────────────────────────────────────────
    elapsed = samples.get("elapsed") or list(range(n))
    power = samples.get("power") or [0] * n
    hr = samples.get("hr") or [0] * n
    cadence = samples.get("cadence") or [0] * n
    # Speed in saved rides is km/h; FIT wants m/s.
    speed_kmh = samples.get("speed") or [0] * n
    # Distance saved as km; FIT wants metres.
    dist_km = samples.get("distance") or [0] * n

    last_ts_ms = start_unix_ms
    for i in range(n):
        rec = RecordMessage()
        ts_ms = start_unix_ms + int(elapsed[i]) * 1000
        try:
            rec.timestamp = ts_ms
        except Exception as e:
            log.debug("FIT field write failed (record.timestamp): %s", e)
        try:
            p = int(power[i] or 0)
            if p >= 0:
                rec.power = p
        except (TypeError, ValueError) as e:
            log.debug("FIT field write failed (record.power): %s", e)
        try:
            h = int(hr[i] or 0)
            if h > 0:
                rec.heart_rate = h
        except (TypeError, ValueError) as e:
            log.debug("FIT field write failed (record.heart_rate): %s", e)
        try:
            c = int(cadence[i] or 0)
            if c >= 0:
                rec.cadence = c
        except (TypeError, ValueError) as e:
            log.debug("FIT field write failed (record.cadence): %s", e)
        try:
            sp_kmh = float(speed_kmh[i] or 0)
            if sp_kmh >= 0:
                rec.speed = sp_kmh / 3.6  # → m/s
        except (TypeError, ValueError) as e:
            log.debug("FIT field write failed (record.speed): %s", e)
        try:
            d_km = float(dist_km[i] or 0)
            if d_km >= 0:
                rec.distance = d_km * 1000.0  # → m
        except (TypeError, ValueError) as e:
            log.debug("FIT field write failed (record.distance): %s", e)
        builder.add(rec)
        last_ts_ms = ts_ms

    # ── LapMessage ───────────────────────────────────────────────────────
    # FIT Activity spec requires Record* -> Lap* -> Session (between records
    # and session). Without this block, downstream analyzers (TrainingPeaks,
    # Intervals.icu, etc.) may drop session totals. We emit a single lap
    # spanning the entire ride.
    lap = LapMessage()
    try:
        lap.start_time = start_unix_ms
    except Exception as e:
        log.debug("FIT field write failed (lap.start_time): %s", e)
    try:
        lap.timestamp = last_ts_ms
    except Exception as e:
        log.debug("FIT field write failed (lap.timestamp): %s", e)
    dur_sec = float(summary.get("duration_sec", 0) or 0)
    try:
        lap.total_elapsed_time = dur_sec
        lap.total_timer_time = dur_sec
    except Exception as e:
        log.debug("FIT field write failed (lap.total_*_time): %s", e)
    try:
        lap.total_distance = float(summary.get("distance_km", 0) or 0) * 1000.0
    except Exception as e:
        log.debug("FIT field write failed (lap.total_distance): %s", e)
    try:
        if summary.get("avg_power") is not None:
            lap.avg_power = int(summary["avg_power"])
        if summary.get("max_power") is not None:
            lap.max_power = int(summary["max_power"])
        if summary.get("avg_hr") is not None and summary["avg_hr"]:
            lap.avg_heart_rate = int(summary["avg_hr"])
        if summary.get("max_hr") is not None and summary["max_hr"]:
            lap.max_heart_rate = int(summary["max_hr"])
        if summary.get("avg_cadence") is not None:
            lap.avg_cadence = int(summary["avg_cadence"])
    except Exception as e:
        log.debug("FIT field write failed (lap optional fields): %s", e)
    try:
        lap.event = Event.LAP
        lap.event_type = EventType.STOP
    except Exception as e:
        log.debug("FIT field write failed (lap.event): %s", e)
    builder.add(lap)

    # ── SessionMessage ───────────────────────────────────────────────────
    session = SessionMessage()
    try:
        session.start_time = start_unix_ms
        session.timestamp = last_ts_ms
    except Exception as e:
        # L10: don't silently swallow — log the underlying error and keep
        # a sane fallback so downstream consumers still see *some* start_time.
        log.warning("FIT start_time fallback: %s", e)
        dur = int(summary.get("duration_sec", 0))
        fallback_dt = datetime.now(timezone.utc) - timedelta(seconds=dur)
        try:
            session.start_time = int(fallback_dt.timestamp() * 1000)
        except Exception:
            pass
    try:
        session.sport = Sport.CYCLING
        # virtual_activity is the canonical sub_sport for indoor / simulator-style.
        session.sub_sport = SubSport.VIRTUAL_ACTIVITY
    except Exception as e:
        log.debug("FIT field write failed (session.sport/sub_sport): %s", e)
    # Indoor trainer flag — set HERE plus on the multipart upload form
    # (master decisions §9 — set in BOTH places).
    try:
        session.trainer = True  # type: ignore[attr-defined]
    except Exception as e:
        log.debug("FIT field write failed (session.trainer): %s", e)
    try:
        session.total_elapsed_time = float(summary.get("duration_sec", 0))
        session.total_timer_time = float(summary.get("duration_sec", 0))
    except Exception as e:
        log.debug("FIT field write failed (session.total_*_time): %s", e)
    try:
        session.total_distance = float(summary.get("distance_km", 0)) * 1000.0
    except Exception as e:
        log.debug("FIT field write failed (session.total_distance): %s", e)
    try:
        if summary.get("avg_power") is not None:
            session.avg_power = int(summary["avg_power"])
        if summary.get("max_power") is not None:
            session.max_power = int(summary["max_power"])
        if summary.get("normalized_power") is not None:
            session.normalized_power = int(summary["normalized_power"])
        if summary.get("avg_hr") is not None and summary["avg_hr"]:
            session.avg_heart_rate = int(summary["avg_hr"])
        if summary.get("max_hr") is not None and summary["max_hr"]:
            session.max_heart_rate = int(summary["max_hr"])
        if summary.get("avg_cadence") is not None:
            session.avg_cadence = int(summary["avg_cadence"])
    except Exception as e:
        log.debug("FIT field write failed (session optional fields): %s", e)
    builder.add(session)

    # ── ActivityMessage ──────────────────────────────────────────────────
    activity = ActivityMessage()
    try:
        activity.timestamp = last_ts_ms
        activity.total_timer_time = float(summary.get("duration_sec", 0))
        activity.num_sessions = 1
        activity.event = Event.ACTIVITY
        activity.event_type = EventType.STOP
    except Exception as e:
        log.debug("FIT field write failed (activity fields): %s", e)
    builder.add(activity)

    return builder.build().to_bytes()
