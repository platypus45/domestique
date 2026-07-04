"""PART A (IP_PLAN_CONTINUITY, v3.1.0) — one-time root→profile plan adoption.

Contract tests GA1-GA6 + the A-LOCKED-1 discriminator cases, against
``app._adopt_legacy_root_plan`` with explicit tmp dirs (hermetic — no HOME,
no network; conftest's urlopen block stays king).

  GA1  root=EVENT / prof=STALE → adopted; STALE among prof .bak*; root
       retired to .pre-v3; /api/plan serves EVENT
  GA2  prof stamp newer → skip; root LEFT in place (support lever); stable
       across a second boot
  GA3  fresh install (no root) and already-adopted (.pre-v3) → zero
       filesystem writes by the adoption step
  GA4  corrupt root → skip + root intact (evidence), prof untouched
  GA5  multi-profile → writes land in the ACTIVE profile dir only; equal
       stamps in a multi-profile home → skip
  GA6  idempotent across 3 consecutive boots (adopt → no-op → no-op)
  +    stamp-beats-mtime (restamped-prof cohort case), equal-stamp
       single-profile adopts, zero-byte prof = missing, byte-identical →
       retire-only, retire-failure → no rotation loop, .bak collision /
       foreign-name semantics
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


# ── helpers ──────────────────────────────────────────────────────────────────

def _plan(marker: str, generated: str | None = "2026-06-01T10:00:00",
          stamp_key: str = "generated") -> dict:
    p = {"goal": {"type": "event", "event_name": marker}, "weeks": []}
    if generated is not None:
        p[stamp_key] = generated
    return p


def _write(path: Path, plan: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")


def _mk_dirs(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "dot" / "plans"
    prof = tmp_path / "dot" / "profiles" / "default" / "plans"
    root.mkdir(parents=True)
    prof.mkdir(parents=True)
    return root, prof


def _snapshot(*dirs: Path) -> dict:
    """{abs path: (bytes, mtime_ns)} over every file under the given dirs."""
    out: dict = {}
    for d in dirs:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (p.read_bytes(), st.st_mtime_ns)
    return out


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


NOW = time.time()
OLD_STAMP = "2026-05-01T09:00:00"
NEW_STAMP = "2026-07-01T18:30:00"


# ── GA1 — the tester's exact shape ───────────────────────────────────────────

def test_ga1_adopts_newer_root_over_stale_profile(tmp_path, monkeypatch):
    root, prof = _mk_dirs(tmp_path)
    event = _plan("EVENT", NEW_STAMP)
    stale = _plan("STALE", OLD_STAMP)
    _write(root / "current_plan.json", event)
    _write(prof / "current_plan.json", stale)

    status = app_module._adopt_legacy_root_plan(root, prof, multi_profile=False)
    assert status == "adopted"

    # Profile live file IS the event plan (byte-for-byte copy).
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "EVENT"
    # STALE preserved among the profile's .bak* rotation.
    baks = list(prof.glob("current_plan.json.bak*"))
    assert any(json.loads(b.read_text()).get("goal", {}).get("event_name") == "STALE"
               for b in baks), "clobbered profile snapshot must survive as .bak*"
    # Root retired to .pre-v3 (nothing deleted), live root gone.
    assert not (root / "current_plan.json").exists()
    latch = root / "current_plan.json.pre-v3"
    assert latch.exists()
    assert json.loads(latch.read_text())["goal"]["event_name"] == "EVENT"

    # Post-boot /api/plan serves EVENT (PLAN_DIR repointed at the profile —
    # what apply_training_dirs does before the adoption step at boot).
    monkeypatch.setattr(tp, "PLAN_DIR", prof)
    r = TestClient(app_module.app).get("/api/plan")
    assert r.status_code == 200
    assert r.json()["plan_json"]["goal"]["event_name"] == "EVENT"

    # Plan-alert channel: the adoption landed in the diag ring (the
    # PLAN_AUTO_RESTORED precedent) so the dashboard can toast it once.
    codes = [e.get("code") for e in app_module._diag_ring_snapshot(limit=50)]
    assert "E_PLAN_ADOPTED_FROM_ROOT" in codes


# ── GA2 — prof newer → skip, root LEFT (support lever) ──────────────────────

def test_ga2_profile_newer_skips_and_leaves_root(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("ROOT", OLD_STAMP))
    _write(prof / "current_plan.json", _plan("PROF", NEW_STAMP))

    before = _snapshot(root, prof)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "skipped"
    assert _snapshot(root, prof) == before, "skip must be write-free"
    assert (root / "current_plan.json").exists(), "GA2 DECIDED: leave skipped root"
    assert not (root / "current_plan.json.pre-v3").exists()

    # Second boot: same stable skip.
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "skipped"
    assert _snapshot(root, prof) == before


# ── GA3 — fresh install / already adopted: zero writes by the adoption step ─

def test_ga3_no_root_and_latch_are_write_free(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    _write(prof / "current_plan.json", _plan("PROF", NEW_STAMP))

    before = _snapshot(root, prof)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "no-root"
    assert _snapshot(root, prof) == before

    # Already adopted: latch present — hard no-op even with a fresh root file.
    _write(root / "current_plan.json.pre-v3", _plan("ADOPTED-EARLIER", OLD_STAMP))
    _write(root / "current_plan.json", _plan("DOWNGRADE-ERA", NEW_STAMP))
    before = _snapshot(root, prof)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched"
    assert _snapshot(root, prof) == before


# ── GA4 — corrupt root: skip + warn + leave evidence ─────────────────────────

def test_ga4_corrupt_root_left_in_place(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    (root / "current_plan.json").write_bytes(b"{ not json !!!")
    _write(prof / "current_plan.json", _plan("PROF", OLD_STAMP))

    before = _snapshot(root, prof)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "corrupt-root"
    assert _snapshot(root, prof) == before
    assert (root / "current_plan.json").read_bytes() == b"{ not json !!!"
    assert not (root / "current_plan.json.pre-v3").exists()


# ── GA5 — multi-profile: active-only writes + equal-stamp skip ──────────────

def test_ga5_multi_profile_active_only_and_equal_stamp_skip(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    other = tmp_path / "dot" / "profiles" / "second" / "plans"
    other.mkdir(parents=True)
    _write(other / "current_plan.json", _plan("OTHER", OLD_STAMP))

    # Equal stamps in a multi-profile home → skip.
    _write(root / "current_plan.json", _plan("ROOT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("PROF", NEW_STAMP))
    before = _snapshot(root, prof, other)
    assert app_module._adopt_legacy_root_plan(root, prof, multi_profile=True) == "skipped"
    assert _snapshot(root, prof, other) == before

    # Root stamp strictly newer → adopt, but ONLY the active profile dir moves.
    _write(root / "current_plan.json", _plan("ROOT2", "2026-07-02T08:00:00"))
    other_before = _snapshot(other)
    assert app_module._adopt_legacy_root_plan(root, prof, multi_profile=True) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "ROOT2"
    assert _snapshot(other) == other_before, "inactive profile dirs must be untouched"


# ── GA6 — idempotent across 3 boots ──────────────────────────────────────────

def test_ga6_three_boot_idempotency(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))

    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    after_first = _snapshot(root, prof)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched"
    assert _snapshot(root, prof) == after_first
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched"
    assert _snapshot(root, prof) == after_first


# ── A-LOCKED-1 discriminator cases ───────────────────────────────────────────

def test_stamp_beats_mtime_restamped_prof_cohort(tmp_path):
    """The live counterexample: the v4.1.1 boot rewrite restamps the profile
    file every 3.0.x boot, so prof mtime is NEWER while its stamp is OLDER.
    Stamp-primary must still adopt (mtime-primary would no-op the fix)."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))
    _set_mtime(root / "current_plan.json", NOW - 3600 * 24 * 30)  # a month old
    _set_mtime(prof / "current_plan.json", NOW)                   # restamped today

    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "EVENT"


def test_equal_stamps_single_profile_adopts(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("ROOT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("PROF", NEW_STAMP))
    assert app_module._adopt_legacy_root_plan(root, prof, multi_profile=False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "ROOT"


def test_mtime_fallback_only_when_stamp_missing(tmp_path):
    """No parseable stamp on the root side → mtime decides; tie → prof wins."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("ROOT", None))         # no stamp
    _write(prof / "current_plan.json", _plan("PROF", "not-a-date"))  # unparseable
    _set_mtime(root / "current_plan.json", NOW - 100)
    _set_mtime(prof / "current_plan.json", NOW - 200)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"

    # Tie (== to the second) → skip, prof wins (conservative).
    (root / "current_plan.json.pre-v3").unlink()  # clear the latch from the adopt above
    _write(root / "current_plan.json", _plan("ROOT2", None))
    _write(prof / "current_plan.json", _plan("PROF2", None))
    _set_mtime(root / "current_plan.json", int(NOW))
    _set_mtime(prof / "current_plan.json", int(NOW))
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "skipped"


def test_zero_byte_prof_treated_as_missing(tmp_path):
    """A zero-byte/unparseable profile plan must count as MISSING (else the
    v1.6.2 boot restore would resurrect a stale .bak over nothing)."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("ROOT", OLD_STAMP))  # even an OLD stamp
    (prof / "current_plan.json").write_bytes(b"")
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "ROOT"


def test_byte_identical_retires_without_copy_or_rotation(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    plan = _plan("SAME", NEW_STAMP)
    _write(root / "current_plan.json", plan)
    _write(prof / "current_plan.json", plan)

    assert app_module._adopt_legacy_root_plan(root, prof, False) == "retired-identical"
    assert not (root / "current_plan.json").exists()
    assert (root / "current_plan.json.pre-v3").exists()
    assert list(prof.glob("current_plan.json.bak*")) == [], "no rotation on retire-only"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "SAME"


def test_retire_failure_no_rotation_loop(tmp_path, monkeypatch):
    """FAULT INJECTION (b), HONEST version (evaluator M1/H2): copy succeeds,
    retire fails → 'adopted' + profile-side latch. The adopted plan is then
    MUTATED between boots (the same-boot stale-classification rewrite and any
    user edit do exactly this: bytes diverge, stamp preserved). Later boots
    must respect the profile-side latch — no re-adopt, no reverted edits, no
    .bak churn. A genuinely NEW root (different stamp) still adopts."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))

    real_replace = Path.replace

    def _flaky_replace(self, target):
        if str(target).endswith(".pre-v3"):
            raise OSError("simulated retire failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"

    assert (root / "current_plan.json").exists(), "retire failed → root remains"
    prof_latch = prof / "current_plan.json.adopted-from-root"
    assert prof_latch.exists(), "failed retire must write the profile-side latch"
    baks_after_first = sorted(p.name for p in prof.glob("current_plan.json.bak*"))
    assert baks_after_first, "first adoption must have rotated STALE to .bak"

    # The killer scenario (H2 PoC): the adopted plan gets MODIFIED (stale-
    # classification rewrite / user edit) — bytes differ, stamp unchanged.
    edited = _plan("EVENT", NEW_STAMP)
    edited["user_edit"] = True
    _write(prof / "current_plan.json", edited)
    edited_bytes = (prof / "current_plan.json").read_bytes()

    # Boots 2 + 3 with retire STILL failing: the profile-side latch must
    # short-circuit — user edits preserved, zero rotation, zero re-adopt.
    for _boot in (2, 3):
        assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched-prof"
        assert (prof / "current_plan.json").read_bytes() == edited_bytes, \
            "re-adopt reverted the user's edits (H2 regression)"
        assert sorted(p.name for p in prof.glob("current_plan.json.bak*")) == baks_after_first
        assert (root / "current_plan.json").exists()
        assert not (root / "current_plan.json.pre-v3").exists()

    # A genuinely NEW root plan (different stamp) must still adopt normally.
    monkeypatch.undo()
    _write(root / "current_plan.json", _plan("EVENT2", "2026-07-02T09:00:00"))
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "EVENT2"
    assert (root / "current_plan.json.pre-v3").exists()  # retire works now


def test_copy2_failure_total_abort(tmp_path, monkeypatch):
    """FAULT INJECTION (a): the root→prof copy2 raises mid-adoption → total
    abort. Prof live plan intact (its pre-rotation content also recoverable
    from .bak), root untouched, no latch written; the next boot retries and
    adopts cleanly."""
    import shutil as _shutil

    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))
    root_bytes = (root / "current_plan.json").read_bytes()
    stale_bytes = (prof / "current_plan.json").read_bytes()

    real_copy2 = _shutil.copy2

    def _flaky_copy2(src, dst, *a, **k):
        if Path(src) == root / "current_plan.json":
            raise OSError("simulated disk-full during adoption copy")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(_shutil, "copy2", _flaky_copy2)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "error"
    monkeypatch.undo()

    # Prof live plan intact; rotation (a safe prefix step) put a recoverable
    # copy in .bak; root untouched; no latch.
    assert (prof / "current_plan.json").read_bytes() == stale_bytes
    assert (prof / "current_plan.json.bak").read_bytes() == stale_bytes
    assert (root / "current_plan.json").read_bytes() == root_bytes
    assert not (root / "current_plan.json.pre-v3").exists()

    # Next boot retries cleanly.
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert (prof / "current_plan.json").read_bytes() == root_bytes
    assert (root / "current_plan.json.pre-v3").exists()


def test_root_bak_collision_noop_and_foreign_names_untouched(tmp_path):
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))
    _write(root / "current_plan.json.bak", _plan("HIST1", OLD_STAMP))
    _write(root / "current_plan.json.bak2", _plan("HIST2", OLD_STAMP))
    _write(root / "current_plan.json.bak-v206", _plan("FOREIGN", OLD_STAMP))

    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"

    # Rotation put STALE at prof/.bak → root's .bak collides → no-op (never
    # clobber profile-side history). .bak2 was free → copied. Foreign name
    # not copied, and every root-side .bak* stays in place untouched.
    assert json.loads((prof / "current_plan.json.bak").read_text())["goal"]["event_name"] == "STALE"
    assert json.loads((prof / "current_plan.json.bak2").read_text())["goal"]["event_name"] == "HIST2"
    assert not (prof / "current_plan.json.bak-v206").exists()
    for name in ("current_plan.json.bak", "current_plan.json.bak2",
                 "current_plan.json.bak-v206"):
        assert (root / name).exists()


def test_stamp_parser_reads_both_keys():
    assert app_module._plan_generated_stamp({"generated": "2026-07-01T18:30:00"}) \
        == datetime(2026, 7, 1, 18, 30)
    assert app_module._plan_generated_stamp({"generated_at": "2026-07-01"}) \
        == datetime(2026, 7, 1)
    # tp:523 pattern: 'generated' wins over legacy 'generated_at'.
    assert app_module._plan_generated_stamp(
        {"generated": "2026-07-02T00:00:00", "generated_at": "2020-01-01"}
    ) == datetime(2026, 7, 2)
    assert app_module._plan_generated_stamp({"generated": "garbage"}) is None
    assert app_module._plan_generated_stamp({}) is None


# ── OWNER HARDENING — exhaustive state matrix ────────────────────────────────
# Enumerates root×prof×latch×multi and asserts the EXACT outcome of every
# combination. The expectation table below re-states the A-LOCKED contract
# INDEPENDENTLY of the implementation — if the code and this table ever
# disagree, the contract wins and the code is wrong.

_STATES = ("missing", "corrupt", "zero", "older_stamp", "newer_stamp", "no_stamp")


def _materialize(state: str, path: Path, marker: str, mtime: float) -> None:
    if state == "missing":
        return
    if state == "corrupt":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json{", encoding="utf-8")
    elif state == "zero":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    else:
        stamp = {"older_stamp": OLD_STAMP, "newer_stamp": NEW_STAMP,
                 "no_stamp": None}[state]
        _write(path, _plan(marker, stamp))
    _set_mtime(path, mtime)


def _expected(root_state: str, prof_state: str, latch: bool,
              multi: bool) -> str:
    """Contract mirror. Root mtime is always set NEWER than prof's, so every
    mtime-fallback row resolves to 'root newer' → adopt."""
    if latch:
        return "latched"
    if root_state == "missing":
        return "no-root"
    if root_state in ("corrupt", "zero"):
        return "corrupt-root"          # unparseable root: skip + leave evidence
    if prof_state in ("missing", "corrupt", "zero"):
        return "adopted"               # unparseable prof counts as missing
    r_has, p_has = root_state != "no_stamp", prof_state != "no_stamp"
    if r_has and p_has:
        order = {"older_stamp": 0, "newer_stamp": 1}
        if order[root_state] > order[prof_state]:
            return "adopted"
        if order[root_state] < order[prof_state]:
            return "skipped"
        return "skipped" if multi else "adopted"   # equal stamps
    return "adopted"                   # mtime fallback, root mtime newer


def test_state_matrix_exhaustive(tmp_path):
    ran = 0
    for latch in (False, True):
        for multi in (False, True):
            if latch and multi:
                continue  # latch short-circuits before multi matters; one latch sweep suffices
            for rs in _STATES:
                for ps in _STATES:
                    case = tmp_path / f"m_{int(latch)}_{int(multi)}_{rs}_{ps}"
                    root, prof = case / "plans", case / "profiles" / "p" / "plans"
                    root.mkdir(parents=True)
                    prof.mkdir(parents=True)
                    _materialize(rs, root / "current_plan.json", "ROOT", NOW - 50)
                    _materialize(ps, prof / "current_plan.json", "PROF", NOW - 500)
                    if latch:
                        (root / "current_plan.json.pre-v3").write_text("{}")
                    before = _snapshot(case)

                    want = _expected(rs, ps, latch, multi)
                    got = app_module._adopt_legacy_root_plan(root, prof, multi_profile=multi)
                    assert got == want, (
                        f"root={rs} prof={ps} latch={latch} multi={multi}: "
                        f"want {want}, got {got}")

                    after = _snapshot(case)
                    if want in ("latched", "no-root", "corrupt-root", "skipped"):
                        # Skip family: byte-for-byte ZERO filesystem changes.
                        assert after == before, (
                            f"skip-family case mutated fs: root={rs} prof={ps} "
                            f"latch={latch} multi={multi}")
                    else:  # adopted
                        live = prof / "current_plan.json"
                        assert json.loads(live.read_text())["goal"]["event_name"] == "ROOT"
                        assert (root / "current_plan.json.pre-v3").exists()
                        assert not (root / "current_plan.json").exists()
                        # NEVER-DELETE: every pre-existing byte string still
                        # exists somewhere in the tree (identity may move).
                        surviving = {b for b, _ in after.values()}
                        for old_bytes, _ in before.values():
                            assert old_bytes in surviving, (
                                f"data lost in adopt: root={rs} prof={ps} multi={multi}")
                    ran += 1
    assert ran == 3 * len(_STATES) ** 2  # 108 enumerated cases


# ── OWNER HARDENING — real-home dry run (sandboxed copy, read-only source) ──

@pytest.mark.skipif(
    not (Path.home() / ".domestique" / "profiles").is_dir(),
    reason="no real ~/.domestique on this machine (CI)")
def test_real_home_dry_run(tmp_path):
    """Copy the dev machine's real ~/.domestique into a sandbox and run the
    adoption decision against it. Asserts SAFETY INVARIANTS (not verdicts —
    those depend on the machine): skip family ⇒ zero writes; adopt ⇒ no byte
    string lost. The real home is never opened for writing."""
    import shutil as _sh
    src = Path.home() / ".domestique"
    home = tmp_path / "dot"
    _sh.copytree(src, home, ignore=_sh.ignore_patterns(
        "*.db", "*.db-shm", "*.db-wal", "rides", "wellness", "__pycache__"))

    reg = home / "profiles.json"
    active = "default"
    if reg.exists():
        try:
            active = (json.loads(reg.read_text()).get("active_profile")
                      or "default")
        except Exception:
            pass
    root = home / "plans"
    prof = home / "profiles" / active / "plans"
    if not root.is_dir() or not prof.is_dir():
        pytest.skip("home lacks the two-era plan layout")
    profiles_dir = home / "profiles"
    multi = len([d for d in profiles_dir.iterdir() if d.is_dir()]) > 1

    before = _snapshot(root, prof)
    status = app_module._adopt_legacy_root_plan(root, prof, multi_profile=multi)
    after = _snapshot(root, prof)

    if status in ("latched", "no-root", "corrupt-root", "skipped", "error"):
        assert after == before, f"dry-run status={status} but fs changed"
    else:
        surviving = {b for b, _ in after.values()}
        for old_bytes, _ in before.values():
            assert old_bytes in surviving, f"dry-run {status} lost data"


def test_adopt_with_full_prof_bak_chain_ages_out_oldest(tmp_path):
    """Evaluator M2: the 2.1.0-migration cohort can hold a FULL profile-side
    .bak chain. Adoption rotates like any plan write: live + recent history
    preserved, the OLDEST (.bak7) ages off the end — standard depth policy,
    asserted here so the behavior is documented, not accidental."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))
    _write(prof / "current_plan.json.bak", _plan("B1", OLD_STAMP))
    for n in range(2, tp.PLAN_BACKUP_DEPTH + 1):
        _write(prof / f"current_plan.json.bak{n}", _plan(f"B{n}", OLD_STAMP))

    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"

    live = json.loads((prof / "current_plan.json").read_text())
    assert live["goal"]["event_name"] == "EVENT"
    # Chain shifted down: STALE → .bak, B1 → .bak2, ..., B6 → .bak7; the
    # previous .bak7 (B7) aged out — the ONLY loss, per rotation policy.
    assert json.loads((prof / "current_plan.json.bak").read_text())["goal"]["event_name"] == "STALE"
    assert json.loads((prof / "current_plan.json.bak2").read_text())["goal"]["event_name"] == "B1"
    assert json.loads(
        (prof / f"current_plan.json.bak{tp.PLAN_BACKUP_DEPTH}").read_text()
    )["goal"]["event_name"] == f"B{tp.PLAN_BACKUP_DEPTH - 1}"


def test_prof_latch_stampless_hash_no_collision(tmp_path, monkeypatch):
    """Evaluator re-verify LOW-1: a stampless root latches on its CONTENT
    hash — a DIFFERENT stampless root must adopt, not inherit the latch."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("STAMPLESS-A", None))
    _write(prof / "current_plan.json", _plan("PROF", None))
    _set_mtime(root / "current_plan.json", NOW - 10)   # mtime fallback: root newer
    _set_mtime(prof / "current_plan.json", NOW - 900)

    real_replace = Path.replace

    def _no_retire(self, target):
        if str(target).endswith(".pre-v3"):
            raise OSError("retire blocked")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _no_retire)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    latch_txt = (prof / "current_plan.json.adopted-from-root").read_text()
    assert latch_txt.startswith("sha256:"), "stampless latch must be a content hash"

    # Same stampless root again → latched (hash matches).
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched-prof"

    # A DIFFERENT stampless root → must adopt, not wrong-skip.
    _write(root / "current_plan.json", _plan("STAMPLESS-B", None))
    _set_mtime(root / "current_plan.json", NOW - 5)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "STAMPLESS-B"


def test_prof_latch_ignored_when_prof_plan_missing(tmp_path, monkeypatch):
    """Evaluator re-verify LOW-2: the GA2 support lever survives the latch —
    delete the profile plan + reboot ⇒ the root is re-adopted."""
    root, prof = _mk_dirs(tmp_path)
    _write(root / "current_plan.json", _plan("EVENT", NEW_STAMP))
    _write(prof / "current_plan.json", _plan("STALE", OLD_STAMP))

    real_replace = Path.replace

    def _no_retire(self, target):
        if str(target).endswith(".pre-v3"):
            raise OSError("retire blocked")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _no_retire)
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert (prof / "current_plan.json.adopted-from-root").exists()
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "latched-prof"

    # Support lever: profile plan removed (user/support) → latch is moot.
    (prof / "current_plan.json").unlink()
    assert app_module._adopt_legacy_root_plan(root, prof, False) == "adopted"
    assert json.loads((prof / "current_plan.json").read_text())["goal"]["event_name"] == "EVENT"
