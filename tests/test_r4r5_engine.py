"""R4/R5 engine wave (2026-07-07) — slot/file coherence + SS intensity gate
+ recalc availability clamp (A8) + day-after glycolytic demotion (R5).

Grill verdict: /tmp/GRILL_R4R5.md against IP_TUESDAY_WAVE.md R4/R5.

  R4b  facts schema v2 (t101/l101, sustained runs at >=1.01 FTP) + the
       SS/tempo D3 row gains `l101 < 300`. Kills the incident class (3x16min
       @1.03 FTP served on a SWEET SPOT card) at the source: the file rode
       the threshold-class fallback (51% of the SS pool) and every pre-v2
       facts field was blind to a 1.03 block. The live incident file was
       amended to 0.98 by the W' wave (1de456dd), so GA2 runs on SYNTHETIC
       1.03-sustained bodies here.
  R4a  slot/file coherence invariant at the four plan tails: trip when
       |file_dur − slot| > max(0.08×slot, 3) + 5; fix by rematch at the slot
       duration (exact_duration); residuals are DOWN-only re-stamped —
       up-stamping is forbidden (grill A1: both oscillation modes are
       up-stamp modes); file>slot residuals keep file + slot and narrate.
  A8   recalculate_plan gains the 3.2.3-style authoritative availability
       clamp (it was the ONE tail with none), BEFORE the coherence pass.
  R5   yesterday's stored-envelope time_in_zone z6+z7 >= 480s AND today hard
       → one notch via _drop_intensity, below G2 in the first-match-wins
       ladder; revert mirrors the C6 DFA auto-swap flag.

Hermetic: synthetic-library tests run in tmp dirs (monkeypatched
WORKOUT_DIR); full-library tests use the committed caches read-only via the
pinned planner env (conftest W8).
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import training_planner as tp  # noqa: E402
import workout_facts as wf  # noqa: E402

from conftest import (  # noqa: E402
    PLANNER_PIN_ANCHOR as _ANCHOR,
    PLANNER_PIN_ARGS as _PIN_ARGS,
)

WK = ROOT / "workouts"


@pytest.fixture(scope="module", autouse=True)
def _pinned_env(planner_pinned_env):
    """W8 pin: frozen date + stubbed ICU fetch (see conftest)."""
    yield


# ── synthetic ZWO helpers (same shapes as test_workout_facts) ────────────────

def _zwo(name: str, body: str) -> str:
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n<workout_file>\n"
        "    <author>t</author>\n"
        f"    <name>{name}</name>\n"
        "    <description>t</description>\n"
        "    <sportType>bike</sportType>\n"
        "    <workout>\n" + body + "    </workout>\n</workout_file>"
    )


STEADY = '        <SteadyState Duration="{d}" Power="{p}" />\n'
WARMUP = '        <Warmup Duration="{d}" PowerLow="{lo}" PowerHigh="{hi}" />\n'


def _mklib(tmp: Path, files: dict[str, str]) -> Path:
    lib = tmp / "workouts"
    lib.mkdir(exist_ok=True)
    for fn, content in files.items():
        (lib / fn).write_text(content, encoding="utf-8")
    return lib


# ═════════════════════════════════════════════════════════════════════════════
# R4b — facts schema v2 + the SS/tempo sustained-supra ceiling (GA2, A4/A5)
# ═════════════════════════════════════════════════════════════════════════════

# 3×16min @1.03 with 5-min floats — the incident body, synthesized (the live
# file was amended to 0.98 in 1de456dd, so GA2 needs this synthetic row).
_SUSTAINED_103 = _zwo(
    "T3x16@103",
    WARMUP.format(d=600, lo=0.4, hi=0.7)
    + (STEADY.format(d=960, p=1.03) + STEADY.format(d=300, p=0.5)) * 3,
)
# Same protocol at 0.98 — the amended incident file's shape: must stay in.
_SUSTAINED_098 = _zwo(
    "T3x16@98",
    WARMUP.format(d=600, lo=0.4, hi=0.7)
    + (STEADY.format(d=960, p=0.98) + STEADY.format(d=300, p=0.5)) * 3,
)
# Legit SS body with ONE 180s supra surge (strides/opener class, grill P3:
# ≤180s surges on legit SS files must ALL be retained at the 300s ceiling).
_SURGE_180 = _zwo(
    "SS+surge180",
    WARMUP.format(d=600, lo=0.4, hi=0.7)
    + STEADY.format(d=1200, p=0.90)
    + STEADY.format(d=180, p=1.05)
    + STEADY.format(d=1200, p=0.90),
)
# 1.00-exact sustained block: strictly-above-FTP semantics keep it admissible
# (the 0.95-1.00 steady-work end of the SS contract).
_EXACT_100 = _zwo(
    "SS@100",
    WARMUP.format(d=600, lo=0.4, hi=0.7) + STEADY.format(d=1200, p=1.00),
)

_R4B_FILES = {
    "threshold_3x16_sustained103_73min.zwo": _SUSTAINED_103,
    "threshold_3x16_sub098_73min.zwo": _SUSTAINED_098,
    "sweetspot_surge180_43min.zwo": _SURGE_180,
    "sweetspot_exact100_30min.zwo": _EXACT_100,
}


@pytest.fixture()
def r4b_lib(tmp_path, monkeypatch):
    lib = _mklib(tmp_path, _R4B_FILES)
    wf.reset_cache()
    monkeypatch.setattr(tp, "WORKOUT_DIR", lib)
    yield lib
    wf.reset_cache()


def _row(fn: str, score: int = 7, if_: float = 0.8) -> dict:
    return {"File": fn, "IF": if_, "Score": score}


def test_facts_v2_t101_l101_semantics(r4b_lib):
    facts = wf.ensure_facts(r4b_lib)
    f103 = facts["threshold_3x16_sustained103_73min.zwo"]
    # 3 contiguous 960s runs at >=1.01, broken by 0.5 floats.
    assert f103["l101"] == 960 and f103["t101"] == 2880
    # every pre-v2 field is blind to the 1.03 block (the R4b rationale)
    assert f103["t130"] == 0 and f103["t150"] == 0 and f103["t200"] == 0
    f098 = facts["threshold_3x16_sub098_73min.zwo"]
    assert f098["l101"] == 0 and f098["t101"] == 0
    fsur = facts["sweetspot_surge180_43min.zwo"]
    assert fsur["l101"] == 180  # single surge, under the 300s ceiling
    f100 = facts["sweetspot_exact100_30min.zwo"]
    assert f100["l101"] == 0    # 1.00-exact is NOT strictly above FTP


def test_sustained_103_blocked_for_ss_and_tempo_fine_for_threshold(r4b_lib):
    """GA2 (synthetic): the incident body is inadmissible on SS AND tempo
    (shared contract row, grill A5) but the threshold contract is unchanged."""
    wf.ensure_facts(r4b_lib)
    row = _row("threshold_3x16_sustained103_73min.zwo")
    assert tp.file_admissible("sweetspot", row) is False
    assert tp.file_admissible("tempo", row) is False
    assert tp.file_admissible("threshold", row) is True   # t240==t200==0
    assert tp.file_admissible("overunder", row) is True


def test_sub_098_and_brief_surge_and_exact_100_stay_admissible(r4b_lib):
    """GA2 negatives: the amended (0.98) protocol, a ≤180s surge and a
    1.00-exact block all stay SS/tempo-admissible (grill P3 false-positive
    contract)."""
    wf.ensure_facts(r4b_lib)
    for fn in ("threshold_3x16_sub098_73min.zwo",
               "sweetspot_surge180_43min.zwo",
               "sweetspot_exact100_30min.zwo"):
        assert tp.file_admissible("sweetspot", _row(fn)) is True, fn
        assert tp.file_admissible("tempo", _row(fn)) is True, fn


@pytest.mark.skipif(not (WK / wf.FACTS_FILENAME).exists(),
                    reason="facts cache absent")
def test_committed_facts_cache_is_schema_v2():
    """The shipped cache must be the v2 rebuild (users never pay the ~45s
    offline rebuild; a v1 cache would be dropped whole at load)."""
    payload = json.loads((WK / wf.FACTS_FILENAME).read_text(encoding="utf-8"))
    assert payload.get("version") == 2
    rows = payload.get("facts") or {}
    assert rows, "committed cache is empty"
    non_null = [r for r in rows.values() if not r.get("null")]
    assert non_null and all("l101" in r and "t101" in r for r in non_null)


# ═════════════════════════════════════════════════════════════════════════════
# R4a — slot/file coherence pass (GA1 per A7, A1-A3)
# ═════════════════════════════════════════════════════════════════════════════

def _mk_session(day: date, session_type: str, duration_min: int,
                zwo_file: str = "", **kw) -> tp.PlannedSession:
    s = tp.PlannedSession(
        day=day, day_name=day.strftime("%a"), session_type=session_type,
        duration_min=duration_min,
        tss_estimate=round(duration_min / 60
                           * tp.TSS_PER_HOUR.get(session_type, 60)),
        description=f"{session_type} ({duration_min}min)",
        zwo_file=zwo_file, zwo_name=zwo_file.rsplit(".", 1)[0] if zwo_file else "",
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _mk_week(sessions: list, start: date | None = None) -> tp.PlannedWeek:
    start = start or _ANCHOR
    return tp.PlannedWeek(
        week_num=1, start=start, end=start + timedelta(days=6),
        phase="build1", tss_target=400, is_stepback=False, sessions=sessions,
    )


@pytest.fixture(scope="module")
def library():
    return tp.load_workout_library()


def _dur_of(library, fn: str) -> float:
    return next(float(r.get("Duration(min)") or 0)
                for r in library if r.get("File") == fn)


def _pick_row(library, slot: str, lo: float, hi: float) -> dict:
    """A real library row admissible for `slot` with duration in [lo, hi]."""
    cats = {tp._TYPE_TO_CONTENT_CLASS[slot], *tp._TYPE_TO_FALLBACK_CLASSES[slot]}
    for r in library:
        cc = (r.get("ContentClass") or "").strip()
        d = float(r.get("Duration(min)") or 0)
        if (cc in cats and lo <= d <= hi and (r.get("Score") or 0) >= 5
                and "ftp_test" not in {t.lower() for t in (r.get("Tags") or [])}
                and tp.file_admissible(slot, r)):
            return r
    pytest.skip(f"no library row for {slot} in [{lo},{hi}]")


def test_oversized_file_is_rematched_never_upstamped(library):
    """The incident pairing: a 90-min SWEET SPOT slot holding a ~118-min file.
    Full library → the rematch lands an in-band file; the slot duration is
    NEVER raised (up-stamp forbidden, grill A1)."""
    row = _pick_row(library, "sweetspot", 112, 130)
    s = _mk_session(_ANCHOR + timedelta(days=1), "sweetspot", 90,
                    zwo_file=row["File"])
    week = _mk_week([s])
    stats = tp._enforce_slot_file_coherence([week], library,
                                            plan_start_date=_ANCHOR)
    assert stats["trips"] == 1
    assert s.duration_min == 90, "coherence pass must never up-stamp the slot"
    assert s.zwo_file, "slot must keep a file"
    fd = _dur_of(library, s.zwo_file)
    assert abs(fd - 90) <= tp._slot_file_band_min(90), \
        f"rematch left an out-of-band file: {s.zwo_file} ({fd}min)"
    assert stats["rematched"] == 1


def test_rematch_failure_keeps_file_and_slot_and_logs(library, caplog):
    """Singleton library = rematch can only return the same oversized file
    (A3 sparse-cell collapse) → file>slot residual: keep file, keep slot,
    narrate. Never up-stamp, never wipe the file."""
    row = _pick_row(library, "sweetspot", 112, 130)
    s = _mk_session(_ANCHOR + timedelta(days=1), "sweetspot", 90,
                    zwo_file=row["File"])
    week = _mk_week([s])
    with caplog.at_level("INFO", logger="training_planner"):
        stats = tp._enforce_slot_file_coherence([week], [row],
                                                plan_start_date=_ANCHOR)
    assert stats["trips"] == 1 and stats["kept_narrated"] == 1
    assert s.duration_min == 90              # up-stamp forbidden
    assert s.zwo_file == row["File"]         # file kept
    assert any("R4a coherence" in r.message for r in caplog.records)


def test_short_file_on_structured_slot_downstamps_down_only(library):
    """Residual file<slot on a STRUCTURED slot: DOWN-only re-stamp — the card
    claims only the minutes the file holds; TSS rescales proportionally."""
    row = _pick_row(library, "vo2max", 38, 52)
    fd = float(row.get("Duration(min)") or 0)
    s = _mk_session(_ANCHOR + timedelta(days=1), "vo2max", 75,
                    zwo_file=row["File"])
    tss_before = s.tss_estimate
    week = _mk_week([s])
    stats = tp._enforce_slot_file_coherence([week], [row],
                                            plan_start_date=_ANCHOR)
    assert stats["restamped_down"] == 1
    assert s.duration_min == int(round(fd))
    assert s.duration_min < 75 and s.tss_estimate < tss_before


def test_short_file_on_easy_slot_is_kept_extend_on_trainer(library):
    """Easy types are EXEMPT from the down-stamp: slot>file is the documented
    extend-on-trainer contract (v1.3.4 coverage fallback + long-ride growth) —
    down-stamping would destroy the event long-ride progression."""
    row = _pick_row(library, "z2", 85, 115)
    s = _mk_session(_ANCHOR + timedelta(days=5), "z2", 180,
                    zwo_file=row["File"])
    week = _mk_week([s])
    stats = tp._enforce_slot_file_coherence([week], [row],
                                            plan_start_date=_ANCHOR)
    assert stats["trips"] == 1 and stats["kept_narrated"] == 1
    assert s.duration_min == 180 and s.zwo_file == row["File"]


def test_coherence_rematch_preserves_content_class(library):
    """The rematch pool is restricted to the outgoing file's content class:
    the variety floor passes install stimulus BY CLASS and the tail
    TYPE_CEILING clamp shrinks some of those slots (anaerobic 50 vs 55-76min
    files) — a class-blind rematch voided the build2/peak anaerobic floor
    (measured 2 → 0 on pinned seed 12345). Whatever the outcome (rematch or
    A3 keep), the served class must not drift."""
    cats = {"anaerobic", "vo2_short", "neuromuscular"}
    row = next((r for r in library
                if (r.get("ContentClass") or "").strip() == "anaerobic"
                and 55 <= float(r.get("Duration(min)") or 0) <= 76
                and (r.get("Score") or 0) >= 5), None)
    if row is None:
        pytest.skip("no 55-76min anaerobic row in library")
    s = _mk_session(_ANCHOR + timedelta(days=2), "vo2max", 50,
                    zwo_file=row["File"])
    week = _mk_week([s])
    stats = tp._enforce_slot_file_coherence([week], library,
                                            plan_start_date=_ANCHOR)
    assert stats["trips"] == 1
    assert s.zwo_file, "slot must keep a file"
    cc_after = tp._content_class_for_zwo(s.zwo_file)
    assert cc_after == "anaerobic", (
        f"coherence rematch drifted the stimulus class: anaerobic → "
        f"{cc_after} ({s.zwo_file})")
    assert cc_after in cats  # sanity: still a hard class


def test_coherence_guards_skip_protected_sessions(library):
    """A2 guards: non-pending / user-moved / adapted / opener / race sessions
    are untouchable even when wildly out of band."""
    row = _pick_row(library, "sweetspot", 112, 130)
    day = _ANCHOR + timedelta(days=1)
    protected = [
        _mk_session(day, "sweetspot", 90, zwo_file=row["File"], status="done"),
        _mk_session(day, "sweetspot", 90, zwo_file=row["File"], user_moved=True),
        _mk_session(day, "sweetspot", 90, zwo_file=row["File"], adapted=True),
        _mk_session(day, "sweetspot", 90, zwo_file=row["File"], is_opener=True),
        _mk_session(day, "sweetspot", 90, zwo_file=row["File"],
                    dismissed_at="2026-01-05T10:00:00"),
    ]
    snap = [(s.zwo_file, s.duration_min) for s in protected]
    stats = tp._enforce_slot_file_coherence([_mk_week(protected)], library,
                                            plan_start_date=_ANCHOR)
    assert stats["trips"] == 0
    assert [(s.zwo_file, s.duration_min) for s in protected] == snap


_GA1_CACHE: dict = {}


def _pinned_plan(seed: int):
    if seed not in _GA1_CACHE:
        goal = tp.Goal(
            goal_type="general",
            target_date=_ANCHOR + timedelta(weeks=24),
            hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=3.5,
            available_days=list(range(7)), rest_days=[], daily_max_hours={},
            plan_weeks=24,
        )
        _GA1_CACHE[seed] = tp.generate_plan(goal, seed_salt=seed, **_PIN_ARGS)
    return _GA1_CACHE[seed]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_ga1_pinned_plans_have_zero_out_of_band_pending(seed, library):
    """GA1 (per grill A7): the pinned 24w plans contain ZERO pending sessions
    whose file leaves the band — measured 28/28 trips fixed by rematch on
    these exact seeds (residual narrated keeps would be file>slot or easy
    extend-on-trainer; none occur here)."""
    _phases, weeks = _pinned_plan(seed)
    durmap = {r["File"]: float(r.get("Duration(min)") or 0)
              for r in library if r.get("File")}
    offenders = []
    for w in weeks:
        for s in w.sessions:
            if s is None or s.session_type == "rest":
                continue
            if getattr(s, "status", "pending") != "pending":
                continue
            if tp._protect_race(s) or getattr(s, "is_opener", False):
                continue
            fn = getattr(s, "zwo_file", "") or ""
            slot = float(s.duration_min or 0)
            if not fn or fn not in durmap or slot <= 0:
                continue
            if abs(durmap[fn] - slot) > tp._slot_file_band_min(slot):
                offenders.append((w.week_num, s.day.isoformat(),
                                  s.session_type, slot, fn, durmap[fn]))
    assert offenders == [], f"out-of-band pending sessions: {offenders}"


def test_coherence_pass_is_a_fixpoint(library):
    """Down-only + last-position = monotone single pass (grill P5): a second
    application over an already-swept plan finds zero trips."""
    _phases, weeks = _pinned_plan(0)
    weeks2 = copy.deepcopy(weeks)
    stats = tp._enforce_slot_file_coherence(weeks2, library,
                                            plan_start_date=weeks2[0].start,
                                            seed_salt=0)
    assert stats["trips"] == 0, f"second application not a fixpoint: {stats}"


# ═════════════════════════════════════════════════════════════════════════════
# A8 — recalculate_plan availability clamp (day-cap invariant)
# ═════════════════════════════════════════════════════════════════════════════

_ATHLETE = {"ftp": 250, "weight_kg": 70}


@pytest.fixture(scope="module")
def recalc_result(library):
    """Tight availability (1.0h weekdays) so the invariant bites: pre-A8 the
    recalc tail had NO final clamp (sampler sweeps only) and floor/long-ride
    passes could leave sessions over their day cap."""
    goal = tp.Goal(
        goal_type="event",
        plan_weeks=20,
        target_date=_ANCHOR + timedelta(weeks=20),
        event_km=160, event_climb_m=2000, event_type="gran_fondo",
        hours_per_week=8.0, max_weekday_hours=1.0, max_weekend_hours=3.0,
        available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
    )
    _phases, weeks = tp.generate_plan(goal, athlete=_ATHLETE, **_PIN_ARGS)
    new_phases, all_weeks, info = tp.recalculate_plan(
        goal, weeks, current_ctl=42.0, athlete=_ATHLETE)
    return goal, new_phases, all_weeks, info


def test_recalc_day_cap_invariant(recalc_result):
    """A8: every rebuilt (future, pending, non-race) session respects
    min(per-weekday goal cap, TYPE_CEILING, stepback long-ride cap) — the
    exact formula generate_plan/regen already enforce at their tails."""
    goal, _np, all_weeks, info = recalc_result
    assert info.get("action") == "recalculated", \
        "fixture must exercise the rebuild path"
    offenders = []
    for w in all_weeks:
        if w.start <= _ANCHOR:  # past/current week — preserved verbatim
            continue
        for s in w.sessions:
            if s is None or s.session_type == "rest" or (s.duration_min or 0) <= 0:
                continue
            if tp._protect_race(s):
                continue
            if (getattr(s, "user_moved", False) or getattr(s, "dismissed_at", "")
                    or getattr(s, "status", "pending") != "pending"):
                continue
            wd = s.day.weekday()
            cap = int(goal.max_hours_for_day(wd) * 60)
            cc = tp._content_class_for_zwo(getattr(s, "zwo_file", "") or "")
            ceil = tp.TYPE_CEILING.get(cc) or tp.TYPE_CEILING.get(s.session_type)
            eff = cap if ceil is None else (ceil if cap <= 0 else min(cap, ceil))
            if getattr(w, "is_stepback", False) and (
                    eff <= 0 or eff > tp.STEPBACK_LONG_RIDE_CAP_MIN):
                eff = tp.STEPBACK_LONG_RIDE_CAP_MIN
            if eff > 0 and s.duration_min > eff:
                offenders.append((w.week_num, s.day.isoformat(),
                                  s.session_type, s.duration_min, eff))
    assert offenders == [], f"recalc emitted over-cap sessions: {offenders}"


def test_recalc_output_is_slot_file_coherent(recalc_result, library):
    """The A8 clamp shrinks slots in place (creating file>slot decouplings);
    the coherence pass runs AFTER it, so the recalc's emitted future weeks
    carry no out-of-band pending sessions either."""
    _goal, _np, all_weeks, info = recalc_result
    assert info.get("action") == "recalculated"
    durmap = {r["File"]: float(r.get("Duration(min)") or 0)
              for r in library if r.get("File")}
    offenders = []
    for w in all_weeks:
        if w.start <= _ANCHOR:
            continue
        for s in w.sessions:
            if s is None or s.session_type == "rest":
                continue
            if getattr(s, "status", "pending") != "pending":
                continue
            if tp._protect_race(s) or getattr(s, "is_opener", False):
                continue
            fn = getattr(s, "zwo_file", "") or ""
            slot = float(s.duration_min or 0)
            if not fn or fn not in durmap or slot <= 0:
                continue
            fd = durmap[fn]
            if abs(fd - slot) <= tp._slot_file_band_min(slot):
                continue
            # A7: narrated residual classes are legitimate — file>slot
            # (rematch failure keeps the longer file) and the easy
            # extend-on-trainer gap (file<slot on z2/long_z2/recovery).
            if fd > slot or s.session_type in tp._COHERENCE_EASY_TYPES:
                continue
            offenders.append((w.week_num, s.day.isoformat(), s.session_type,
                              slot, fn, fd))
    assert offenders == [], f"non-narratable incoherence after recalc: {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# R5 — day-after glycolytic demotion (A6)
# ═════════════════════════════════════════════════════════════════════════════

def _fresh_readiness() -> dict:
    return {"score": 90, "dfa_cap": {}}


def _planned_hard(session_type: str = "threshold") -> tp.PlannedSession:
    d = date.today()
    return tp.PlannedSession(
        day=d, day_name=d.strftime("%A").lower(), session_type=session_type,
        duration_min=60,
        tss_estimate=round(tp.TSS_PER_HOUR.get(session_type, 75)),
        description=f"Planned {session_type} 60min",
    )


def _yesterday_iso() -> str:
    # tp.date is FROZEN by the module fixture (2026-01-05); the R5 helper
    # computes "yesterday" from tp.date.today(), so the fixture ride dates
    # must come from the same clock.
    return (tp.date.today() - timedelta(days=1)).isoformat()


def test_r5_720s_z67_demotes_one_notch_with_chip_reason():
    """720s of z6+z7 yesterday (the incident ride's class: low TSS, heavy
    glycolytic content) → one Seiler-ladder notch + the adjustment-chip
    reason; never to rest.

    3.4.1 M2 — the user-facing reason is ONE plain sentence ("Yesterday had
    12 minutes of very hard riding (Z6/Z7) — easing today to …"): no
    internal "Z6+Z7 ≥8"-style notation, and the demoted description carries
    only the type change (the banner's Now-line must not repeat the reason).
    ⑨b — this fixture is z6-DOMINANT (a VO2max day), so the wording must be
    the neutral "very hard riding", never "sprint intensity"."""
    planned = _planned_hard("threshold")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 700, "z7": 20}}],
        daily_log_today={},
    )
    new_type = tp._drop_intensity("threshold")
    assert adj.session_type == new_type
    assert adj.session_type not in ("rest",)
    assert adj.adapted is True
    assert "Yesterday had 12 minutes of very hard riding (Z6/Z7)" in reason
    assert "easing today to" in reason
    assert "sprint" not in reason, "z6-dominant day must not claim sprints"
    # No internal notation leaks into the user-visible strings.
    for leaked in ("≥", "Z6+Z7", "glycolytically", "→"):
        assert leaked not in reason, f"{leaked!r} leaked into reason"
    # Description = type change only — the reason lives ONLY in `reason`.
    assert adj.description == f"{new_type} (was threshold)"


def test_r5_reason_copy_tempo_case_matches_banner_sentence():
    """3.4.1 M2 — exact user-facing copy for the screenshot case (tempo day
    after a 12-min z6-dominant ride): one plain sentence with the
    display-cased Z2, and a description that carries only the type change."""
    planned = _planned_hard("tempo")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 700, "z7": 20}}],
        daily_log_today={},
    )
    assert adj.session_type == "z2"
    assert reason == ("Yesterday had 12 minutes of very hard riding (Z6/Z7) "
                      "— easing today to Z2")
    assert adj.description == "z2 (was tempo)"


def test_r5_z7_dominant_day_reads_sprint_intensity():
    """⑨b — "sprint intensity" is reserved for a genuinely z7-dominant dose
    (z7 seconds > half the z6+z7 total); anything else is "very hard
    riding (Z6/Z7)"."""
    planned = _planned_hard("tempo")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 100, "z7": 500}}],
        daily_log_today={},
    )
    assert adj.session_type == "z2"
    assert reason == ("Yesterday had 10 minutes at sprint intensity "
                      "— easing today to Z2")


def test_r5_300s_z67_stays_untouched():
    """Below the 480s floor: no demotion, no reason (the gate must not flag
    ordinary rides with a little top-end)."""
    planned = _planned_hard("threshold")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 300}}],
        daily_log_today={},
    )
    assert adj.session_type == "threshold" and reason == ""


def test_r5_missing_time_in_zone_stays_untouched():
    """Power-less envelope (no time_in_zone) = no durable signal = no gate —
    the volatile intervals[] arm was DROPPED per grill A6, never substituted."""
    planned = _planned_hard("vo2max")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(), "tss": 57}],
        daily_log_today={},
    )
    assert adj.session_type == "vo2max" and reason == ""


def test_r5_easy_today_never_touched():
    """R5 is hard-slot-gated: a Z2 day after a glyco-heavy ride stays Z2."""
    d = date.today()
    planned = tp.PlannedSession(
        day=d, day_name=d.strftime("%A").lower(), session_type="z2",
        duration_min=60, tss_estimate=45, description="z2",
    )
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 900}}],
        daily_log_today={},
    )
    assert adj.session_type == "z2" and reason == ""


def test_r5_revert_flag_suppresses_demotion():
    """C6 mirror (the DFA auto-swap revert): once the rider clicked Revert,
    today's glyco demotion stays down until the flag clears at midnight."""
    planned = _planned_hard("threshold")
    readiness = dict(_fresh_readiness(), cap_reverted_today=True)
    adj, reason = tp.adjust_today_session(
        planned, readiness,
        rides_recent=[{"date": _yesterday_iso(),
                       "time_in_zone": {"z6": 700, "z7": 20}}],
        daily_log_today={},
    )
    assert adj.session_type == "threshold" and reason == ""


def test_r5_sits_below_g2_first_match_wins():
    """A G2-grade 48h dose (≥25min z5-z7) must take the STRONGER Z2 cap —
    R5 never runs when G2 fired (no double-demotion, grill A6 ordering).

    The ride arms BOTH gates at once: `date` = the frozen planner-yesterday
    (R5's calendar trigger, z6 600s ≥ 480) and `start_date_local` = 2h ago on
    the REAL clock (G2's rolling-48h window uses datetime.now(), which the
    fixture does not freeze). First-match-wins must hand the day to G2."""
    from datetime import datetime as _dt
    planned = _planned_hard("threshold")
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[{
            "date": _yesterday_iso(),
            "start_date_local": (_dt.now() - timedelta(hours=2)).isoformat(),
            "time_in_zone": {"z5": 1000, "z6": 600, "z7": 0},
        }],
        daily_log_today={},
    )
    assert adj.session_type == "z2"
    assert reason.startswith("G2"), f"expected G2 to own the day, got {reason!r}"
