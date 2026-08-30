"""Ride archive: save/load/list/delete completed rides.

Rides are stored as JSON in ~/.domestique/profiles/{id}/rides/
(v3.0.0: data-dir migrated from ~/.chickencycling/ — see
profile_manager._maybe_migrate_data_dir).
The columnar sample format keeps a 2-hour ride under 500KB (50KB gzipped).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("domestique.rides")

# v2.2.9 — locally-computed DFA augmentation keys (written by app.py's
# _augment_icu_record_with_dfa from the raw FIT, NOT present in the ICU
# payload). persist_icu_activity carries these forward on re-persist so a
# re-sync never wipes them. Mirror the augment's full write set.
_DFA_CARRY_KEYS = (
    "dfa_alpha1", "dfa_alpha1_avg", "dfa_alpha1_series",
    "dfa_alpha1_lt1_minutes", "dfa_alpha1_status", "rr_intervals_count",
    "dfa_hrvt1", "dfa_hrvt2", "dfa_zone_minutes",
    "dfa_algo_version", "dfa_timeout_count",
)

# v3.5.3 — locally-hydrated stream data the ICU payload never carries. The
# power-curve backfill writes ride["streams"] into the persisted envelope;
# re-persisting on the hourly re-sync used to overwrite the file WITHOUT it,
# flipping power_curve's needs-refetch back on and re-downloading per-
# activity streams for the whole window every open (and starving every
# streams consumer — fidelity axis, fatigue resistance — down to whatever
# the last backfill managed). Same carry-forward pattern as _DFA_CARRY_KEYS.
#
# DELIBERATELY NOT carried: the terminal markers no_streams_available /
# streams_fetch_failed_at. Carrying them made a TRANSIENT failure permanent
# (an ICU 429 mid-backfill stamped the marker, every future re-persist kept
# it, _needs_refetch never fired again, and no reader implements a retry-
# after). Letting the re-persist clear them restores the pre-v3.5.3 retry
# semantics — a genuinely streamless ride is re-probed at most once per
# backfill pass, which is exactly what the hourly wipe used to cost.
_BACKFILL_CARRY_KEYS = ("streams",)

# v3.5.6 — the rider's OWN post-ride input. Never present in any ICU payload,
# so a re-sync would silently erase it (the laps/streams lesson). Carried
# unconditionally: unlike streams there is no "fresh payload wins" case,
# because ICU can never supply these.
#   rpe        — Foster CR-10 integer 0-10 (the scale the whole session-RPE
#                evidence base uses; Borg 6-20 / CR-100 are statistically
#                interchangeable but produce different absolute numbers, which
#                would break every published threshold).
#   rpe_at     — ISO timestamp of the rating, so staleness is auditable.
#   rpe_scale  — pinned scale id; a future scale change must not silently
#                reinterpret old ratings.
_RIDER_INPUT_CARRY_KEYS = ("rpe", "rpe_at", "rpe_scale")

# FTP-tests IP W2a — sync-path FTP-test artifacts, computed locally (never in
# any ICU payload). Two carry classes, mirroring the lessons above:
#   * detection output (suggestion + flags) — payload-wins semantics like
#     _DFA_CARRY_KEYS: a fresh local re-detection may legitimately replace it,
#     but a plain re-sync must not erase it (the modal typically fires the
#     NEXT morning, well past the 30-min re-persist that used to wipe it).
#   * the rider's review verdict (accepted / declined / edited) — rider input,
#     carried UNCONDITIONALLY like RPE: nothing ICU sends can ever supply it,
#     and losing it re-nags the rider with a modal they already answered.
_FTP_TEST_CARRY_KEYS = (
    "is_ftp_test", "ftp_test_type", "ftp_test_suggestion",
    "ftp_test_halted", "ftp_test_halt_step",
)
_FTP_TEST_REVIEW_CARRY_KEYS = ("ftp_test_review",)


def _active_profile_dir() -> Path:
    """AC2a: the ACTIVE profile's directory — every archive dir hangs off it.

    Raises RuntimeError when no profile is active (post delete-last): the
    active_dir fallback is the profiles ROOT, and mkdir-ing archive dirs
    there is exactly the root-artifact resurrection bug (A12). Read paths
    catch this and return empty; write paths let it propagate.
    """
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    if pm.active_id is None:
        raise RuntimeError("no active profile: ride/wellness archives unavailable")
    return pm.active_dir


def _rides_dir() -> Path:
    """Get the rides directory for the active profile."""
    d = _active_profile_dir() / "rides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _icu_rides_dir() -> Path:
    """v4.4.0 — directory for ICU-synced normalized activity records.

    v3.0.0 AC2a: per-profile — ``<profile>/rides/icu/`` (was the global
    ``~/.domestique/rides/icu/``, which leaked every rider's ICU archive to
    whoever was active; migrate_profiles.migrate_archives_to_profiles moves
    the legacy global tree on first boot). One JSON file per ICU activity,
    keyed by ICU's external id. power_curve._icu_rides_dir delegates here —
    single source of truth.

    Tests can patch this helper to redirect the dir into a tmp path.
    """
    base = _active_profile_dir() / "rides" / "icu"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fit_rides_dir() -> Path:
    """v4.4.0 — directory containing raw FIT imports.

    Mirror of app._rides_fit_dir() (kept here so ride_storage.load_all_rides
    doesn't have to import app.py — that would be a circular import).
    v3.0.0 AC2a: per-profile ``<profile>/rides/`` (same dir as the ride
    summaries — FITs and summaries have always shared it, different
    extensions).
    """
    base = _active_profile_dir() / "rides"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _wellness_dir() -> Path:
    """v4.5.0 — directory for ICU-synced wellness records.

    v3.0.0 AC2a: per-profile ``<profile>/wellness/`` (was global). One JSON
    file per day, keyed by ISO date.

    Tests can patch this helper to redirect the dir into a tmp path.
    """
    base = _active_profile_dir() / "wellness"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _pick_first(d: dict, *keys):
    """First of ``keys`` present on ``d`` with a truthy value, else None."""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _first_num(d: dict, *keys) -> "int | None":
    """First of ``keys`` present on ``d`` as an int; None if none are usable.

    0 is a real offset (the first lap starts there), so this cannot use the
    usual ``or`` chain.
    """
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool) or v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _normalize_icu_activity(a: dict) -> dict:
    """v4.4.0 — translate an ICU activity dict to the §3 normalized shape.

    ICU's payload uses fields like icu_pm_p_avg / icu_intensity / icu_efftp
    that the calendar UI doesn't speak. Map them onto the canonical names so
    the rest of the server can treat ICU and FIT rides interchangeably.

    Missing fields stay None (the UI handles gracefully).
    """
    if not isinstance(a, dict):
        return {}

    icu_id = a.get("id") or a.get("activity_id") or ""
    if not icu_id:
        return {}
    icu_id = str(icu_id)

    # v3.8.1 — a STUB is not a ride. For an activity that reached
    # intervals.icu via Strava, ICU returns five keys and nothing else: id,
    # source="STRAVA", start_date_local, and two nulls. It does not re-share
    # Strava's data. Taken at face value that became a record with no name and
    # every number zero, which the calendar drew as a row called "Activity"
    # with empty fields — a ride the rider appears to have done with no data.
    #
    # Worse, it could not be repaired: importing the real FIT deduped INTO the
    # stub, and the ICU side wins that dedupe by design ("ICU is richer"),
    # which is true of every ICU record except this one. So the rider's own
    # file was discarded in favour of a blank.
    #
    # Refusing the stub fixes all three: nothing blank is written, the FIT has
    # no twin to lose to, and the dedupe rule needs no exception. The ride is
    # absent rather than wrong — and absent is recoverable by importing the
    # file, which now works.
    # All three absent. A stub has NOTHING — the real one carries only id,
    # source and a start time. Requiring a missing name too is what keeps a
    # genuine ride that merely lacks a type (an untagged local FIT relayed to
    # ICU) from being thrown away with it.
    if (not _pick_first(a, "name")
            and not _pick_first(a, "type", "sport_type")
            and not _pick_first(a, "elapsed_time", "moving_time", "duration",
                                "icu_distance", "distance")):
        log.info("ICU activity %s is a %s stub (no type, no duration) — not "
                 "persisting; import the FIT to get this ride",
                 icu_id, a.get("source") or "source-less")
        return {}

    # Distance comes in meters from ICU.
    distance_m = a.get("distance")
    distance_km = None
    if distance_m is not None:
        try:
            distance_km = round(float(distance_m) / 1000.0, 2)
        except (TypeError, ValueError):
            distance_km = None

    # Some ICU fields live under raw_json (string JSON). Try to parse if
    # present so we don't drop NP / IF when the top-level fields are absent.
    raw_val = a.get("raw_json")
    raw: dict = {}
    if isinstance(raw_val, dict):
        raw = raw_val
    elif isinstance(raw_val, str) and raw_val:
        try:
            decoded = json.loads(raw_val)
            if isinstance(decoded, dict):
                raw = decoded
        except (json.JSONDecodeError, ValueError):
            raw = {}

    def _pick(*keys):
        """Return first non-None value across both top-level and raw."""
        for k in keys:
            if k in a and a[k] is not None:
                return a[k]
            if k in raw and raw[k] is not None:
                return raw[k]
        return None

    # v3.8.1 — icu_average_watts FIRST. The other three names are not returned
    # by intervals.icu at all: probed live against both an indoor and an
    # outdoor activity, icu_pm_p_avg / average_watts / avg_watts were absent on
    # both while icu_average_watts carried the value. So avg_power_w has been
    # None on every ICU ride ever synced. It went unnoticed because np_w reads
    # icu_weighted_avg_watts and is correct, and that is the number most views
    # show — but this field also decides load_source, so a power ride could be
    # tagged as HR-derived.
    avg_power = _pick("icu_average_watts", "icu_pm_p_avg",
                      "average_watts", "avg_watts")
    np_w = _pick(
        "icu_weighted_avg_watts",
        "weighted_average_watts",
        "icu_pm_p_norm",
    )
    if_pct = _pick("icu_intensity", "intensity_factor")
    # ICU stores intensity as a 0-1 fraction in some payloads; normalize to %.
    if isinstance(if_pct, (int, float)) and 0 < if_pct < 2:
        if_pct = round(float(if_pct) * 100.0, 1)
    elif if_pct is not None:
        try:
            if_pct = round(float(if_pct), 1)
        except (TypeError, ValueError):
            if_pct = None
    tss = _pick("icu_training_load", "training_load")
    avg_hr = _pick("average_heartrate", "icu_average_hr")
    hr_max = _pick("max_heartrate", "icu_max_hr")
    avg_cad = _pick("average_cadence", "icu_average_cadence")
    elevation = _pick("total_elevation_gain", "icu_elevation_gain")
    moving = _pick("moving_time", "icu_moving_time")
    duration = _pick("elapsed_time", "moving_time", "icu_moving_time")
    kj = _pick("kilojoules", "icu_joules")
    # ICU's icu_joules is in joules (not kJ).
    if kj is not None and a.get("icu_joules") and not a.get("kilojoules"):
        try:
            kj = round(float(kj) / 1000.0, 1)
        except (TypeError, ValueError):
            pass
    kj_above_ftp = _pick("icu_joules_above_ftp", "kj_above_ftp")
    if kj_above_ftp is not None:
        try:
            # Sometimes joules; convert if it's clearly large.
            kjv = float(kj_above_ftp)
            if kjv > 1000:
                kj_above_ftp = round(kjv / 1000.0, 1)
            else:
                kj_above_ftp = round(kjv, 1)
        except (TypeError, ValueError):
            kj_above_ftp = None
    kcal = _pick("calories", "icu_calories")
    weight = _pick("icu_athlete_weight", "athlete_weight")
    ftp_at_ride = _pick("icu_ftp", "ftp")
    eftp_at_ride = _pick("icu_efftp", "icu_eftp", "eftp")

    # Power time-in-zone — ICU exposes two shapes:
    #   1. legacy: list[int] (seconds per zone, ordered Z1..Z7)
    #   2. v4.5.5: list[{"id": "Z1"|..|"Z7"|"SS", "secs": int}]   (full activity GET)
    # We persist the canonical {z1..z7, ss} dict so the detail UI can read it
    # without re-deriving zone order. SS = sweet-spot bucket (84-97% FTP).
    tiz: dict = {f"z{i}": 0 for i in range(1, 8)}
    pzt = _pick("icu_power_hr_zone_times", "icu_zone_times", "power_zone_times")
    if isinstance(pzt, list):
        if pzt and isinstance(pzt[0], dict):
            # New shape from /api/v1/activity/<id>: list of {id, secs}
            for entry in pzt:
                if not isinstance(entry, dict):
                    continue
                zid = (entry.get("id") or "").lower()
                try:
                    secs = int(entry.get("secs") or 0)
                except (TypeError, ValueError):
                    secs = 0
                if zid in {"z1", "z2", "z3", "z4", "z5", "z6", "z7"}:
                    tiz[zid] = secs
                elif zid == "ss":
                    tiz["ss"] = secs
        else:
            for i, sec in enumerate(pzt[:7], start=1):
                try:
                    tiz[f"z{i}"] = int(sec or 0)
                except (TypeError, ValueError):
                    tiz[f"z{i}"] = 0

    # HR time-in-zone — ICU returns icu_hr_zone_times as list[int] of seconds.
    hr_tiz: dict | None = None
    hzt = _pick("icu_hr_zone_times", "hr_zone_times")
    if isinstance(hzt, list) and hzt:
        hr_tiz = {f"z{i}": 0 for i in range(1, 8)}
        for i, sec in enumerate(hzt[:7], start=1):
            try:
                hr_tiz[f"z{i}"] = int(sec or 0)
            except (TypeError, ValueError):
                hr_tiz[f"z{i}"] = 0

    # Intervals — ICU's /api/v1/activity/<id>/intervals returns
    # icu_intervals as a list of objects with start_index, elapsed_time,
    # moving_time, average_watts, average_heartrate, label, type, zone, etc.
    # The bare /activity/<id> usually returns icu_intervals=null; the caller
    # has to hit the /intervals subpath to populate. We accept both shapes.
    intervals_out: list[dict] = []
    iv = _pick("icu_intervals", "intervals")
    # ONE clock for the whole activity. start_index counts moving seconds and
    # start_time elapsed ones; they part company the moment the rider stops, so
    # taking whichever each interval happens to carry mixes the two and can
    # hand a later lap an EARLIER offset than the one before it.
    _use_index = (isinstance(iv, list) and iv
                  and all(isinstance(x, dict) and x.get("start_index") is not None
                          for x in iv))
    if isinstance(iv, list):
        for i, ivd in enumerate(iv):
            if not isinstance(ivd, dict):
                continue
            avg_w = ivd.get("average_watts") or ivd.get("avg_watts")
            zone_band = ivd.get("zone")
            z_band: str | None = None
            if isinstance(zone_band, (int, float)):
                z_band = f"Z{int(zone_band)}"
            ftp_pct: float | None = None
            if avg_w is not None and ftp_at_ride:
                try:
                    ftp_pct = round(
                        float(avg_w) / float(ftp_at_ride) * 100.0, 0
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    ftp_pct = None
            # v3.7.0 — WHERE on the ride this lap sits, not just how long it
            # was. ICU carries two clocks: start_index/end_index index the
            # sample stream (moving seconds), start_time/end_time are the
            # elapsed wall clock. They agree until the rider stops — measured
            # on a real ride here, 2717 s of moving time inside 2721 s
            # elapsed. Block grading uses the MOVING clock, because that is
            # the one that keeps running with the workout: a five-minute stop
            # at the lights must not read as five minutes of the plan gone by.
            # Without this the only clock available was the sum of the lap
            # durations before it, which is the same number only while nobody
            # pauses and the laps tile with no gaps.
            start_s = (_first_num(ivd, "start_index") if _use_index
                       else _first_num(ivd, "start_time"))
            elapsed_start_s = _first_num(ivd, "start_time", "start_index")
            ivobj = {
                "id": ivd.get("id") if ivd.get("id") is not None else i,
                "name": (
                    ivd.get("label")
                    or ivd.get("name")
                    or ivd.get("group_id")
                    or ivd.get("type")
                    or f"Interval {i+1}"
                ),
                "type": ivd.get("type"),
                "start_s": start_s,
                "elapsed_start_s": elapsed_start_s,
                "duration_s": int(
                    ivd.get("moving_time")
                    or ivd.get("elapsed_time")
                    or ivd.get("duration")
                    or 0
                ),
                "avg_power_w": int(avg_w) if avg_w is not None else None,
                "avg_hr_bpm": int(ivd.get("average_heartrate") or 0) or None,
                "ftp_pct": ftp_pct,
                "z_band": z_band,
            }
            intervals_out.append(ivobj)

    # Efforts — ICU's icu_efforts is a list of {label, watts, secs, ...} for
    # power-curve highlights (e.g. best 5s / 1m / 5m / 20m). Pass-through.
    efforts_out: list[dict] = []
    eff = _pick("icu_efforts", "efforts")
    if isinstance(eff, list):
        for e in eff:
            if isinstance(e, dict):
                efforts_out.append(e)

    name = a.get("name") or raw.get("name") or ""
    started_at = (
        a.get("start_date_local")
        or a.get("start_date")
        or raw.get("start_date_local")
        or ""
    )

    # Polarization (computed from the persisted time_in_zone — no extra ICU call).
    from analytics import compute_polarization_block
    polarization = compute_polarization_block(tiz)
    # If ICU provided its own polarization_index (it does on the activity GET),
    # prefer that for the index value but keep our computed pcts + classification.
    icu_pi = a.get("polarization_index")
    if polarization and isinstance(icu_pi, (int, float)):
        polarization["polarization_index"] = round(float(icu_pi), 2)

    # v4.6.6 IMPL-C — per-ride RPE for IMPL-B's G7 gate (Foster 1998 session-RPE).
    # ICU pushes `feel` (1=easy..5=very hard) on every activity GET; some
    # imports also include `perceivedExertion` (Borg CR-10, 1..10). Both are
    # optional — legacy rides have neither and load with None.
    feel_raw = _pick("feel")
    feel = None
    if isinstance(feel_raw, (int, float)) and 1 <= feel_raw <= 5:
        feel = int(feel_raw)
    pe_raw = _pick("perceivedExertion", "perceived_exertion")
    perceived_exertion = None
    if isinstance(pe_raw, (int, float)) and 1 <= pe_raw <= 10:
        perceived_exertion = int(pe_raw)

    return {
        "ride_id": f"icu_{icu_id}",
        "source": "icu",
        "external_id": icu_id,
        # v3.5.3 — persist ICU's activity type ("Hike", "Ride", "VirtualRide",
        # …). Same source fields db.py uses for the activities table. Without
        # this every ICU record was sport-less, and _is_cycling_sport("")'s
        # deliberate empty→cycling default (for untagged local FITs) let a
        # HIKE become the calendar day's "actual" ride — green ✓ on a planned
        # cycling session the rider never did.
        "sport": _pick("type", "sport_type") or "",
        "name": name,
        "started_at": started_at,
        "duration_s": int(duration or 0),
        "moving_s": int(moving or 0) if moving else None,
        "distance_km": distance_km,
        "elevation_m": int(elevation) if elevation else None,
        "avg_power_w": int(avg_power) if avg_power else None,
        "np_w": int(np_w) if np_w else None,
        "if_pct": if_pct,
        "tss": round(float(tss), 1) if tss is not None else None,
        # W4 (v2.5.0): provenance tag for the load number — "icu" when the
        # ride has power, "hr_icu" when ICU derived the load from HR only.
        # Pure tag: the tss VALUE above is untouched (ICU's own hr-based
        # load is already the ride's TSS for no-power rides).
        "load_source": (
            None if tss is None else ("icu" if avg_power else "hr_icu")
        ),
        "kj": round(float(kj), 1) if kj is not None else None,
        "kj_above_ftp": kj_above_ftp,
        "kcal": int(kcal) if kcal else None,
        "avg_hr": int(avg_hr) if avg_hr else None,
        "hr_max": int(hr_max) if hr_max else None,
        "avg_cadence": int(avg_cad) if avg_cad else None,
        "weight_kg": round(float(weight), 1) if weight else None,
        "ftp_at_ride": int(ftp_at_ride) if ftp_at_ride else None,
        "eftp_at_ride": int(eftp_at_ride) if eftp_at_ride else None,
        "decoupling_pct": _pick("decoupling"),
        "dfa_alpha1": _pick("dfa_alpha1"),
        "feel": feel,                               # 1..5 | None  (G7 input)
        "perceived_exertion": perceived_exertion,   # 1..10 | None (G7 input)
        "time_in_zone": tiz,
        "hr_time_in_zone": hr_tiz,
        "intervals": intervals_out,
        "efforts": efforts_out,
        "polarization": polarization,
        "samples_url": f"/api/ride/icu_{icu_id}/detail?include=samples",
    }


def purge_stub_icu_records() -> int:
    """v3.8.1 — delete blank records a Strava-origin stub already wrote.

    The guard in _normalize_icu_activity stops new ones, but a rider who
    synced before the fix still has rows in their calendar called "Activity"
    with every field empty — and those rows still out-rank the rider's own FIT
    in the dedupe, so importing the real file cannot repair them.

    Only a record with NO name AND no duration AND no load is removed: that
    combination cannot describe a ride anyone actually did, and the ride
    returns intact the moment the FIT is imported. Returns the count removed.
    """
    removed = 0
    try:
        icu_dir = _icu_rides_dir()
    except RuntimeError:
        return 0
    for f in icu_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        if (not (d.get("name") or "").strip()
                and not (d.get("duration_s") or 0)
                and d.get("tss") is None
                and d.get("avg_power_w") is None):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                log.debug("purge_stub_icu_records(%s) skipped: %s", f.name, e)
    if removed:
        log.info("purged %d blank ICU stub record(s)", removed)
    return removed


_PRUNE_MAX_PER_PASS = 20


def prune_deleted_icu_records(fetched_ids: "set[str]",
                              window_days: int) -> int:
    """Delete local ICU records whose activity no longer exists upstream.

    Sync only ever UPSERTED, so an activity deleted on intervals.icu lived on
    here forever — the reported case was a ride uploaded twice (Karoo and
    Garmin), one copy deleted upstream, both still on the calendar after a
    resync.

    Safety invariants, in order of importance:
      * Only ICU-sourced records (this function only reads the icu/ dir);
        a rider's own FIT imports are never candidates.
      * Only records whose start date lies INSIDE the just-fetched window —
        outside it the fetch says nothing about existence. The window is
        shrunk by one day on the old edge so a timezone straddle at the
        boundary can never delete a real ride.
      * The caller must only pass ids from a SUCCESSFUL fetch. An empty set
        is valid (everything in-window was deleted upstream) — a failed or
        throttled fetch must not reach this function at all.
      * At most _PRUNE_MAX_PER_PASS deletions per pass. A payload change that
        parses to "no activities" looks identical to mass upstream deletion,
        and the difference between the two is that no rider deletes their
        whole archive between syncs (the v3.3.0 lesson: infra failure must
        not be treated as per-record truth). Over the cap: delete nothing,
        log loudly.

    Removes both stores per record — the icu/<id>.json file and the
    activities row in SQLite (best-effort; the DB may not be initialised in
    minimal test harnesses). Returns the number of records pruned.
    """
    from datetime import date as _date, timedelta as _td
    try:
        icu_dir = _icu_rides_dir()
    except RuntimeError:
        return 0
    oldest = (_date.today() - _td(days=max(0, window_days - 1))).isoformat()
    stale = []
    for f in icu_dir.glob("*.json"):
        rid = f.stem
        if rid in fetched_ids:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        started = str((d or {}).get("started_at") or "")[:10]
        if not started or started < oldest:
            continue  # outside the window the fetch can vouch for
        stale.append((f, rid, started))
    if not stale:
        return 0
    if len(stale) > _PRUNE_MAX_PER_PASS:
        log.warning(
            "EVENT=icu_prune_capped candidates=%d cap=%d — refusing to prune: "
            "this many upstream deletions in one window smells like a fetch "
            "that parsed to nothing, not a rider clearing their archive",
            len(stale), _PRUNE_MAX_PER_PASS)
        return 0
    pruned = 0
    for f, rid, started in stale:
        try:
            f.unlink()
        except OSError as e:
            log.debug("prune_deleted_icu_records(%s) skipped: %s", rid, e)
            continue
        pruned += 1
        log.info("EVENT=icu_record_pruned id=%s started=%s (deleted upstream)",
                 rid, started)
        try:
            import db as _db
            conn = _db.get_db()
            conn.execute("DELETE FROM activities WHERE id = ?", (rid,))
            conn.commit()
        except Exception as e:  # noqa: BLE001 — file removal is the source of truth
            log.debug("prune sqlite row (%s) skipped: %s", rid, e)
    return pruned


def backfill_icu_sports_from_db() -> int:
    """v3.5.3 one-shot: copy ``sport`` from db.activities into ICU envelopes
    that predate the sport key (older than the incremental sync window, so
    they will never be re-fetched to heal themselves).

    db.activities.id is the ICU id verbatim and its sport column was always
    mapped from the same payload fields, so this is a pure key copy. Marker-
    guarded per profile ICU dir; returns the number of files healed (0 on
    marker present / nothing to do). Best-effort per file.
    """
    icu_dir = _icu_rides_dir()
    marker = icu_dir / ".sport_backfill_done"
    if marker.exists():
        return 0
    try:
        import db as _db
        rows = _db.get_db().execute(
            "SELECT id, sport FROM activities WHERE sport IS NOT NULL AND sport != ''"
        ).fetchall()
    except Exception as e:  # noqa: BLE001 — no db, nothing to copy from
        log.debug(f"sport backfill: db read failed: {e}")
        return 0
    sport_by_id = {str(r[0]): str(r[1]) for r in rows}
    if not sport_by_id:
        # Nothing to copy from — do NOT stamp the marker: an empty read can
        # mean "wrong db context" (profile not active yet) rather than "no
        # sports exist"; leaving the marker off lets a later, correctly-
        # contexted sync heal.
        return 0
    healed = 0
    for p in icu_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("sport"):
                continue
            sport = sport_by_id.get(str(data.get("external_id") or p.stem))
            if not sport:
                continue
            data["sport"] = sport
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            healed += 1
        except Exception as e:  # noqa: BLE001 — skip the file, heal the rest
            log.debug(f"sport backfill: {p.name}: {e}")
    # Stamp the marker only when every envelope now carries a sport — a
    # partially-healed dir (rows missing from db) stays retryable: a later
    # sync may know more, and the sweep itself is milliseconds for ~100
    # files, so retrying until clean is cheaper than permanent misclass.
    try:
        unhealed = sum(
            1 for p in icu_dir.glob("*.json")
            if not (json.loads(p.read_text(encoding="utf-8")) or {}).get("sport")
        )
    except Exception:  # noqa: BLE001
        unhealed = 1  # unknown state — do not stamp
    if unhealed == 0:
        try:
            marker.write_text("v3.5.3\n", encoding="utf-8")
        except OSError:
            pass
    if healed:
        log.info(f"sport backfill: healed {healed} ICU envelope(s) from db")
    return healed


def persist_icu_activity(activity: dict, carry_hydrated: bool = True) -> Path | None:
    """v4.4.0 — write a normalized ICU activity record to the ICU rides dir.

    Idempotent: overwrites an existing file with the same id. Returns the
    written path, or None if the activity is malformed (no id).

    v1.3.0 IMPL-PR-DETECTION: after the envelope is on disk, lazily compute
    the per-ride PR list via ``power_curve.compute_ride_prs`` and merge it
    back into the persisted JSON. Failure is non-fatal — the ride still
    persists; the PR list will be regenerated by the lazy /api/ride/{id}/prs
    endpoint on first read.
    """
    norm = _normalize_icu_activity(activity)
    icu_id = norm.get("external_id")
    if not icu_id:
        return None
    path = _icu_rides_dir() / f"{icu_id}.json"
    # W2B-G9 fix: read prior `prs[]` (if any) BEFORE we overwrite, so a
    # subsequent compute_ride_prs failure doesn't silently drop the
    # existing PR list. We carry it forward into the fresh `norm`.
    # v2.2.9 fix: ALSO carry forward locally-computed DFA augmentation. The ICU
    # payload has no DFA α1 / HRVT thresholds — those are computed locally from
    # the FIT by _augment_icu_record_with_dfa. Re-persisting on a re-sync
    # (status="updated") used to overwrite the file with the DFA-less ICU
    # payload, wiping dfa_alpha1_avg / dfa_hrvt1 / dfa_hrvt2; the per-sync
    # augment budget then only recomputed a few, collapsing the DFA threshold
    # aggregate to "no thresholds detected". Carrying them forward keeps the
    # augment's sticky-skip working too (already-computed rides stay fast).
    prior_prs = None
    prior_dfa: dict = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                if isinstance(prior.get("prs"), list):
                    prior_prs = prior["prs"]
                for k in _DFA_CARRY_KEYS:
                    if prior.get(k) is not None:
                        prior_dfa[k] = prior[k]
                if carry_hydrated:
                    for k in _BACKFILL_CARRY_KEYS:
                        if prior.get(k) is not None:
                            prior_dfa[k] = prior[k]
                    # `efforts` IS a norm key (ICU intervals-derived), so
                    # setdefault can't carry it — but the backfill's version
                    # (derived from full 1 Hz streams) is strictly richer
                    # than a fresh payload's empty list. Only prefer prior
                    # when the fresh normalization produced nothing.
                    if prior.get("efforts") and not norm.get("efforts"):
                        norm["efforts"] = prior["efforts"]
                    # v3.5.6 — same for `intervals` (the rider's Garmin laps).
                    # Laps only arrive via the /intervals subpath on a DETAIL
                    # fetch (fetch_activity_full); the activity-LIST payload the
                    # hourly sync uses has none. So a re-sync used to overwrite
                    # a lap-carrying envelope with an empty list and silently
                    # destroy them — measured: a 61-lap ride went to 0 laps on
                    # one forced sync. Laps are the primary signal for
                    # block-by-block ride evaluation, so losing them is not
                    # cosmetic. Prefer prior only when the fresh payload brought
                    # nothing, so a genuine detail refresh still wins.
                    if prior.get("intervals") and not norm.get("intervals"):
                        norm["intervals"] = prior["intervals"]
                # The rider's own RPE always survives — carried even when
                # carry_hydrated is False (that flag exists so a fresh DETAIL
                # fetch can replace stale ICU-derived data; it must never
                # discard something only the rider could provide).
                for k in _RIDER_INPUT_CARRY_KEYS:
                    if prior.get(k) is not None:
                        norm.setdefault(k, prior[k])
                # W2a: locally-computed FTP-test detection (payload-wins) +
                # the rider's review verdict (unconditional, like RPE).
                for k in _FTP_TEST_CARRY_KEYS:
                    if prior.get(k) is not None:
                        norm.setdefault(k, prior[k])
                for k in _FTP_TEST_REVIEW_CARRY_KEYS:
                    if prior.get(k) is not None:
                        norm.setdefault(k, prior[k])
        except (json.JSONDecodeError, OSError) as e:
            log.debug(f"persist_icu_activity({icu_id}) prior read: {e}")
    if prior_prs is not None:
        norm["prs"] = prior_prs
    # Only fill DFA keys the fresh ICU payload didn't already provide.
    for k, v in prior_dfa.items():
        norm.setdefault(k, v)
    try:
        path.write_text(json.dumps(norm, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"persist_icu_activity({icu_id}) failed: {e}")
        return None
    # v1.3.0 PR-DETECTION final step (audit §4): compute PRs against the
    # 90-day rolling window after the envelope is on disk so compute_ride_prs
    # can read the just-written ride_id from cache. On failure keep
    # `prior_prs` (already in `norm`) so we don't lose data.
    try:
        import power_curve
        prs = power_curve.compute_ride_prs(norm["ride_id"])
        norm["prs"] = prs
        path.write_text(json.dumps(norm, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug(f"persist_icu_activity({icu_id}) PR compute failed: {e}")
    return path


def load_icu_rides() -> list[dict]:
    """v4.4.0 — load every persisted ICU normalized record."""
    out: list[dict] = []
    try:
        d = _icu_rides_dir()
    except RuntimeError:
        return out  # AC6a: no active profile — nothing to load, nothing created
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("ride_id"):
                out.append(data)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load ICU ride {f}: {e}")
    return out


_SAME_RIDE_START_TOLERANCE_S = 300.0


def _same_ride_start(a: dict, b: dict) -> bool:
    """Are these two records the same ride, judged on when they started?

    The dedupe bucket rounds to the day and the whole minute, which is fine for
    deciding "don't list this twice" and NOT fine for moving rider data between
    records. Unknown or unparseable timestamps answer False: refusing to carry a
    rating is recoverable, attaching it to the wrong ride is not.
    """
    import datetime as _dt

    def _parse(rec):
        raw = rec.get("started_at") or rec.get("start_date_local") or ""
        try:
            return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    ta, tb = _parse(a), _parse(b)
    if ta is None or tb is None:
        return False
    if (ta.tzinfo is None) != (tb.tzinfo is None):
        ta = ta.replace(tzinfo=None)
        tb = tb.replace(tzinfo=None)
    return abs((ta - tb).total_seconds()) <= _SAME_RIDE_START_TOLERANCE_S


def _persist_rider_input(external_id, values: dict) -> None:
    """Write carried rider input onto the ICU record on disk (best effort).

    Called when a rated FIT import is deduped into its intervals.icu twin.
    Silent on failure: a listing must never break because a convenience write
    could not land — the value is still on the returned dict for this call.
    """
    if not external_id or not values:
        return
    try:
        path = _icu_rides_dir() / f"{external_id}.json"
        if not path.exists():
            return
        rec = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for k, v in values.items():
            if rec.get(k) is None:
                rec[k] = v
                changed = True
        if changed:
            path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"_persist_rider_input({external_id}) skipped: {e}")


def load_all_rides() -> list[dict]:
    """v4.4.0 — return ICU-synced records + raw FIT imports as one merged
    flat list, deduped (ICU preferred when both have the same activity).

    Each entry carries ``ride_id`` (icu_<id> or fit_<sha8>), ``source``
    ("icu"/"fit"), and ``started_at`` (ISO local timestamp). Sort: newest
    first by started_at.

    Dedupe rule: if a FIT and ICU record share the same started_at-day +
    duration ±60s, keep the ICU version (richer metadata).
    """
    icu = load_icu_rides()

    fits: list[dict] = []
    try:
        fit_dir = _fit_rides_dir()
    except RuntimeError:
        return icu  # AC6a: no active profile (icu is [] too)
    for f in sorted(fit_dir.glob("*.fit")):
        try:
            st = f.stat()
        except OSError:
            continue
        # Use a sha-stable id derived from the filename stem (which is the
        # ISO-timestamp set at upload-time). Prefix with fit_ per §3.
        entry = {
            "ride_id": f"fit_{f.stem}",
            "source": "fit",
            "external_id": None,
            "name": "",
            "started_at": _fit_iso_started_at(f),
            "duration_s": None,
            "size_bytes": st.st_size,
            "_fit_path": str(f),
        }
        # W4 (v2.5.0): attach the persisted load summary (tss + load_source,
        # plus true duration/start when derivable) so compute_local_atl /
        # recent_mean_weekly_tss / analytics count imported FITs without any
        # reader edits — and so the ICU dedupe below can match the ICU copy
        # of a ride the importer relayed to intervals.icu. A missing sidecar
        # (pre-W4 import) is lazily computed + persisted once.
        load = read_fit_load_sidecar(f)
        if load is None:
            load = write_fit_load_sidecar(f)
        if isinstance(load, dict):
            if load.get("duration_s"):
                try:
                    entry["duration_s"] = int(load["duration_s"])
                except (TypeError, ValueError):
                    pass
            if load.get("started_at"):
                entry["started_at"] = str(load["started_at"])
            if load.get("tss") is not None:
                entry["tss"] = load["tss"]
                entry["load_source"] = load.get("load_source")
            # v3.6.0 — the rider's own session-RPE lives in the sidecar for a
            # FIT import (there is no ICU record to hold it). Surface it so the
            # G7 3-day-mean gate sees a FIT-only rider's ratings too.
            for k in ("rpe", "rpe_at", "rpe_scale"):
                if load.get(k) is not None:
                    entry[k] = load[k]
        fits.append(entry)

    # Dedupe: index ICU rides by (date-prefix, duration-bucket).
    def _bucket(r: dict) -> tuple[str, int]:
        s = (r.get("started_at") or "")[:10]
        dur = r.get("duration_s") or 0
        try:
            return (s, int(dur) // 60)
        except (TypeError, ValueError):
            return (s, 0)

    # The bucket is only (date, whole minutes), so it narrows the search — it
    # does not decide identity. Two different rides on one day of the same
    # rounded length land in the same bucket, and treating that as "same ride"
    # made one of them disappear from every listing. _same_ride_start is the
    # predicate that decides; the bucket may hold several candidates.
    icu_by_key: dict = {}
    for r in icu:
        if r.get("started_at"):
            icu_by_key.setdefault(_bucket(r), []).append(r)

    merged: list[dict] = list(icu)
    for r in fits:
        bk = _bucket(r)
        twin = next((t for t in icu_by_key.get(bk, []) if _same_ride_start(r, t)),
                    None) if bk[0] else None
        if twin is not None:
            # Carry the rider's own input across before dropping the FIT. A
            # FIT-only rider rates a ride, then the importer relays that same
            # file to intervals.icu; from then on the ICU twin wins the dedupe
            # and the rating would simply vanish from every reader, including
            # the 3-day gate. Nothing else can regenerate it. ICU's own value
            # wins if it has one — that surface is the newer edit.
            moved = {}
            # Grill pipeline/F2: only rider-input keys move here. FIT listing
            # rows never carry the FTP-test detection/review keys (detection
            # is recomputed from the file on read; FIT reviews have no
            # envelope), so carrying them was dead code claiming a guarantee
            # the FIT side never feeds. The ICU sync path re-detects the twin
            # from its own streams and persists its own review marker.
            for k in _RIDER_INPUT_CARRY_KEYS:
                if r.get(k) is not None and twin.get(k) is None:
                    twin[k] = r[k]
                    moved[k] = r[k]
            if moved:
                # PERSIST it onto the ICU record, don't just patch the dict
                # we happen to be returning. The ride-detail modal reads the
                # record straight off disk, so an in-memory carry left the
                # rider looking at their own rating missing.
                _persist_rider_input(twin.get("external_id"), moved)
            continue
        merged.append(r)

    merged.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return merged


def _fit_iso_started_at(fit_path: Path) -> str:
    """Best-effort started_at for a FIT file: prefer the file's mtime since
    we don't want to parse the full FIT just to get a sort key.
    """
    import datetime as _dt
    try:
        st = fit_path.stat()
        return _dt.datetime.fromtimestamp(
            st.st_mtime, _dt.timezone.utc
        ).astimezone().isoformat()
    except OSError:
        return ""


# ── W4 (v2.5.0) — post-ride load (TSS) for locally imported FITs ────────────
# POST /api/ride/import used to persist raw FIT bytes only, so imported rides
# (with or without power) contributed 0 to compute_local_atl /
# recent_mean_weekly_tss / analytics compliance. Fix is ingestion-side: a
# small ``<stem>.load.json`` sidecar next to the FIT carries {tss,
# load_source, duration_s, started_at}; load_all_rides attaches it to the
# FIT entry so every existing reader picks it up unchanged.


def _fit_load_sidecar_path(fit_path: Path) -> Path:
    """Sidecar JSON next to an imported FIT: ``<stem>.load.json``.

    Lives in ``~/.domestique/rides/`` alongside the FIT — nothing globs
    ``*.json`` there (ICU records live in the ``icu/`` subdir), so the
    sidecar can't be mistaken for a ride record.
    """
    return fit_path.with_name(f"{fit_path.stem}.load.json")


def compute_power_tss(
    power: list,
    ftp: int,
    duration_s: int | None = None,
) -> float | None:
    """Standard power TSS from a 1 Hz power series.

    NP = 30-s rolling 4th-power mean — the same windowing as
    ``training_live.MetricsEngine._wp_for_slice`` and
    ``fitness_estimation.aerobic_decoupling``'s per-half NP (full 30-sample
    windows only; sub-30-sample rides fall back to the plain average).
    TSS = dur_h × (NP/FTP)² × 100, matching the live engine's
    ``MetricsEngine.tss`` formula.

    Returns None when the series is empty or FTP is unusable (≤0).
    """
    if not power or not ftp or int(ftp) <= 0:
        return None
    series = [float(p or 0) for p in power]
    n = len(series)
    dur_s = int(duration_s) if duration_s and int(duration_s) > 0 else n
    if dur_s <= 0:
        return None
    if n < 30:
        np_w = sum(series) / n
    else:
        from collections import deque
        win: deque = deque(maxlen=30)
        p4_sum = 0.0
        count = 0
        for p in series:
            win.append(p)
            if len(win) == 30:
                avg30 = sum(win) / 30.0
                p4_sum += avg30 ** 4
                count += 1
        np_w = (p4_sum / count) ** 0.25 if count else 0.0
    if np_w <= 0:
        return None
    if_ = np_w / float(int(ftp))
    return round((dur_s / 3600.0) * (if_ ** 2) * 100.0, 1)


def compute_hr_tss(
    hr: list,
    lthr: int,
    max_hr: int | None = None,
) -> float | None:
    """hrTSS from a 1 Hz HR series: Σ((hr/lthr)²) / 3600 × 100.

    TRIMP-flavored APPROXIMATION — NOT TrainingPeaks-equivalent. It reuses
    the power-TSS analogy (TSS = IF² × hours × 100) with per-second hr/LTHR
    as the IF proxy; TrainingPeaks' hrTSS additionally regresses per-zone
    weighting that we deliberately don't replicate (IP_V250 W4, grill-locked).

    Samples with hr ≤ 0 are dropped (sensor gaps); hr is clamped to
    ``max_hr`` when provided so spikes can't inflate load. Assumes 1 Hz
    sampling (each sample contributes one second).

    Returns None when the series has no usable samples or LTHR is
    unusable (≤0).
    """
    if not hr or not lthr or int(lthr) <= 0:
        return None
    lthr_f = float(int(lthr))
    cap = float(max_hr) if max_hr and int(max_hr) > 0 else None
    total = 0.0
    used = 0
    for h in hr:
        try:
            hv = float(h or 0)
        except (TypeError, ValueError):
            continue
        if hv <= 0:
            continue
        if cap is not None and hv > cap:
            hv = cap
        total += (hv / lthr_f) ** 2
        used += 1
    if used == 0:
        return None
    return round(total / 3600.0 * 100.0, 1)


def compute_fit_load(fit_path: Path) -> dict | None:
    """Parse an imported FIT once and derive its training load.

    Priority (IP_V250 W4, grill-locked):
      1. power samples present → power TSS, load_source="power". When the
         producing app stored ``training_stress_score`` in the file, that
         value wins (it is the number ``_build_fit_normalized`` already
         displays — one number app-wide); otherwise NP-based TSS against
         profile FTP (``compute_power_tss``).
      2. no power, HR samples present → local hrTSS (``compute_hr_tss``),
         load_source="hr_local".
      3. neither → ``{"tss": None, "load_source": None}`` — a definitive
         marker (the file simply has no load signal) that stops re-parsing.

    Also carries ``duration_s`` + ``started_at`` (true ride start from the
    FIT session) so load_all_rides' existing FIT-vs-ICU dedupe can match
    the ICU copy of a ride the importer relayed to intervals.icu — without
    them the FIT copy (mtime date, no duration) never buckets with the ICU
    record and the ride's TSS would double-count.

    Returns None when nothing persistable was produced — FIT unparseable,
    or the required profile input (FTP for power / LTHR for HR) is not set
    yet — so the caller retries on a later listing instead of freezing a
    zero into a sidecar.
    """
    try:
        from fit_activity import parse_record_streams
        streams = parse_record_streams(fit_path)
    except Exception as e:
        log.debug(f"compute_fit_load({fit_path.name}) parse failed: {e}")
        return None
    if not isinstance(streams, dict):
        return None

    power = streams.get("power") or []
    hr = streams.get("hr") or []
    duration_s = int(streams.get("duration_s") or 0) or None
    has_power = any((p or 0) > 0 for p in power)
    has_hr = any((h or 0) > 0 for h in hr)

    started_at = None
    ms = streams.get("start_time_ms")
    if ms:
        import datetime as _dt
        try:
            dt = _dt.datetime.fromtimestamp(
                float(ms) / 1000.0, _dt.timezone.utc
            )
            if 2000 <= dt.year <= 2100:
                started_at = dt.astimezone().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            started_at = None

    out: dict = {
        "tss": None,
        "load_source": None,
        "duration_s": duration_s,
        "started_at": started_at,
    }
    if not has_power and not has_hr:
        return out  # definitive: no load signal in this file

    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        ftp = int(pm.ftp or 0)
        lthr = int(pm.lthr or 0)
        max_hr = int(pm.max_hr or 0)
    except Exception as e:
        log.debug(f"compute_fit_load({fit_path.name}) profile read failed: {e}")
        return None

    if has_power:
        file_tss = streams.get("file_tss")
        if file_tss is not None:
            out["tss"] = round(float(file_tss), 1)
            out["load_source"] = "power"
            return out
        tss = compute_power_tss(power, ftp, duration_s)
        if tss is None:
            return None  # FTP not set yet — retry once the profile is complete
        out["tss"] = tss
        out["load_source"] = "power"
        return out

    tss = compute_hr_tss(hr, lthr, max_hr)
    if tss is None:
        return None  # LTHR not set yet — retry once the profile is complete
    out["tss"] = tss
    out["load_source"] = "hr_local"
    return out


def write_fit_load_sidecar(fit_path: Path) -> dict | None:
    """Compute + persist the load sidecar for one imported FIT.

    Called eagerly by ``POST /api/ride/import`` and lazily by
    ``load_all_rides`` for FITs imported before W4. Only definitive results
    are persisted — a parse failure or a not-yet-configured profile returns
    None WITHOUT writing so the next listing retries.

    Returns the load dict even if the disk write fails (callers can still
    attach it for this listing).
    """
    # ponytail: sidecar is never invalidated when profile FTP/LTHR later
    # change; delete <stem>.load.json to force a recompute.
    load = compute_fit_load(fit_path)
    if load is None:
        return None
    side = _fit_load_sidecar_path(fit_path)
    # v3.6.0 — the sidecar now also holds the rider's own session-RPE, and this
    # function RECOMPUTES the whole file (a re-import calls it eagerly). Carry
    # rider input forward: it is the one thing no recompute can regenerate.
    # Same lesson as the laps/streams carry on the ICU path.
    prior = read_fit_load_sidecar(fit_path)
    if isinstance(prior, dict):
        for k in _RIDER_INPUT_CARRY_KEYS:
            if prior.get(k) is not None:
                load.setdefault(k, prior[k])
    try:
        side.write_text(json.dumps(load, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"write_fit_load_sidecar({fit_path.name}) failed: {e}")
    return load


def set_fit_rpe(stem: str, rpe: int | None, rpe_at: str | None) -> bool:
    """v3.6.0 — store (or clear) a session-RPE for an imported FIT.

    FIT-only riders have no ICU record to hold rider input, so it goes in the
    ``<stem>.load.json`` sidecar, which ``load_all_rides`` already attaches to
    the FIT entry. Returns False when the FIT does not exist.
    """
    fit_path = _fit_rides_dir() / f"{stem}.fit"
    if not fit_path.exists():
        return False
    load = read_fit_load_sidecar(fit_path) or write_fit_load_sidecar(fit_path) or {}
    if rpe is None:
        for k in ("rpe", "rpe_at", "rpe_scale"):
            load.pop(k, None)
    else:
        load["rpe"] = int(rpe)
        load["rpe_at"] = rpe_at
        load["rpe_scale"] = "foster_cr10"
    try:
        _fit_load_sidecar_path(fit_path).write_text(
            json.dumps(load, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"set_fit_rpe({stem}) failed: {e}")
        return False
    return True


def read_fit_load_sidecar(fit_path: Path) -> dict | None:
    """Read a previously persisted load sidecar; None when absent/corrupt
    (corrupt → the caller recomputes and rewrites)."""
    side = _fit_load_sidecar_path(fit_path)
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as e:
        log.debug(f"read_fit_load_sidecar({fit_path.name}) failed: {e}")
        return None


def get_icu_ride(external_id: str) -> dict | None:
    """v4.4.0 — load one normalized ICU record by external id.

    Accepts either the bare id ("12345678") or the prefixed ride_id
    ("icu_12345678"). Returns None for a clearly malformed id (path
    traversal attempt) or unknown id.
    """
    if not isinstance(external_id, str) or not external_id:
        return None
    if external_id.startswith("icu_"):
        external_id = external_id[4:]
    import re
    if not re.match(r"^[\w\-]+$", external_id) or len(external_id) > 40:
        log.warning(f"Rejected external_id in get_icu_ride: {external_id!r}")
        return None
    try:
        path = _icu_rides_dir() / f"{external_id}.json"
    except RuntimeError:
        return None  # AC6a: no active profile
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to load ICU ride {external_id}: {e}")
        return None


def _build_summary_dict(summary, dc: dict, session=None) -> dict:
    """Serialize a RideSummary + decoupling dict into the on-disk summary shape.

    Writes BOTH `calories` (legacy field, always in kJ — see training_live.py
    RideSummary docstring) AND `kj_mechanical` (canonical name for the same
    value) so old readers and new readers both work while the training_live
    rename lands. New RideSummary fields (total_kj, max_gradient, dfa_alpha1_avg,
    hr_cap_*) are persisted when present.
    """
    # Legacy kJ value — `summary.calories` was always kJ despite the name.
    # Prefer `kj_mechanical` if present (new training_live), fall back to
    # `calories` for older RideSummary shapes.
    kj_value = getattr(summary, "kj_mechanical", None)
    if kj_value is None:
        kj_value = getattr(summary, "calories", 0)

    out = {
        "duration_sec": summary.duration_sec,
        "distance_km": summary.distance_km,
        "elevation_gain_m": summary.elevation_gain_m,
        "avg_power": summary.avg_power,
        "max_power": summary.max_power,
        # "weighted_power" is canonical; "normalized_power" kept as alias for backward-compat with older saved rides
        "weighted_power": summary.weighted_power,
        "normalized_power": summary.weighted_power,
        "intensity_factor": summary.intensity_factor,
        "tss": summary.tss,
        "avg_hr": summary.avg_hr,
        "max_hr": summary.max_hr,
        "avg_cadence": summary.avg_cadence,
        "avg_speed": summary.avg_speed,
        # Persist both field names: `calories` for old readers, `kj_mechanical`
        # for new code. Same kJ number — the legacy field is misnamed.
        "calories": kj_value,
        "kj_mechanical": kj_value,
        "wbal_min_kj": summary.wbal_min_kj,
        "compliance_pct": getattr(summary, "compliance_pct", None),
        # v3.6.0-fix25: prefer the locked RideSummary value (§DEC-2) over
        # the live re-compute — after stop() they should match, but a
        # defensive fallback to `dc["pct"]` keeps this safe if a future
        # caller persists a summary without hitting .stop().
        "decoupling_pct": (
            getattr(summary, "decoupling_pct", None)
            if getattr(summary, "decoupling_pct", None) is not None
            else dc.get("pct")
        ),
        "decoupling_color": dc.get("color", "gray"),
        # §DEC-3 canonical EF = NP / avg_HR (ride aggregate).
        # v3.6.0-fix25e (QA-COMPUTE LOW-4): explicit None-check rather
        # than `or`; `ef=0.0` is a legitimate "no usable HR / zero NP"
        # result and MUST NOT be swapped for the decoupling-compute
        # side-effect EF.
        "efficiency_factor": (
            getattr(summary, "efficiency_factor", None)
            if getattr(summary, "efficiency_factor", None) is not None
            else dc.get("ef", 0)
        ),
    }

    # New RideSummary fields — write only when the RideSummary carries them
    # (older code paths may return a trimmed dataclass).
    for attr in (
        "total_kj",
        "max_gradient",
        "dfa_alpha1_avg",
        "hr_cap_ceiling_bpm",
        "hr_cap_time_capped_sec",
        "hr_cap_avg_adjustment_w",
        # v3.6.0-fix25 (§DFA-7): persist full α1 time-series for post-ride.
        "dfa_history",
        # v3.6.0-fix25 (§DEC-2/§DEC-3): locked decoupling + reason + EF.
        "decoupling_reason",
    ):
        val = getattr(summary, attr, None)
        if val is not None:
            out[attr] = val

    # v3.6.0-fix35e: post-FTP-test suggestion. Session stamps is_ftp_test +
    # compute_ftp_test_suggestion(); persist on the summary so the ride-detail
    # modal can render the Update Profile / Keep Current / Custom banner.
    if session is not None and getattr(session, "is_ftp_test", False):
        try:
            suggestion = session.compute_ftp_test_suggestion()
            if suggestion and suggestion.get("ftp"):
                out["ftp_test_suggestion"] = suggestion
                out["is_ftp_test"] = True
        except Exception as e:
            log.warning(f"ftp_test_suggestion compute failed: {e}")
    # v1.3.0 IMPL-PR-DETECTION (audit §4): mirror the persist_icu_activity()
    # hook on the FIT-live import path so the field shape is consistent
    # across both ingest paths. When the session carries a ride_id we
    # delegate to power_curve.compute_ride_prs; otherwise we stamp an
    # empty list so callers can rely on the field's presence and the
    # lazy GET /api/ride/{id}/prs endpoint can populate later.
    ride_id = getattr(session, "ride_id", None) if session is not None else None
    out["prs"] = []
    if isinstance(ride_id, str) and ride_id:
        try:
            import power_curve
            out["prs"] = power_curve.compute_ride_prs(ride_id)
        except Exception as e:
            log.debug(f"_build_summary_dict PR compute failed: {e}")
    return out


# v1.0.6 IMPL-3D-INGEST: per-ride 3D strain decomposition hook.
def compute_ride_xss(
    power_series: list,
    started_at: str | None = None,
    summary: dict | None = None,
    cp: int | None = None,
    wprime_j: int | None = None,
    pmax: int | None = None,
) -> dict:
    """Compute and persist 3D strain decomposition for one ride.

    Calls ``strain_score.compute_xss_components(power_series, cp, wprime_j,
    pmax)`` to get per-ride totals (Kontro 2026 PLOS ONE — see
    MASTER_DECISIONS_v106 §1) and:

      * mutates ``summary`` in place to add ``xss_total``, ``xss_cp``,
        ``xss_w_prime``, ``xss_pmax`` keys (so the on-disk summary the
        ride-detail UI reads carries them).
      * writes per-day aggregates ``ss_cp_daily`` / ``ss_w_prime_daily``
        / ``ss_pmax_daily`` into ``athlete_metrics`` via
        ``db.log_metric`` (source="computed").

    A ride without a usable power trace (empty / None / all-zeros) returns
    an empty dict and writes nothing — the caller can pass through without
    branching.

    Returns the components dict (or {} when no power data).

    The CP / W' / Pmax inputs default to the active profile's values when
    not supplied. strain_score is imported lazily so this module loads even
    before IMPL-3D-MODEL lands.
    """
    if not power_series or not any(int(p or 0) > 0 for p in power_series):
        if summary is not None:
            # Mark the ride explicitly so the UI can render "no power trace"
            # rather than mistakenly treating xss as zero.
            summary.setdefault("xss_total", None)
            summary.setdefault("xss_cp", None)
            summary.setdefault("xss_w_prime", None)
            summary.setdefault("xss_pmax", None)
        return {}

    # Default CP / W' / Pmax from the active profile.
    if cp is None or wprime_j is None or pmax is None:
        try:
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
            cp = cp if cp is not None else pm.cp
            wprime_j = wprime_j if wprime_j is not None else pm.wprime_j
            pmax = pmax if pmax is not None else pm.pmax_w
        except Exception as e:
            log.warning("compute_ride_xss: profile lookup failed: %s", e)
            return {}

    try:
        import strain_score
        components = strain_score.compute_xss_components(
            power_series, cp=cp, wprime_j=wprime_j, pmax=pmax,
        )
    except ImportError:
        # IMPL-3D-MODEL hasn't landed in this run — graceful no-op.
        log.info("compute_ride_xss: strain_score module not yet available")
        return {}
    except Exception as e:
        log.warning("compute_ride_xss: strain_score raised: %s", e)
        return {}

    if not isinstance(components, dict):
        return {}

    # Cache on the summary dict so the on-disk ride.json and the
    # /api/ride/<id>/detail endpoint serializer can both read it.
    if summary is not None:
        summary["xss_total"] = components.get("xss_total")
        summary["xss_cp"] = components.get("xss_cp")
        summary["xss_w_prime"] = components.get("xss_w_prime")
        summary["xss_pmax"] = components.get("xss_pmax")

    # Write per-day aggregates into athlete_metrics (one row per metric per
    # ride day; INSERT OR REPLACE so re-imports don't double-count). The
    # date prefix from started_at is what the dashboard time-series reads.
    if started_at:
        try:
            import db
            day = str(started_at)[:10]
            for metric_key, value in (
                ("ss_cp_daily", components.get("xss_cp")),
                ("ss_w_prime_daily", components.get("xss_w_prime")),
                ("ss_pmax_daily", components.get("xss_pmax")),
            ):
                if value is None:
                    continue
                try:
                    db.log_metric(day, metric_key, float(value), source="computed")
                except Exception as inner:
                    log.warning(
                        "compute_ride_xss: log_metric(%s) failed: %s",
                        metric_key, inner,
                    )
        except Exception as e:
            log.warning("compute_ride_xss: db write failed: %s", e)

    return components


# v4.0.0-alpha (FIX-SERVER): save_ride() removed. The live TrainingSession
# runtime it depended on (session._recorder, session._metrics, session.mode,
# session._workout) is gone; rides now arrive via ``POST /api/ride/import``
# which writes a FIT directly under ``~/.domestique/profiles/<id>/rides/``.
# No caller remains in app.py or any live module. The removed-but-not-deleted
# tests in test_route_picker_api.py:1028+ are handled separately by FIX-B.


# v3.6.0-fix33d-log-hygiene: dedupe warnings for unparsable ride JSON —
# `list_rides()` is invoked on every dashboard refresh and on several
# app endpoints, so a single malformed file on disk produces N× duplicate
# WARNings per process lifetime. Scout M3 saw 12 identical lines on
# startup for 2 bad files. Warn once per (path, reason) and drop silently
# on subsequent calls in the same process.
_RIDE_LOAD_WARNED: set[tuple[str, str]] = set()


def list_rides() -> list[dict]:
    """List all saved rides (summary only, no samples).

    Sort order: lexical descending on filename. This relies on the
    `ride_<ISO-8601>` naming scheme in save_ride() — ISO-8601 timestamps
    are lexicographically sortable, so filename order == chronological order.
    If the naming scheme ever changes (e.g. dropping the ISO prefix), switch
    to `sorted(..., key=lambda p: p.stat().st_mtime, reverse=True)`.

    v3.6.0-fix33d-log-hygiene: entries missing the canonical schema keys
    (`id`, `started_at`, `finished_at`) are skipped without taking the
    whole listing down, and the WARN is deduped per (path, reason) across
    repeated calls within the process so a stale `ride_*.strava.json`
    orphan does not spam the log on every dashboard refresh.
    """
    rides = []
    try:
        rides_dir = _rides_dir()
    except RuntimeError:
        return rides  # AC6a: no active profile
    for f in sorted(rides_dir.glob("ride_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # v1.6.3: legacy Strava exports (and FIT-only intermediate
            # records) sometimes lack 'id' / 'started_at' / 'finished_at'.
            # Fall back to the filename stem for 'id', and to None for the
            # timestamps, so one malformed ride doesn't break the load and
            # doesn't spam a WARN per dashboard refresh.
            ride_id = data.get("id") or f.stem
            started_at = data.get("started_at")
            finished_at = data.get("finished_at")
            if not started_at or not finished_at:
                # No usable timestamps -> the ride can't be placed on the
                # calendar. Skip silently (the file is on disk for triage
                # but adds nothing to the dashboard).
                dedupe_key = (str(f), "missing-timestamps")
                if dedupe_key not in _RIDE_LOAD_WARNED:
                    log.info(f"Skipping ride without timestamps: {f}")
                    _RIDE_LOAD_WARNED.add(dedupe_key)
                continue
            rides.append({
                "id": ride_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "ride_type": data.get("ride_type", "free"),
                "metadata": data.get("metadata", {}),
                "summary": data.get("summary", {}),
                "zones": data.get("zones", {}),
            })
        except (json.JSONDecodeError, OSError) as e:
            # Dedupe per (filename, error-repr). KeyError is now handled
            # via .get() above so it can no longer reach this except clause.
            dedupe_key = (str(f), repr(e))
            if dedupe_key not in _RIDE_LOAD_WARNED:
                log.warning(f"Failed to load ride {f}: {e}")
                _RIDE_LOAD_WARNED.add(dedupe_key)
    return rides


def _safe_ride_path(ride_id: str) -> Optional[Path]:
    """Resolve ride path with path traversal protection.

    The regex `^[\\w\\-]+$` allows only [A-Za-z0-9_-] (no dots, slashes, or
    NUL bytes), which rejects `..`, `/`, `\\`, and absolute paths. The
    resolve()+relative-to check is a belt-and-braces second layer. An explicit
    length cap prevents pathologically long ids from creating surprise
    filenames (most filesystems cap at 255; our ISO-timestamp ids are ~24).
    Raises ValueError for clearly malformed ids (length/regex fail) so callers
    can distinguish "bad input" from "not found"; returns None only when the
    resolved path escapes the rides dir (shouldn't be reachable after regex).
    """
    import re
    if not isinstance(ride_id, str) or not ride_id:
        raise ValueError(f"ride_id must be a non-empty string, got {ride_id!r}")
    if len(ride_id) > 80:
        raise ValueError(f"ride_id too long ({len(ride_id)} > 80)")
    if not re.match(r'^[\w\-]+$', ride_id):
        raise ValueError(f"Invalid ride_id: {ride_id!r}")
    rides_dir = _rides_dir()
    path = (rides_dir / f"{ride_id}.json").resolve()
    if not str(path).startswith(str(rides_dir.resolve())):
        log.warning(f"Path traversal attempt blocked: {ride_id!r}")
        return None
    return path


def get_ride(ride_id: str) -> Optional[dict]:
    """Load a complete ride (including samples).

    Returns None for any invalid/unknown id (malformed, too long, traversal
    attempt, not found) so API callers don't need a try/except around every
    lookup. Use _safe_ride_path directly if you need to distinguish "bad
    input" from "not found".
    """
    try:
        path = _safe_ride_path(ride_id)
    except ValueError as e:
        log.warning(f"Rejected ride_id in get_ride: {e}")
        return None
    except RuntimeError:
        return None  # AC6a: no active profile
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to load ride {ride_id}: {e}")
        return None


def compute_local_ctl(
    days: int = 90,
    tau: int = 42,
) -> float | None:
    """F4 (v4.1.0) — compute CTL locally from the saved-ride archive.

    EWMA over FIT-imported ride TSS values (``ride_<iso>.json`` summaries)
    with the Coggan τ=42-day time constant. Falls back gracefully when the
    archive is empty or no ride has a TSS field:

        ctl_new = ctl_prev + (tss_today - ctl_prev) / τ

    Used as a local replacement for the hardcoded ``current_ctl = 37.0``
    fallback in training_planner.generate_plan when Intervals.icu is
    offline or returns no wellness data.

    Returns None if no usable TSS was found so callers can keep the
    original 37.0 constant as a last-resort safety net.
    """
    import datetime as _dt
    rides = list_rides()
    if not rides:
        return None
    cutoff_iso = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    per_day: dict[str, float] = {}
    for r in rides:
        started = (r.get("started_at") or "")[:10]
        if not started or started < cutoff_iso:
            continue
        summary = r.get("summary") or {}
        tss = summary.get("tss") or 0
        if not tss:
            continue
        try:
            per_day[started] = per_day.get(started, 0.0) + float(tss)
        except (TypeError, ValueError):
            continue
    if not per_day:
        return None
    today = _dt.date.today()
    ctl = 0.0
    d = _dt.date.fromisoformat(min(per_day.keys()))
    while d <= today:
        tss_today = per_day.get(d.isoformat(), 0.0)
        ctl = ctl + (tss_today - ctl) / float(tau)
        d += _dt.timedelta(days=1)
    return round(ctl, 1)


def recent_mean_weekly_tss(
    weeks: int = 6,
    extra_rides: list[dict] | None = None,
) -> float | None:
    """E1 (v2.1.0) — rider's RECENT mean weekly TSS over the last ``weeks``.

    Sums TSS per ISO-calendar-week from the full local ride archive (plus any
    ``extra_rides`` the caller passes, e.g. ICU activities in the app's shape)
    over the trailing ``weeks`` window, then averages across the weeks that
    actually contain a ride. The mean-over-active-weeks (not over all N weeks)
    avoids a few rest weeks from deflating the ceiling for a rider who trains
    in blocks.

    This is the load anchor for the generation-time weekly volume ceiling
    (training_planner.generate_phases): the plan should ramp from what the
    rider has recently been doing, not from the sum of daily availability.

    Best-effort: returns None when there is no usable TSS in the window, so the
    caller falls back to the legacy availability-driven cap (hours_per_week×65).
    """
    import datetime as _dt
    rides = list_rides()
    if extra_rides:
        rides = rides + list(extra_rides)
    if not rides:
        return None
    today = _dt.date.today()
    cutoff = today - _dt.timedelta(weeks=max(1, weeks))
    cutoff_iso = cutoff.isoformat()
    per_week: dict[tuple[int, int], float] = {}
    for r in rides:
        started = (r.get("started_at") or "")[:10]
        if not started or started < cutoff_iso:
            continue
        summary = r.get("summary") or {}
        # Accept both list_rides() shape (summary.tss) and load_all_rides()
        # shape (top-level tss) — mirrors compute_local_atl's dual read.
        tss = r.get("tss")
        if tss is None:
            tss = summary.get("tss") or 0
        if not tss:
            continue
        try:
            d = _dt.date.fromisoformat(started)
            iso_year, iso_week, _ = d.isocalendar()
            per_week[(iso_year, iso_week)] = (
                per_week.get((iso_year, iso_week), 0.0) + float(tss))
        except (TypeError, ValueError):
            continue
    if not per_week:
        return None
    return round(sum(per_week.values()) / len(per_week), 1)


def persist_wellness(record: dict) -> Path | None:
    """v4.5.0 — write a single ICU wellness record to ``~/.domestique/wellness/``.

    The record's ``id`` field is the ISO date (YYYY-MM-DD) — used as the
    filename. Idempotent: overwrites any existing file with the same date.
    Returns the written path, or None on a malformed record (no id) or write
    failure.
    """
    if not isinstance(record, dict):
        return None
    rid = record.get("id")
    if not rid or not isinstance(rid, str):
        return None
    # Defensive: ensure id looks like an ISO date — reject path-traversal-y ids.
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", rid):
        return None
    path = _wellness_dir() / f"{rid}.json"
    try:
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning(f"persist_wellness({rid}) failed: {e}")
        return None
    return path


def load_recent_wellness(days: int = 90) -> list[dict]:
    """v4.5.0 — load up to N most recent wellness records (newest first).

    Walks ``~/.domestique/wellness/``, parses each ``YYYY-MM-DD.json`` file,
    sorts by date descending, returns the first ``days`` records. Bad files
    are skipped silently. Returns [] when the dir is empty or unreadable.
    """
    out: list[dict] = []
    try:
        d = _wellness_dir()
    except RuntimeError:
        return out  # AC6a: no active profile
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id"):
                out.append(data)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to load wellness {f}: {e}")
        if len(out) >= days:
            break
    return out


def compute_local_atl(
    rides: list[dict],
    today=None,
    days: int = 7,
) -> float | None:
    """v4.5.0 — 7-day EWMA over local ride TSS (mirror of compute_local_ctl).

    Takes an explicit ``rides`` list and an optional ``today`` date so callers
    can compose with their own ride source (the in-memory list_rides() output
    or app's _load_all_rides_safe()). EWMA τ = ``days`` (default 7).

    Returns None if no usable TSS values exist in the rides list.
    """
    import datetime as _dt
    if not rides:
        return None
    if today is None:
        today = _dt.date.today()
    cutoff = today - _dt.timedelta(days=90)
    cutoff_iso = cutoff.isoformat()
    per_day: dict[str, float] = {}
    for r in rides:
        # Accept both list_rides() shape (started_at + summary.tss) and
        # load_all_rides() shape (started_at + tss).
        started = (r.get("started_at") or "")[:10]
        if not started or started < cutoff_iso:
            continue
        summary = r.get("summary") or {}
        tss = r.get("tss")
        if tss is None:
            tss = summary.get("tss") or 0
        if not tss:
            continue
        try:
            per_day[started] = per_day.get(started, 0.0) + float(tss)
        except (TypeError, ValueError):
            continue
    if not per_day:
        return None
    atl = 0.0
    d = _dt.date.fromisoformat(min(per_day.keys()))
    while d <= today:
        tss_today = per_day.get(d.isoformat(), 0.0)
        atl = atl + (tss_today - atl) / float(days)
        d += _dt.timedelta(days=1)
    return round(atl, 1)


def delete_ride(ride_id: str) -> bool:
    """Delete a saved ride. Returns False for any invalid/unknown id."""
    try:
        path = _safe_ride_path(ride_id)
    except ValueError as e:
        log.warning(f"Rejected ride_id in delete_ride: {e}")
        return False
    except RuntimeError:
        return False  # AC6a: no active profile
    if path and path.exists():
        path.unlink()
        log.info(f"Ride deleted: {ride_id}")
        return True
    return False


def detect_wbal_overshoot(ride: dict, wprime_j: int | None = None) -> bool:
    """v1.1.0 IMPL-NORWEGIAN-HR — return True when W'bal trough during
    a sub-threshold session dropped below 60% of W'.

    "Overshoot" means the rider went HARDER than the prescribed power
    suggested — useful as a post-ride flag distinct from TSS overshoot
    (Skiba 2015 W'bal recovers fast; the trough captures peak depletion
    even when total work is moderate).

    Inputs:
        ride: dict containing ``wbal_min_kj`` (W'bal trough in kJ; written
              by the v1.0.6 strain_score path during _summarise_ride).
        wprime_j: optional explicit W' in joules. Falls back to the
              athlete metric if omitted; default 20000 J (20 kJ) when
              neither is available.

    Returns:
        True iff the trough exists AND is strictly below 60 % of W'.
        False when the signal is missing (key absent or value <= 0) or
        the trough is at-or-above the 60 % threshold.

    Threshold rationale:
        Master §1 locks "< 60 % of W'". Strict less-than: a trough at
        exactly 60 % is not flagged. Below that the rider was burning
        glycolytic capacity faster than a sub-LT2 prescription should.
    """
    if not isinstance(ride, dict):
        return False
    trough_kj = ride.get("wbal_min_kj")
    if not isinstance(trough_kj, (int, float)):
        return False
    if trough_kj <= 0.0:
        # 0 = unpopulated default, NOT a signal.
        return False
    if wprime_j is None:
        wprime_j = 20000  # Coggan/Skiba default if profile not supplied.
    if not isinstance(wprime_j, (int, float)) or wprime_j <= 0:
        return False
    threshold_kj = (wprime_j / 1000.0) * 0.60
    return trough_kj < threshold_kj


def summarise_fit_device(fit_path: Path) -> dict:
    """v1.0.7 IMPL-HRV-PROMPT — extract recording-device fields for the ride summary.

    Wraps ``fit_activity.parse_device_info`` and shapes the result for
    inclusion in the on-disk ride summary. Used by ``_build_fit_normalized``
    so the home-page HRV-recording-prompt toast can name the rider's
    specific Garmin device ("Edge 530" / "Fēnix 8" / etc.).

    Returns a dict with keys ``device_manufacturer``, ``device_product_name``,
    ``device_product_id`` — all None / "unknown" for non-Garmin or
    unparseable FITs (the toast handles those gracefully by not naming a
    specific device).
    """
    try:
        from fit_activity import parse_device_info
        info = parse_device_info(fit_path)
    except Exception as e:
        log.debug(f"summarise_fit_device({fit_path}) parse failed: {e}")
        info = None

    if not isinstance(info, dict):
        return {
            "device_manufacturer": None,
            "device_product_name": None,
            "device_product_id": None,
        }

    return {
        "device_manufacturer": info.get("manufacturer"),
        "device_product_name": info.get("garmin_product_name"),
        "device_product_id": info.get("garmin_product_id"),
    }
