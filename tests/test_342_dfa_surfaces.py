"""3.4.2 — DFA α1 surfaces: regression pins for the dead-archive read path.

ROOT CAUSE (owner report: "DFA α1 doesn't work — neither the DFA tab nor
the homepage; both need a loading progress bar"): the v3.0.0 AC2a profile
migration moved the ICU envelope archive to ``<profile>/rides/icu/`` but
left the two DFA READ sites scanning the legacy GLOBAL
``~/.domestique/rides/icu/`` — empty forever post-migration:

  * ``app._iter_icu_dfa_rides``   — feeds /api/profile/dfa-alpha1 (home
    Recovery card), /api/profile/dfa-rides (DFA tab) and the readiness DFA
    cap → every surface rendered its empty-state despite computed values
    sitting in the profile archive.
  * ``app._run_dfa_backfill_job`` — the backfill worker saw 0 files →
    ``total=0, candidates=0`` → instantly ``done`` → the frontend strip
    (``_renderDfaProgress``: terminal + candidates===0 → clear) rendered
    NOTHING, so neither surface ever showed a progress bar either.

The 3.4.1 loading-state work (759a5a8a) was NOT the regression — it sat on
top of a pipeline dead since v3.0.0; its always-open Recovery card just
made the emptiness visible.

Server pins here plant a DECOY envelope in the legacy global location and
assert both read sites surface ONLY the profile archive (the
``ride_storage._icu_rides_dir()`` seam the sync writer uses) — reverting to
``app._user_data_dir / "rides" / "icu"`` fails all three.

Node pins complete the {no rides, indexing, computed} × {home card, DFA
tab} render matrix started in test_341_loading_states.py (which covers
home×no-rides, home×indexing, and the tab throttle bypass).

Hermetic: tmp-dir archives + patched module seams + node harnesses over
template-extracted functions. No network, no real HOME reads.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as _app_mod
import ride_storage as _rs_mod

from test_341_loading_states import (  # noqa: F401 — shared node harness
    _ESC_STUB,
    _EL_STUB,
    _extract_js_function,
    _run_node,
    _src,
    needs_node,
)


def _seed_envelope(dir_: Path, ext_id: str, *, status: str = "computed",
                   alpha: "float | None" = 0.93,
                   started_at: str = "2026-07-13T10:00:00") -> Path:
    """Flat ICU envelope exactly as the sync writer lays it down."""
    dir_.mkdir(parents=True, exist_ok=True)
    rec = {
        "external_id": ext_id,
        "started_at": started_at,
        "duration_s": 3600,
        "moving_s": 3500,
        "avg_hr": 141,
        "name": f"ride {ext_id}",
        "dfa_alpha1_status": status,
        "dfa_alpha1_avg": alpha,
        "dfa_algo_version": _app_mod._DFA_ALGO_VERSION,
    }
    p = dir_ / f"{ext_id}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


class TestDfaReadsProfileArchive(unittest.TestCase):
    """Both DFA read sites must scan the PROFILE archive, never the legacy
    global ``_user_data_dir / "rides" / "icu"`` (dead since v3.0.0 AC2a)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="dfa_342_"))
        # The real archive: what ride_storage._icu_rides_dir() resolves to.
        self._profile_icu = self._tmp / "profile" / "rides" / "icu"
        self._profile_icu.mkdir(parents=True, exist_ok=True)
        # The legacy global location, seeded with a DECOY that must never
        # surface. If a reader regresses to _user_data_dir, the decoy's
        # 9.99 α1 / extra file count leaks into the assertions below.
        self._global_root = self._tmp / "global_home"
        _seed_envelope(self._global_root / "rides" / "icu", "iDECOY",
                       alpha=9.99, started_at="2026-07-14T10:00:00")
        self._patches = [
            patch.object(_rs_mod, "_icu_rides_dir",
                         return_value=self._profile_icu),
            patch.object(_app_mod, "_user_data_dir", self._global_root),
            # dfa-alpha1 merges ride_storage.list_rides() (FIT imports) —
            # pin it empty so the test never touches a real profile.
            patch.object(_rs_mod, "list_rides", return_value=[]),
        ]
        for p in self._patches:
            p.start()
        with _app_mod._dfa_backfill_thread_lock:
            _app_mod._dfa_backfill_tasks.clear()
        _app_mod._dfa_backfill_cancel.clear()
        try:
            _app_mod._dfa_backfill_lock.release()
        except RuntimeError:
            pass
        self._client = TestClient(_app_mod.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        try:
            _app_mod._dfa_backfill_lock.release()
        except RuntimeError:
            pass

    def test_home_card_endpoint_reads_profile_archive(self):
        """/api/profile/dfa-alpha1 (home Recovery card) surfaces the profile
        archive's computed value — not null, not the global decoy."""
        _seed_envelope(self._profile_icu, "iREAL", alpha=0.93)
        d = self._client.get("/api/profile/dfa-alpha1").json()
        self.assertEqual(d["value"], 0.93)
        self.assertEqual(d["n_rides"], 1)
        self.assertGreaterEqual(d["n_recent_total"], 1)

    def test_tab_endpoint_reads_profile_archive(self):
        """/api/profile/dfa-rides (DFA tab) lists the profile archive's
        rides and counts ONLY them (decoy excluded)."""
        _seed_envelope(self._profile_icu, "iREAL", alpha=0.93)
        d = self._client.get("/api/profile/dfa-rides").json()
        self.assertEqual(d["n_total"], 1)
        self.assertEqual([r["id"] for r in d["rides"]], ["iREAL"])
        self.assertEqual(d["rides"][0]["alpha1_avg"], 0.93)

    def test_backfill_worker_scans_profile_archive(self):
        """POST /api/profile/dfa-backfill counts the profile archive's
        files (total) and its non-sticky rides (candidates) — the counts
        that drive the 'Updating yy of zz' progress strip. Before the fix
        this was total=0/candidates=0 → instant done → no bar ever."""
        _seed_envelope(self._profile_icu, "iSTICKY", status="computed",
                       alpha=0.93)
        _seed_envelope(self._profile_icu, "iRETRY", status="no_rr_data",
                       alpha=None)
        augmented_paths: list[Path] = []

        def _fake_augment(p, ext, force=False):
            augmented_paths.append(Path(p))

        with patch.object(_app_mod, "_augment_icu_record_with_dfa",
                          side_effect=_fake_augment):
            r = self._client.post("/api/profile/dfa-backfill").json()
            self.assertEqual(r["status"], "started")
            tid = r["task_id"]
            s = None
            for _ in range(100):
                s = self._client.get(
                    f"/api/profile/dfa-backfill/status?task_id={tid}").json()
                if s.get("state") in ("done", "cancelled", "error"):
                    break
                time.sleep(0.05)
        self.assertEqual(s["state"], "done")
        self.assertEqual(s["total"], 2, "decoy in the global dir counted!")
        self.assertEqual(s["candidates"], 1)
        self.assertEqual(s["augmented"], 1)
        self.assertEqual([p.name for p in augmented_paths], ["iRETRY.json"])
        # Direction pin: the worker walked the PROFILE archive.
        self.assertEqual(augmented_paths[0].parent, self._profile_icu)


# ═══ Node matrix — the cells test_341_loading_states doesn't cover ══════════


@needs_node
def test_home_card_computed_value_renders_without_sync_detour():
    """{computed × home card}: a non-null α1 renders the value line and the
    sync-progress branch is NEVER consulted (fetch throws if touched) — the
    data branch can't be shadowed by the 3.4.1 sync bar."""
    src = _src()
    fn = _extract_js_function(src, "_renderDfaCard")
    harness = _ESC_STUB + _EL_STUB + """
const fetch = async (url) => { throw new Error('unexpected fetch ' + url); };
const host = mkEl();
""" + fn + """
(async () => {
  await _renderDfaCard(host, { value: 1.08, n_rides: 3,
                               last_computed_at: '2026-07-13T09:00:00',
                               n_recent_total: 5 });
  if (host.innerHTML.indexOf('Last ride DFA α1') < 0 ||
      host.innerHTML.indexOf('1.08') < 0)
    throw new Error('computed value must render: ' + host.innerHTML);
  if (host.innerHTML.indexOf('Indexing rides') >= 0)
    throw new Error('sync bar must not shadow a computed value');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


def _load_dfa_tab_harness() -> str:
    """loadDfaTab extracted verbatim; renderers/pollers stubbed to recorders
    so the CONTROL FLOW (which branch fires per matrix cell) is what's
    under test."""
    src = _src()
    fn = "\n".join(_extract_js_function(src, n)
                   for n in ("loadDfaTab", "_icuSyncBarHtml"))
    return _ESC_STUB + _EL_STUB + """
let _dfaTabData = null, _dfaView = 'aggregate';
const _DFA_MIGRATE_LS_KEY = 'k', _DFA_MIGRATE_THROTTLE_MS = 600000;
const localStorage = { getItem: () => '0', setItem: () => {} };
const calls = { agg: 0, per: 0, view: 0, update: 0, migrate: 0, syncPoll: 0 };
const _renderDfaAggregate = () => { calls.agg++; };
const _renderDfaPerRide = () => { calls.per++; };
const setDfaView = () => { calls.view++; };
const _dfaTabUpdateProgress = () => { calls.update++; };
const _dfaRunMigrate = () => { calls.migrate++; };
const _pollIcuSyncInto = (host, onDone) => { calls.syncPoll++; };
const statusEl = mkEl(), aggEl = mkEl(), perEl = mkEl();
const document = { getElementById: (id) =>
  id === 'dfa-tab-status' ? statusEl :
  id === 'dfa-aggregate-host' ? aggEl :
  id === 'dfa-perride-host' ? perEl : null };
""" + fn


@needs_node
def test_dfa_tab_computed_data_renders_and_kicks_update_strip():
    """{computed × DFA tab}: rides present → renderers run, the update
    strip (real bar) is kicked, and /api/sync/progress is never consulted."""
    harness = _load_dfa_tab_harness() + """
const fetch = async (url) => {
  if (String(url).indexOf('/api/profile/dfa-rides') === 0)
    return { ok: true, json: async () => ({
      rides: [{ id: 'iA', alpha1_avg: 0.93 }], n_total: 3,
      n_computed: 3, n_stale_version: 0 }) };
  throw new Error('unexpected fetch ' + url);
};
(async () => {
  await loadDfaTab();
  if (calls.agg !== 1 || calls.per !== 1)
    throw new Error('renderers must run on data');
  if (calls.update !== 1)
    throw new Error('update strip (progress bar) must be kicked');
  if (calls.migrate !== 0 || calls.syncPoll !== 0)
    throw new Error('no migrate / sync detour on healthy data');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_dfa_tab_zero_rides_while_indexing_shows_sync_bar():
    """{indexing × DFA tab}: zero indexed rides + ICU sync running → the
    live sync bar renders into the status strip, the poller attaches, and
    the backfill/migrate kicks are DEFERRED (nothing to augment yet)."""
    harness = _load_dfa_tab_harness() + """
const fetch = async (url) => {
  if (String(url).indexOf('/api/profile/dfa-rides') === 0)
    return { ok: true, json: async () => ({
      rides: [], n_total: 0, n_computed: 0, n_stale_version: 0 }) };
  if (String(url).indexOf('/api/sync/progress') === 0)
    return { ok: true, json: async () => ({
      state: 'running', done: 9, total: 45 }) };
  throw new Error('unexpected fetch ' + url);
};
(async () => {
  await loadDfaTab();
  if (statusEl.innerHTML.indexOf('Indexing rides from intervals.icu') < 0 ||
      statusEl.innerHTML.indexOf('9 of 45') < 0)
    throw new Error('live sync bar must render: ' + statusEl.innerHTML);
  if (calls.syncPoll !== 1) throw new Error('sync poller must attach');
  if (calls.update !== 0 || calls.migrate !== 0)
    throw new Error('backfill/migrate must wait for the sync');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_dfa_tab_zero_rides_sync_idle_is_honest_empty():
    """{no rides × DFA tab}: zero rides + no sync running → no fake bar;
    falls through to the backfill kick (server-side 0 candidates → strip
    stays empty = the honest empty-state)."""
    harness = _load_dfa_tab_harness() + """
const fetch = async (url) => {
  if (String(url).indexOf('/api/profile/dfa-rides') === 0)
    return { ok: true, json: async () => ({
      rides: [], n_total: 0, n_computed: 0, n_stale_version: 0 }) };
  if (String(url).indexOf('/api/sync/progress') === 0)
    return { ok: true, json: async () => ({ state: 'idle' }) };
  throw new Error('unexpected fetch ' + url);
};
(async () => {
  await loadDfaTab();
  if (statusEl.writes.some(w => w.indexOf('Indexing rides') >= 0))
    throw new Error('no sync bar when nothing is syncing');
  if (calls.syncPoll !== 0) throw new Error('no poller on idle sync');
  if (calls.update !== 1)
    throw new Error('must still kick the (0-candidate) backfill');
  console.log('OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_progress_strip_renders_bar_for_real_candidates():
    """The 'Updating yy of zz' strip renders a REAL bar once the worker
    reports profile-archive candidates — and clears only for the terminal
    0-candidate case (the pre-fix permanent state)."""
    src = _src()
    fn = _extract_js_function(src, "_renderDfaProgress")
    harness = _ESC_STUB + _EL_STUB + fn + """
const host = mkEl();
_renderDfaProgress(host, { state: 'running', task_id: 't1',
                           augmented: 12, candidates: 67, computed: 9,
                           no_rr_data: 3, failed: 0, current: 'i99' });
if (host.innerHTML.indexOf('Updating 12 of 67') < 0 ||
    host.innerHTML.indexOf('lbar') < 0)
  throw new Error('running strip must show bar + counts: ' + host.innerHTML);
_renderDfaProgress(host, { state: 'done', augmented: 0, candidates: 0,
                           computed: 0, no_rr_data: 0, failed: 0 });
if (host.innerHTML !== '')
  throw new Error('terminal 0-candidate pass must clear the strip');
console.log('OK');
"""
    _run_node(harness)
