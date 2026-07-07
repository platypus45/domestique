"""Push planned workouts to the intervals.icu calendar (v3.0.1, IP_ICU_PUSH).

ONE reconcile engine, two triggers (the plan-tab button and the debounced
``atomic_write_plan`` post-write hook — app.py wires both). Garmin and
MyWhoosh pull structured workouts from the ICU calendar, so mirroring the
plan there removes the manual ZWO-upload friction.

Contract highlights (GRILL OUTCOME — LOCKED, /tmp/IP_ICU_PUSH.md):
  - external_id = "domestique:<profile_id>:<day_iso>:<n>" where n is the
    session's index within that day's sessions in stored plan order
    (double-threshold pairs → :0 and :1; singletons → :0).
  - Format by mode: power → the matched ZWO bytes VERBATIM (ICU forwards ZWO
    to Garmin natively, forum #1521); hr target_mode → the HR-target FIT from
    app.build_fit_workout_bytes(view='hr'), guarded: a broken lthr invariant
    skips with reason "needs_lthr" (never a silently-degraded power FIT), and
    a FIT whose LAST step is OPEN is skipped (G-E — ICU drops trailing OPEN
    steps, forum #17647).
  - ONE bulk POST /athlete/{id}/events/bulk?upsert=true via this module's OWN
    urllib helper (pattern: training.upload_fit_to_icu). 403 with scope text
    in the body → {"needs_reconnect": True}, never an exception (G4). Do NOT
    route through training._get (it maps 403 to ICUServerError).
  - Orphan sweep: only events whose external_id carries OUR profile prefix
    are ever deleted (G5); foreign "domestique:" prefixes are warned about,
    never touched (G-B). Sweep is skipped when the upsert failed. The window
    starts TODAY — past-day events are never touched (G-H).
  - Toast counts derive from the local diff vs the pre-upsert GET, not from
    ICU response fields (G-G).
  - apikey auth = always write-capable (G-F); OAuth needs the CALENDAR:WRITE
    stamp (pm.icu_granted_scopes) — unstamped legacy connections report
    needs_reconnect without a network call.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import config
import training as _training

_log = logging.getLogger("domestique.icu_push")

HORIZON_DAYS = 14
_ID_ROOT = "domestique:"
# calendar-push (2026-07-07): a DISTINCT root for user-initiated LIBRARY pushes
# (the workout-detail "Send to intervals.icu calendar" button). Load-bearing on
# the ":" boundary: "domestique-manual:".startswith("domestique:") is FALSE
# (index 10 is '-' not ':'), so _ours_in_window skips these BEFORE the foreign
# counter — a manual entry is never in `ours`, never swept, never counted. This
# is what makes library pushes PERMANENT (they outlive the plan-mirror sweep).
_MANUAL_ID_ROOT = "domestique-manual:"

# Raw-int values fit_tool yields when DECODING (profile_type enums encode to
# these): WorkoutStepTarget.OPEN == 2, WorkoutStepDuration.OPEN == 5.
_FIT_TARGET_OPEN = 2
_FIT_DURATION_OPEN = 5


def _enum_val(v):
    return getattr(v, "value", v)


# ── auth / capability ────────────────────────────────────────────────────────

def write_ok(pm=None) -> bool:
    """Can the current connection write the ICU calendar?

    apikey → always yes (G-F: API keys are full-access). OAuth → only when
    the CALENDAR:WRITE stamp is present (legacy connections predate the
    stamp ⇒ reconnect required). No connection → no.
    """
    token = getattr(config, "ICU_ACCESS_TOKEN", "") or ""
    key = getattr(config, "ICU_API_KEY", "") or ""
    if token:
        if pm is None:
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
        scopes = [s for s in re.split(r"[,\s]+", (pm.icu_granted_scopes or "").upper()) if s]
        return "CALENDAR:WRITE" in scopes
    return bool(key)


# ── transport (the ONE seam tests mock) ──────────────────────────────────────

def _http(method: str, path: str, payload=None, timeout: float = 30.0):
    """Engine-owned ICU transport. Returns ``(status:int, body:bytes)``.

    Network-level failures return status 0 (callers map that to a clean
    "couldn't reach intervals.icu" result — no exceptions escape). HTTP
    errors return their real status + body so the 403-scope check can read
    the message. Bearer-or-Basic auth via training._auth_header (same
    header the rest of the app sends); the retry-happy training._get is
    deliberately NOT reused — its 403 semantics don't fit here.
    """
    url = f"{config.ICU_BASE}/{path}"
    headers = {"User-Agent": _training.ICU_USER_AGENT}
    try:
        headers.update(_training._auth_header())
    except _training.ICUCredentialsMissing:
        return 0, b"no_credentials"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, str(e).encode("utf-8", "replace")


def _scope_403(status: int, body: bytes) -> bool:
    """403 whose body names a scope problem → the token can't write the
    calendar → surface the reconnect prompt instead of a generic error."""
    if status != 403:
        return False
    text = (body or b"").decode("utf-8", "replace").lower()
    return "scope" in text or "calendar:write" in text


# ── plan → desired events ────────────────────────────────────────────────────

def _load_plan() -> dict | None:
    import training_planner as tp
    p = Path(getattr(tp, "PLAN_DIR", Path.home() / ".domestique" / "plans")) / "current_plan.json"
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return plan if isinstance(plan, dict) and plan.get("weeks") else None


def _display_name(s: dict, classifications: dict) -> str:
    """Session display title — same cascade the dashboard uses:
    content-classification display_name → zwo_name → description → type."""
    entry = classifications.get(s.get("zwo_file") or "") or {}
    return (str(entry.get("display_name") or "")
            or str(s.get("zwo_name") or "")
            or str(s.get("description") or "")
            or str(s.get("session_type") or "Workout"))


def _load_classifications(workout_dir: Path) -> dict:
    try:
        d = json.loads((workout_dir / ".content_classification.json")
                       .read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _fit_last_step_is_open(fit_bytes: bytes) -> bool:
    """G-E: True when the FIT's LAST workout step is OPEN (by duration OR
    target) — ICU drops a trailing OPEN step on import, so such a file must
    be skipped rather than pushed truncated."""
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    steps = [r.message for r in FitFile.from_bytes(fit_bytes).records
             if isinstance(r.message, WorkoutStepMessage)]
    if not steps:
        return True
    last = steps[-1]
    return (_enum_val(last.duration_type) == _FIT_DURATION_OPEN
            or _enum_val(last.target_type) == _FIT_TARGET_OPEN)


def _build_event(s: dict, ext_id: str, pm, classifications: dict,
                 workout_dir: Path, hr_mode: bool, lthr_ok: bool):
    """calendar-push (2026-07-07): build ONE ICU bulk-event dict for a session.

    Extracted VERBATIM from the _desired_events per-session body so the plan
    reconcile AND the single-workout push endpoint share ONE builder — no
    divergence in name / attachment / filename. Returns ``(event, None)`` on
    success or ``(None, reason)`` where reason is one of the skip strings the
    reconcile toast already knows (unmatched / file_missing / needs_lthr /
    build_failed / trailing_open). The 6 keys are emitted in a FIXED order
    (start_date_local, category, name, external_id, filename,
    file_contents_base64) — byte-identical to the pre-extraction output, which
    is the regression bar (test_icu_push stays green).

    The structured workout rides entirely on filename + file_contents_base64
    (there is NO description/workout_doc field): a power profile ships the ZWO
    bytes verbatim (ICU forwards ZWO to Garmin natively); an hr profile ships
    the transcoded HR-target FIT. ``ext_id`` is passed IN (the caller owns the
    day-relative <n> for planner events / the manual uuid for library pushes).
    """
    import app as _app

    # by_day is keyed on s["day"], so s.get("day") IS this session's day_iso —
    # deriving it here (vs threading day_iso through) keeps the byte-identical
    # start_date_local without an extra parameter.
    day_iso = s.get("day")
    zwo_file = (s.get("zwo_file") or "").strip()
    if not zwo_file:                        # unmatched slot (no ZWO to attach)
        return None, "unmatched"
    zwo_path = _app._safe_path(workout_dir, zwo_file)
    if not zwo_path or not zwo_path.exists():
        return None, "file_missing"
    name = _display_name(s, classifications)
    if hr_mode:
        if not lthr_ok:
            # Guard BEFORE building: view='hr' silently degrades to a power FIT
            # when the invariant is broken — surface needs_lthr, never push it.
            return None, "needs_lthr"
        try:
            fit_bytes = _app.build_fit_workout_bytes(
                s.get("session_type") or "z2",
                int(s.get("duration_min") or 60),
                name, zwo_file, view="hr")
        except Exception as e:
            _log.warning("icu push: FIT build failed for %s: %s", zwo_file, e)
            return None, "build_failed"
        if _fit_last_step_is_open(fit_bytes):
            return None, "trailing_open"
        filename = Path(zwo_file).stem + ".fit"
        contents = fit_bytes
    else:
        try:
            contents = zwo_path.read_bytes()
            # task #24: when the profile's measured-capacity cap is ON
            # (pmax_is_set + power mode + toggle "on"), push the CAPPED file --
            # the workout the rider should actually do. With the cap OFF (or a
            # no-op) the bytes remain byte-identical to disk. hr branch above is
            # untouched (target_mode wins).
            if _app._capacity_cap_active(pm):
                contents = _app._cap_zwo_bytes(
                    contents, Path(zwo_file).name, pm)
        except OSError:
            return None, "file_missing"
        filename = Path(zwo_file).name
    return {
        "start_date_local": f"{day_iso}T00:00:00",
        "category": "WORKOUT",
        "name": name,
        "external_id": ext_id,
        "filename": filename,
        "file_contents_base64": base64.b64encode(contents).decode("ascii"),
    }, None


def _desired_events(pm, plan: dict, today: date, horizon_days: int,
                    profile_id: str):
    """Collect the horizon's pushable sessions as ICU bulk-event payloads.

    Returns (events, skipped, broken_ids):
      events     — list of ICU event dicts ready for the bulk upsert
      skipped    — [{"day", "reason"}] for the toast (unmatched / needs_lthr /
                   trailing_open / file_missing / build_failed)
      broken_ids — external_ids of sessions we WANTED to push but could not
                   build; the sweep spares these so a config hiccup (e.g. a
                   broken lthr) never deletes previously-pushed events.
    """
    import app as _app

    horizon_end = today + timedelta(days=horizon_days)
    # Raw athlete value, NOT pm.target_mode: the property silently degrades
    # hr→power when the lthr invariant breaks, which is exactly the case that
    # must become a needs_lthr skip instead of a power-FIT push.
    raw_mode = (pm._athlete.get("target_mode") or "power")
    hr_mode = raw_mode == "hr"
    lthr_ok = bool(pm.lthr_is_set and pm.max_hr > pm.lthr)

    workout_dir = Path(_app.WORKOUT_DIR)
    classifications = _load_classifications(workout_dir)

    # Day → that day's sessions in stored plan order (weeks are ordered and a
    # day lives in exactly one week, so plain traversal preserves order).
    by_day: dict[str, list[dict]] = {}
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            d = s.get("day")
            if isinstance(d, str) and d:
                by_day.setdefault(d, []).append(s)

    events, skipped, broken_ids = [], [], set()
    for day_iso in sorted(by_day):
        try:
            day = date.fromisoformat(day_iso)
        except ValueError:
            continue
        if day < today or day > horizon_end:   # G-H: window starts TODAY
            continue
        for n, s in enumerate(by_day[day_iso]):
            if (s.get("session_type") or "") == "rest":
                continue
            if s.get("is_race"):                # race days are never pushed
                continue
            status = str(s.get("status") or "")
            if status == "dismissed" or status.startswith("moved_from"):
                continue                        # not desired → sweep removes
            # calendar-push (2026-07-07): the per-session event body is now
            # _build_event (shared with the single-workout push endpoint). ext_id
            # is computed HERE — it needs the day-relative <n> (index among this
            # day's sessions in plan order, rest/race/dismissed INCLUDED, per the
            # contract) — and handed in, so a build failure still tags the SAME id
            # into broken_ids and the sweep spares that slot's prior event.
            ext_id = f"{_ID_ROOT}{profile_id}:{day_iso}:{n}"
            event, reason = _build_event(
                s, ext_id, pm, classifications, workout_dir, hr_mode, lthr_ok)
            if event is None:
                skipped.append({"day": day_iso, "reason": reason})
                broken_ids.add(ext_id)
                continue
            events.append(event)
    return events, skipped, broken_ids


# ── reconcile ────────────────────────────────────────────────────────────────

def _result(**overrides) -> dict:
    out = {"pushed": 0, "updated": 0, "deleted": 0, "skipped": []}
    out.update(overrides)
    return out


def _connection(pm):
    """(athlete_id, error_key) — error_key None when creds are usable."""
    token = getattr(config, "ICU_ACCESS_TOKEN", "") or ""
    key = getattr(config, "ICU_API_KEY", "") or ""
    aid = getattr(config, "ICU_ATHLETE_ID", "") or ""
    if not (token or key) or not aid:
        return "", "not_connected"
    return aid, None


def _get_window_events(athlete_id: str, today: date, horizon_days: int):
    """GET our reconcile window. Returns (events|None, error_result|None)."""
    newest = today + timedelta(days=horizon_days)
    status, body = _http(
        "GET", f"athlete/{athlete_id}/events?oldest={today.isoformat()}"
               f"&newest={newest.isoformat()}")
    if _scope_403(status, body):
        return None, _result(needs_reconnect=True)
    if status == 0:
        return None, _result(error="network")
    if not (200 <= status < 300):
        return None, _result(error=f"http_{status}")
    try:
        events = json.loads(body or b"[]")
    except (json.JSONDecodeError, ValueError):
        return None, _result(error="bad_response")
    if not isinstance(events, list):
        events = []
    return events, None


def _ours_in_window(events: list, profile_id: str, today: date) -> dict:
    """Our profile's events keyed by external_id — and the G-B warning for
    foreign domestique prefixes (multi-install: warn, NEVER delete)."""
    prefix = f"{_ID_ROOT}{profile_id}:"
    ours: dict[str, dict] = {}
    foreign = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("external_id") or "")
        if not eid.startswith(_ID_ROOT):
            continue
        if not eid.startswith(prefix):
            foreign += 1
            continue
        # G-H belt-and-braces: even if the server returned an older event,
        # never consider a past-day event for deletion. Unparseable date =
        # fail CLOSED (not deletable) — this feeds the delete set.
        try:
            ev_day = date.fromisoformat(
                str(ev.get("start_date_local") or "")[:10])
        except ValueError:
            continue
        if ev_day < today:
            continue
        ours[eid] = ev
    if foreign:
        _log.warning(
            "EVENT=icu_push_foreign_prefix count=%d — another Domestique "
            "install pushes to this athlete's calendar (one device per "
            "athlete is supported); leaving those events alone.", foreign)
    return ours


def reconcile(horizon_days: int = HORIZON_DAYS) -> dict:
    """Mirror [today, today+horizon] of the stored plan onto the ICU calendar.

    Returns {pushed, updated, deleted, skipped:[{day,reason}]} plus at most
    one of needs_reconnect / needs_lthr / error. Never raises.
    """
    from profile_manager import ProfileManager
    try:
        pm = ProfileManager.get()
        profile_id = pm.active_id
        if not profile_id:
            return _result(error="no_active_profile")
        athlete_id, err = _connection(pm)
        if err:
            return _result(error=err)
        if not write_ok(pm):
            return _result(needs_reconnect=True)
        plan = _load_plan()
        if plan is None:
            return _result(error="no_plan")
        today = date.today()
        desired, skipped, broken_ids = _desired_events(
            pm, plan, today, horizon_days, profile_id)
        result = _result(skipped=skipped)
        if any(s.get("reason") == "needs_lthr" for s in skipped):
            result["needs_lthr"] = True

        # GET BEFORE the upsert: one call feeds both the G-G count diff
        # (pre-push state) and the orphan sweep candidate list.
        existing, err_res = _get_window_events(athlete_id, today, horizon_days)
        if err_res is not None:
            err_res["skipped"] = skipped
            return err_res
        ours = _ours_in_window(existing, profile_id, today)

        # ONE bulk upsert (idempotent: keyed by external_id, G1).
        if desired:
            status, body = _http(
                "POST", f"athlete/{athlete_id}/events/bulk?upsert=true",
                payload=desired)
            if _scope_403(status, body):
                result["needs_reconnect"] = True
                return result
            if status == 0:
                result["error"] = "network"
                return result
            if not (200 <= status < 300):
                result["error"] = f"http_{status}"
                return result

        # G-G: counts from the local diff vs the pre-push GET.
        desired_ids = set()
        for ev in desired:
            eid = ev["external_id"]
            desired_ids.add(eid)
            prev = ours.get(eid)
            if prev is None:
                result["pushed"] += 1
            elif (str(prev.get("name") or "") != ev["name"]
                  or str(prev.get("start_date_local") or "") != ev["start_date_local"]
                  or (prev.get("filename") and str(prev["filename"]) != ev["filename"])):
                result["updated"] += 1

        # Orphan sweep — ours minus (desired ∪ broken). Skipped entirely when
        # the upsert failed (returns above); broken sessions keep their prior
        # event rather than losing it to a build hiccup.
        protected = desired_ids | broken_ids
        orphans = [ev for eid, ev in ours.items() if eid not in protected]
        if orphans:
            status, body = _http(
                "PUT", f"athlete/{athlete_id}/events/bulk-delete",
                payload=[{"id": ev.get("id"),
                          "external_id": str(ev.get("external_id") or "")}
                         for ev in orphans])
            if 200 <= status < 300:
                result["deleted"] = len(orphans)
            elif _scope_403(status, body):
                result["needs_reconnect"] = True
            else:
                result["error"] = "sweep_failed"
        _log.info(
            "EVENT=icu_calendar_push pushed=%d updated=%d deleted=%d "
            "skipped=%d horizon=%dd",
            result["pushed"], result["updated"], result["deleted"],
            len(skipped), horizon_days)
        return result
    except Exception as e:                      # G4: zero 500s, ever
        _log.exception("icu calendar reconcile failed")
        return _result(error=f"internal:{type(e).__name__}")


def sweep_all(horizon_days: int = HORIZON_DAYS) -> dict:
    """G-A: delete ALL of our pushed events in [today, today+horizon].

    Used when the sync toggle turns OFF and (best-effort, BEFORE the token
    purge) on disconnect. Only our profile-prefixed events are touched.
    Never raises."""
    from profile_manager import ProfileManager
    try:
        pm = ProfileManager.get()
        profile_id = pm.active_id
        if not profile_id:
            return _result(error="no_active_profile")
        athlete_id, err = _connection(pm)
        if err:
            return _result(error=err)
        today = date.today()
        existing, err_res = _get_window_events(athlete_id, today, horizon_days)
        if err_res is not None:
            return err_res
        ours = _ours_in_window(existing, profile_id, today)
        result = _result()
        if ours:
            status, body = _http(
                "PUT", f"athlete/{athlete_id}/events/bulk-delete",
                payload=[{"id": ev.get("id"),
                          "external_id": str(ev.get("external_id") or "")}
                         for ev in ours.values()])
            if 200 <= status < 300:
                result["deleted"] = len(ours)
            elif _scope_403(status, body):
                result["needs_reconnect"] = True
            else:
                result["error"] = "sweep_failed"
        _log.info("EVENT=icu_calendar_sweep deleted=%d horizon=%dd",
                  result["deleted"], horizon_days)
        return result
    except Exception as e:
        _log.exception("icu calendar sweep failed")
        return _result(error=f"internal:{type(e).__name__}")
