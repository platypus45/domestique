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
    never touched (G-B). Sweep is skipped when the upsert failed. Deletes and
    re-dates never touch past days, and the sweep only considers events
    dated [today, today+horizon] (G-H); the read window looks 7 days back
    and 7 days past the horizon (#28) so a workout the rider moved in
    intervals.icu stays visible (one moved further out is invisible and
    still re-dated by the upsert — status quo).
  - Window (3.14.0 F3): the horizon is the rider's own setting, 14, 28 or 42
    days in <profile>/user_prefs.json, read by profile id under the lock.
    An engine-owned <profile>/icu_calendar_push.json remembers how far the
    mirror reached ("pushed_until"), so a shorter window cleans up what a
    longer one put further out: the sweep reaches to that record, sparing
    moved and copied events, moves the plan took over (user_moved), and
    planner Send pushes made beyond the window while a clean-up is pending
    ("sent"). Only a window longer than 14 days ever writes it; a record
    written on a later day than today, or one that cannot be read, reaches
    nothing (fail closed). Only runs that read the rider's setting move the
    record (an explicit horizon, or unreadable prefs, never do).
  - Own rides (3.14.0, owner O3): a ride the rider added in Domestique is
    pushed as a plain planned ride (name, length, load; no steps, no file)
    under its own id "domestique:<profile_id>:<day_iso>:own", in a POST of
    its own after the workouts' (a rejected one is skipped "own_rejected",
    never a run error). A ride imported from intervals.icu is never pushed
    back. A rider move of an own event is held like #28 and reported as
    own_moved; the plan follows it only when the rider says so (the plan's
    own ride, moved_from the old day, then supersedes the event), and
    "put it back" (own.put_back_from = the event's date) re-dates it. Own
    rides follow the window like workouts. A workout sent with the Send
    button past the window stays there until the window reaches its day,
    also when an own ride has since replaced it (grill X9).
  - Rider moves in intervals.icu (#28, step 1 — reconcile never writes the
    plan): our event dated on another day than the one in its external_id
    was moved by the rider. Unless the plan agrees with the move (a session
    on that date moved from the encoded day, same workout), the id is HELD:
      * no visible re-date of a rider-moved event by reconcile (never in
        the upsert);
      * a rider-moved event is never deleted by reconcile unless the plan
        agrees with the move — and then only once the session's own new
        event is in the same run's upsert;
      * one event per session: the session a held event stands for is not
        pushed anywhere else (found through its moved_from chain); no bumped
        id is ever minted.
    The hold is released when Domestique has since changed that slot — the
    day now holds a different workout, was dismissed, lost the session, or
    its moved_from chain leads to a different workout: the plan's day wins
    and the id is pushed / swept as usual. Never for an event dated before
    today (past days are never re-dated or deleted).
    The planner's single "Send to intervals.icu calendar" push (the
    push-workout endpoint) applies the same hold through moved_copy_day,
    under the same lock: a rider report showed the button pulling a move
    back.
  - broken_ids (#27): an unbuildable session spares its slot's event only
    while that event still carries this session's workout — after a swap
    the slot's old event is the other session's.
  - One reconcile / sweep_all at a time (module lock; reconcile waits 60 s,
    sweep_all 120 s, then "busy"). sweep_all deletes our events dated
    [today, reach+7] (reach: the window, or further while the record says
    the mirror went further) — rider-moved ones and own rides included.
  - Toast counts derive from the local diff vs the pre-upsert GET, not from
    ICU response fields (G-G).
  - apikey auth = always write-capable (G-F); OAuth needs the CALENDAR:WRITE
    stamp (pm.icu_granted_scopes) — unstamped legacy connections report
    needs_reconnect without a network call.
"""
from __future__ import annotations

import clock  # the one clock every module reads (see src/clock.py)
import base64
import functools
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
import tls_trust  # v3.11.4 — one verifier for every intervals.icu connection
from datetime import date, timedelta
from pathlib import Path

import config
import training as _training
from user_home import domestique_home

_log = logging.getLogger("domestique.icu_push")

HORIZON_DAYS = 14                      # the default window (2 weeks)
# 3.14.0 F3: the rider keeps 2, 4 or 6 weeks of the plan in intervals.icu,
# per profile; anything else reads as the default.
HORIZON_CHOICES = (14, 28, 42)
_HORIZON_PREF = "icu_calendar_horizon_days"
# Engine-owned per-profile state, written only under _RECONCILE_LOCK:
# {"pushed_until": iso|null, "written_on": iso, "sent": {external_id: day}}.
_STATE_FILE = "icu_calendar_push.json"
# C7 (grill M3): intervals.icu deletes by external_id when a bulk-delete row
# names both (public API docs), which also removes a copy the rider made of
# our event outside the read window. True sends the event id alone; it stays
# off until the owner has checked it live.
_DELETE_BY_ID_ONLY = False
_OWN_DESCRIPTION = "Your own ride, added in Domestique."
# #28: reconcile reads this far either side of [today, today+horizon] so an
# event the rider moved just before today (rode it early) or just past the
# horizon is still seen — and held — instead of being yanked back blind by
# the upsert. The sweep never looks outside [today, today+horizon].
_LOOKBACK_DAYS = 7
_LOOKAHEAD_DAYS = 7
_MAX_CHAIN_HOPS = 7
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
        return pm.icu_has_scope("CALENDAR:WRITE")
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
        with urllib.request.urlopen(req, timeout=timeout, context=tls_trust.make_context()) as resp:
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


def _http_error_detail(step: str, status: int, body: bytes) -> str:
    """3.3.1 hotfix (B3b): one WARNING per non-2xx with step + status +
    body[:300], returning the excerpt so callers can carry it as
    ``error_detail``. v3.3.0 logged only ``error=http_422`` — ICU's actual
    rejection reason (which event, which field) was unrecoverable from the
    tester's logs, so a persistent 422 silently stalled ALL syncing with no
    way to diagnose it. The 300-byte cap keeps a pathological body from
    flooding the log; ICU validation messages fit comfortably."""
    detail = (body or b"")[:300].decode("utf-8", "replace")
    _log.warning("EVENT=icu_push_http_error step=%s status=%s body=%s",
                 step, status, detail)
    return detail


# ── plan → desired events ────────────────────────────────────────────────────

def _plan_path() -> Path:
    import training_planner as tp
    return Path(getattr(tp, "PLAN_DIR", domestique_home() / "plans")) / "current_plan.json"


def _load_plan(p: "Path | None" = None) -> dict | None:
    p = p or _plan_path()
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
    """file -> classification entry. The file nests its entries under
    "classifications"; returning the envelope made every lookup miss, so every
    pushed event lost its display name (notes/review/dupes.md DUP-7)."""
    try:
        d = json.loads((workout_dir / ".content_classification.json")
                       .read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("classifications"), dict):
            return d["classifications"]
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
    zwo_path = _app._workout_file(zwo_file, base=workout_dir)
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


def _own(s: dict) -> "dict | None":
    """The rider's own ride on this row (3.14.0 ``session["own"]``), or None
    for a planner session."""
    own = s.get("own")
    if not own:
        return None
    return own if isinstance(own, dict) else {}


def _own_is_imported(own: dict) -> bool:
    """C5: a ride that came from the rider's intervals.icu calendar is there
    already, as their own event: never pushed back. A row that carries an
    intervals.icu event id counts as imported whatever its source says, so a
    lost source can never turn it into a duplicate."""
    return own.get("source") == "icu" or own.get("icu_event_id") not in (None, "", 0)


def _is_own_id(eid) -> bool:
    return str(eid or "").endswith(":own")


def _build_own_event(s: dict, ext_id: str):
    """3.14.0 (owner O3, C1): the rider's own ride as a plain planned ride:
    name, length and load; no workout steps and no file, so no ``filename``
    and no ``file_contents_base64`` (every reader uses .get). The description
    is fixed text: the bulk endpoint reads a description as workout text, so
    the rider's words never go there. Returns ``(event, None)``, or
    ``(None, "own_invalid")`` when the row has no usable length."""
    own = _own(s) or {}
    try:
        minutes = float(s.get("duration_min"))
    except (TypeError, ValueError):
        return None, "own_invalid"
    if not (math.isfinite(minutes) and minutes > 0):
        return None, "own_invalid"
    event = {
        "start_date_local": f"{s.get('day')}T00:00:00",
        "category": "WORKOUT",
        "type": "Ride",
        "name": (str(own.get("name") or "").strip()
                 or str(s.get("description") or "").strip() or "Own ride"),
        "external_id": ext_id,
        "moving_time": int(round(minutes * 60)),
        "description": _OWN_DESCRIPTION,
    }
    load = s.get("tss_estimate")
    if (isinstance(load, (int, float)) and not isinstance(load, bool)
            and math.isfinite(load) and load >= 0):
        event["icu_training_load"] = int(round(load))
    return event, None


def _sessions_by_day(plan: dict) -> dict[str, list[dict]]:
    """Day → that day's sessions in stored plan order (weeks are ordered and a
    day lives in exactly one week, so plain traversal preserves order). The
    index in each list IS the external_id's <n>; #28's hold logic resolves
    ids with this same enumeration, so it lives in one place."""
    by_day: dict[str, list[dict]] = {}
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            d = s.get("day")
            if isinstance(d, str) and d:
                by_day.setdefault(d, []).append(s)
    return by_day


def _desired_events(pm, plan: dict, today: date, horizon_days: int,
                    profile_id: str):
    """Collect the horizon's pushable sessions as ICU bulk-event payloads.

    Returns (events, skipped, broken_ids):
      events     — list of ICU event dicts ready for the bulk upsert; own
                   rides (3.14.0) are among them under their ``:own`` ids
      skipped    — [{"day", "reason"}] for the toast (unmatched / needs_lthr /
                   trailing_open / file_missing / build_failed / own_invalid)
      broken_ids — external_ids of sessions we WANTED to push but could not
                   build; the sweep spares these so a config hiccup (e.g. a
                   broken lthr) never deletes previously-pushed events.
                   (#27: reconcile spares one only while the event at that
                   id is still this session's workout.)
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

    by_day = _sessions_by_day(plan)

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
            own = _own(s)
            if own is not None:
                # 3.14.0 (C1, C5): the rider's own ride, under its own id, so
                # the workout it replaced is swept as an orphan. Imported from
                # their intervals.icu calendar: never pushed back. Never in
                # broken_ids either way (that would spare the replaced
                # workout's event, #27).
                if _own_is_imported(own):
                    continue
                event, reason = _build_own_event(
                    s, f"{_ID_ROOT}{profile_id}:{day_iso}:own")
                if event is None:
                    skipped.append({"day": day_iso, "reason": reason})
                else:
                    events.append(event)
                continue
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
                # 3.3.1 hotfix (B3a): broken_ids exists to spare a previously
                # pushed event from the sweep when we WANT to push the same
                # session but temporarily can't (needs_lthr / file_missing /
                # build hiccup). A FILELESS session whose content was
                # DELIBERATELY changed — user_swapped (manual type swap,
                # app.py stamps it) or adapted (readiness tier-down; both can
                # clear zwo_file on NoCandidate) — is not a transient failure:
                # protecting it left the STALE old-type event on the athlete's
                # calendar forever (tester: threshold→z2 swap kept mirroring
                # THRESHOLD). Deliberate + unmatched ⇒ let the sweep remove
                # the stale event; every other skip reason stays protected.
                if not (reason == "unmatched"
                        and (s.get("user_swapped") or s.get("adapted"))):
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


# ── the rider's window and the clean-up record (3.14.0 F3) ───────────────────

def _profile_dir(profile_id: str) -> Path:
    from profile_manager import ProfileManager, _safe_profile_dir
    return _safe_profile_dir(ProfileManager.get(), profile_id)


def _profile_prefs(profile_id) -> "dict | None":
    """This rider's user_prefs.json, read from THEIR profile dir (a profile
    switch swaps the active cache mid-run): {} when there is none, None when
    it cannot be read (grill L1: unreadable is not "the default")."""
    try:
        prefs = json.loads((_profile_dir(profile_id) / "user_prefs.json")
                           .read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return None
    return prefs if isinstance(prefs, dict) else None


def _horizon_pref(profile_id) -> "int | None":
    """The rider's window in days, or None when their prefs cannot be read."""
    prefs = _profile_prefs(profile_id)
    if prefs is None:
        return None
    v = prefs.get(_HORIZON_PREF)
    ok = isinstance(v, int) and not isinstance(v, bool) and v in HORIZON_CHOICES
    return v if ok else HORIZON_DAYS


def profile_horizon_days(profile_id) -> int:
    """This rider's window in days: 14, 28 or 42, else the default."""
    if not profile_id:
        return HORIZON_DAYS
    return _horizon_pref(profile_id) or HORIZON_DAYS


def _no_state() -> dict:
    return {"pushed_until": None, "sent": {}, "held_days": [], "own_moved": []}


def _read_state(profile_id: str, today: date) -> dict:
    """{"pushed_until": date | None, "sent": {external_id: day_iso},
    "held_days": [day_iso], "own_moved": [{day, icu_day}]}.

    The record fails closed: a missing, garbled or unreadable one, or one
    written on a later day than today (the clock went back, grill M1),
    reaches nothing; never past today + the widest window. held_days and
    own_moved are what the last run that read this rider's calendar saw
    (QA C9b): kept whatever day they were written, so a failed run or
    another rider's run never erases them."""
    out = _no_state()
    try:
        raw = json.loads((_profile_dir(profile_id) / _STATE_FILE)
                         .read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return out
    except (OSError, ValueError, TypeError):
        return out
    try:
        out["held_days"] = sorted({str(d)[:10] for d in raw.get("held_days") or []
                                   if _iso_week(str(d)[:10]) is not None})
        out["own_moved"] = [{"day": str(m["day"])[:10], "icu_day": str(m["icu_day"])[:10]}
                            for m in raw.get("own_moved") or []
                            if isinstance(m, dict) and m.get("day") and m.get("icu_day")]
    except (TypeError, AttributeError):
        out["held_days"], out["own_moved"] = [], []
    try:
        if date.fromisoformat(str(raw["written_on"])) > today:
            return out
        until = raw["pushed_until"]
        if until is not None:
            until = min(date.fromisoformat(str(until)),
                        today + timedelta(days=max(HORIZON_CHOICES)))
        out["sent"] = {str(k): str(v) for k, v in (raw.get("sent") or {}).items()}
        out["pushed_until"] = until
    except (ValueError, KeyError, TypeError, AttributeError):
        out["pushed_until"], out["sent"] = None, {}
    return out


def _write_state(profile_id: str, state: dict, today: date) -> None:
    """Write the state of the run's own rider (under _RECONCILE_LOCK)."""
    try:
        path = _profile_dir(profile_id) / _STATE_FILE
        tmp = path.with_name(path.name + ".tmp")
        until = state.get("pushed_until")
        tmp.write_text(json.dumps({
            "pushed_until": until.isoformat() if until else None,
            "written_on": today.isoformat(), "sent": state.get("sent") or {},
            "held_days": list(state.get("held_days") or []),
            "own_moved": list(state.get("own_moved") or [])}), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError):
        # ponytail: a lost record only leaves a stale tail after a LATER
        # shrink, which rolls off as the window passes it; never blocks a push.
        _log.warning("EVENT=icu_calendar_state_write_failed profile=%s", profile_id)


def held_plan_days(profile_id) -> list:
    """Plan days whose workout the rider moved in intervals.icu, as the last
    run that read their calendar saw them (C9, QA C9b)."""
    return list(_read_state(profile_id, clock.today())["held_days"]) if profile_id else []


def own_moved_report(profile_id) -> list:
    """[{day, icu_day}]: the rider's own rides whose event is on another day
    in intervals.icu, as the last run that read their calendar saw them
    (O4); none after a sweep (QA-UX QX-M2)."""
    return list(_read_state(profile_id, clock.today())["own_moved"]) if profile_id else []


def _sent_within(sent: dict, last_day: date, until: "date | None") -> dict:
    """The Send records that still matter: past the window and inside the
    record (a day inside the window is mirrored; one past the record is
    never a clean-up candidate)."""
    if until is None:
        return {}
    lo, hi = last_day.isoformat(), until.isoformat()
    return {eid: d for eid, d in sent.items() if lo < d <= hi}


def _resolve_window(profile_id: str, today: date,
                    horizon_days: "int | None" = None):
    """(horizon_days, reach, state, managed) for one run. None reads the
    rider's setting; only such a run, with readable prefs (L1), is managed:
    it reads the clean-up record and may move it (L2). reach is how far its
    sweep may look: the window, or the record while that is further."""
    managed = horizon_days is None
    if managed:
        pref = _horizon_pref(profile_id)
        managed = pref is not None
        horizon_days = pref or HORIZON_DAYS
    state = _read_state(profile_id, today) if managed else _no_state()
    last_day = today + timedelta(days=horizon_days)
    until = state["pushed_until"]
    return horizon_days, (max(last_day, until) if until else last_day), state, managed


def send_window_days(profile_id: str, today: date, target: date) -> int:
    """Grill L3: the window the planner Send button builds and reads: as far
    as this rider's own reconcile reads (a pending clean-up included) and at
    least to the target day, so the Send sees the same holds."""
    _h, reach, _state, _managed = _resolve_window(profile_id, today)
    return max((reach - today).days, (target - today).days)


def note_sent(profile_id: str, external_id: str, day: date) -> None:
    """Grill H1: a planner Send push past the rider's window while a clean-up
    is pending (the record reaches past the window) is spared by that
    clean-up. The caller holds _RECONCILE_LOCK. A rider without a record
    (every default rider) writes nothing."""
    today = clock.today()
    state = _read_state(profile_id, today)
    until = state["pushed_until"]
    last_day = today + timedelta(days=_horizon_pref(profile_id) or HORIZON_DAYS)
    if until is None or not (last_day < day <= until):
        return
    sent = _sent_within(state["sent"], last_day, until)
    sent[external_id] = day.isoformat()
    _write_state(profile_id, {**state, "sent": sent}, today)


def _delete_rows(chosen: list, existing: list, today: date,
                 last: "date | None") -> list:
    """Bulk-delete rows for the chosen events of ours. Default: one
    {"id", "external_id"} row per id, so intervals.icu deletes every event
    under that external_id (public API docs), a copy the rider made of it
    included. C7 (_DELETE_BY_ID_ONLY): one {"id"} row per event of those ids
    that this run read, dated [today, last]; anything else is left alone."""
    if not _DELETE_BY_ID_ONLY:
        return [{"id": ev.get("id"), "external_id": str(ev.get("external_id") or "")}
                for ev in chosen]
    ids = {str(ev.get("external_id") or "") for ev in chosen}
    rows = []
    for ev in existing:
        if not isinstance(ev, dict) or ev.get("id") is None:
            continue
        d = _event_day(ev)
        if (str(ev.get("external_id") or "") in ids and d is not None and d >= today
                and (last is None or d <= last)):
            rows.append({"id": ev.get("id")})
    return rows


def _get_window_events(athlete_id: str, today: date, horizon_days: int,
                       lookback: int = 0, lookahead: int = 0):
    """GET our reconcile window. Returns (events|None, error_result|None).

    lookback/lookahead widen the READ only (#28: reconcile passes 7/7 to see
    rider moves); sweep_all keeps the defaults, i.e. [today, today+horizon]."""
    oldest = today - timedelta(days=lookback)
    newest = today + timedelta(days=horizon_days + lookahead)
    status, body = _http(
        "GET", f"athlete/{athlete_id}/events?oldest={oldest.isoformat()}"
               f"&newest={newest.isoformat()}")
    if _scope_403(status, body):
        return None, _result(needs_reconnect=True)
    if status == 0:
        return None, _result(error="network")
    if not (200 <= status < 300):
        # 3.3.1 hotfix (B3b): keep the rejection reason, not just the code.
        return None, _result(error=f"http_{status}",
                             error_detail=_http_error_detail(
                                 "window_get", status, body))
    try:
        events = json.loads(body or b"[]")
    except (json.JSONDecodeError, ValueError):
        return None, _result(error="bad_response")
    if not isinstance(events, list):
        events = []
    return events, None


def _ours_in_window(events: list, profile_id: str, today: date,
                    last_day: date | None = None) -> dict:
    """Our profile's events keyed by external_id — and the G-B warning for
    foreign domestique prefixes (multi-install: warn, NEVER delete).

    ``last_day`` clamps the far end (#28): reconcile's read reaches past the
    horizon, but an event dated there (a planner push-workout, a rider move)
    must never become a sweep candidate."""
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
        if last_day is not None and ev_day > last_day:
            continue
        ours[eid] = ev
    if foreign:
        _log.warning(
            "EVENT=icu_push_foreign_prefix count=%d — another Domestique "
            "install pushes to this athlete's calendar (one device per "
            "athlete is supported); leaving those events alone.", foreign)
    return ours


# ── rider moves made in intervals.icu (#28, step 1: hold, never fight) ───────

def _stem(name) -> str:
    """Workout identity across formats: an hr profile pushes ``x.fit`` for the
    session's ``x.zwo``, so files compare by stem."""
    return Path(str(name or "").strip()).stem


def _is_stub(s: dict) -> bool:
    return str(s.get("status") or "").startswith("moved_from:")


def _iso_week(day_iso: str):
    try:
        return date.fromisoformat(day_iso).isocalendar()[:2]
    except ValueError:
        return None


def _event_day(ev: dict) -> date | None:
    try:
        return date.fromisoformat(str(ev.get("start_date_local") or "")[:10])
    except ValueError:
        return None


def _same_workout(s: dict, ev: dict, names):
    """Is the session the workout the event carries? True / False, or None
    when it cannot be told: a session without a workout file, or an event
    with neither filename nor name. Files compare by stem; when intervals.icu
    does not echo the filename, the event's name is compared with the name
    we would push (``names`` lazily returns the classifications)."""
    have = _stem(s.get("zwo_file"))
    if not have:
        return None
    there = _stem(ev.get("filename"))
    if there:
        return there == have
    name = str(ev.get("name") or "")
    return name == _display_name(s, names()) if name else None


def _week_rows(by_day: dict, day_iso: str):
    """(day, index, session) for every live (non-stub) row in day_iso's ISO week."""
    week = _iso_week(day_iso)
    return [(d, k, x) for d, ss in by_day.items() if _iso_week(d) == week
            for k, x in enumerate(ss) if not _is_stub(x)]


def _plan_agrees(by_day: dict, enc: str, n: int, ev_day: str, ev: dict):
    """grill_simple C1 + grill_data M4: the plan made the same move when a
    session on the event's day was moved from the encoded day — and, when
    both sides name a file, it is the same workout (a #27 swap followed by
    an intervals.icu stack must not look agreed). Returns that session's
    index on the event's day (its new id, W1), or None."""
    want = _stem(ev.get("filename"))
    if not want:
        # W4 (qa_code M2): without an echoed filename the content cannot be
        # checked. Agree only with a plain move — the encoded slot is a stub
        # and exactly one session in the week left that day. After a swap the
        # slot's id is pushed again, so "agreeing" re-dated the rider's event.
        slot = by_day.get(enc) or []
        if not (n < len(slot) and _is_stub(slot[n])):
            return None
        if sum(1 for _d, _k, x in _week_rows(by_day, enc)
               if x.get("moved_from") == enc) != 1:
            return None
    for k, s in enumerate(by_day.get(ev_day) or []):
        # 3.14.0: an own ride is pushed under its own id, so it never agrees
        # with a planner workout's move (that agreement released the hold
        # and pulled the rider's workout back).
        if s.get("moved_from") != enc or _own(s) is not None:
            continue
        have = _stem(s.get("zwo_file"))
        if not want or not have or want == have:
            return k
    return None


def _slot_changed(by_day: dict, enc: str, n: int, ev: dict, names) -> bool:
    """W2 (qa_scenario H1/M1): has Domestique changed the slot since the rider
    moved its event? The day now holds a different workout, a dismissed
    session, no session at that index, or a stub whose chain leads to a
    different workout. Then the plan's day wins and the hold is released.
    Not comparable (no file, no name) or a day outside the plan → hold."""
    slot = by_day.get(enc)
    if slot is None:
        return False
    if n >= len(slot):
        return True
    s = slot[n]
    if str(s.get("status") or "") == "dismissed":
        return True
    if _is_stub(s):
        return _resolve_moved_session(by_day, enc, n, ev, names)[1]
    return _same_workout(s, ev, names) is False


def _held_ids(events: list, by_day: dict, profile_id: str,
              today: date, names) -> tuple[dict, dict]:
    """(held, agreed) for every id of ours the rider moved in intervals.icu.

    held:   external_id → (encoded day, n, the moved copy) — moves the plan
            has not followed and has not overridden since (W2).
    agreed: external_id → the id the plan now pushes that session under; the
            moved event may go only once that id is in the upsert (W1).

    Read from the RAW GET list (an intervals.icu copy can share our id; the
    ``ours`` dict keeps one copy per id). Only ids that strictly match our
    profile's ``domestique:<pid>:<YYYY-MM-DD>:<n>`` count — never
    ``domestique-manual:``, never another profile. An unparseable event date
    is not a move (legacy fail-closed behaviour). Every writer of these ids
    writes date == encoded day, so a different date is the rider's move."""
    prefix = f"{_ID_ROOT}{profile_id}:"
    pattern = re.compile(re.escape(prefix)
                         + r"([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9]+)")
    groups: dict[str, tuple] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("external_id") or "")
        m = pattern.fullmatch(eid)
        if m is None or _iso_week(m.group(1)) is None:
            continue
        groups.setdefault(eid, (m.group(1), int(m.group(2)), []))[2].append(ev)
    held, agreed = {}, {}
    for eid, (enc, n, evs) in groups.items():
        moved = [(ev, d) for ev, d in ((ev, _event_day(ev)) for ev in evs)
                 if d is not None and d.isoformat() != enc]
        if not moved:
            continue
        ev, d = moved[0]
        # A copy (two events, one id) is never agreed nor released: either
        # upsert or delete could hit the rider's copy. A move into the past
        # is never agreed nor released either: following it would re-date a
        # past (possibly ride-paired) event — past days are never re-dated.
        if len(evs) == 1 and d >= today:
            k = _plan_agrees(by_day, enc, n, d.isoformat(), ev)
            if k is not None:
                agreed[eid] = f"{prefix}{d.isoformat()}:{k}"
                continue
            if _slot_changed(by_day, enc, n, ev, names):
                continue
        held[eid] = (enc, n, ev)
    return held, agreed


def _chain_end(by_day: dict, enc: str, s: dict):
    """Follow the ``moved_from:X`` stubs from the encoded day to X's row that
    came from there (or X's own stub when it moved on again), never
    revisiting a day: (day, index, session) of the live row it ends on, or
    None when it breaks (a cycle, a fork, a swap midway, too many hops)."""
    prev, seen = enc, {enc}
    for _ in range(_MAX_CHAIN_HOPS):
        nxt = str(s.get("status"))[len("moved_from:"):]
        if nxt in seen:
            return None
        seen.add(nxt)
        hits = [(k, x) for k, x in enumerate(by_day.get(nxt) or [])
                if x.get("moved_from") == prev or _is_stub(x)]
        if len(hits) != 1:
            return None
        k, s = hits[0]
        if not _is_stub(s):
            return nxt, k, s
        prev = nxt
    return None


def _resolve_moved_session(by_day: dict, enc: str, n: int, ev: dict, names):
    """((day, index) | None, elsewhere) — where the plan now holds the session
    a held event was pushed for (grill_cal C-1, prototype R1), and whether the
    slot's stub chain positively leads to a different workout (W2).

    A stub on the encoded day is followed (``_chain_end``). W3 (qa_code M1):
    the chain's end must be the event's workout — a swap midway hands the
    chain to the partner — else the one live session in that ISO week that
    moved and carries the event's file is taken. No stub means a #27 swap
    or a later move refilled the slot: the session is the one in that ISO
    week that left the encoded day with the event's workout — without an
    echoed filename it cannot be told apart, so nothing is resolved."""
    slot = by_day.get(enc) or []
    s = slot[n] if n < len(slot) else None
    if s is None:
        return None, False
    want = _stem(ev.get("filename"))
    if not _is_stub(s):
        if not want:
            return None, False
        hits = [(d, k) for d, k, x in _week_rows(by_day, enc)
                if x.get("moved_from") == enc and _stem(x.get("zwo_file")) == want]
        return (hits[0] if len(hits) == 1 else None), False
    end = _chain_end(by_day, enc, s)
    if end is not None:
        day, k, x = end
        if str(x.get("status") or "") == "dismissed":
            return None, True
        if _same_workout(x, ev, names) is not False:
            return (day, k), False
    hits = [(d, k) for d, k, x in _week_rows(by_day, enc)
            if want and x.get("moved_from") and _stem(x.get("zwo_file")) == want]
    if len(hits) == 1:
        return hits[0], False
    return None, end is not None and not hits


def _still_this_workout(s: dict, ev: dict, by_day: dict) -> bool:
    """#27 (MASTER §4.1): may the sweep spare the event at an unbuildable
    session's id? Only while that event is this session's workout. After a
    swap the slot's prior event carries the OTHER session — sparing it put
    one workout on two days (or the wrong day) on the watch. Unknown on
    either side → spare a session that never moved (a library outage on an
    unmoved session still keeps its event), and (W5, qa_code M3) one that
    arrived by a move: the day it left holds a stub pointing here, so the
    event at its id was pushed for it. A swap leaves no stub."""
    have, there = _stem(s.get("zwo_file")), _stem(ev.get("filename"))
    if have and there:
        return have == there
    left = s.get("moved_from")
    if not left:
        return True
    return any(str(x.get("status") or "") == f"moved_from:{s.get('day')}"
               for x in by_day.get(left) or [])


def _stand_ins(held: dict, by_day: dict, profile_id: str, names) -> dict:
    """external_id → the rider's moved copy, for every id a push must leave
    alone: each held id, and the id its session is planned under now (so a
    later auto-reschedule or swap cannot put the workout on two days)."""
    out = {}
    for eid, (enc, n, ev) in held.items():
        out[eid] = ev
        loc, _ = _resolve_moved_session(by_day, enc, n, ev, names)
        if loc is not None:
            out.setdefault(f"{_ID_ROOT}{profile_id}:{loc[0]}:{loc[1]}", ev)
    return out


def _pushes_own(s: dict) -> bool:
    """The row is an own ride Domestique puts in intervals.icu (manual)."""
    own = _own(s)
    return own is not None and not _own_is_imported(own)


def _own_held_ids(events: list, by_day: dict, profile_id: str, today: date,
                  pushed: set) -> tuple[dict, dict, dict]:
    """C3 (owner O4, O8): (held, agreed, released) for our own-ride ids
    ``domestique:<pid>:<day>:own`` the rider moved in intervals.icu.

    held:     external_id → (encoded day, the moved copy): never upserted,
              never swept, shown to the rider (own_moved). Nothing follows
              it silently, and #33 never learns it.
    agreed:   external_id → the plan's own ids that supersede it. The ride
              left the encoded day in Domestique ("Move it in my plan too",
              or to any other day): every ride Domestique pushes that came
              from that day and is not out under its new id yet may be this
              event's ride, and the event goes once those of them this run
              pushes are in its upsert (the agreed rule below), whatever day
              it is on now. A ride already out under its new id has its own
              event: an old moved_from on it never keeps this one, or
              doubles a ride (QA S1, M3).
    released: external_id → (encoded day, the moved copy) for "Put it back"
              (the ride on its plan day carries put_back_from = the event's
              day) while this run pushes the id: the upsert re-dates it.
    O8: when no ride stands for the event any more (the encoded day holds no
    ride Domestique pushes and none came from it: the rider removed it, it
    was linked to their own intervals.icu ride, or a race took the day) it
    is neither held nor agreed, so the sweep deletes it. Copies (two events,
    one id, possibly the rider's) and moves into the past are never agreed,
    released or deleted."""
    prefix = f"{_ID_ROOT}{profile_id}:"
    pattern = re.compile(re.escape(prefix) + r"([0-9]{4}-[0-9]{2}-[0-9]{2}):own")
    groups: dict[str, tuple] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("external_id") or "")
        m = pattern.fullmatch(eid)
        if m is None or _iso_week(m.group(1)) is None:
            continue
        groups.setdefault(eid, (m.group(1), []))[1].append(ev)
    came_from: dict[str, list] = {}         # moved_from day → rides from it
    for row_day, rows in sorted(by_day.items()):
        for s in rows:
            if _pushes_own(s) and s.get("moved_from"):
                came_from.setdefault(str(s.get("moved_from")), []).append(row_day)
    held, agreed, released = {}, {}, {}
    for eid, (enc, evs) in groups.items():
        moved = [(ev, d) for ev, d in ((ev, _event_day(ev)) for ev in evs)
                 if d is not None and d.isoformat() != enc]
        if not moved:
            continue
        ev, d = moved[0]
        day = d.isoformat()
        if len(evs) == 1 and d >= today:
            here = [s for s in by_day.get(enc) or [] if _pushes_own(s)]
            if not here:
                live = [y for y in came_from.get(enc) or []
                        if f"{prefix}{y}:own" not in groups]
                if live:
                    agreed[eid] = frozenset(f"{prefix}{y}:own" for y in live)
                continue                    # O8: nothing stands for it: swept
            if eid in pushed and any(_own(s).get("put_back_from") == day for s in here):
                released[eid] = (enc, ev)
                continue
        held[eid] = (enc, ev)
    return held, agreed, released



def _day_unavailable(plan: dict, day_iso: str) -> bool:
    """The rider marked the day unavailable (a 0 h day or a holiday/injury/
    illness period)."""
    avail = plan.get("availability")
    if isinstance(avail, dict):
        v = avail.get(day_iso)
        if isinstance(v, dict):             # the stored shape: {hours, type}
            v = v.get("hours")
        try:
            if v is not None and not isinstance(v, bool) and float(v) <= 0:
                return True
        except (TypeError, ValueError):
            pass
    for per in plan.get("unavailable_periods") or []:
        if isinstance(per, dict) and str(per.get("start") or "") <= day_iso <= str(per.get("end") or ""):
            return True
    return False


def _learnable_moves(held: dict, existing: list, plan: dict, today: date,
                     names) -> list:
    """#33: the held moves the plan can follow as if the rider had dragged
    them in Domestique: [(external_id, from_day, to_day)].

    Only the plain case, everything else stays held (#28 step 1): one event
    under the id (never a copy), both days today or later and in the same
    week, the day it left holds exactly that one pending workout, and the
    day it went to is a rest, empty or vacated day the rider has not marked
    unavailable, that is no race and that no other move goes to. A move onto
    a training day is never learned: in intervals.icu the two now share a
    day, and Domestique cannot tell which of them the rider means to ride.

    Dates read back wrong must never look like moves (a learned move would
    otherwise walk on, run after run): the event keeps the midnight we wrote
    (a timezone shift changes the hour, a drag does not), and an event we
    pushed in the last few minutes that reads back elsewhere is a reading
    problem, not the rider."""
    by_day = _sessions_by_day(plan)
    counts = {}
    for ev in existing:
        if isinstance(ev, dict):
            eid = str(ev.get("external_id") or "")
            counts[eid] = counts.get(eid, 0) + 1
    out = []
    for eid, (enc, n, ev) in held.items():
        d = _event_day(ev)
        if counts.get(eid) != 1 or d is None or d < today:
            continue
        if str(ev.get("start_date_local") or "")[10:] not in ("", "T00:00:00"):
            continue
        pushed = _RECENT_PUSH.get(eid)
        if pushed and pushed[0] != d.isoformat() and time.monotonic() - pushed[1] < _RECENT_PUSH_S:
            continue
        dst = d.isoformat()
        if enc < today.isoformat() or _iso_week(enc) != _iso_week(dst):
            continue
        src_rows, dst_rows = by_day.get(enc) or [], by_day.get(dst) or []
        if n != 0 or len(src_rows) != 1 or len(dst_rows) > 1:
            continue
        src = src_rows[0]
        # 3.14.0: never an own ride. It has no file, so _same_workout cannot
        # tell it from the workout the rider moved, and following the move
        # would carry the rider's ride off its day.
        if (_is_stub(src) or (src.get("session_type") or "") == "rest"
                or str(src.get("status") or "pending") != "pending"
                or src.get("is_race") or _own(src) is not None
                or _same_workout(src, ev, names) is False):
            continue
        if dst_rows and not ((dst_rows[0].get("session_type") or "") == "rest"
                             or _is_stub(dst_rows[0])):
            continue
        if (dst_rows and dst_rows[0].get("is_race")) or _day_unavailable(plan, dst):
            continue
        out.append((eid, enc, dst))
    # Two moves onto one day: the second would land on a training day and
    # swap -- neither is learned.
    dsts = [dst for _e, _s, dst in out]
    return [m for m in out if dsts.count(m[2]) == 1]


# #33: the day each event was last pushed on, and when (this process only).
# An event read back on another day minutes after we wrote it was not moved
# by the rider: the dates are being read wrong.
_RECENT_PUSH: dict = {}
_RECENT_PUSH_S = 600.0


def note_pushed(events: list) -> None:
    now = time.monotonic()
    for ev in events or []:
        eid = str(ev.get("external_id") or "")
        if eid:
            _RECENT_PUSH[eid] = (str(ev.get("start_date_local") or "")[:10], now)

def _looks_like_a_date_shift(existing: list, profile_id: str, today: date) -> bool:
    """#33 guard: when most of our events from today on sit off their own
    day, the dates are being read wrong (a timezone shift), not moved one by
    one by the rider. Learn nothing then."""
    prefix = f"{_ID_ROOT}{profile_id}:"
    total = off = 0
    for ev in existing:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("external_id") or "")
        d = _event_day(ev)
        if not eid.startswith(prefix) or d is None or d < today:
            continue
        parts = eid[len(prefix):].split(":")
        total += 1
        off += parts[0] != d.isoformat()
    return total >= 2 and off * 2 > total


def _learn_moves(existing: list, profile_id: str, today: date, names,
                 plan_path: Path) -> list:
    """#33: write the learnable moves into the plan, under the plan lock, on
    a fresh read. Returns [{external_id, from, to, name}] for what moved.
    Lock order is the reconcile lock, then the plan lock; no plan-lock holder
    ever waits for the reconcile lock."""
    import app as _app
    import training_planner as tp
    from profile_manager import ProfileManager
    learned = []
    with tp.plan_write_lock():
        # A profile switch during the run (the GET can take 30 s) must never
        # write this rider's move into the other rider's plan.
        if ProfileManager.get().active_id != profile_id:
            _log.info("EVENT=icu_calendar_learn_skipped reason=profile_switch")
            return []
        plan = _load_plan(plan_path)
        if plan is None:
            return []
        held, _agreed = _held_ids(existing, _sessions_by_day(plan), profile_id,
                                  today, names)
        for eid, enc, dst in _learnable_moves(held, existing, plan, today, names):
            moved = _app._apply_move_session(plan, enc, dst)
            if moved:
                learned.append({"external_id": eid, "from": enc, "to": dst,
                                "name": str(moved.get("zwo_name") or moved.get("session_type") or "")})
        if learned:
            tp.atomic_write_plan(plan_path, plan)
    for m in learned:
        _log.info("EVENT=icu_calendar_move_learned from=%s to=%s", m["from"], m["to"])
    return learned


# #28 (grill_cal M-3): the sync thread, the 30 s debounce timer and the Push
# button can each start a reconcile; two overlapping bulk upserts of a new
# external_id could leave two events under one id. One run at a time.
_RECONCILE_LOCK = threading.Lock()
_RECONCILE_LOCK_TIMEOUT_S = 60.0
# W7: toggle-off and disconnect must not skip their final sweep behind one
# slow reconcile (three 30 s HTTP calls) — the token is purged right after.
_SWEEP_LOCK_TIMEOUT_S = 120.0


def _one_at_a_time(timeout_s: float, fn, *args):
    """Run fn under the module lock; "busy" when it stays taken for timeout_s
    (read at call time, so tests can shorten it)."""
    if not _RECONCILE_LOCK.acquire(timeout=timeout_s):
        _log.warning("EVENT=icu_calendar_busy fn=%s", fn.__name__)
        return _result(error="busy")
    try:
        return fn(*args)
    finally:
        _RECONCILE_LOCK.release()


def reconcile(horizon_days: "int | None" = None, abort_if_swept: bool = True,
              profile_id: "str | None" = None, require_sync: bool = False) -> dict:
    """Mirror [today, today+window] of the stored plan onto the ICU calendar.

    ``horizon_days`` None is the rider's own window (F3), read from their
    prefs under the lock; an explicit number pushes that window and never
    moves the clean-up record (grill L2). ``profile_id`` binds the run to
    that rider: if another profile is active once the lock is free, the
    answer is {"error": "profile_changed"}; with ``require_sync`` it is
    {"error": "sync_off"} when that rider's sync is off by then (grill M2).

    Returns {pushed, updated, deleted, skipped:[{day,reason}], held,
    own_moved} plus at most one of needs_reconnect / needs_lthr / error
    ("busy" when another run held the lock for a minute). ``held`` counts our
    events dated from today to 7 days past the window that were left where
    the rider moved them in intervals.icu (#28, L6); it is 0 on every early
    return (W6). ``own_moved`` lists [{day, icu_day}] for the rider's own
    rides whose event is on another day there after this run (O4; a
    put-back counts until its upsert went through), ``held_rides`` how many
    of ``held`` they are, ``held_days`` the plan days of held workouts; all
    three only on a run that read the calendar (an error before that says
    nothing about them, QA P1). Never raises; never writes the plan.
    """
    # The rider's own Push (abort_if_swept=False) is never dropped: it asked
    # for this run after whatever sweep it waited behind.
    out = _one_at_a_time(_RECONCILE_LOCK_TIMEOUT_S, _reconcile_unless_swept,
                         horizon_days, _SWEEP_GENERATION if abort_if_swept else None,
                         profile_id, require_sync)
    out.setdefault("held", 0)
    return out


# A sweep_all (sync turned off, disconnect) bumps this. A reconcile that was
# already waiting for the lock when the sweep ran must not push everything
# back afterwards (3.13.0 review): it reads the generation before waiting and
# gives up, as "swept", when it changed.
_SWEEP_GENERATION = 0


def _reconcile_unless_swept(horizon_days: "int | None", generation: "int | None",
                            profile_id: "str | None" = None,
                            require_sync: bool = False) -> dict:
    if generation is not None and generation != _SWEEP_GENERATION:
        _log.info("EVENT=icu_calendar_push_skipped reason=swept")
        return _result(error="swept")
    return _reconcile(horizon_days, profile_id, require_sync)


def moved_copy_day(athlete_id: str, plan: dict, external_id: str,
                   profile_id: str, horizon_days: int):
    """The single "Send to intervals.icu" push of a planned session (#28):
    would upserting ``external_id`` pull back, or copy, a workout the rider
    moved in intervals.icu? Returns (day of the rider's copy, None) when it
    would, (None, None) when the push is free, (None, error_result) when the
    window GET failed (then nothing may be pushed). Reads the same window
    and applies the same hold as reconcile; the caller holds the reconcile
    lock across this and its upsert. Never writes."""
    import app as _app
    today = clock.today()
    existing, err_res = _get_window_events(
        athlete_id, today, horizon_days, _LOOKBACK_DAYS, _LOOKAHEAD_DAYS)
    if err_res is not None:
        return None, err_res
    by_day = _sessions_by_day(plan)
    names = functools.lru_cache(maxsize=None)(
        lambda: _load_classifications(Path(_app.WORKOUT_DIR)))
    held, agreed = _held_ids(existing, by_day, profile_id, today, names)
    ev = _stand_ins(held, by_day, profile_id, names).get(external_id)
    if ev is None:
        # The plan followed the move: the rider's copy (still under the old
        # id until the next reconcile replaces it) is this workout on this
        # day, so a push now would put it there twice.
        old = next((eid for eid, new_id in agreed.items()
                    if new_id == external_id), None)
        ev = next((e for e in existing if isinstance(e, dict)
                   and str(e.get("external_id") or "") == old), None) if old else None
    return (_event_day(ev).isoformat() if ev is not None else None), None


def _plan_took_over(eid: str, ev: dict, by_day: dict, profile_id: str,
                    names) -> bool:
    """Grill M4: is this our event of a session the rider moved (dragged in
    Domestique, or moved in intervals.icu and learned), sitting where the
    plan now has it? A shorter window's clean-up spares it: a stale tail
    until the window reaches it again, rather than a workout the rider
    placed vanishing from their calendar."""
    m = re.fullmatch(re.escape(f"{_ID_ROOT}{profile_id}:")
                     + r"([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9]+)", eid)
    d = _event_day(ev)
    if m is None or d is None or d.isoformat() != m.group(1):
        return False
    slot = by_day.get(m.group(1)) or []
    n = int(m.group(2))
    if n >= len(slot):
        return False
    s = slot[n]
    return (bool(s.get("user_moved")) and _own(s) is None and not _is_stub(s)
            and str(s.get("status") or "pending") == "pending"
            and _same_workout(s, ev, names) is not False)


def _reconcile(horizon_days: "int | None", want_profile: "str | None" = None,
               require_sync: bool = False) -> dict:
    from profile_manager import ProfileManager
    try:
        import app as _app
        pm = ProfileManager.get()
        profile_id = pm.active_id
        if not profile_id:
            return _result(error="no_active_profile")
        # Grill M2: a run asked for one rider never runs for another (a
        # profile switch during the lock wait), nor after that rider turned
        # sync off meanwhile.
        if want_profile is not None and profile_id != want_profile:
            return _result(error="profile_changed")
        if require_sync and not (_profile_prefs(profile_id) or {}).get("icu_calendar_sync"):
            return _result(error="sync_off")
        athlete_id, err = _connection(pm)
        if err:
            return _result(error=err)
        if not write_ok(pm):
            return _result(needs_reconnect=True)
        plan_path = _plan_path()          # #33: this run's rider, fixed
        plan = _load_plan(plan_path)
        if plan is None:
            return _result(error="no_plan")
        today = clock.today()
        # F3: this rider's window, and how far this run's sweep may look: a
        # shorter window cleans up what a longer one put further out.
        horizon_days, reach, state, managed = _resolve_window(
            profile_id, today, horizon_days)
        last_day = today + timedelta(days=horizon_days)
        until = state["pushed_until"]
        desired, skipped, broken_ids = _desired_events(
            pm, plan, today, horizon_days, profile_id)
        result = _result(skipped=skipped, profile_id=profile_id)
        if any(s.get("reason") == "needs_lthr" for s in skipped):
            result["needs_lthr"] = True

        # GET BEFORE the upsert: one call feeds both the G-G count diff
        # (pre-push state) and the orphan sweep candidate list.
        existing, err_res = _get_window_events(
            athlete_id, today, (reach - today).days, _LOOKBACK_DAYS, _LOOKAHEAD_DAYS)
        if err_res is not None:
            err_res["skipped"] = skipped
            return err_res
        ours = _ours_in_window(existing, profile_id, today, reach)

        # #28 step 1: a move the rider made in intervals.icu is HELD — its id
        # is never upserted (no tug-of-war back to the planned day) and never
        # swept (it is no longer deleted the next morning). The session it
        # stands for is not pushed anywhere else either, so a later
        # auto-reschedule or swap cannot put the workout on two days.
        by_day = _sessions_by_day(plan)
        names = functools.lru_cache(maxsize=None)(
            lambda: _load_classifications(Path(_app.WORKOUT_DIR)))
        held, agreed = _held_ids(existing, by_day, profile_id, today, names)
        # #33 (#28 step 2): a plain move the rider made in intervals.icu is
        # learned into the plan, as if dragged in Domestique; the agreed-move
        # rule below then re-keys its event in this same run.
        result["learned"] = []
        if (held and _learnable_moves(held, existing, plan, today, names)
                and not _looks_like_a_date_shift(existing, profile_id, today)):
            result["learned"] = _learn_moves(existing, profile_id, today, names,
                                             plan_path)
            if result["learned"]:
                plan = _load_plan(plan_path) or plan
                desired, skipped, broken_ids = _desired_events(
                    pm, plan, today, horizon_days, profile_id)
                result["skipped"] = skipped
                by_day = _sessions_by_day(plan)
                held, agreed = _held_ids(existing, by_day, profile_id, today, names)
        # C3 (O4, O8): our own-ride events the rider moved in intervals.icu.
        own_ids = {ev["external_id"] for ev in desired if _is_own_id(ev["external_id"])}
        held_own, agreed_own, released_own = _own_held_ids(
            existing, by_day, profile_id, today, own_ids)
        # W6: a move into the past is still held, but no longer news; L6: nor
        # is one beyond what the window itself reads.
        far = last_day + timedelta(days=_LOOKAHEAD_DAYS)
        learned_ids = {m["external_id"] for m in result["learned"]}
        result["held_rides"] = sum(1 for _e, ev in held_own.values()
                                   if today <= _event_day(ev) <= far)
        result["held"] = result["held_rides"] + sum(
            1 for eid, (_e, _n, ev) in held.items()
            if today <= _event_day(ev) <= far and eid not in learned_ids)

        def _moved(still_released):
            # O4: the rider's rides whose event is on another day in
            # intervals.icu; a put-back counts until its upsert went through.
            return sorted(
                ({"day": enc, "icu_day": _event_day(ev).isoformat()}
                 for enc, ev in list(held_own.values()) + still_released
                 if _event_day(ev) >= today
                 and any(_pushes_own(s) for s in by_day.get(enc) or [])),
                key=lambda m: m["day"])
        result["own_moved"] = _moved(list(released_own.values()))
        # 3.14.0 F2 (C9): plan days whose workout the rider moved in
        # intervals.icu and the plan has not followed; the own-ride writer
        # never eases those (app._icu_held_plan_days reads them).
        result["held_days"] = sorted({enc for eid, (enc, _n, _ev) in held.items()
                                      if eid not in learned_ids})
        if held:
            stand_ins = _stand_ins(held, by_day, profile_id, names)
            desired = [ev for ev in desired
                       if ev["external_id"] not in stand_ins]
        desired = [ev for ev in desired if ev["external_id"] not in held_own]
        workouts = [ev for ev in desired if not _is_own_id(ev["external_id"])]
        own_events = [ev for ev in desired if _is_own_id(ev["external_id"])]

        def _save(**changes):
            # The rider's state (their own folder, this lock): written only
            # by a run that read the rider's window, and only on a change.
            nonlocal state
            new = {**state, **changes}
            if managed and new != state:
                _write_state(profile_id, new, today)
                state = new

        # F3: record how far this run puts events, before sending them (a
        # response lost after intervals.icu applied the batch still counts as
        # pushed). Only a window longer than the default writes a record, so
        # a rider who never chose one never has one (grill M1). QA C9b: what
        # this run saw of the rider's moves is kept with it, so a failed run
        # or another rider's run never erases it.
        extend = (managed and desired and horizon_days > HORIZON_DAYS
                  and (until is None or last_day > until))
        if extend:
            until = last_day
        _save(**({"pushed_until": last_day, "sent": {}} if extend else {}),
              held_days=result["held_days"], own_moved=result["own_moved"])

        def _new_or_redated(evs):
            # Only what we put on a day it was not on: an unchanged re-push
            # must not hide a move the rider makes right after it.
            return [ev for ev in evs
                    if str((ours.get(ev["external_id"]) or {}).get("start_date_local") or "")[:10]
                    != ev["start_date_local"][:10]]

        def _count(evs):
            # G-G: counts from the local diff vs the pre-push GET.
            for ev in evs:
                prev = ours.get(ev["external_id"])
                if prev is None:
                    result["pushed"] += 1
                elif (str(prev.get("name") or "") != ev["name"]
                      or str(prev.get("start_date_local") or "") != ev["start_date_local"]
                      # X3b: an own ride carries no file; only a workout compares one.
                      or (ev.get("filename") and prev.get("filename")
                          and str(prev.get("filename")) != ev.get("filename"))
                      # An own ride's length and load (grill_sessions L10).
                      or any(ev.get(k) is not None and prev.get(k) is not None
                             and prev.get(k) != ev.get(k)
                             for k in ("moving_time", "icu_training_load"))):
                    result["updated"] += 1

        # ONE bulk upsert (idempotent: keyed by external_id, G1).
        if workouts:
            status, body = _http(
                "POST", f"athlete/{athlete_id}/events/bulk?upsert=true",
                payload=workouts)
            if 200 <= status < 300:
                note_pushed(_new_or_redated(workouts))
            if _scope_403(status, body):
                result["needs_reconnect"] = True
                return result
            if status == 0:
                result["error"] = "network"
                return result
            if not (200 <= status < 300):
                # 3.3.1 hotfix (B3b): a non-2xx here rejects the WHOLE batch
                # and stalls all syncing (ICU 422s the batch when ONE event
                # fails validation) — the body names the offending event/field,
                # so it must reach the log + the result.
                result["error"] = f"http_{status}"
                result["error_detail"] = _http_error_detail(
                    "bulk_upsert", status, body)
                return result
        desired_ids = {ev["external_id"] for ev in workouts}
        _count(workouts)

        # C2 (grill X2): own rides go in a POST of their own, after the
        # workouts. intervals.icu rejects a whole batch when one event fails
        # validation, and a rejected own ride must never keep the workouts
        # off the watch: it is skipped as own_rejected, and its event from an
        # earlier run is kept. QA P1 / L4: a ride that could not be sent (no
        # answer, a server error) is the run's error, as for the workouts,
        # and nothing is swept.
        own_kept = set()
        if own_events:
            status, body = _http(
                "POST", f"athlete/{athlete_id}/events/bulk?upsert=true",
                payload=own_events)
            if 200 <= status < 300:
                note_pushed(_new_or_redated(own_events))
                desired_ids |= {ev["external_id"] for ev in own_events}
                _count(own_events)
                result["own_moved"] = _moved([v for k, v in released_own.items()
                                              if k not in desired_ids])
                _save(own_moved=result["own_moved"])
            elif _scope_403(status, body):
                result["needs_reconnect"] = True
                return result
            elif status in (400, 422):
                _http_error_detail("own_upsert", status, body)
                own_kept = {ev["external_id"] for ev in own_events}
                result["skipped"] = result["skipped"] + [
                    {"day": ev["start_date_local"][:10], "reason": "own_rejected"}
                    for ev in own_events]
            else:
                result["error"] = "network" if status == 0 else f"http_{status}"
                if status:
                    result["error_detail"] = _http_error_detail("own_upsert", status, body)
                return result

        # Orphan sweep — ours minus (desired ∪ spared broken ∪ held). Skipped
        # entirely when the upsert failed (returns above); broken sessions
        # keep their prior event rather than losing it to a build hiccup —
        # while it is still their workout (#27).
        spared = set()
        for eid in broken_ids:
            ev = ours.get(eid)
            if ev is not None:
                day, n = eid.rsplit(":", 2)[-2:]
                if _still_this_workout(by_day[day][int(n)], ev, by_day):
                    spared.add(eid)
        # W1 (qa_code H1): an agreed move's event goes only once the session's
        # new id is in this upsert — while that id is held, a stand-in or
        # unbuildable, the rider's event is the only copy of the workout.
        kept = {eid for eid, new_id in agreed.items()
                if new_id not in desired_ids}
        # Own rides (C3): the event goes once every ride that may be its own
        # and that this run pushes is in the upsert; one beyond the window
        # is not in intervals.icu anyway (C4).
        kept |= {eid for eid, new_ids in agreed_own.items()
                 if any(i in own_ids and i not in desired_ids for i in new_ids)}
        # F3: past the window (a clean-up after a shorter window) also spare
        # a planner Send push made since (H1: while it is still on its day)
        # and a move the plan took over (M4).
        beyond = {eid for eid, ev in ours.items()
                  if _event_day(ev) > last_day
                  and (state["sent"].get(eid) == _event_day(ev).isoformat()
                       or _plan_took_over(eid, ev, by_day, profile_id, names))}
        protected = (desired_ids | spared | set(held) | set(held_own) | kept
                     | beyond | own_kept)
        orphans = [ev for eid, ev in ours.items() if eid not in protected]
        rows = _delete_rows(orphans, existing, today, reach)
        swept = True
        if rows:
            status, body = _http(
                "PUT", f"athlete/{athlete_id}/events/bulk-delete", payload=rows)
            swept = 200 <= status < 300
            if swept:
                result["deleted"] = len(rows)
            elif _scope_403(status, body):
                result["needs_reconnect"] = True
            else:
                # 3.3.1 hotfix (B3b): same body-excerpt treatment as the
                # upsert — "sweep_failed" alone was undiagnosable.
                result["error"] = "sweep_failed"
                result["error_detail"] = _http_error_detail(
                    "orphan_sweep", status, body)
        # F3: once a clean-up went through, the record shrinks to the
        # farthest event of ours it left out there (moved, copied, taken
        # over, sent), so later runs and sync off still reach those; back
        # within the default window there is no record any more. A failed
        # sweep keeps the record, so the next run retries.
        if managed and swept and until is not None and (
                until > last_day or horizon_days == HORIZON_DAYS):
            left = [d for ev in existing if isinstance(ev, dict)
                    and str(ev.get("external_id") or "") in protected
                    for d in [_event_day(ev)] if d is not None and last_day < d <= reach]
            new_until = max([last_day] + left)
            if new_until <= today + timedelta(days=HORIZON_DAYS):
                new_until = None
            _save(pushed_until=new_until,
                  sent=_sent_within(state["sent"], last_day, new_until))
        _log.info(
            "EVENT=icu_calendar_push pushed=%d updated=%d deleted=%d "
            "skipped=%d held=%d horizon=%dd reach=%dd",
            result["pushed"], result["updated"], result["deleted"],
            len(result["skipped"]), result["held"], horizon_days,
            (reach - today).days)
        return result
    except Exception as e:                      # G4: zero 500s, ever
        _log.exception("icu calendar reconcile failed")
        return _result(error=f"internal:{type(e).__name__}")


def sweep_all(horizon_days: "int | None" = None,
              profile_id: "str | None" = None) -> dict:
    """G-A: delete ALL of our pushed events dated [today, reach+7]; reach is
    the rider's window (None: their setting, F3), or further while the
    clean-up record says the mirror went further (a longer window, or a
    clean-up still pending).

    Used when the sync toggle turns OFF and (best-effort, BEFORE the token
    purge) on disconnect. Only our profile-prefixed events are touched —
    held rider moves and own rides included, also those moved up to 7 days
    past the reach (W7: toggle off means everything ours goes); the Send
    records are ignored and cleared with the record. Past days are never
    touched. ``profile_id`` binds the sweep to that rider (grill M2):
    {"error": "profile_changed"} when another is active once the lock is
    free. A failed sweep keeps the record, but nothing retries it while sync
    is off (grill L9: a disconnect while offline leaves up to 49 days of
    events of a 6-week rider). Waits up to 120 s for a running reconcile.
    Never raises."""
    return _one_at_a_time(_SWEEP_LOCK_TIMEOUT_S, _sweep_then_bump, horizon_days,
                          profile_id)


def _sweep_then_bump(horizon_days: "int | None",
                     profile_id: "str | None" = None) -> dict:
    global _SWEEP_GENERATION
    refused = False
    try:
        res = _sweep_all(horizon_days, profile_id)
        refused = res.get("error") == "profile_changed"   # it removed nothing
        return res
    finally:
        if not refused:
            _SWEEP_GENERATION += 1


def _sweep_all(horizon_days: "int | None", want_profile: "str | None" = None) -> dict:
    from profile_manager import ProfileManager
    try:
        pm = ProfileManager.get()
        profile_id = pm.active_id
        if not profile_id:
            return _result(error="no_active_profile")
        if want_profile is not None and profile_id != want_profile:
            return _result(error="profile_changed")
        athlete_id, err = _connection(pm)
        if err:
            return _result(error=err)
        today = clock.today()
        if horizon_days is None:
            horizon_days = _horizon_pref(profile_id) or HORIZON_DAYS
        state = _read_state(profile_id, today)
        until = state["pushed_until"]
        reach = max(today + timedelta(days=horizon_days), until or today)
        existing, err_res = _get_window_events(
            athlete_id, today, (reach - today).days, 0, _LOOKAHEAD_DAYS)
        if err_res is not None:
            return err_res
        ours = _ours_in_window(existing, profile_id, today)
        result = _result()
        rows = _delete_rows(list(ours.values()), existing, today, None)
        swept = True
        if rows:
            status, body = _http(
                "PUT", f"athlete/{athlete_id}/events/bulk-delete", payload=rows)
            swept = 200 <= status < 300
            if swept:
                result["deleted"] = len(rows)
            elif _scope_403(status, body):
                result["needs_reconnect"] = True
            else:
                result["error"] = "sweep_failed"
        if swept and state != _no_state():
            # Nothing of ours out there: no reach, no Send record, and no
            # move left to show or protect (QA-UX QX-M2).
            _write_state(profile_id, _no_state(), today)
        _log.info("EVENT=icu_calendar_sweep deleted=%d horizon=%dd reach=%dd",
                  result["deleted"], horizon_days, (reach - today).days)
        return result
    except Exception as e:
        _log.exception("icu calendar sweep failed")
        return _result(error=f"internal:{type(e).__name__}")
