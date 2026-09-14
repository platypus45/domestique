# Test-suite audit — Domestique @ a95447e8 (2026-09-14), measured

Lens: whether the tests can judge, and what they cost. Independent agent; one full
run `-n 5 --durations=40`: 3697 collected, 3640 passed, 15 failed, 9 skipped,
21 xfailed, 12 xpassed, 749 s wall (12:27). The 15 failures equal
`tests/known-failures-clean-main.txt` exactly. Kept verbatim; the synthesis is in
../architecture-audit-2026-09-14.md.

### Ranked findings

| # | Sev | Finding | Evidence |
|---|---|---|---|
| 1 | **CRITICAL** | **130 tests in 45 files read the real clock while the product may read another one** (the 03237915 class). Freezing only `tp.date`/`app.date`/`datetime` via a plugin: product clock = Sun 2026-09-20 → **62 flip**; Mon 2026-10-26 → **127 flip**; union 130 (59 on both). All 45 files call `date.today()`/`datetime.now()` in the test file; 6 also have a partial `FrozenPlannerDate` pin and still read the real clock. 13/13 sampled flips reproduce alone and pass on the real clock. | Top: `test_362_g7_under_rider_rpe` (10), `test_332_tab_flatten` (6), `test_plan_entry_api::TestPhaseEditorApi` (6), `test_v175_apply_tier_down` (5), `test_calendar_endpoint` (4), `test_v171_availability_per_day_cap` (4), `test_v172_cap_rematches_zwo` (4), `test_v180_auto_adjust`, `test_v188_apply_rest_day`, `test_drift_chip`, `test_341_m3`, `test_calendar_push_workout` (3 each)… Static: 145/347 files read the clock; 42 pin something; 30 private frozen-date classes in 26 files. `conftest` never pins `app.date`. |
| 2 | **CRITICAL** | **No test checks that two endpoints agree on the same week** (the owner's defect). One function references two card endpoints (`test_session_payload_display_name.py::test_weekly_plan_payload_includes_display_name`) and its `/api/calendar` mention is a comment; 0 cross-endpoint asserts. `/api/weekly-plan` appears in 3 files, `/api/week-summary` 2, `/api/calendar` 18, `/api/plan` 58 — the weekly-plan files only *seed* `tss_target` in fixtures, none compares it with the stored week. | script over all `def test_` bodies |
| 3 | **HIGH** | **7 of the 15 "known failures" are a real product bug, not environmental.** `src/app.py:13272,13474` set `step.duration_value = seconds*1000`; fit_tool 0.9.16 (the production venv; `fit-tool>=0.9.13,<1`) applies the `duration_time` sub-field scale ×1000 again → a 60-min step decodes raw `3 600 000 000` = 1000 h. `KNOWN_FAILURES.md:36` lists them as "FIT export in HR mode" with no cause. | `test_fit_hr_mode.py` ×5, `test_ftp_test_freeride.py` ×2 |
| 4 | **HIGH** | **Characterization self-test FAILS today** (3 m 51 s): `event/wkend5h/reforecast@21` and `event/wkend3h/reforecast@21` "changed nothing" — two entry cases that cannot see a fault. The plan's Step 0 says `--self-test PASS`. | selftest.log |
| 5 | **HIGH** | **Invariants are reported, not asserted.** `compare()` sets `rc=1` only for added/removed/changed fingerprints; `inv_new` only prints. **93 violations are blessed** in the golden (owner0: weekly_volume 18, hard_share 13, under_delivery 11, stepback 1; owner1: under_delivery 28, weekly_volume 11, empty_week 7, hard_share 3, stepback 1). | `characterize_planner.py:214-247` |
| 6 | **HIGH** | **The golden is not reviewable**: 1.10 MB, 71 902 lines, 217 cases; the last four blesses rewrote 22 579 / 19 237 / 10 223 / 30 559 lines. The diff summary shows 15 cases; the human is asked to judge the rest. Also `content_classification cache stale` fires on every gate run (mtime hash, `training_planner.py:262`). | `git log --stat tests/characterization.json` |
| 7 | **HIGH** | **Cost is concentrated and avoidable**: sum of test time 1890 s vs 3745 worker-s (749 s × 5) → ~50 % of wall is xdist tail/idle, not collection (5.6 s) or `import app` (0.65 s). `test_357_block_evaluation` = **689 s / 36 %** of all test time (16 behaviours × re-globbing and re-parsing >1000 .zwo per param; one param 141.8 s, longer than the ideal wall of 378 s). | `--durations=40` |
| 8 | MEDIUM | **What assertions judge (60 largest files, 3487 asserts)**: heuristic — served-plan behaviour 693 (20 %), labels/strings/shapes 545 (16 %), private helpers 91 (3 %), snapshot 23 (1 %), **source-text pins 124 (4 %)**, generic equality 2011 (58 %). File-level: 28/60 run the planner, **only 11/60 run it and assert on the served file or minutes**; 10 run it and assert labels only (`test_plan_api`, `test_plan_entry_api`, `test_event_and_goal_focus`, `test_planner_variety_bonus`, `test_planner_full_library_utilization`, …); 26/60 call `_private` functions; 10/60 pin `dashboard.html`/JS text. Whole suite: **253 source-text assertions in 36 files**. | classifier |
| 9 | MEDIUM | **Fixture duplication**: `tp.Goal(` 158 sites / 75 files with 28 distinct local factory names (`_goal` ×19, `_event_goal` ×8, `_mk_plan_dict` ×14); hand-built `"weeks": [` 78 / 61 files; `PlannedWeek(` 30 / 21; `PlannedSession(` in 36 files. `TestClient(` 208 sites / 122 files (89 inside test bodies in 34 files, 85 in `setUp`, 31 in fixtures) with **29 distinct patch combinations**; 52 files patch nothing, 38 hand-patch `PLAN_DIR`, 21 re-stub the ICU fetch conftest already stubs. conftest gives: HOME sandbox, profile+db bootstrap, `planner_pinned_env` (used by 12 files), `PLANNER_PIN_ARGS` (22), network block. It gives no Goal/plan factory, no client fixture, no app-date pin. | greps |
| 10 | MEDIUM | **Timing assertions run inside the parallel bulk**: `addopts` excludes only `hardware`, so `release_serial` (`test_v133_frontpage_perf` <250 ms/<1000 ms, `test_v133_update_plan_perf` <1.5 s) plus 9 other wall-clock asserts (`test_v132…::test_generate_plan_under_5s`, `test_v181_library_cache::test_cold_call_under_5s_and_hot_call_under_100ms`, `test_v181_reforecast_fastpath::…under_500ms`, `test_route_picker_api` ×2, `test_fatigue_resistance::test_perf_under_2s_on_50_rides`, `test_v163_dfa_augment_timeout` ×2, `test_v163_lazy_sync_async`) ran under 5 workers. | pytest.ini |
| 11 | LOW | **Load flake not reproduced**: `test_week_solver::test_the_same_problem_gives_the_same_answer` 3/3 pass under an 8-process CPU hog (14.5 s vs 9.3 s). The mechanism is real — `week_solver.solve(time_limit_s=2.0)` HiGHS — but this problem finishes early. `time.sleep` in 11 files, threads in 9, subprocess in 21. | run under hogs |
| 12 | LOW | **Order sensitivity: none found.** 5 planner suites forward and reversed: 92 passed / 5 xfailed both ways. Four historical pairs (`v134→lthr_icu_mirror`, `pmax_ingest→hr_mode_api`, `profiles→recalc_preserves_state`, `capacity_cap→week_solver`) identical alone and paired. | order logs |

### Endpoint coverage — 148 routes in `app.py`; **31 untested** (0 files by path and by handler name); 48 more hit by exactly one file

`GET /api/setup/defaults`, `/api/setup/icu-hr`, `/api/profile/ftp-history`, `/profile-picker`, `/profile-setup`, `POST /api/wellness/manual-hrv`, `GET /api/activities/blocks`, `POST /api/workouts/bulk-segments`, `GET /api/courses`, `/api/surface-types`, `/api/profiles-bulk`, `/api/climb-workout`, `/api/climb-zwo/{r}/{f}`, `/api/download/crs/{r}/{f}`, `/api/course/{r}/{f}/download`, `/api/download/gpx/{r}/{f}`, `/api/gpx/{r}/{f}`, `/api/logs`, `POST /api/settings/zones`, `GET /api/sync/status`, `/api/metrics/history`, `/api/metrics/latest`, `POST /api/metrics/log`, `GET+POST /api/blood-markers`, **`POST /api/plan/mark-unavailable`**, **`POST /api/plan/add-race`**, `GET /api/gc/status`, `/api/rides/legacy-envelope`, `/api/rides/{id}/fit`, `/api/programme/summary/png`.

HTTP-review pins today: **HTTP-6** partial (`test_plan_api::TestDailyAdaptEndpointIsProjectionOnly` — one GET of several); **HTTP-13** partial (`test_training_planner.py:367` pins `SESSION_TYPE_TO_BAND` against `dashboard.html` text — one of four maps); **HTTP-1, 2, 3, 4, 5, 7, 8, 9, 12, 14: no pinning test** (no lock/concurrency test, no budget-bound after update/reforecast, no weekly-plan-vs-stored, no calendar-zones-vs-served-file). HTTP-10/11: `test_plan_codec` exists but does not pin agreement across the 21 conversion sites.

### Slow tests and shared-fixture savings

| File | s | tests | Why | Fix / saving |
|---|---|---|---|---|
| test_357_block_evaluation | 689 | 52 | `_library_cases()` re-globs+parses all .zwo per behaviour param (16×); one param 142 s | module-scoped parsed library; split params → ≈ −350 s test-time, removes the 142 s tail |
| test_planner_weekly_hit_cap | 250 | 147 | `_all_weeks()` not memoised; two parametrised tests regenerate the same (goal,seed) | `lru_cache` → ≈ −120 s |
| test_planner_full_library_utilization | 83 | 8 | generate_plan per test (55 s test) | module plan → −40 s |
| 36 files with `generate_plan` in test bodies | 430 | — | 102 calls in bodies; only 17/54 files use a module/session fixture | share → ≈ −180 s |
| xdist tail | ~370 | — | 749 wall vs 378 ideal | put the heavy file first / `--dist loadgroup` |

Estimate: 1890 s test-time → ~1400 s; with the tail fixed, **wall ≈ 5–6 min from 12.5** without deleting a test.

### Known failures (15)
- 4 `test_download_pywebview_bridge`: environmental (`webview` absent from venv).
- 4 `test_tls_trust`: environmental + stale fixture (OpenSSL 3.6.2 rejects the fixture root: "Missing Authority Key Identifier"; interceptor tests expect `'invalid CA certificate'`).
- 7 `test_fit_hr_mode` + `test_ftp_test_freeride`: **real** (finding 3).

### Eight changes, best judgement per hour
1. One autouse conftest fixture freezing `app.date`, `tp.date` and both `datetime`s (kills the 130-test class; ~3 h).
2. One test: generate → assert `/api/plan` week, `/api/weekly-plan.tss_target`, `/api/week-summary.tss_target`, `/api/calendar.planned_tss` agree (fails today; pins HTTP-2/7; ~1 h).
3. `compare()`: `rc=1` on `inv_new`; print per-rule counts, not per-case lines (~1 h).
4. Fix the self-test: make `reforecast@21` edit the event riders or drop it from `EDITORS` (~1 h).
5. Un-"know" the 7 FIT failures: fix the double scale or `xfail(strict=True, reason=…)` with the cause (~2 h).
6. `test_357` module fixture + `_all_weeks` cache + module plans in the 36 files (~4 h, −6 min per run).
7. conftest Goal/plan-dict factories and one `client` fixture patching PLAN_DIR/profile/db one way (29 combos → 1; ~6 h, incremental).
8. Move `release_serial` out of the parallel run; replace the 253 source-text pins with behaviour tests as touched.

### Not measured
Whether fit_tool 0.9.13 scaled differently (could not install); parse-vs-score split inside `test_357`; mutation kill-rate of the unit tests; the 52 "no patches" TestClient files' real write targets; the assert classifier is regex-heuristic (58 % unclassified), the file-level counts are the reliable ones.
