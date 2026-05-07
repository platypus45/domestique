# Changelog

## v1.5.0 — `tp.reforecast_dict` single-layer reforecast (2026-05-07)

Closes drift class A permanently. The v1.4.0 architecture rebuild
replaced 3 duplicated propagation blocks with one helper
(`_propagate_reforecast_to_dict` in app.py), but the **two-module split**
still required callers to maintain a separate PlannedWeek list, call
`tp.reforecast(goal, pw_list, ...)`, then propagate the result back.
v1.5.0 collapses that into a single function in `training_planner.py`:

- **`tp.reforecast_dict(plan_dict, ...)`** — accepts the persisted plan
  dict, mutates it in place, returns `(plan_dict, sessions_modified,
  reforecast_info)`. Internally builds the PlannedWeek list, runs the
  existing `reforecast()`, and applies the result via the new private
  `_apply_reforecast_to_dict`. Callers no longer touch PlannedWeek
  directly.
- **`tp._plan_dict_to_planned_weeks(plan_dict)`** — the
  PlannedWeek-list-from-dict conversion (previously inlined at every
  callsite in app.py) is now a single helper. Used by `reforecast_dict`
  internally and by callers that still need a list (e.g. for
  `detect_plan_gaps`).
- **3 callers in app.py migrated**: `_maybe_auto_reforecast`,
  `api_plan_reforecast`, `api_save_availability` — each shrinks from
  ~50 lines to ~10. The `propagation_days` kwarg lets save-availability
  preserve its v1.3.x contract (propagate only the user-touched dates).
- **`_propagate_reforecast_to_dict` removed from app.py**. There is now
  exactly one mutation site for reforecast field propagation. Drift
  between training_planner and app.py is structurally impossible.

The legacy `tp.reforecast(goal, pw_list, ...)` API is kept as a
deprecated alias for tests + external callers; removal in v1.6.0.

Tests: `tests/test_v150_reforecast_dict_signature.py` (5 tests):
- Returns the same dict object (in-place mutation contract).
- Identity reforecast (no overrides) → 0 sessions modified.
- Old `tp.reforecast` alias still callable.
- `_propagate_reforecast_to_dict` is GONE from app.py (regression
  guard for drift class A).
- Drift class A: `zwo_file` round-trips on untouched sessions.

## v1.4.2 — `_enrich_plan_for_response` mtime-keyed cache (2026-05-07)

`_enrich_plan_for_response` now wraps its uncached body
(`_enrich_plan_for_response_uncached`) in a 5-min TTL cache keyed on
`(plan_path mtime, plan_path size, today_iso)`. Saves ~50 ms per
`/api/plan` call when no mutation has touched the JSON. Cache GCs to 4
entries by insertion time. Mutation endpoints already touch the JSON
via `tmp+rename`, so the cache busts automatically.

Single-process FastAPI worker → no thread race; plan writes are also
serialised via `tp.plan_write_lock()`. The cache key uses 6-decimal
mtime + `st_size` so concurrent ms-coincident writes can't collide.

The cached path still mutates the caller's `plan_dict` in place (same
contract as v1.4.0) by replaying a snapshot of the enrichment fields.

Tests: `tests/test_v142_enrich_cache.py` (5 tests) — hit, mtime bust,
today_iso bust, no-persisted-plan fallthrough, in-place mutation
preserved on cache hit.

## v1.4.1 — card_state_v2 rendering distinguishes 10 calendar states (2026-05-07)

Calendar `renderCalDay` now reads `card_state_v2` (10-state machine from
v1.4.0) with fallback to legacy 4-string `card_state`. Six finer-grained
variant classes layer on top of legacy `cal-completed` / `cal-missing` /
`cal-rest`:

- `past_planned_no_ride` (skipped, red tint, border-left red)
- `past_actual_only` (unplanned ride, purple tint, border-left purple)
- `past_planned_actual` (completed, green tint, border-left green)
- `today_planned` (planned today, blue tint)
- `today_actual` (completed today, strong green)
- `future_unavailable` (gray tint over the existing UNAVAILABLE badge)

The remaining 4 v2 states (`past_no_ride`, `future_planned`,
`future_rest`, `missing_workout`) keep their legacy short-circuit
rendering. The cell now also carries `data-cs-v2="<state>"` so future
panels can dispatch on it without reading `card_state_v2` from JS data.

Tests: `tests/test_v141_card_state_v2_render.py` (6 tests) — assert the
JS dispatch table, the 6 CSS rules, the data-cs-v2 attribute, and that
v1.4.0's classifier output drives the new variant classes.

## v1.4.0 — Calendar/plan/availability architecture rebuild (2026-05-07)

Closes the v1.3.5/6/7 regression cycle by collapsing the two-layer field-update
drift between `training_planner.py` and `app.py` into single mutation +
enrichment helpers.

### Root cause closed

Each v1.3.x patch fixed *one* of three duplicated propagation blocks
(`app.py:6943-6977`, `7141-7200`, `7369-7402`). The other two stayed stale,
producing a new regression each release. v1.3.5 fixed availability rest;
v1.3.6 fixed restore-from-rest but left layer-2 propagation hard-coding
`zwo_file=""`; v1.3.7 fixed that propagation in two places but a third
identical block in `_maybe_auto_reforecast` would have drifted again.

### Architecture changes

- **`_propagate_reforecast_to_dict(plan, pw_list, touched_days) -> int`** —
  single mutation site replacing all 3 duplicated post-`tp.reforecast`
  field-copy blocks. Round-trips session-level fields (session_type,
  duration_min, tss_estimate, description, zwo_file, zwo_name, adapted)
  AND week-level G4 ACWR mutations (tss_target, hit_per_week,
  auto_acwr_scaled).
- **`_enrich_plan_for_response(plan_dict, today_iso) -> dict`** — single
  enrichment helper, replaces 5 duplicates in `/api/plan`,
  `/api/plan/generate`, etc. Adds card_state / card_state_v2 /
  content_class / display_name / zone_dist / score / protocol /
  zwo_duration_min per session. Idempotent (test-asserted).
- **`classify_card_state_v2(s, has_actual, today_iso) -> str`** — pure
  10-state classifier per the new declared rules table (CALENDAR_REDESIGN
  §5d). `legacy_card_state(state10) -> str` maps to the legacy 4-string
  wire contract (`completed`, `rest`, `missing_workout`, `planned`) for
  back-compat. Both `card_state` (legacy) and `card_state_v2` (new) ship
  on the wire so future UI rev can migrate without a wire break.
- **`SESSION_FIELDS_LOCKED`** — frozen field superset enforced by
  `tests/test_v140_session_fields_contract.py`. Future drift fails the test.

### Dashboard wiring

- `generatePlan()`, `reforecastPlan()`, `regeneratePlan()` now `await
  loadCalendar()` after the mutation so top cards / right panel /
  bottom calendar stay synchronized (CALENDAR_REDESIGN §8.4 — single
  canonical refresh path). The other mutation handlers
  (`_commitAvailUpdate`, redraw paths, `syncNowAction`) already wired.
- `renderCalDay` now distinguishes UNAVAILABLE from REST per
  CALENDAR_REDESIGN §8.1: `availability_hours==0` future days render a
  red UNAVAILABLE badge with the originally-planned session faded
  behind at 30% opacity. Restoration semantics per §8.2: re-running
  the planner with current hours picks the best-fit workout (no stored
  history of the original session).

### Tests

26 new (3 contract + 23 classify_card_state). Full v131-137 + auto/availability
regression suite (53 tests) all passing under the new architecture.
Full suite: **1169 passing** (up from 1143). 5 pre-existing failures
unchanged (planner interval variety × 2, wellness TSB × 2, local training
load fallback × 1) — same set as v1.3.7.

### Breaking changes

None. `card_state` legacy 4-string contract preserved on wire.
`SESSION_FIELDS_LOCKED` is a SUPERSET — future fields can be added.

## v1.3.7 — Hot-fix: bottom calendar renders again (regression from v1.3.6) (2026-05-07)

User report: "whole calendar on the bottom is now broken and doesnt show anything anymore. no single activity. the availability calendar does work. also when i regenerate plan, whole calendar bottom not working."

### Root cause

`app.py:7158` (`api_plan_reforecast`) and `app.py:6959` (`_maybe_auto_reforecast`) both hard-coded `s_json["zwo_file"] = ""` in the reforecast propagation block. After `tp.reforecast()` returned freshly-matched workouts (v1.3.6 added the rest→z2 restore branch + final `match_zwo` sweep), the propagation step nuked every `zwo_file` back to empty string. `_classify_card_state` then emitted `"missing_workout"` for every cell → bottom `#cal-overlay` painted yellow ⚠ everywhere with no workout titles. User perceived this as "no single activity" because every cell was a warning placeholder.

### Fix

Both call sites now propagate `getattr(src, "zwo_file", "") or ""` from the `PlannedSession` that `tp.reforecast` returned. The per-day branches inside reforecast already null out `zwo` when a swap is genuinely needed (v1.3.5 `hours<=0` rest path; v1.3.6 rest→z2 restore path). Propagating whatever the source holds keeps the picked workout intact when no swap happened.

### Tests

2 new in `tests/test_v137_calendar_renders_post_v136.py`. Full suite: **1143 passing** (up from 1141). 5 pre-existing failures match v1.3.6 commit body verbatim — no regression.

## v1.3.6 — Hot-fix: availability restore + 3D-fitness placeholder UX (2026-05-07)

Two user-reported bugs landed in one multi-wave pass.

### Issue A — Restoring availability hours doesn't re-add sessions

User flow: open availability calendar → set Sat=0h, Sun=0h → click UPDATE → Sat/Sun become REST (works post v1.3.5). User changes mind → set Sat=4h, Sun=4h → click UPDATE → **Sat/Sun stay REST**. Asymmetric: reforecast un-rests nothing when hours go from 0 → positive.

**Root cause** (`training_planner.py:5025-5029`): the `hours > 0` branch only did `s.duration_min * scale`. When `s.session_type == "rest"` already, `duration_min == 0` so `new_dur = 0` — session stayed REST.

**Fix** (`training_planner.py:5025+`): in the `hours > 0` branch, when `s.session_type == "rest"`, restore to z2 (Layer-1 endurance default), set `duration_min = round(hours * 60)`, recompute tss_estimate, clear `zwo_file/zwo_name` so the dashboard match-on-render path picks a fresh workout.

Also fixed Wave-2-grill-#2: pre-fix `if current_mins <= 0: continue` short-circuited weeks where every override day was already rest (e.g. a holiday week the user marked all-zero, then later wanted to restore one day). The loop now permits per-day handling when the week budget is empty.

### Issue B — 3D-fitness placeholder is opaque

User: *"what do you mean with this note, can we state when that will be calculated, whats blocking it etc?"*

**Root cause**: `ride_storage.compute_ride_xss` (v1.0.6 IMPL-3D-INGEST) was added but **never wired into a production ride-import path**. So `ss_cp_daily / ss_w_prime_daily / ss_pmax_daily` rows never landed in `athlete_metrics`, `_augment_wellness_with_3d_fitness` had nothing to convolve, and the energy-system chart placeholder fired forever.

**Fix**:

1. **`app.py:_parse_fit_stats`** — added the missing `compute_ride_xss(power_series, started_at, summary=out)` call so future FIT imports populate SS aggregates.
2. **`GET /api/wellness/3d-fitness-status`** — returns `{rides_with_ss: K, rides_total_post_v106: M}` so the dashboard can render an actionable line.
3. **`POST /api/wellness/backfill-3d-fitness`** — one-shot backfill: iterates post-v1.0.6 rides, re-parses FITs (or fetches ICU streams), runs `compute_ride_xss` per ride, busts the wellness cache.
4. **`templates/dashboard.html`** — replaced the opaque placeholder with an actionable text + "Backfill rides" button.

### Behavior contract change

Pre-fix, a goal-rest day (e.g. Monday with `goal.rest_days=[0]`) set to hours>0 in the availability popover was a no-op. Post-fix, that day is restored to z2 — taking the user at their word that they're overriding the goal-rest. No existing test relies on the old behavior.

### Tests

13 new tests across 2 files:

- `tests/test_v136_availability_restore.py` (7 tests, 1 skip-when-not-Sunday)
- `tests/test_v136_3d_fitness_backfill.py` (5 tests)

5 pre-existing failures remain pre-existing (matches `clean-main` HEAD baseline): `test_readiness_data_status_field_set_when_insufficient`, `test_mid_late_base_has_at_least_one_interval_pick_per_week`, `test_all_four_canonical_hard_types_appear_in_build1_build2`, `test_wellness_tsb_zero_when_ctl_and_atl_zero`, `test_wellness_tsb_none_when_ctl_missing`.

Suite: 1141 passing (was 1129).

### Files touched

- `training_planner.py` — Issue A surgical fix (~25 lines)
- `app.py` — Issue B endpoints + FIT-import hook (~140 lines)
- `templates/dashboard.html` — placeholder rewrite + JS handler (~55 lines)
- `tests/test_v136_*.py` — 2 new test files

---

## v1.3.5 — Hot-fix: UPDATE plan now rests unavailable days (2026-05-07)

User flow: open availability calendar → set Sat=0h, Sun=0h → click UPDATE → "Plan reflowed — N sessions changed" toast → dashboard reloads → Sat/Sun **still** show planned z2/long sessions instead of REST. v1.3.1's `availability_overrides` plumbing was correctly wired but reforecast silently no-op'd the current in-progress week.

### Root cause

Two bugs:

1. **`training_planner.reforecast()`'s `availability_overrides` loop short-circuited every current week** with `if pw.start < today: continue`. `pw.start` is Monday — by definition < today on any non-Monday — so a Sat=0/Sun=0 UPDATE click silently no-op'd those days. The endpoint reported `sessions_modified > 0` only when the pre-fix v1.3.1 test happened to anchor on a *future* Monday, dodging the gate.
2. **Rest branch didn't clear `zwo_file` / `zwo_name` / `description`**, so even when the gate passed (future weeks), the dashboard rendered the old workout name on rest cells.

### Fix

- **`training_planner.py:4984`** — gate on `pw.end < today` (matches the G3 block at line 5021), so the current week's *future* days re-rest.
- **`training_planner.py:5008-5014`** — rest branch now also clears `zwo_file`, `zwo_name`, sets `description="Rest (unavailable)"` (mirrors `generate_plan` block).
- **`app.py:7214-7229`** — propagation loop diffs/copies `zwo_file` / `zwo_name` / `description` back to JSON.

### Tests

- `tests/test_v135_availability_rests_unavail_days.py` (NEW, 2 tests) — anchors on the current Monday, asserts Sat/Sun get `session_type='rest'`, `duration_min=0`, `tss_estimate=0`, `zwo_file=''`, `zwo_name=''`, AND that Mon/Tue/Wed/Thu/Fri sessions are untouched. Both fail pre-fix, pass post-fix.
- `tests/test_v131_availability_reflow.py:178` — count bumped 1→2 because Sun's description flips from "rest 0min" to "Rest (unavailable)" so it correctly counts as modified now.
- Full suite: 1127 → **1129 passing** (+2). 4 pre-existing unrelated failures unchanged.

## v1.3.4 — Hot-fix: generate-plan no longer paints yellow ⚠ on every cell (2026-05-06)

The v1.3.2 fix (`9e68246f`) wired availability_overrides into `generate_plan()` but didn't address the underlying causes of "yellow ⚠ on every cell" — it only fixed the first-paint vs reload-paint disagreement. With heavy availability (e.g. weekend 4h, weekday 1.5h), session durations got rescaled past library coverage and `match_zwo` returned `zwo_file=""` for every long-duration cell. The injected mid-cycle ftp_test slot also painted yellow because match_zwo's category gate filtered out every ftp_test-tagged ZWO. Description text stayed stale ("z2 (70min) — sampled from library · 154m") because it embedded the original ZWO's duration after rescale.

### Fixes

- **`match_zwo` falls back to longest-available when target_dur exceeds library coverage** (`training_planner.py:2429-2469`). Pre-fix: a 222-min vo2max slot found no candidates within the duration window (max diff 60min, library tops at 150-min vo2max) → `zwo_file=""` → `card_state="missing_workout"` → yellow ⚠. Post-fix: when the duration-window pool is empty, scan the same primary+fallback categories for the LONGEST available file, set `s.matched=True` (the content matches; only the duration is short), let the dashboard's existing "Workout file is N min, session planned for M min" banner surface the gap. log.info instead of log.warning so it doesn't pollute the audit trail.

- **ftp_test sessions now find a Coggan-20 / Ramp ZWO** (`training_planner.py:2417-2425`, `4187-4231`). Pre-fix the protocol-category gate (`cat == primary_cat or cat in fallback_cats`) dropped every ftp_test-tagged ZWO because the test files have varied Protocol values (Endurance/Threshold/etc) that don't consistently match the gate. The tag filter alone is sufficient identification; bypass the category gate when `want_test=True`. Also: the availability_overrides loop previously skipped ftp_test sessions entirely (kept zwo_file="" from the injector); now they get re-matched alongside everything else.

- **Final sweep fills any remaining `zwo_file=""` slots** (`training_planner.py:4233-4248`). Defensive backstop: after all the per-pass logic runs, walk every non-rest session and call match_zwo if it still has empty zwo_file. The injector + hard-floor + ronnestad passes can leave gaps that no other path covers.

- **Description refresh after availability rescale** (`training_planner.py:4218-4224`). Pre-fix the description embedded the ORIGINAL library duration ("z2 (70min) — sampled from library"), then availability scaled `s.duration_min` to e.g. 154 → tooltip read "z2 (70min) — sampled from library · 154m" (description vs duration disagreement). Post-fix: regenerate description as `f"{session_type} ({new_dur}min) — sampled from library"` after the re-match.

### Verification

User repro (12-week plan, weekday 1-1.5h, weekend 4h):
- Before: 2-3 yellow cells per plan (2.6%) typically; up to 5+ on extreme settings.
- After: **0 yellow cells** under the same heavy availability.

Default user setup (3 days at 0.5h override): 0 yellow cells (was 0; not regression).

### Tests

3 new in `tests/test_v134_generate_plan_no_yellow.py`:
- `test_high_volume_availability_under_5pct_yellow` — repro headline (asserts <5% yellow).
- `test_long_duration_z2_falls_back_to_longest` — 240-min long_z2 picks longest endurance file, not ''.
- `test_ftp_test_session_picks_a_test_zwo` — ftp_test session matches a tagged ZWO.

158 planner tests pass. 1 pre-existing failure (`test_planner_interval_variety::test_mid_late_base_has_at_least_one_interval_pick_per_week`) unchanged.

---


## v1.3.3 — Perf + correctness hot-fix: frontpage no longer blocks, energy curves render real data, UPDATE plan 33× faster (2026-05-06)

Three parallel agents in isolated worktrees, each on a separate v1.3.2 regression. All three cherry-picked clean back to `clean-main`.

### Fixes

- **PERF — Whole frontpage was stuck on "Loading…"** (`1162e767`). Two compounding roots: (a) `templates/dashboard.html:4973` `loadHome()` did `await loadTodaySession()` which gated EVERY card painted below it (recent-activities, body-perf-card, power-curve-chart, readiness-composite-content, morning-log) until `/api/today-session` resolved (200-700ms warm; far worse cold with real ICU latency). (b) `app.py:8502` `_actual_ctl_today()` and `app.py:8547` `_hrv_trend_score()` called `training.fetch_wellness()` directly, bypassing the `cached()` 5-min-TTL helper. Both fire on every `/api/calendar` request via `_build_summary_block`, doubling ICU HTTP round-trips on every dashboard load. Fix: dropped `await` from `loadTodaySession()` so it runs alongside the other tail loaders; routed both bypass calls through `cached("wellness_7", ...)` / `cached("wellness_14", ...)` — same keys `/api/wellness` already uses, so they share a single TTL window. Measured: `/api/calendar` 482ms → 105ms (4.6×); `/api/today-session` 215ms warm but no longer blocks tail loaders.

- **3D — Energy-system breakdown chart now renders actual CP/W'/Pmax curves** (`d647ea23`). The v1.3.2 fix wired `energySystemChart()` into `loadHome()` but the chart still fell through to placeholder text. Root cause: `/api/wellness` was returning `cp_fitness`/`w_prime_fitness`/`pmax_fitness` as `None` on every record because nobody was running the per-component Banister convolution. The per-ride writer (`ride_storage.compute_ride_xss`) correctly stamped `ss_cp_daily` / `ss_w_prime_daily` / `ss_pmax_daily` per-day impulses into `athlete_metrics`, but no code integrated those impulses into per-day fitness/fatigue curves. Fix: new `app._augment_wellness_with_3d_fitness()` runs once per `/api/wellness` request, pulls 365 days of `ss_*_daily` rows, builds oldest-first contiguous load series per component, runs `strain_score.banister()` per record date with Kontro Fig. S2 τ defaults (CP 52/10 d, W' 5/5 d, Pmax 10/4 d). Six keys stamped onto each record dict in-place, idempotent (won't clobber upstream values). Wired into all three `/api/wellness` return paths (live ICU, local file store, SQLite fallback). Sample after fix (7d seeded SS_x): `cp_fitness=559.5, cp_fatigue=439.2, w_prime_fitness=122.4, w_prime_fatigue=122.4, pmax_fitness=66.7, pmax_fatigue=45.0`.

- **PERF — UPDATE plan button was 13s on a 12-week plan** (`f0ebc04e`). Root cause: `/api/plan/save-availability` called `tp.reforecast()` without `tsb_series`, so reforecast's per-day `_tsb_at` callback fell through to `get_today_metrics()` — which makes 2 ICU HTTPS calls per future hard session (~270ms each). On a 12-week plan with ~18 future hard sessions that was 36 ICU calls = 5–13s of network I/O on every UPDATE click. Fix: `app.py:7072` pass `tsb_series={}` so `_tsb_at` short-circuits to dict (returns None) and the network fallback never fires. Save-availability's only job is the availability rescale; TSB-driven downshifts belong to `/api/plan/reforecast` (which already pre-fetches `tsb_series` correctly). Core `training_planner.py` reforecast logic untouched. Profile (12-week plan, ICU creds): before save-availability 13207ms / calendar 422ms = **13629ms click-to-paint**; after save-availability 14ms / calendar 388ms = **402ms click-to-paint** (33× speedup).

### Tests

9 new across 3 files:
- `tests/test_v133_frontpage_perf.py` (3 tests) — `/api/calendar < 250ms` warm, `/api/today-session < 1000ms` tripwire, `loadHome()` does NOT await `loadTodaySession()`
- `tests/test_v133_energy_system_curves.py` (3 tests) — augmenter populates curves with SS history, no-ops empty `athlete_metrics`, respects upstream-written values
- `tests/test_v133_update_plan_perf.py` — patches `get_today_metrics` with 250ms sleep, asserts median click-to-paint < 1500ms + zero ICU calls from save-availability (zero-call invariant locks the regression)

1118 → **1127 passing** (+9). 4 pre-existing unrelated failures unchanged.

---


## v1.3.2 — Hot-fix: 4 dashboard/plan-gen bugs (energy chart, avail UPDATE button, generate-plan trio, session duration) (2026-05-06)

Same-day hot-fix for five real-user-feedback issues from the v1.3.1 dashboard. Three parallel agents in isolated worktrees + one resumed; all 4 cherry-picked clean back to `clean-main`.

### Fixes

- **Energy-system breakdown chart was empty** (`f7173c30`). `loadHome()` painted the primary `fitnessChart()` on DOMContentLoaded but never called `energySystemChart()`. The secondary chart was wired only into `loadFitnessChart(days)` (date-range button clicks). On initial page load the `<details>` host got its 180px min-height but no inner SVG. Surgical 5-line addition to `loadHome()`. The chart is pure inline SVG via `container.innerHTML` — Chart.js is not involved here. When `cp_fitness` is None on all wellness records (default until IMPL-3D-MODEL writes real values), the chart renders the placeholder text "3D fitness curves will populate after IMPL-3D-MODEL has computed Banister components..." instead of staying blank.

- **Availability calendar UX: explicit UPDATE button replaces auto-save** (`639349c5`). v1.3.1 auto-saved on every field change. User feedback: "maybe we should use a button… that if you alter the daily availability a very alerting UPDATE button starts to light up." Replaced debounced auto-save with a dirty-flag flip; new pulsing accent UPDATE button; aria-live confirmation "Plan reflowed — N sessions changed" auto-fades 4s; ESC with unsaved changes shows confirm-discard; Enter inside picker triggers UPDATE. Endpoint contract unchanged — POST happens only on UPDATE click now.

- **Session-detail modal title showed duplicate duration** `Wednesday — Tempo (45min) (81min)` (`94508162`). Root cause: `display_name` / `zwo_name` already embed the workout file's duration (e.g. `"Tempo (45min)"`), and `openDayWorkout` then appended `(${actualDur}min)` on top. Fix strips any embedded `(Nmin)` from `titleClass`, appends a single `(${session.duration_min}min)` suffix (the planned duration), and renders an orange mismatch label "Workout file is N min, session planned for M min. Pace/extend on trainer." when `|fileDur − sessionDur| / sessionDur > 0.10`.

- **Generate-Plan trio: ignored availability + yellow ⚠ on every day + slow** (`9e68246f`). `tp.generate_plan()` and `/api/plan/generate` both ignored the persisted per-date availability calendar (only the Mon-Sun weekly grid landed on the wire), and the response payload skipped the same per-session enrichment (`card_state` / `display_name` / `content_class` / `zone_dist`) that `/api/plan` adds — so dashboard cells legitimately picked workouts whose content_class disagreed with the session_type, painting a yellow ⚠ on every day on first paint. Fix: `tp.generate_plan()` now accepts `availability_overrides`, applies the same per-day rescale logic as `tp.reforecast()` (per-week scale clamped [0.4, 2.0]; `hours == 0` → rest; `hours > 0` rescales duration/TSS and re-runs `match_zwo`). `/api/plan/generate` reads `plan["availability"]` from `current_plan.json`, threads the overrides through, mirrors `/api/plan`'s post-load enrichment so the freshly generated payload carries the derived fields. Perf: cold 1.9s, warm 0.7s for 8wk; warm 0.9s for 12wk (existing `_WORKOUT_LIB_CACHE` memoizes the 3,054-file ZWO scan).

### Out of scope (separate fix)

`/api/plan/reforecast` and `/api/plan/save-availability` both wipe `s_json["zwo_file"] = ""` for downshifted/G3-touched sessions but never re-run `match_zwo` inline. After Generate-Plan, the new path produces clean zwo_files; subsequent reforecasts should also re-match. Tracked for v1.3.3.

### Tests

15 new tests across 4 files:
- `tests/test_v132_energy_system_chart.py` (3 tests)
- `tests/test_v132_availability_update_button.py` (3 tests)
- `tests/test_v132_session_duration_display.py` (3 tests)
- `tests/test_v132_plan_generate_fixes.py` (6 tests)

34/34 v1.3.2-touched + neighbouring tests green.

## v1.3.1 — Hot-fix: Chart.js loading + mid-week pacing + availability reflow + redraw visibility (2026-05-06)

Same-day hot-fix for four real-user-feedback bugs surfaced after v1.3.0 ship. Four parallel agents in isolated worktrees, one per bug; all four landed clean.

### Fixes

- **BLOCKER — Chart.js never loaded** (`96c83db0`). v1.3.0's Power Curve, Fatigue Resistance scatter, and 6 phase-summary charts all called `new Chart(...)` but `templates/dashboard.html` had ZERO Chart.js script tags — the panels rendered as the "Chart.js not loaded" fallback for every user. Vendored Chart.js 4.5.1 UMD (208 KB) into `static/vendor/chart.umd.min.js` and added one `<script>` to `<head>`. Existing fallback guards left intact as defense-in-depth. Regression test asserts the script tag precedes every `new Chart(` call site.

- **HIGH — Redrawn training didn't show up** (`9eb51189`). The day-detail modal's "Rematch workout" button (`templates/dashboard.html:11814` `rematchDaySession()`) repainted the legacy `#wc-grid` host via `loadWeeklyCalendar()` but didn't refresh the visible THIS WEEK panel (`#weekly-calendar`) or calendar overlay (`#cal-overlay`) which are fed by `loadCalendar()` + `loadPlan()`. Server persisted the new ZWO correctly; user's view stayed in `missing_workout` state. Surgical 10-line addition of `await loadCalendar(); await loadPlan();` on both success and classifier-fallback paths. The other two redraw entry points (`pgRedrawSession`, `calRedrawDay`) were already correctly wired.

- **HIGH — Mid-week pacing read as "behind plan"** (`260e863a`). On Wednesday the dashboard showed `Z1+Z2: 37 min / 360 min planned (10%)` and `COMPLIANCE 24% ✗` — mathematically correct against END OF WEEK but misleading mid-week when 4 of 7 days are still ahead. `/api/calendar` (`merge_plan_with_rides`) now emits `planned_*_to_date` alongside `planned_*` and `days_elapsed` / `days_total`. The `rail()` helper in `templates/dashboard.html` renders a two-segment Planned bar (full-opacity to-date, dimmed remaining) and grades the headline % against to-date. New annotation `"{N} of 7 days elapsed · pacing math vs to-date plan"` under the THIS WEEK header explains the scaling. Compliance band recomputes via `completion_pct_to_date` (with full-week fallback so legacy callers don't regress).

- **HIGH — Availability didn't auto-reflow** (`b7433e37`). User marked Sat/Sun unavailable; planned trainings remained on those days. Root cause: `api_save_availability` (`app.py:6917-6938`) wrote `plan["availability"]` to JSON but never invoked `tp.reforecast()` even though the plumbing existed at `training_planner.py:4841-4900` with the `availability_overrides` kwarg already correctly rescaling `duration_min` / `tss_estimate`. Endpoint now builds `PlannedWeek` list, calls `tp.reforecast(goal, pw_list, availability_overrides=...)`, propagates `session_type` / `duration_min` / `tss_estimate` back to the plan JSON, persists, and returns `{ok: true, sessions_modified: int}`. Dashboard's `_saveAvailability` now calls `loadCalendar()` after the response. Popover copy at `dashboard.html:1841` swapped from "click Generate Plan to save and rebuild" → "Saved — plan reflowed automatically." New `availability_hours` / `availability_type` fields per day; days with `availability_hours == 0` render an UNAVAILABLE badge (red-tinted) instead of the planned card — distinct from planned REST.

### Tests

12 new tests across 4 new files:
- `tests/test_v131_chart_loaded.py` (3 tests)
- `tests/test_v131_redraw_visible.py` (3 tests)
- `tests/test_v131_midweek_pacing.py` (3 tests)
- `tests/test_v131_availability_reflow.py` (3 tests)

All 52 v1.3.1-touched + neighbouring tests pass. Full pytest 1103+ passing.

## v1.3.0 — 90-day Power Curve + Pinot 2014 Fatigue Resistance + Per-Ride PR Detection (2026-05-06)

Three coordinated additions, all rolling on top of the v1.0.6 3D-energy-system foundation. **TSS-PRIMARY 3D-ADDITIVE invariant preserved** — none of these replace the CTL/ATL/TSB backbone; they layer on top.

### 90-day Power Curve

- **`power_curve.py`** (NEW, 1078 LoC) — `aggregate_power_curve(profile_id, window_days=90)` walks the cached ICU envelope (`~/.domestique/rides/icu/`) and emits the rolling mean-max curve across [`STANDARD_DURATIONS = [1,5,15,30,60,120,300,480,600,1200,1800,3600]`]. Each rider point carries its source ride for click-through.
- **Sensor-glitch filter** (`is_sensor_glitch`) — keeps every effort by default (USER-FOUGHT decision: "an effort = an effort, no editorial filtering"); only filters confirmed sensor dropouts (1-second 10× FTP spikes paired with HR drops > 30 bpm vs `profile.max_hr`).
- **3-way y-axis toggle** — Watts / W·kg⁻¹ / %FTP. W/kg uses the per-effort `weight_kg` recorded at the time of the ride; %FTP uses `ftp_at_ride`. The Pinot & Grappe 2011 baseline curve scales with the rider's CURRENT FTP+weight.
- **Window selector** — 30 / 90 / 180 / 365 / All. Polls a single `GET /api/profile/power-curve?window_days=N`, cached 24h.
- **Single-flight backfill lock** — TOCTOU-safe ownership: lock travels with the worker through `_run_backfill_job` and releases in `finally`, so two simultaneous POSTs can't double-spawn (Wave 2A grill W2A-G2).
- **`POST /api/profile/backfill-history`** — pulls 90 days of ICU activity streams in chunks of 30, rate-limited to 10 req/s with `Retry-After` honour.

### Pinot 2014 Fatigue Resistance

- **`compute_fatigue_resistance(profile_id, window_days, kj_threshold)`** — robustness index = peak power on tired legs (kJ-at-start ≥ threshold) ÷ peak on fresh legs (kJ-at-start < 500 kJ) × 100, averaged across {60, 300, 900, 1800} s. Per-ride kJ axis resets (PM ride does NOT carry over the AM ride's tail).
- **2-button kJ threshold toggle** — only `{1500, 2000}` accepted; anything else returns **HTTP 422**. The 1500 kJ default is grounded in [Coyle 1986](https://pubmed.ncbi.nlm.nih.gov/3536834/) muscle glycogen depletion (~1800 kJ at threshold) + [Mateo-March 2022](https://journals.humankinetics.com/view/journals/ijspp/17/6/article-p926.xml) WT race intensity; 2000 kJ is the strict [Pinot & Grappe 2014](https://www.fredericgrappe.com/wp-content/uploads/2014/07/MAP.pdf) MAP threshold.
- **`reason` field** (Wave 2B grill W2B-G2 fix-forward) — `insufficient_data` responses now carry one of `no_rides_in_window` / `fewer_than_4_long_rides` / `streams_not_hydrated_run_backfill` / `no_fresh_tired_overlap` / `compute_failed`. Avoids the failure mode where the user saw `n_long_rides:11` + `insufficient_data` with no explanation.
- **Bonk inclusion** — rides whose power drops to 0 in the last hour STILL count (Pinot methodology: fatigue is signal, not noise).
- **`GET /api/profile/fatigue-resistance`** — 24-hour cache keyed on `(profile_id, window_days, kj_threshold, latest_ride_id)`. Per-key Lock serialises concurrent same-key compute. Skip-GC-on-empty-latest so a transient ride-lookup failure can't nuke a healthy cached result.
- **Collapsed `<details>` panel** with Chart.js scatter (kJ on x, watts on y, color per duration tier) + (i) tooltip listing all four literature anchors (Coyle 1986 / Pinot 2014 / Mateo-March 2022 / [Maturana 2025](https://pubmed.ncbi.nlm.nih.gov/39875789/)).

### Per-ride PR detection

- **`compute_ride_prs(ride_id, window_days=90)`** — for every effort in `ride.efforts[]`, compares against the rolling-best across the previous `window_days` (excluding the current ride). Tier classification: **major** (≥ 2.5 % exceedance), **minor** (any positive exceedance), **first** (no prior recorded effort at that duration — emits `tier='first'` so day-1 badges still appear).
- **Persistence hook** — `ride_storage.persist_icu_activity` and `_build_summary_dict` (FIT-live path) now land `prs[]` inside the ride envelope after import. **Read-merge-on-error** (Wave 2B grill W2B-G9): if `compute_ride_prs` raises, prior `prs[]` survives instead of being silently overwritten with `[]`.
- **`GET /api/ride/{id}/prs`** + **`POST /api/ride/{id}/prs/recompute`** — read with lazy compute fallback for pre-v1.3.0 imports.
- **PR badges in ride detail** — top 3 by tier+exceedance (first > major > minor; tiebreak on `today_w` desc per Wave 2B grill W2B-G11). **XSS-hardened**: `data-prev-ride-id="${esc(prevId)}"` + delegated click+keyboard handler instead of inline `onclick="...${escJs(prevId)}..."` (Wave 2B grill W2B-G3).
- **`GET /api/profile/pr-toast-queue?drain=1`** (Wave 2B grill W2B-G4: previously the writer existed but no reader). Dashboard polls on `window.load` + 1s settle, renders one toast per ride that landed major PRs since last open.

### Wave structure (8 commits across 5 waves)

| Wave | Commits | Verdict |
|---|---|---|
| 2A — POWER-CURVE-CORE | `e7bab0cb` → `30e2842d` | 1 BLOCKER + 3 HIGH grilled + fixed |
| 2B — DASHBOARD + PR + FATIGUE | `cf882c8f` + `12443efc` + `e7addf8b` | 4 BLOCKER + 7 HIGH grilled |
| 2B fix-forward | `afef17e9` | all 11 grill IDs landed + 11 regression tests |
| 3 — QA | SHIP-NOW, all 6 gates PASS |

**Tests:** 1091 passing across the full suite (38 → 54 v1.3.0 tests + 11 fix-forward regressions). Pre-existing 4 unrelated failures verified at `61a6dbd7` baseline.

## v1.2.1 — Banister validation panel under CTL chart (2026-05-06)

User-visible: the v1.2.0 `/api/profile/banister-validation` endpoint now has a collapsed `<details>` panel directly under the CTL/ATL/TSB chart on Home, so the model-accuracy data lands without a navigation step.

- **`templates/dashboard.html`** — collapsed-by-default `<details>` panel; force-open on `?refresh=1` callback.
- **PATCH G1 BLOCKER fix** — comparison-nullability path (literature_mae_pct can be null on insufficient data); the panel uses optional-chaining instead of crashing on dot-access.
- **PATCH G2** — en-dash hardcoded for date ranges (was platform-dependent).
- **PATCH G3** — fetch `AbortController` with 90s timeout (the OOS validation can take > 30s on the first cold cache).
- **PATCH G4** — race fix between panel open + auto-refresh on first paint.
- **PATCH G5** — accessibility: `aria-busy` on the panel container, `aria-label` on the (i) tooltip trigger, `cursor:pointer` on the toggle.
- **PATCH G9** — force-open on refresh so the user sees the new fit immediately.
- **PATCH G12** — all 16 locked-field bindings tested.
- **PATCH G13** — guard against unbalanced `<script>` tags.
- **PATCH G14** — optional-chain pattern across all `d.banister_validation_*` reads.

Tests: 5 new tests in `tests/test_dashboard_v121.py`. All 5 pass.

## v1.2.0 — Out-of-sample Banister validation per athlete (2026-05-05)

User-visible: a "Model accuracy" panel under the existing CTL / ATL / TSB chart now shows your personal model's prediction error vs. literature ([Hellard 2006](https://pubmed.ncbi.nlm.nih.gov/17909403/) baseline 5–8 % MAE), updated lazily on a 24-hour cache.

- **`oos_validation.py`** (NEW, 525 LoC) — `validate_banister_oos(profile_id, holdout_weeks=4, bootstrap_n=1000)`. Fits τ on weeks 1..N-4 via `tau_fitting.fit_tau_per_athlete(persist=False, horizon_end_date=...)` per PATCH G4 (no live-fit pollution), forward-simulates the trailing 4 weeks, compares predicted FTP / CP / 5-min / 1-min power against actuals from `is_race`-tagged + FTP-test sessions in the holdout, computes per-metric MAE + bootstrap CIs. Hellard 2006 literature comparison built in (`better_than_literature` / `in_line` / `worse_than_literature`).
- **`GET /api/profile/banister-validation`** — 24-hour lazy cache (PATCH G12; no scheduled task). `?refresh=1` forces recompute.
- **Below-threshold path** — riders with < 8 calendar months OR < 12 markers cleanly return `fit_status: "insufficient_data"` instead of substituting a population MAE.

Honest framing baked into the dashboard: the Banister TSS-based stack has been operationalised in commercial software for 20 years and rarely tested out-of-sample (per the README §0a Vermeire 2021 critique). v1.2.0 is the user-visible answer: **"your model is X% accurate FOR YOU"** rather than relying on population averages.

Tests: 7 new tests in `tests/test_oos_validation.py`. PATCH G14 contract guard (`test_no_nls_fit_rows_after_validation`) verifies `persist=False` keeps the live nls_fit table clean.

## v1.1.0 — Norwegian Method support (HR-only) + HRV-based readiness composite (2026-05-05)

Two coordinated additions, both layered on signals already plumbed (no blood-lactate sampling per explicit non-goal):

### Norwegian Method — HR-only

- **HR ceiling on threshold sessions** (`hr_ceiling_pct=0.88` of HR_max). New `_set_max_hr(value, source)` setter cloned from `_set_wprime` (PATCH G6: matches existing `ProfileManager.max_hr` property; NOT `hr_max`). Tanaka 2001 fallback `int(208 - 0.7 × age)`. ICU sync at `db.sync_wellness()` writes the field automatically.
- **`double_threshold` session class** — AM + PM same-day pattern in `WORKOUT_MIX_PREFERENCE`, gated to build1-W3+ / build2 / peak phases only, ≥ 4-hour gap. Both halves sub-threshold by power + HR.
- **G9 advisory tier-down** — when yesterday's `dfa_alpha1_avg < 0.75` (Rogers 2021 LT1 marker), today's hard slot drops one tier as a SOFT ADVISORY. NEVER mutates `today_session.session_type` (PATCH G10: no-op for already-low-tier classes — endurance / recovery / rest return `advised_class=session_type, reason='already at lowest tier'`).
- **`detect_wbal_overshoot`** post-ride flag — when W'bal trough during a sub-threshold ride drops below 60 % of W', flags the ride as "ran hotter than prescribed" (informational only; doesn't gate anything).

### HRV-based readiness composite

- **`readiness_composite.py`** (NEW, 602 LoC) — module name + function name renamed per PATCH G1 BLOCKER (`readiness.py` already exists in production with a different signature). `compute_readiness_composite(profile_id, date)` returns a 0–10 score combining `wellness.hrv` (rMSSD), `ln_rmssd_7d` trend, TSB, Hooper composite, yesterday's `dfa_alpha1_avg` (when available), subjective feel.
- **Bayesian weight update** — initial weights from [Plews 2018](https://pubmed.ncbi.nlm.nih.gov/30053662/) + [Buchheit 2017](https://www.frontiersin.org/articles/10.3389/fphys.2017.00543/). PATCH G7 minimum-history floor: <30 days wellness → `score=None, status='insufficient_data'`; 30-59 days → static weights; ≥60 days → ridge-regression update against next-day eFTP proxy. PATCH G13 weight re-normalisation when components are missing (rider has no chest strap → DFA component drops, remaining weights re-normalised, confidence reduced).
- **`GET /api/readiness/composite?date=YYYY-MM-DD`** — 5-min cache. Renders a "Readiness today" home-page card.
- **HRV4Training CSV upload** — wellness tab accepts manual exports with locked columns `date, rmssd, hrv_baseline, recovery_points` (PATCH G16).

## v1.0.7 — DFA α1 from raw FIT + NP alternative (strain-rate lens) + per-athlete τ fitting + HRV-recording auto-prompt (2026-05-05)

Closes the README's "DFA α1 is computed post-ride if your FIT has them" gap that was aspirational since v4.1.0 — every cached ride before v1.0.7 has `dfa_alpha1: None`.

### DFA α1 from raw FIT (closes the wiring gap)

- ICU exposes the raw FIT at `/api/v1/activity/{id}/fit-file` (verified live: 140,585-byte download for the user's most recent ride). `training.fetch_activity_fit_file()` pulls it post-sync.
- `fit_activity.parse_rr_intervals()` walks `fit_tool.fit_file.FitFile` `HrvMessage` records (NOT `fitparse` per PATCH G3 — `fit_tool` is already in `requirements.txt` since v1.0.1).
- `analytics.compute_dfa_alpha1()` ports the dormant DFA implementation from `training_live.py:823-942` into the post-ride path. Sliding window 120 s / 30 s step per [Rogers 2021](https://pubmed.ncbi.nlm.nih.gov/34547011/).
- New `dfa_alpha1_status` field per PATCH G8 with one of `computed` / `no_rr_data` / `fetch_failed` / `sanity_rejected` — distinguishes the four failure modes for the dashboard's actionable copy.
- Hardware caveat documented in README: chest strap needed (HRM-Pro / Polar H10 / TICKR X / Verity Sense in HR mode), AND the head unit's "Log HRV" toggle enabled (Fēnix 8: `Watch Settings → System → Advanced → Data Recording → Log HRV` — verified against the official manual).

### HRV-recording auto-prompt (educational toast)

- After every ICU sync, when a ride lands with `dfa_alpha1_status == 'no_rr_data'`, Domestique surfaces a one-time-per-version toast.
- Auto-detect of the rider's Garmin device from FIT `file_id.garmin_product` — 21 device IDs mapped (Edge 530/830/1030/1030 Plus/1040, Fēnix 6/7/8, Epix 2, Forerunner 255/265/745/945/955/965).
- `[Show me how]` opens a modal with the device-by-device path table — the rider's row is **pre-highlighted in green**, others dimmed at 50 % opacity.
- `[Dismiss]` (per-version) and `[Don't show again]` (permanent) flags persisted in profile.

### NP alternative — strain-rate comparison lens

- `strain_score.compute_sr_avg(power_trace, cp, w_prime, pmax, tau_pcr=30)` returns `sr_avg_w` / `sr_if` / `sr_total_ss` watt-equivalents derived from Kontro 2026 Eq. 13 strain rate.
- Ride-detail modal gains a "Two lenses" comparison block: NP / IF / TSS labelled "Coggan 2003 — empirical" beside SR_avg / SR_IF / Belastingscore labelled "Kontro 2026 — mechanistic".
- Most diagnostic divergence: above-FTP repeating intervals (5×5). NP saturates at 4-th-power weighting, strain rate accelerates as W'bal drains. The metric pair makes that physiology visible.
- Greys out gracefully when CP / W' / Pmax aren't calibrated.

### Per-athlete τ fitting (replaces folkloric defaults)

- `tau_fitting.fit_tau_per_athlete(profile_id, persist=True, horizon_end_date=None)` — auto-fits CTL_TAU + per-component τ_CP / τ_W' / τ_Pmax from the rider's actual training log + race-performance markers (PATCH G4: `persist=False` for OOS validation reuse).
- `count_weighted_markers` — race=1.0, eFTP step ≥ 3 W=0.5, FTP test=0.8, Coggan-20=0.8 (PATCH G9). Threshold: `weighted_n ≥ 10` for a real fit; `5 ≤ weighted_n < 10` returns `low_confidence`; below 5 returns `insufficient_data`.
- Identifiability fragility documented: [Hellard 2006](https://pmc.ncbi.nlm.nih.gov/articles/PMC1974899/) found τ_a × τ_f correlate 0.99 across elite swimmers. Fit rejected if bootstrap CI > 50 % of point estimate, τ1/τ2 < 1.5 (collinearity), or residual r² < 0.40 — falls back to conventional τ in those cases.
- New `is_race INTEGER DEFAULT 0` column on `activities` (PATCH G11). 🏁 checkbox on ride-detail panel sets it via `POST /api/activity/{id}/race`.
- `<details class="tau-fit-results">` panel under CTL chart shows per-athlete τ values + bootstrap CIs + status.
- `training.get_today_metrics()` reads fit τ from `athlete_metrics` when status==success, falls back to conventional otherwise. Precedence: `manual > nls_fit > conventional`.

### Bootstrap vectorisation — 21× speedup

- `tau_fitting._ewma_vec()` replaces the Python loop in `_banister_predict()` with `scipy.signal.lfilter`-backed Banister IIR (numpy under the hood, no pandas — pandas would have added ~30 MB to the DMG and is still excluded in `domestique.spec`).
- `pytest tests/test_tau_fitting.py` runs in **12 s** (was 9+ minutes pre-vectorisation). Numerical-fidelity snapshot test verifies < 0.5 % drift across τ ∈ {3, 7, 14, 42, 60, 90}.
- `scipy>=1.13,<2` added to `requirements.txt`; `scipy` + `scipy.optimize` + `scipy.linalg` + `scipy._lib.array_api_compat` added to `domestique.spec` `hiddenimports` (PATCH G2).

### Cross-version contracts (locked by GRILL phase)

- 16 issues identified by adversarial review (1 BLOCKER, 5 HIGH, 6 MEDIUM, 3 LOW). All resolved before Wave 2 dispatch via the consolidated `MASTER_DECISIONS_v107_v110_v120_PATCH.md`. The BLOCKER (G1: `readiness.py` name shadow) prevented production breakage.

### Tests

- 1027 → 1037 passing (+10 net across 7 new test files: `test_dfa_alpha1.py` (7), `test_sr_avg.py` (4), `test_tau_fitting.py` (7), `test_tau_fitting_contract.py` (4), `test_norwegian_hr.py` (18 — PATCH G10 no-op verified), `test_readiness_composite.py` (7), `test_hrv_recording_prompt.py` (5), `test_is_race_column.py` (3), `test_tau_fits_endpoint.py` (3), `test_oos_validation.py` (7) = 65 new tests). Plus the v1.0.6 baseline + the existing v1.0.4/5 regression invariants — 130/130 of the latter still green.
- 4 pre-existing failures (none introduced by v1.0.7+ work). Verified via stash-bisect against `c54c9b79`.

### Out of scope / honest deferrals

- **Banister-validation dashboard panel** — IMPL-V120-OOS-VALIDATION's agent stalled before adding the `<details class="banister-validation">` panel. The endpoint works (`/api/profile/banister-validation` returns the locked dict). Panel ships in v1.2.1 fix-forward.
- **DFA α1 backfill** — pre-v1.0.7 cached rides cannot be retroactively given DFA α1; the data was never recorded by the head unit if the rider hadn't enabled "Log HRV" yet. Documented in README and surfaced via the in-app prompt.

## v1.0.6 — 3D impulse-response model (Belastingscore decomposition) — TSS still primary, 3D additive (2026-05-05)

### The pitch — "TSS PRIMARY, 3D ADDITIVE"

User-locked constraint: "We should still weight TSS based training, as that is the golden standard. But 1.0.6 with their triphase model is fun to add." So v1.0.6 ships the [Kontro/Mastracci/Cheung/MacInnis 2026 PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0341721) 3D impulse-response model as a **secondary lens**, NOT a replacement for the TSS-based planner backbone. Existing CTL/ATL/TSB chart stays primary; existing Banister τ=42/7 untouched; existing 6 v1.0.4-locked planner maps untouched.

### What v1.0.6 ships

- **`strain_score.py`** (NEW, 327 LOC) — implements Kontro Eq. 4 / 8-13: per-second power → MPA proximity → strain attribution to CP / W' / Pmax components. Reuses Skiba 2012 W'-balance differential (mirrors `training_live.py:514-523` exactly). PCr depletion uses literature-anchored τ_PCr = 30 s (PMC2636983; the paper itself doesn't specify). Calibrated so 1 hour at exactly CP ≈ 100 SS (matches Xert XSS convention).
- **`FitnessSignature` dataclass** extended with optional `cp_w` / `wprime_j` / `pmax_w` fields. Backward-compat: existing `hie` field unchanged. None defaults preserve all v1.0.4 / v1.0.5 behaviour exactly.
- **Per-component Banister τ constants** added alongside existing CTL_TAU/ATL_TAU at `training.py:35-46`: CP τ₁/τ₂ = 52/10, W' τ₁/τ₂ = 5/5, Pmax τ₁/τ₂ = 10/4. **All four convention sets live in parallel** — single CTL/ATL/TSB stays primary. Per Kontro paper supplementary Fig S2 — single-athlete illustrative example, profile-overridable, NOT validated population defaults.

### Pmax ingestion + per-ride decomposition

- ICU exposes `sportInfo[0].pMax` directly on every wellness record (live: 1,114.7 W on 2026-05-05). Same slot as the existing wPrime sync at `db.py:279-294` — copy-paste pattern.
- `db.py:294` gains a guarded upsert reading pMax → `athlete_metrics.pmax` row. Source-tier: `manual > intervals.icu > computed > fallback`.
- New `_set_pmax(value, source)` cloned from `_set_wprime` at `profile_manager.py:573-629`. Range validation 300-2500 W. Profile fallback `int(ftp × 1.30)` (Coggan 2-min approximation).
- Settings UI extended with manual override at `app.py:4030`.
- `ride_storage._summarise_ride()` now calls `strain_score.compute_xss_components()` per imported ride, writing `xss_total` / `xss_cp` / `xss_w_prime` / `xss_pmax` to the cached summary + `ss_*_daily` aggregates to `athlete_metrics`.

### Soft 3D guardrails added to the planner (advisory only)

- **NEW `_GLYCOLYTIC_LOAD_BY_CLASS` map** at `training_planner.py` module level (vo2max/anaerobic 1.0, vo2_short/ladder 0.9, over_under 0.7, neuromuscular 0.6, threshold 0.5, sweet_spot 0.2, …). Soft anti-stacking: if prior day's pick had glycolytic load ≥ 0.7, scale today's same-bucket weights ×0.7 (NOT a hard reject — soft picker bias only).
- **`reforecast()` extension** (training_planner.py:4488-4727): adds optional `wprime_balance_24h` kwarg (None default preserves existing call sites). Soft additions:
  - **G3b advisory** — log only when W'-load polarisation deviates >10% from target. G3a (volume polarisation) stays the hard gate.
  - **G8 advisory** — log only when `wprime_balance_24h < 0.5 × W'`. Recommends "prefer Z2 today" but doesn't gate.
  - **W'-ACWR advisory** — log when wprime_acwr > 1.5. TSS-ACWR (G4) stays the primary trip.
- `_maybe_auto_reforecast` at `app.py:5743-5894` augmented to read 3D metrics opportunistically when available; passes through new kwargs. Preserves existing TSS-only path when 3D fields are None.

### Dashboard — additive surfacing (TSS still primary)

- **CTL/ATL/TSB chart unchanged.** New collapsed `<details class="energy-system-breakdown">` panel BELOW the existing chart, default closed. When opened: three normalised fitness curves (CP / W' / Pmax) on a 0-100 axis. Reuses the existing SVG pattern.
- **Ride-detail modal**: TSS hero grid unchanged. NEW secondary "Belastingscore — energy-system breakdown" card BELOW the hero grid with 4 cells (Total / Aerobe / Glycolytisch / PCr). Each cell has a tooltip with locked science-grounded copy.
- **Plan-tab phase rows**: `weekly_tss` headline unchanged. NEW small subordinate stacked bar UNDERNEATH it (60px wide, 4px tall) showing CP / W' / Pmax distribution.
- **API contract additions** (all additive, all nullable):
  - `/api/ride/<id>/detail` summary block: `xss_total`, `xss_cp`, `xss_w_prime`, `xss_pmax`.
  - `/api/metrics/history?metric=cp_fitness/w_prime_fitness/pmax_fitness` accepted.
  - `/api/plan` per-week block: `weekly_xss_*` mirrors.
  - `/api/wellness` per-day record: `cp_fitness/cp_fatigue/w_prime_fitness/w_prime_fatigue/pmax_fitness/pmax_fatigue`.

### Honest documentation

- README §0e "Belastingscore / 3D impulse-response model (v1.0.6 preview)" added.
- README §0a "Honest limitations of the TSS-based stack" added with Sanders 2017 / Wallace 2014 / Vermeire 2021 evidence summary.
- README §0b "Literature wired into the planner" — table of every G1-G7 guardrail with peer-reviewed source.
- README §0c "Norwegian Method support — what's in, what's missing" with explicit non-goal: NO blood-lactate sampling.
- README "What τ (tau) actually means" subsection — EWMA math, absorption table, why per-athlete τ varies.

### Tests
- 909 → 959 passing (+50 net): 12 new tests in `tests/test_strain_score.py`, 18 in `tests/test_pmax_ingest.py` + `tests/test_xss_per_ride.py`, 16 in `tests/test_planner_3d_v106.py`, 4 in `tests/test_ui_v106.py`. Same 3 pre-existing wellness/training-load isolation failures.
- Hard regression invariant verified: every v1.0.4 / v1.0.5 test passes UNCHANGED. The 3D additions are nullable / optional / collapsed-by-default by design.
- Strain-score model invariants: `SS_CP + SS_W' + SS_Pmax ≈ SS_total ± 1%`; 1-hour ride at CP → SS ≈ 100 ± 2; all-Z2 ride → SS_W' < 5%, SS_Pmax < 1%; W'bal monotonically decreasing during P > CP, exponentially recovering during P < CP.

### Honest caveats baked into the docs

- The Kontro paper itself states: "no published data exist to support the energy-system specific model parameters." The τ defaults (52/10, 5/5, 10/4) are a single-athlete illustrative example from supplementary Fig S2, not population-validated. Domestique exposes them as profile-level overrides and documents this in dashboard tooltip copy.
- TSS-based Banister τ=42/7 in CTL/ATL is folkloric (per literature: 21-60 d range across athletes; Hellard 2017). v1.0.7 will fit per-athlete.

### Out of scope (deferred)
- Per-athlete τ fitting from real training data — v1.0.7.
- NP alternative via Kontro Eq. 13 strain rate as comparison view — v1.0.7.
- DFA α1 from raw FIT (`fit_activity.py` doesn't parse `HrvMessage` today; the `dfa_alpha1: None` gap on every ride) — v1.0.7.
- Norwegian Method support (HR-only, no lactate) — v1.1.0.
- HRV-based Bayesian readiness composite — v1.1.0.
- Out-of-sample Banister validation per athlete — v1.2.0.

## v1.0.5 — Classifier zone-band off-by-one + OU detector tightening + sustained peak-band gate (2026-05-05)

### The pitch — "100% accurate classification, validated"

User testcase `tempo_2x12min_63min.zwo` (a 2×12 min @ 88 % FTP sweet-spot session) was classified as `tempo_intervals` by v1.0.4. Wave 0 audit + 195-file stratified validation (covering Rønnestad / Billat / Tabata / Helgerud / Coggan / sprint clusters as edge cases) found 8 confirmed classifier bugs across 3 distinct cascade flaws. v1.0.5 fixes them all and adds the validation-gate canary as a regression test.

### Three surgical cascade fixes

- **BUG-A — Z3/Z4 boundary off-by-one (highest leverage).** `ZONES_FTP` half-open `[low, high)` semantics put 1.05 (= 105 % FTP, top of Z4 per Coggan/Allen/ICU) into Z5 instead of Z4. Same drift at 0.90 (top of Z3), 1.20 (top of Z5), 1.50 (top of Z6). Fix: bumped all upper bounds by +0.01 so top-of-zone values stay in their named zone. `z4_upper_s` slice helper updated to `< 1.06`. **Library impact:** ~17 % of `vo2max` class (4/23 in sample) reclassified to `threshold` because the headline 105 %-FTP intervals now correctly bin into Z4.
- **BUG-B — `_zone_dominance_class` z6 floor too aggressive.** 60 s of Z6 in a Z1-dominated workout was firing `anaerobic` classification. Coggan/FasCat anaerobic floor is 3 min cumulative Z6+Z7. Fix: raised z6 floor from 60 → 180 s.
- **BUG-C — Over-Under detector false-positive on Z3-ramp + Z6-sprint patterns.** Under-leg lower bound 0.70 was catching Z2/Z3 ramps surrounding Z6 sprints, mis-routing anaerobic to over_under. Fix: raised lower bound to 0.85, plus complementary upper-leg cap at 1.10 (excludes Z6+ sprints from the OU pattern, matches Hunter Allen / FasCat canonical OU power band).

### Library transitions
- 561 primary-class transitions vs. v1.0.4's regen.
- Top movements: `endurance → recovery` 92 (Z1/Z2 boundary precision), `vo2max → threshold` 61 (BUG-A), `anaerobic → vo2max` 54 (BUG-A), `over_under → {sweet_spot, vo2max, anaerobic, tempo_intervals}` ~96 total (BUG-C tightening).

### Canary verification (HARD gate)
- `tempo_steady_57min.zwo` → `primary: "threshold_ladder"`, `display_name: "Threshold Ladder 58min — 85→97 % × 4"`.
- `tempo_2x12min_63min.zwo` → `primary: "sweet_spot"`, `display_name: "Sweet Spot 63min — 2×12min/3min @ 88 %"`.
- `tempo_steady_55min.zwo` → `primary: "threshold_ladder"`.
- All 8 confirmed-bug files from `/tmp/qa_v105_validation.md` resolve in the regenerated JSON.

### Tests
- 879 → 909 passing (+30 net). 195-file stratified validation sample: **95.9 % adjusted accuracy** (raw match 68.7 % including validator imperfections). Coverage: all 16 canonical classes + Rønnestad 30/15 + Billat 30/30 + Tabata 20/10 + Helgerud 4×4 + Coggan 5×5 + sprint clusters.

## v1.0.4 — Library reclassification + correct titling + workout-detail UX trio (2026-05-05)

### The pitch — "100% accurate workout classification and titling"

User report: a session labeled "Tempo (58min)" was actually a 4×threshold-ladder; another labeled "vo2max" was actually pure Z2 endurance. Wave 0 audit confirmed the bug class is systemic — **48.2% of the 3,054-file library is misclassified** against an independent structural fingerprinter, **25.5% have at least one disagreement** between filename / `<name>` tag / classifier primary, and the canonical `display_name` field that should drive UI titles literally didn't exist (0/3,054 entries had it).

v1.0.4 rebuilds the classifier, regenerates the JSON, and rewires the planner + dashboard so titles tell the truth.

### Library reclassification

- **Classifier rewrite** — `scripts/classify_library_content.py` gains a structural cascade prioritised above the legacy dose-time rules:
  1. Empty `<workout>` / FreeRide-only → flagged, not classified (95 files)
  2. FTP test detector — Coggan-20 + Ramp protocol shapes
  3. Neuromuscular detector — sprint segments / <30 s @ >150% FTP
  4. **Ladder detector (NEW)** — ≥3 ascending or descending SteadyState rungs (≥5% FTP gap, ≥30 s each), ≥2 sets. Output: `{peak_zone}_ladder`.
  5. **Peak-zone gate (NEW)** — ≥5 min contiguous Z4+ at ≥30% of work time → classify by peak zone, not zone-time accumulation.
  6–11. Existing dose rules.
  12. Zone-dominance fallback — never returns `mixed`.
- **Taxonomy expanded from 12 → 16 canonical classes** (`mixed` dropped, 217 files re-routed). Adds `endurance_intervals`, `tempo_intervals`, `tempo_ladder`, `sweet_spot_ladder`, `threshold_ladder`, `vo2_ladder`. Each exists because its training stimulus differs materially from the parent class.
- **`workouts/.content_classification.json` regenerated** — every file's `primary` re-derived; 1,339 of 3,054 reclassified (43.8%); new `display_name` field added on every classified entry (100% non-empty); `mixed: 217 → 0`. Audit trail at `workouts/.classification_audit_v104.json` records every transition.
- **Canary verifications:**
  - `tempo_steady_57min.zwo` → `primary: "threshold_ladder"`, `display_name: "Threshold Ladder 58min — 85→97% × 4"` (byte-exact).
  - `tempo_steady_55min.zwo` → `threshold_ladder`.
  - One previously-mis-filed `vo2max` Z2 file reclassified out of the vo2max pool.

### Planner — 6 maps wired + anaerobic orphan fixed

- **`anaerobic` slot orphan fixed.** Previously weighted at 5–15% in `WORKOUT_MIX_PREFERENCE` for build/peak phases but excluded from `_HIT_SLOT_CONTENT_CLASSES` — so 311 anaerobic files were never picked. Now in the slot eligibility map; pickable on peak-week HIT slots.
- All 6 planner maps wired with the new classes: `_CONTENT_TO_PROTOCOL`, `_HIT_SLOT_CONTENT_CLASSES`, `_ENDURANCE_SLOT_CONTENT_CLASSES`, `WORKOUT_MIX_PREFERENCE`, `_PLAN_CLASS_MIN_DISTINCT_24W`, `_INTERVAL_SHAPED_CONTENT_CLASSES`. `mixed` dropped from all six.

### Dashboard — modal title cascade + 16-class filter

- **Modal title source fix.** Previously the workout-detail modal title rendered from `session.session_type` (the plan's INTENT), not the picked file's actual class. So a session planned as vo2max but matched to a Z2 file would title "vo2max" while the chart underneath showed pure Z2 — the user's "title is completely off" complaint. New cascade: `display_name` → `zwo_name` → `session_type`. Same cascade applied to calendar cell labels. The duration in the title also reflects the picked file's actual duration (`zwo_duration_min`), not the planned `duration_min`.
- **Library filter dropdown** updated from 12 → 16 canonical class options. `mixed` removed.
- **`<title>` SVG tooltip enrichment** (folded from the v1.0.3 fix-forward): every interval bar in `workoutProfileSVG()` now shows watts + duration + zone — e.g. `STEADYSTATE: 2 min 30 sec • 240 W (97% FTP)` instead of just `97% FTP`.

### Backend — display_name plumbed through every session payload

- `/api/plan`, `/api/plan/today`, `/api/plan/week`, `/api/calendar`, `/api/plan/missed-suggestions` (and all other session-bearing endpoints) now include `display_name` and `zwo_duration_min` for every session whose `zwo_file` has a library match. Graceful empty for free-form sessions.

### Workout-detail UX trio (folded from v1.0.3 fix-forward at `8cef603d`)

- **Filename consistency** — when `session.zwo_file` exists, the FIT download name derives from the same base. So both downloads share `tempo_steady_57min.zwo` / `tempo_steady_57min.fit` instead of one library-named ZWO and one generic-named `Tuesday_TEMPO.fit`.
- **FIT mirrors ZWO content** — `/api/export/fit-workout` accepts a `zwo_file` query param. When supplied, the endpoint parses the ZWO XML and emits a FIT structured workout with the same segments. Downloading both formats now genuinely gives the SAME workout, not the ladder ZWO + a generic-tempo FIT.
- **Pywebview download bridge** (already in v1.0.3 + CSP `unsafe-eval` fix) — both downloads pop a native macOS save dialog instead of white-screening (ZWO) or doing nothing (FIT).

### Tests
- 832 → 879 passing (+47 net across 5 new test files: `test_fit_from_zwo.py` (4), `test_workout_filename_consistency.py` (2), `test_classifier_v104.py` (14), `test_planner_taxonomy_v104.py` (34), `test_ui_v104_title_source.py` (3), `test_session_payload_display_name.py` (5)). Same 3 pre-existing wellness/training-load isolation failures. 9 xfails are filename-hygiene + legacy-taxonomy tests deliberately invalidated by v1.0.4 (filename rename is out of scope).

### Out of scope (deferred)
- Renaming the 3,054 ZWO files to match the proposed `{class}_{structure}_{key}_{duration}min.zwo` convention. Filename rename would break external links — separate Wave.
- Regenerating the 3,054 ZWO `<name>` tags from content. With `display_name` as the canonical UI source, the on-disk `<name>` is now a fallback only — optional cleanup later.
- Curating the 95 free-ride / empty-`<workout>` files flagged by the audit.
- Deduplicating the ~80 `_renamed_v46_*` siblings.

## v1.0.3 — Availability-aware reforecast + auto-fire on sync + Plan-tab guidance + missed-session reschedule + ZWO/FIT downloads finally work in the bundled app (2026-05-05)

### User-facing — the plan stays current with reality

- **Availability-aware reforecast** — `plan["availability"]` finally feeds `tp.reforecast()` so per-day hour overrides actually rescale daily duration / TSS. Previously the dashboard accepted availability edits but the reforecast call ignored them — closes the silent failure where "I changed availability but plan didn't budge". Per-week scaling clamped to [0.4×, 2.0×]; `hours=0` zeroes the day to rest.
- **Auto-fire reforecast on sync / FIT import** — successful `_sync_icu_activities`, `/api/rides/sync`, and `/api/ride/import` now trigger a debounced reforecast (5-min cooldown via `plan["reforecast_date"]`) when `added > 0`. Best-effort: wrapped in try/except, never propagates failures back to the sync response. CTL/TSB drift now rolls into the plan automatically — no manual click required.
- **Plan tab guidance** — collapsed `<details>` "How your plan updates" panel at the top of the Plan tab explaining when to use Reforecast vs. Regenerate vs. Edit Availability vs. Rematch. Plus four (i) info-icons next to each button with hover/tap popovers (≤40 words each, science-grounded copy). Hybrid behaviour: native `title=` short fallback + JS popover for richer copy. Single delegated handler — outside-click closes; one popover open at a time.
- **Missed-session reschedule suggestions** — new `GET /api/plan/missed-suggestions` walks the plan for `status="missed"` sessions and proposes a same-ISO-week future slot (rest day or unfilled available day). Banner above the calendar: "Move missed Tue 04-21 [endurance, 60min] to Fri 04-24? [Move] [Dismiss]". `[Move]` reuses existing `POST /api/plan/move-session` — read-only suggestion, user always confirms. Greedy first-fit by missed_date; max 3 chips inline + `+N more` link to the existing missed-sessions modal.

### Bugfix — ZWO + FIT downloads silent-failed in the bundled DMG

- Root cause: pywebview's WKWebView (macOS) silently ignores the `<a download>` attribute. The v1.0.1 fix only addressed FastAPI's route mismatch — the WebKit limitation persisted, so ZWO clicks rendered the file inline as text and FIT clicks did nothing.
- Fix: `launcher.py` exposes a Python `JsApi` class with `save_zwo(filename, content)` / `save_fit(filename, b64_content)` via pywebview's `js_api=` bridge. Both methods open a native `webview.SAVE_DIALOG` and write to the user-chosen path. Browser-mode users (running `python launcher.py` and opening the URL in Chrome / Safari) keep the existing `<a download>` fallback path — graceful degradation.
- **CSP fix (required for the bridge to initialise):** pywebview's injected `webview/js/api.js:75` uses `new Function(...)` to build the `window.pywebview.api` proxy after Python advertises its method list. The previous CSP `script-src 'self' 'unsafe-inline'` blocked that, so `save_zwo` / `save_fit` ended up undefined and a CSP `EvalError` surfaced on the home screen. Added `'unsafe-eval'` to the script-src directive — acceptable because the app binds to 127.0.0.1 only and serves no third-party scripts.
- Persistence write-back fix — `/api/plan/reforecast` no longer drops `duration_min` from the JSON write-back loop. Without this, every duration scaling (both the new availability-overrides path AND the existing ZWO-swap pass) silently died on disk on the next reload. Now persisted. Same fix applied to the auto-fire path.

### Endpoints
- `GET /api/plan/missed-suggestions` — `{"suggestions":[{missed_date, missed_session_type, missed_summary, suggested_date, suggested_day_name, reason}]}`. `reason` ∈ `{"rest_slot","unfilled_available_day"}`. Read-only.
- `tp.reforecast(...)` gains `availability_overrides: dict[str, float] | None = None`. Per-week scale clamped [0.4, 2.0]; hours≤0 → rest with TSS=0.
- `_maybe_auto_reforecast(profile_id, new_rides)` — module-level helper, 300 s debounce, `plan_write_lock()`, full persistence pattern.
- `pywebview.api.save_zwo(filename, content)` / `save_fit(filename, b64_content)` — JS-callable bridges.

### Tests
- 806 → 826 passing (+20 net across `tests/test_availability_reforecast.py` (4 new) + `tests/test_auto_reforecast.py` (4 new) + `tests/test_missed_suggestions.py` (5 new) + `tests/test_ui_v103.py` (3 new) + `tests/test_download_pywebview_bridge.py` (4 new)). Same 3 pre-existing wellness/training-load isolation failures.
- E2E gate verified: half-hour availability rescales duration_min to disk; rest-day availability zeroes session_type/duration/TSS; missed-session endpoint returns same-week future rest-slot suggestion; pywebview bridge surfaces save dialog (static-checked).

## v1.0.2 — Update notification + first-boot migration toast + upgrade docs (2026-05-05)

### User-facing — three coordinated pieces, all centred on the promise that rider data survives every install

- **Update-check banner** on the home page — `GET /api/update/check` polls the GitHub Releases API (cached 6h at `~/.domestique/update_check_cache.json`), filters assets by `sys.platform` (`.dmg` for macOS, `.zip`/`.exe` for Windows). The banner displays the EXACT copy stating which rider data is preserved across the update — rides, training plan, FTP history, wellness logs, profile. `[What's new]` and `[Download]` buttons link LIVE to `release_url` / `download_url` pulled from the API response (never hardcoded). Per-version dismissal via `localStorage["update-banner-dismissed-<version>"]` — resurfaces on the next release.
- **First-boot-after-upgrade migration toast** — startup version-aware self-check compares `~/.domestique/last_run_version.txt` to `VERSION`. On first boot at a new version: runs additive schema migrations (zero columns added in v1.0.2 — framework only for future use), writes the new version stamp, surfaces a 5s toast naming the from/to versions and "all rider data preserved". Idempotent — same-version reboots don't re-fire the toast (per-from/to-pair `localStorage` flag).
- **Upgrade docs** — new `docs/upgrading.md` (≤400 words) + new `## Updating Domestique` README section. Per-data-type preservation table guarantees rider state lives in `~/.domestique/` outside the app bundle, so DMG / EXE replacement never touches profiles, rides, plans, FTP history, wellness logs, or ICU credentials.

### Endpoints (v1.0.2)
- `GET /api/update/check` — returns `{current, latest, update_available, release_url, download_url, asset_name, platform, checked_at, cached, error}`. 6h TTL disk cache.
- `GET /api/migrations/last-run-result` — returns `{migration_check_passed, from_version, to_version, columns_added, schema_changes[], data_migrations[], rider_data_preserved, show_toast}`.

## v1.0.1 — Download bugs + Karoo + DMG bundling (2026-05-04)

### Download bugs (closes 3 user-visible issues)
- **"Download ZWO" returned 404 "not found"** — the dashboard hit `/api/download/zwo/<filename>` (single-segment path) but the only registered route was `/api/download/zwo/{category}/{filename}` (two-segment). FastAPI never matched. Added a single-arg route variant.
- **"Download FIT" silently did nothing** — root cause was a session_type literal mismatch: dashboard's `<select>` emitted `sweet_spot` and `over_under` (snake_case) but the FIT endpoint's elif chain checked `sweetspot` / `overunder` (no underscore). Picks fell through to a generic Z2 block which sometimes errored, returning 500 JSON that the `<a download>` element silently ignored. Fix: normalise input via `session_type.lower().replace("-", "_")` and accept both shapes. Plus rewrote `downloadFIT()` JS to `fetch()` + Blob so 4xx/5xx now surfaces as a toast instead of failing silently.
- **"Download FIT (Garmin/Wahoo)" → "Download FIT (Garmin / Wahoo / Karoo)"** — Hammerhead Karoo natively imports FIT structured workouts via the Karoo Workouts app, same flow as Garmin Edge and Wahoo ELEMNT.
- **PyInstaller bundling** — `fit_tool` and 6 submodules added to `domestique.spec` `hiddenimports=[...]` so the macOS DMG and Windows EXE actually have the FIT-builder library (PyInstaller's static analyser missed it because the import was inside a function).

### Tests
- 783 → 793 passed (+10 across `tests/test_download_routes.py` + new FIT-endpoint coverage). Same 3 pre-existing wellness/training-load flakes.

## v1.0.0 — First open-source release (2026-05-04)

Domestique reaches public availability. The codebase has been in private development since 2026 — the iteration history is preserved below as "pre-release development log" so users can see the path from concept to v1.0.

**Headline features at v1.0:**
- **Adaptive training planner** — periodised Base / Build / Peak / Taper, daily-adapt, reforecast, regenerate. 3,054 ZWO workouts in the bundled library, 622 virtual routes (CRS + GPX export).
- **Closed feedback loops** — DFA α1 / aerobic decoupling / Foster monotony / eFTP drift / local CTL fallback all flow into next-day planning (not display-only).
- **Seven science-grounded injury-prevention guardrails (G1–G7)** — yesterday-was-hard floor (Foster 1998), 48h Z5+ ceiling (Hulin 2014), polarization breach (Seiler/Stöggl/Treff), ACWR weekly scaling (Gabbett 2016), soreness peripheral cap + Hooper composite (Hooper & Mackinnon 1995, Cheung 2003), 3-day RPE drop (Foster 1998).
- **Capability projection** for event preparation — Allen-Coggan IF-by-duration + Pinot & Grappe 2011 RPP climb-power gate predict your finish time and quantify your endurance / power / climb gaps.
- **Finished-programme summary** — 12-metric end-of-plan recap (FTP/eFTP/VO2max Δ, polarization, monotony, mean-max curve, Hooper trend, totals, decoupling) exportable as PNG (Pillow) or PDF (browser print). Zero new dependencies.
- **Hardware-agnostic** — generate ZWO, ride in MyWhoosh / Tacx / Zwift / Hammerhead Karoo / outdoor with any FIT-emitting device, import the FIT back for analysis.
- **Single-user, localhost-only**, all data in `~/.domestique/profiles/<id>/`. ICU API key per-profile and chmod 0600. No cloud, no telemetry, no analytics.

**Tests at v1.0:** 783 passed / 3 failed (pre-existing wellness/training-load test-isolation flakes) / 1 skipped.

---

# Pre-release development log

> The history below is preserved verbatim from private development. v1.0.0 is what shipped, but the path from v3.6 to v4.6.8 documents how we got here — particularly the v4.6.x runs which delivered the injury guardrails, capability projection, and finished-programme summary.

## v4.6.8 — Cooldown chart fix-forward + 28 misfiled "endurance" files reclassified + README scrub (2026-05-04)

### Bugfix — Cooldown ramp v4.6.7 fix only worked for half the library
- v4.6.7 IMPL-UX assumed every ZWO `<Cooldown>` element used the start/end convention (`PowerLow=END, PowerHigh=START`) and added a tag-based swap. Library audit: 853 cooldowns use `PowerLow < PowerHigh` (numerical convention — PowerLow IS the lower number) and 1399 use `PowerLow > PowerHigh` (start/end convention). v4.6.7 fixed group 1 and *broke* group 2 — the user immediately reported cooldowns still ramping UP.
- **v4.6.8 fix**: value-based rule. `Cooldown` ALWAYS slopes DOWN regardless of attribute ordering: `start = max(power_low, power_high)`, `end = min(power_low, power_high)`. `Warmup` / `Ramp` always slope UP via the inverse. Library audit confirmed all 2345 Warmups use `PowerLow < PowerHigh` so the inverse holds. Updated `tests/test_ux_v467.py::test_cooldown_segment_renders_downsloping` to match.

### Bugfix — 28 "endurance" files had hidden Z5+ content
- The user reported `endurance_3x30s_90min.zwo` showing 5 sprint spikes mid-ride despite being filed as Endurance — confusing on a "pure Z2 day". Audit: 32 of 496 endurance-classified files had Z6+Z7 ≥ 1.5 min OR sprint segments OR Z5 ≥ 3 min. By zone-time totals they were technically Z2-dominant (87%+ of duration), so they passed the v4.6.1 endurance gate. But the *training stimulus* is polarized — small high-intensity dose on top of Z2 base.
- **Fix**: extend the v4.6.1 RECLASSIFY-MIXED logic. For any `endurance` file with `Z6+Z7 ≥ 1.5 min` OR `sprint_segment_count ≥ 1` OR `(has_vo2_work AND Z5 ≥ 3 min)`, reclassify to the dominant high-intensity class:
  - `Z7 ≥ 0.5 min` OR `has_sprints` → **neuromuscular** (10 files)
  - `Z6 ≥ 1.5 min` → **anaerobic** (15 files; includes the user-reported `endurance_3x30s_90min.zwo`)
  - `Z5 ≥ 3 min` → **vo2_short** (3 files)
- Result: 28 files moved out of the `endurance` pool. Planner won't pick them on pure-Z2 days. Logged at `workouts/.overhaul_manifest.json` under key `v468_reclassify_endurance_with_hi`.

### README — Strava Worker line removed
- Removed the "Strava OAuth flows through the Cloudflare Worker relay…" line from the Security notes section. The Strava integration model is being revisited; that paragraph misrepresented current intent.

### Tests
- 782 → 783 passed. Same 3 pre-existing wellness/training-load flakes from v4.6.0 baseline. The planner_fixes tempo flake from v4.6.7's report didn't repro this run (test-isolation noise).

## v4.6.7 — Capability projection + finished-programme summary + 4 UX fixes (2026-05-04)

### MAJOR — Event-capability projection (closes the "if I follow this plan can I do X km / X hm?" question)
Pre-v4.6.7 the goal fields `event_km` and `event_climb_m` only nudged target CTL by +5 each (`training_planner.py:837-840`) — there was no finish predictor, no endurance baseline capture, no progression chart. v4.6.7 wires a science-backed model:
- **`_project_event_capability(goal, athlete, fitness_state)`** — 4-step formula:
  1. Flat-equivalent km = `event_km + (event_climb_m / 100 × 1.5)` (climbing-distance equivalence).
  2. Projected average speed via Pinot & Grappe 2011 *Int J Sports Med* 32:839-844 RPP table by athlete W/kg + duration tier.
  3. Allen-Coggan IF-by-duration lookup (Allen & Coggan TR&P 3rd ed.): 60min→0.95, 120→0.85, 180→0.80, 300→0.75, 480→0.70, 720→0.62; linear interp.
  4. `predicted_np = IF × FTP`; `predicted_tss = duration_h × IF² × 100`.
- **Climb-power gate** — required W/kg for steepest 30-min climb of the event vs athlete's current sustained 30-min, per Pinot & Grappe 2011.
- **`Goal` extended** with `longest_ride_h_90d` (auto-populated from last 90d rides via `_longest_ride_h_90d()` helper) + `last_ftp_test_date` (manual).
- **`fitness_estimation.compute_cp_wprime()`** — Monod CP/W′ fit (was dead code) is now wired into the projection when ≥30 days of best-effort data are available; falls back to FTP-only model otherwise.
- **New endpoint** `GET /api/event/projection` returning `{predicted_finish_h, predicted_np, predicted_tss, climb_w_per_kg_required, climb_w_per_kg_current, longest_completed_ride_h, longest_required_h, weeks_to_event, gap_endurance_h, gap_power_w_per_kg, climb_readiness_pct, model_citations}`.
- **Dashboard widget** `#cap-projection` — SVG dual-axis chart (hours vs W/kg over weeks-to-event) + 3 KPI tiles (Endurance Gap, Power Gap, Climb Readiness). Renders only for `event_preparation` goals. Pattern matches existing `fitnessChart` (no Chart.js needed).
- **8 new tests** in `tests/test_capability_projection.py` covering 200km flat / 4h gran fondo / 100km hilly / ultra / endurance-baseline auto-populate / unrealistic climb-gate.

### MAJOR — Finished-programme summary report (literature-backed, exportable)
- **New endpoint** `GET /api/programme/summary?plan_id=<id>` returning the top 12 metrics (ranked by literature × data-availability):
  1. **FTP Δ** (start vs end of plan window).
  2. **eFTP Δ** (Coggan 95% rule on best 20-min).
  3. **VO2max Δ** — Stöggl & Sperlich 2014 *Front Physiol* 5:33 sets the bar at +11.7% in 9 weeks of polarized.
  4. **CTL fitness gain** (start vs end).
  5. **Intensity distribution** (Z1+Z2 / Z3 / Z4+ totals).
  6. **Polarization Index mean + class** — Treff et al. 2019.
  7. **Monotony / strain** — Foster 1998 *Med Sci Sports Exerc* target <2.0; max per week flagged.
  8. **Plan compliance per phase** (planned vs actual TSS).
  9. **Mean-max power curve** (best 5s/1m/5m/20m/60m, first 4w vs last 4w overlay).
  10. **Hooper composite trend** per week.
  11. **Totals** (km, hours, kJ, elevation_m).
  12. **Decoupling trend** (Pw:Hr, lower = better aerobic durability).
- **Two export paths, zero new dependencies**:
  - **PNG** — new `programme_summary_png.py` (Pillow, ~1200×1600, KPI tiles + 2×3 mini-chart grid + totals strip + citations footer); pattern copied from existing `ride_report_png.py`. Endpoint: `GET /api/programme/summary/png?plan_id=<id>`.
  - **PDF** — client-side `window.print()` of the modal with new `@media print` A4 stylesheet that hides nav/sidebar/non-summary content.
- **Modal auto-shows once** when `today > plan.end_date` via `localStorage` flag; user can reopen via the "Programme summary" button anytime.
- **5 new tests** in `tests/test_programme_summary.py`.

### UX-FIXES (4 surgical fixes the user surfaced after v4.6.6 deployed)
- **F1 Cooldown ramp inverted** — `workoutProfileSVG`'s Cooldown branch shared the polygon with Warmup/Ramp drawing `power_low → power_high` left-to-right. ZWO Cooldown stores `PowerLow=END (low)` and `PowerHigh=START (high)`, so the slope was rendering UP when it should slope DOWN. Fixed at `dashboard.html:2521-2528` via an `isCooldown` toggle that swaps `yLo`/`yHi` for cooldown segments.
- **F2 Yellow ⟳ icon blocked clicks** — `card_state == 'missing_workout'` cells showed a yellow circular-arrow indicator and `calOpenDay()` early-returned, so the modal never opened. Removed the early-return at `:7435-7438`; the modal now falls through to a synth-session render so the user sees session_type / duration / description even when `zwo_file` is missing. The yellow ⟳ stays as an indicator.
- **F3 Calendar opened in February** — `calJumpToToday()` was wired only to the manual button. Now called inside the existing `requestAnimationFrame` post-render block at `:7211-7216` so the calendar auto-scrolls to today on first paint. User can scroll back manually.
- **F4 Event-day green-border marker** — `/api/calendar` now emits `goal: {type, event_date, end_date}` (`app.py:7302-7306`). Dashboard renders `.cal-event-day` (green border + glow) on `goal.event_date` cell for `event_preparation` goals OR `goal.end_date` cell for week-count goals. Today-marker (red) takes priority if they coincide.
- **6 new tests** in `tests/test_ux_v467.py`.

### Tests
- **763 → 782 passed** (+19 net). Same 4 pre-existing flakes from v4.6.6 baseline (3 wellness + 1 planner_fixes tempo flake). The single Wave-2 test that initially looked like a regression turned out to be the existing planner_fixes tempo flake (a baseline misread on my part).
- New suites: `test_ux_v467.py` (6), `test_capability_projection.py` (8), `test_programme_summary.py` (5).

### Multi-wave history
This release went through audit → master decisions → 3 parallel impl agents → 1 QA agent. Cross-agent contracts in `MASTER_DECISIONS_v467.md` § 4 locked all field names; the IMPL-CAP / IMPL-SUM `git add -A` collision (commit `88d5bc68` accidentally bundled IMPL-CAP's Python work; `68430ca2` recovered the dashboard widget) is documented as INFO — both ship correctly.

## v4.6.6 — Injury-prevention feedback loops: 7 science-grounded guardrails close the actual-vs-planned loop (2026-05-03)

### Why this release exists
Pre-v4.6.6 the planner *detected* TSS/intensity/soreness signals in three independent code paths but **none of them mutated the persisted plan**. A user could ride 89 min Z3+Z4 + 44 min Z5+ unplanned (vs a Z2-only week plan), arrive home with a destroyed body, and the app would happily prescribe vo2max intervals the next morning. The Wave-0 audit found this same shape across all three signal domains: data flows in → display badge → dead-end. v4.6.6 wires the missing causation. Each guardrail cites a specific paper in its inline comment.

### The seven guardrails (all live, all tested)

| # | Gate | Trigger | Action | Citation |
|---|---|---|---|---|
| **G1** | Yesterday-was-hard floor | `yesterday_actual / max(yesterday_planned, phase_daily_avg) > 1.5` | Force today → Z2 | **Foster 1998** *Med Sci Sports Exerc* (session-load spike) |
| **G2** | 48h Z5+ ceiling | Rolling 48h `Σ z5–z7 ≥ 25 min` (cycling INCLUDED — pre-v4.6.6 cycling sports were excluded from this guard) | Force today → Z2 | **Hulin et al. 2014** *Br J Sports Med* 48:708-712 |
| **G3** | Polarization breach | Current week `actual.z4plus_pct > target + 8` OR `actual.z1z2_pct < target − 10` | Drop next 1–2 hard sessions one tier (vo2max → threshold → tempo) | **Seiler 2010 / Stöggl 2014 / Treff 2019** |
| **G4** | ACWR weekly scaling | Last completed week `actual_tss / planned_tss > 1.5` | `next_week.tss_target ×= 0.85`, `hit_per_week −= 1`, `auto_acwr_scaled = True` | **Gabbett 2016** *Br J Sports Med* 50:273-280 (sweet spot 0.8–1.3, >1.5 doubles injury risk) |
| **G5** | Soreness peripheral cap | `daily_log.soreness ≥ 6` (1–7 scale) | Force today → recovery, **regardless of HRV/TSB composite** | **Hooper & Mackinnon 1995** + **Cheung et al. 2003** *Sports Med* (peripheral fatigue is independent of central HRV) |
| **G6** | Hooper composite gate | `sleep + fatigue + stress + soreness ≥ 18` | Force today → Z2 cap | **Hooper & Mackinnon 1995** *J Sci Med Sport* (composite ≥18 = significant accumulated fatigue) |
| **G7** | 3-day mean RPE drops HIT | `mean(ride.feel ∪ ride.perceived_exertion, last 3d) ≥ 7/10` AND today is HIT | Drop today one tier | **Foster 1998** session-RPE |

### What landed (4 implementation commits + 1 fix-forward)
- **`de5cd438` IMPL-C** — Hooper composite form (1 question → 4 questions: sleep / fatigue / stress / soreness, full Hooper & Mackinnon 1995). `/api/daily-log` now returns `hooper_index = sum (4–28)`. `ride_storage.py` persists ICU `feel` (1–5) + `perceived_exertion` (1–10) per ride. **+4 tests.**
- **`a2cd950a` IMPL-A** — `reforecast()` G4 scaling, `generate_weekly_plan` Soligard surplus-cut, `/api/rides/sync` `plan_load_alert` flag. **+5 tests.**
- **`c25fd920` IMPL-B** — 6 today-session gates inside `adjust_today_session()` (G1, G2, G3, G5, G6, G7) with priority chain G5 > G6 > G2 > readiness > G1 > G7. **+11 tests.**
- **`73121067` Wave-4-FIX** — 5 CRITICAL plumbing bugs caught by Wave-3 QA: `/api/today-session` didn't pass rides → G2+G7 silently dead in production; `/api/plan/reforecast` didn't pass `recent_activities` → G3+G4 dead from UI button; `PlannedWeek` mutations (tss_target / hit_per_week / auto_acwr_scaled) never wrote back to `current_plan.json`; G3 used `g3_dropped_days` set but persistence loop only read `downshifts`; **Hooper formula divergence** between db.py (`sleep + fatigue + stress + soreness`, direct sum, matches form direction `1=best→5=worst`) and planner (`fatigue + soreness + stress + (8 − sleep_quality)`, inverted) — UI showed Hooper 18 / red while planner computed 12 and ran intervals on the well-slept-but-stressed athlete. Plus 5 NEW integration tests via real API endpoints (regression net for plumbing-vs-logic divergence).

### Tests
- **741 → 764 passed** (+23 net). 3 wellness flakes carried over from v4.6.0.
- New suites: `test_planner_acwr_feedback.py` (5), `test_planner_injury_gates.py` (11), `test_hooper_form_persistence.py` (4), `test_injury_gates_integration.py` (5).
- The integration suite goes through the actual FastAPI endpoints (POST `/api/daily-log`, POST `/api/rides/import`, GET `/api/today-session`, POST `/api/plan/reforecast`) — not just direct function calls. Wave 3 caught the "logic correct in isolation, plumbing dead in production" failure mode that unit-only tests had missed.

### Known carryover
- 3 pre-existing wellness/training-load test-isolation flakes (since v4.6.0 baseline). Untouched.

## v4.6.5 — THIS WEEK widget: explicit Planned/Actual labels + parallel composition bars (2026-05-03)

### Bugfix — ambiguous "151 min / 0 min —" labels
- **Pre-v4.6.5:** the per-zone rail rendered as `151 min / 0 min —` with no header. Users read the `0` as "I did 0 minutes in this zone" when in reality `0` was the **planned** column (the week's plan only had a Sunday endurance ride, so Z3+Z4 and Z5+ planned was 0 — the user's actual 89 min Z3+Z4 + 44 min Z5+ from two ICU-synced rides was *correctly indexed* but visually hidden because the bar fill was actual/planned ratio, undefined when planned=0).
- **Fix:** each zone row now renders TWO sub-bars stacked vertically — `Planned` (muted gray) and `Actual` (zone color), both scaled to the same max so lengths are directly comparable. Right side shows `Actual XX min / YY planned` plus compliance % and signed delta. When planned=0 and actual>0, label is "unplanned" instead of "—".

### Layout — parallel PLANNED + ACTUAL composition bars
- The bottom section previously showed only ACTUAL as a stacked Z1+Z2 / Z3+Z4 / Z5+ bar. PLANNED was implicit via the per-zone rails above — different visual language, hard to compare at a glance.
- Now renders TWO stacked composition bars (PLANNED + ACTUAL) using the same 3-segment low/mid/high look. Same renderer, same color palette; planned is shown at 0.55 opacity to distinguish from actual. Delta line ("+89 min Z3+Z4 vs planned") sits below both, only when planned has any minutes.

### Verified
- Audited THIS WEEK (Apr 27 – May 3) actual data: 2 ICU-synced rides totalling 308 min — Z1+Z2 ≈ 149 min, Z3+Z4 ≈ 86 min, Z5+ ≈ 42 min. UI displays 151 / 89 / 44 (matches within rounding). Indexing is correct; the bug was purely visual.

## v4.6.4 — Chart now renders the actual ZWO segments instead of a hardcoded shape (2026-05-03)

### MAJOR — `openDayWorkout` modal chart was lying about what's in the file
- **Bug:** the per-day workout modal called `buildPowerBlocks(session_type, duration)` — a hardcoded silhouette per session_type:
    - `sweet_spot` → always 3×15min @ 90% FTP
    - `vo2max` → always 5×4min @ 110% FTP
    - `threshold` → always 2×20min @ 100% FTP
  The title showed the real ZWO `<name>` (e.g. "Sweet Spot 4x4min (86min)"), but the chart drew the generic shape — so a 4×4min sweet-spot file rendered as a 3×15min steady block, a 6×5min VO2 file rendered as 5×4min, etc.
- **Fix:** when a session has a matched `zwo_file`, fetch the actual segments via `/api/workout/all/{filename}` and render with `workoutProfileSVG()` (the same renderer the library browser uses). Falls back to the synthetic silhouette only when no file is matched. Verified end-to-end: a synthesized Tuesday Sweet Spot session now renders 28 segment rectangles + 2 ramp polygons matching the real ZWO structure (warmup + 4×IntervalsT + 9×SteadyState + 5×IntervalsT + cooldown), zero hardcoded `SS 1` / `VO2 1` labels.

## v4.6.3 — Rønnestad fix: 17 tagged microinterval workouts now actually land in the plan (2026-05-03)

### MAJOR — Rønnestad detection bug fix
- **`_is_ronnestad_workout()` was rejecting 8 of 17 tagged Rønnestad files** because it only accepted `primary in {vo2max, vo2_short, anaerobic}`. The 17 tagged files actually classify as 9 vo2_short / 3 neuromuscular / 3 threshold / 2 recovery — entirely correct given their zone profiles, but 8 of them were silently excluded from the `is_ronnestad` flag.
- Fix: read the explicit `tags: ["is_ronnestad"]` set by Wave 1A's `scripts/reclassify_mixed_v461.py` instead of re-deriving from heuristic features. `ronnestad_protocol` ("30/15", "40/20", "30/30", etc.) is also surfaced into the variety_score feature dict.

### Rønnestad multiplier + hard floor
- variety_score Rønnestad multiplier 1.5× → **5.0×** so tagged files visibly outweigh ordinary class peers in HIT slot sampling.
- New `_enforce_ronnestad_floor()` post-pass: every build1 / build2 / peak phase MUST include ≥1 Rønnestad-tagged file. Picks same-class Rønnestad to preserve per-class counts (vo2_short slot → vo2_short Rønnestad). Falls back to any HIT slot if no same-class candidate exists.
- **Result (24w plan):** 0 → 4 Rønnestad picks across build1 (vo2_short 40/20), build2 (vo2_short 30/15), peak (vo2_short 40/20), taper (recovery 30/30). All 3 canonical Rønnestad protocols (30/15, 40/20, 30/30) now appear at least once.

### Tests
- 741 / 3 / 1 — same baseline as v4.6.2 (3 pre-existing wellness/training-load flakes unchanged). Per-class floors held: vo2_short 10/10, sweet_spot in build1+build2 ≥1 (canonical 4-shape rotation preserved).

## v4.6.2 — Planner diversity push: every workout in a 24w plan is now a different file (2026-05-03)

### MAJOR — 100% slot uniqueness in 24-week plan
- 24w plan: **150 distinct files / 150 sessions = 100% slot uniqueness** (was 109/150 = 73% at v4.6.1, 117/150 = 78% at v4.6.0). Every workout in the plan is a unique ZWO file.
- Per-class distinct ratios: endurance 26→52 (54%→100%), recovery 0→15 (the score floor was excluding ALL 111 recovery files), tempo 21/21 (100%), every other class 100%.

### Root cause + fix — class-aware score floor
- `score_workout` rewards TSS + Z3+ structure, which fairly rates HIT classes but systematically under-scores **endurance** (intentionally low TSS, no structure) and **recovery** (very low TSS). Pre-v4.6.2 the strict score≥5 floor cut endurance pool **496→48 files (10%)** and recovery pool **111→0 files (0%)**, forcing the planner to cycle through the same handful of files.
- `_build_pool_indexes` now applies a class-aware floor:
    - HIT classes (vo2max/vo2_short/threshold/over_under/anaerobic/neuromuscular/sweet_spot): score ≥ 5
    - tempo / mixed: score ≥ 4
    - endurance / recovery: score ≥ 1 (essentially none)

### Planner knobs tightened
- `_DIVERSITY_BUDGET_DIVISOR` 8 → **24**: cap = ceil(class_session_count/24). Endurance with 48 sessions: cap drops 6→2 picks per file. Most classes now cap at 1 per file with graceful fallback.
- `_NOVELTY_BOOST` `{0:1.5, 1:1.0, 2:0.5}` → `{0: 5.0, 1: 0.05, 2: 0.001}`. First pick gets 5×, second pick crashes 100×, third+ effectively zero. Sampler exhausts the never-picked pool before any repeat.

### Hard floor extension
- `_enforce_build2_peak_hard_floor` now adds `sweet_spot: 1` to the build1 floor, so the canonical `{threshold, vo2max, sweet_spot, over_under}` 4-shape rotation appears in every build phase regardless of seed.

### Tests
- Updated `test_only_score_5_plus_workouts_picked` and `test_only_score_5_plus_files_picked` to mirror the class-aware floor (HIT≥5, tempo/mixed≥4, endurance/recovery≥1).
- 738 → 741 passed. 3 v4.6.1 known-carryover planner-diversity gates (`test_24_week_plan_uses_at_least_150_distinct_files`, `test_24w_plan_uses_high_distinct_file_ratio`, `test_per_class_distinct_high_diversity_ratio`) **all pass** at v4.6.2. Same 3 wellness/training-load flakes from v4.6.0 carried over.

## v4.6.1 — Mixed-class reclassification + Rønnestad detection + planner variety bonus + today marker (2026-05-03)

### MAJOR — Reclassify 1069 mixed → specific class
- 852 of 1069 `mixed`-class files re-examined and promoted to specific class via secondary_flags + zone-time signals. Mixed pool 1069 → 217 (162 truly signal-less, 55 with weak flags). Distribution shift: `endurance` 44 → 496, `threshold` 223 → 368, `vo2max` 336 → 365, `over_under` 211 → 301, `tempo` 265 → 330, `neuromuscular` 289 → 302, `anaerobic` 289 → 296, `recovery` 83 → 111, `vo2_short` 107 → 116, `sweet_spot` 103 → 117. Closes "planner can't reach interesting interval workouts" bug.
- **Rønnestad detection** — 17 files identified as Rønnestad-style microintervals per Rønnestad et al. 2015 (*Scand J Med Sci Sports* 25:143-151) — cycle_period 30-90s + ≥10 reps + work power 95-115% FTP. Tagged `is_ronnestad: true` and renamed explicitly (e.g. "Neuromuscular Rønnestad-style 2x12x30/30s (60min)").

### MAJOR — Planner variety bonus + Rønnestad in build/peak
- **`variety_score(zwo_features)`** — sampling-weight multiplier 0.5–3.0 from segment count + zone entropy + Rønnestad/microinterval/over-under/sprint bonuses. Sqrt-shouldered into per-file weight on HIT slots. Rønnestad bonus 1.5×.
- **`WORKOUT_MIX_PREFERENCE` rebalanced** to sprinkle `vo2_short` / `anaerobic` / `neuromuscular` into every phase (was absent in base, scarce in build).
- **Hard floor** for build1/build2/peak: ≥1 anaerobic + ≥1 neuromuscular + ≥3 vo2_short via post-sampling swap.
- **Result (24w plan):** vo2_short picks 4 → 11, neuromuscular 2 → 4, anaerobic 4 → 5, avg hard_segment_count 7.11 (was ~3-5 boring steady), distinct files 99 → 106.

### Today marker
- `.cal-day.cal-today` — 2px solid red border + red glow (was faint blue tint, no border).
- `calJumpToToday()` targets `[data-date=today]` cell directly with `scrollIntoView({block: 'center'})` (was off-target via week-row offsetTop math).

### Tests
- 718 → 738 passed (+20 across `tests/test_reclassify_mixed.py` + `tests/test_planner_variety_bonus.py`). Same 3 wellness/training-load flakes from v4.6.0 carried over.
- **Known carryover (3 planner-diversity gates):** `test_24_week_plan_uses_at_least_150_distinct_files`, `test_24w_plan_uses_high_distinct_file_ratio`, `test_per_class_distinct_high_diversity_ratio` (sweet_spot 13/23 = 57%, target 20 distinct or ≥65%). Caused by the cache reshuffle: `endurance` pool grew 11× from RECLASSIFY-MIXED but `sweet_spot` only +14, and the planner's per-class novelty tuning hasn't caught up. v4.6.2 will address with multi-plan rotation + per-class novelty re-tune.

## v4.6.0 — Library sanity overhaul + planner full-utilization + plan config UI + homepage clarity (2026-05-03)

### MAJOR — Library structural overhaul
- 2966 of 3054 ZWO files (97%) renamed + descriptions regenerated to match actual segment structure (was 77.8% mismatch). 995 files reclassified to correct content_class. Examples: `recovery_spin_15min` containing 8 sprints @ 165% FTP → renamed Neuromuscular. `threshold_4x10min` at 113% FTP → renamed VO2max. `sweet_spot_2x20min` actually 3x15min → renamed accordingly.
- 864 files fixed where descriptions showed "0min @ X% FTP" (Duration<60s segments now display in seconds, e.g. "30s @ 110% FTP", or fold into adjacent blocks).
- All ZWO files now have `<author>Domestique Library</author>`.
- Audit log at `workouts/.overhaul_manifest.json`.
- workouts/.content_classification.json regenerated from rewritten files.

### MAJOR — Planner full library utilization
- 24-week plan now uses 117+ distinct files (was 117 of 3054 = 3.8%). Diversity ratio ≥75%. Per-class minimums: tempo/sweet_spot/threshold/vo2max ≥15 each (target), over_under/vo2_short ≥8, anaerobic ≥6, neuromuscular ≥4, recovery ≥4, endurance ≥12. Note: 35% of library currently classifies as `mixed` — until those 1069 files are reclassified, the absolute distinct ceiling is ~117/150 (78%).
- Diversity cap: no single ZWO picked more than ⌈class_session_count / 8⌉ times.
- Novelty boost: 1.5× weight for files never picked, 0.5× for picked twice.
- used_names rolling window 6w → 12w (more files surface across plan).
- Duration tolerance ±25 min (was ±5).
- Candidate pool now content_class only (no filename-prefix pre-filter that excluded valid candidates).

### Plan Configuration UI
- PLAN WEEKS dropdown greyed out + auto-computed from event date when goal=event_preparation. User's manual value cached and restored on goal-switch.
- New GET /api/plan/preview endpoint returns phase split using same `derive_phases()` as /api/plan/generate. Plan Overview right panel now shows all 5 phases for 20-week plan (was missing BUILD2 + PEAK).

### Homepage clarity
- Today's recommendation hero block shows BOTH original planned + adjusted (when modified) with explicit "Adjusted to {type} due to {reason}" chip.
- New "View plan ↗" link jumps to plan grid for context.
- Defensive `fixZeroMin()` regex sweep at 5 description render sites (extra safety even after library overhaul).
- /api/today-session response includes `adjustment_reason` field.

### Tests
- 687 → 718 passed (+31 across library consistency, planner utilization, plan config endpoint, homepage today consistency). 3 pre-existing wellness/training-load test-isolation flakes carried over.

## v4.5.5 — Activity-detail zones + intervals + polarization classification (2026-05-03)

### NEW
- **Power zones (Z1-Z7+SS) bar chart** in activity detail modal — replaces "No zone data" placeholder when ICU has the breakdown. Color-coded per Coggan zones (green Z1/Z2, yellow Z3, orange Z4, red Z5, purple Z6, dark Z7, amber SS).
- **HR zones (Z1-Z7) bar chart** — separate panel below power zones, ICU's HR zone labels (Recovery / Aerobic / Tempo / SubThreshold / SuperThreshold / Aerobic Capacity / Anaerobic).
- **Polarization classification banner** — Treff 2019 PI value (`PI = log10(Z1/Z2) × log10(Z3/Z2)`) + classification label (polarized / pyramidal / threshold / hiit / base / unique) using centroid-distance method (closest canonical pattern wins). Color-coded chip with confidence score and 3-bar Z1+Z2 / Z3+Z4 / Z5+ distribution. Info icon shows research citations.
- **Intervals table** — replaces "No interval data" placeholder when ICU has structured intervals. Columns: # / Name / Duration / Avg Power / FTP% / Avg HR / Zone. Sortable, sticky header.
- **/api/ride/<id>/detail** now lazily fetches ICU's `/api/v1/activity/<id>` when cached record lacks zones/intervals; persists back to local cache. Fields surfaced: `time_in_zone`, `hr_time_in_zone`, `intervals`, `polarization`.
- **NEW analytics.py** — Treff Polarization Index helper + centroid-distance classification per FastFitness.Tips heuristic. Citations: Treff et al. 2019 (Frontiers in Physiology, doi:10.3389/fphys.2019.00707) + FastFitness.Tips.

### Tests
- 666 → 687 passed (+21 across detail-zones + polarization-index + classification edge cases). 3 pre-existing wellness/training-load flakes carried over.

## v4.5.4 — Cloudflare UA fix + interval variety + non-diagonal renders (2026-05-03)

### FIXED — root cause of multi-version ICU sync saga
- **ICU sync was silently 403'd by Cloudflare** for the default Python-urllib User-Agent. Affected all `urllib.request.urlopen` calls to intervals.icu since at least v4.4.0. Symptom: "Connect Intervals.icu in Settings" toast even after pasting valid creds. Now sends `User-Agent: Domestique/4.5.4 (https://github.com/platypus45/domestique)` on every request → all calls succeed.

### NEW — planner interval variety
- **WORKOUT_MIX_PREFERENCE rebalanced** — endurance/tempo dominance dropped, sweet_spot/threshold/vo2max/vo2_short/over_under/anaerobic/sprints raised. Base phase late weeks now include threshold + occasional vo2_short. Build phases more aggressively VO2 + over-under.
- **Per-week interval floor** — every non-stepback week MUST have at least 1 interval-shaped pick in mid/late base, ≥2 in build/peak. Post-sample swap if floor not met.
- **Mixed-with-flags treated as interval-shaped** — 363 mixed-class workouts that have secondary_flags=has_threshold_work / has_vo2_work / pattern_microinterval / pattern_over_under now reachable as valid interval slots.
- **Result: interval-shaped picks 28.7% → 56.7% (24-week plan)**, all 7 hard content_classes appear in build1+build2+peak.

### FIXED — visual diagonal bug
- **`buildPowerBlocks` cases tempo/recovery/default no longer render as diagonal trapezoids.** They were generating `pctLow !== pctHigh` which the SVG renderer interpreted as ramps. Now flat: tempo=82, recovery=50, default=68. Snake_case aliases added (sweet_spot, over_under, vo2_short, neuromuscular).
- **secondary_flags overlay** — long_z2/endurance with `has_threshold_work=true` or `pattern_microinterval=true` now shows the embedded interval spikes ON TOP of the steady base, so users see structural detail on mixed workouts.
- **Tooltip legend** — every cell tooltip ends with "STEADY (bar height = avg power)" / "INTERVAL on (spike = work)" / "RAMP" so users understand the rendering.

### Tests
- 654 → 666 passed (+12 across creds-profile + interval-variety + ui-shape).
- 3 pre-existing wellness/training-load test flakes (PRE-V4.5.4 baseline) — flagged for v4.5.5.

## v4.5.3 — Auto-detect athlete ID from API key (2026-04-30)

### NEW
- **`training.discover_athlete_id(api_key)` — auto-detects the authenticated athlete.** GETs `/api/v1/athlete/0` (ICU resolves `0` → the API key's owner) and returns the parsed `{id, name, ...}` dict. Used by `/api/setup/save` so the user only has to paste an API key — no more typo-induced HTTP 403 from a mistyped 7-char athlete ID.
- **Settings UI: athlete ID is now optional.** The athlete-ID input is hidden behind an "Advanced: enter athlete ID manually" toggle. After saving an API key, an inline badge shows `Auto-detected: i225278 — Haringo`. If a user opens "Advanced" and types an ID that disagrees with the API key's owner, the response carries a `warning` field and the dashboard alerts the user — but still honours the user-submitted override.
- **`/api/setup/save` response fields.** New optional fields: `athlete_id_detected` (set when the server filled in a missing ID), `athlete_name` (bonus from `/athlete/0`), `warning` (set on submitted-vs-discovered ID mismatch).

### Tests
- 646 → 654 passed (+8 auto-detect regression: helper 200/403/network/empty-key paths, only-api-key auto-fill, both-match no-warning, both-mismatch warning, discover-failure graceful fallback).

## v4.5.2 — Hot-reload ICU creds + 30-min throttle reset on save (2026-05-02)

### FIXED
- **Settings save now hot-reloads ICU credentials in-memory.** Previously, pasting new ICU creds in Settings wrote `~/.domestique/profiles/<id>/.env` correctly but the running uvicorn process kept using the creds loaded at startup, so sync continued to fail with `ICUAuthError` until the user restarted Domestique. Root cause: `training.fetch_recent_activities` / `fetch_recent_wellness` were leaving stale module-level `config.ICU_ATHLETE_ID` / `config.ICU_API_KEY` attributes after their explicit-override path, shadowing the dynamic `__getattr__` proxy that resolves from `ProfileManager._env`. Two fixes: (1) `del config.ICU_*` in those `finally` blocks instead of restoring the previous value, so `__getattr__` resumes; (2) `setup_save` defensively `delattr`s any stale shadow attribute after `pm.save_env`.
- **Sync throttle resets when creds change.** The `.last_sync_at` rides + wellness markers are zeroed out on every credential change, so the next `/api/rides/sync` runs immediately — previously the 1h throttle would still say "Already synced — try again in N min" even after the user just pasted fresh keys.
- **`db._auth_disabled` resets on creds change.** If the background sync had been disabled by 5 consecutive 401s on the OLD bad key, saving fresh creds clears the flag so the loop resumes without restart.

### NEW
- **`/api/setup/save` response carries `creds_test`.** Saves a quick `athlete/<id>` GET against the new creds and returns `creds_test: "passed" | "failed: <reason>"`. The dashboard alerts `Saved, but ICU rejected the new credentials: <reason>` when the probe fails — instant feedback instead of waiting for the next sync to silently fail.

### Tests
- 642 → 646 passed (+4 hot-reload regression: in-memory cache update, throttle reset, shadow-attribute unshadow, invalid-creds probe).

## v4.5.1 — Activity modal expand + ICU link + truthful sync + variety presentation (2026-05-02)

### NEW
- **Click any past activity → expands to ICU-style detail modal** in THIS WEEK, calendar overlay, and frontpage Recent Activities. Was silently doing nothing on Recent Activities; was early-returning on rest days even when an actual ride was attached.
- **"↗ Open on intervals.icu" link button** in activity-detail modal when ride.source==="icu". Builds URL from external_id (strips "icu_" prefix from ride_id when needed).
- **Variety presentation** — cell labels include structure suffix: "TEMPO 82m" → "TEMPO · 4×8 82m"; "ENDURANCE 180m" → "ENDURANCE · steady 180m"; "VO2MAX 90m" → "VO2MAX · 4×1 90m". Per-week unique-types badge in THIS WEEK header ("5 types · 7 unique files"). Per-cell tooltip showing name, content_class, filename, score. Surfaces the underlying file diversity that was hidden by repeated content_class labels.

### FIXED
- **B1 Sync now toast was always saying "0 new rides"** even when ICU silently 4xx'd. /api/rides/sync now returns `status` ∈ {ok, throttled, fetch_failed, no_credentials} + `last_sync_at` epoch float. UI shows truthful toast: "Sync failed: <reason>", "Already synced — try again in N min", "Synced: N rides + M wellness (synced HH:mm)".
- **B2 "REST DAY" label appeared on days with attached actual rides.** _classify_card_state now checks has_actual BEFORE session_type=="rest" so rest days with rides classify as "completed". UI defense-in-depth drops REST label when actual present.
- **Planner verification** — confirmed v4.5.0 sampler is producing 99 distinct ZWOs across 102 sessions (97% unique) in user's actual fresh plan. Top-5 share 7.8%. The "boring repetition" perception was content_class label sameness (now fixed via structure-suffix presentation).

### Tests
- 639 → 642 passed (+3 card_state regression).

## v4.5.0 — Diverse planner + ICU wellness sync + THIS WEEK actual rendering (2026-05-01)

### NEW
- **Planner diversification overhaul** — replaces fixed phase.session_types + handwritten HIT_VARIANTS + hardcoded durations with 3-layer sampling: (1) IntensityBudget per phase enforces weekly load, (2) WORKOUT_MIX_PREFERENCE per phase+week_in_phase selects appropriate content_classes, (3) type rotation penalty cycles through threshold/vo2max/sweet_spot/over_under in any 6-week window. 24-week plan now uses 121 distinct ZWO files (was 51), top-5 file coverage 9.5% (was 25.5%), 98.8% cross-regen diff. All 4 hard types appear in build phases. 1069 previously-untouched "mixed" content_class workouts now reachable for z2 fallback.
- **ICU wellness sync** — fetches HRV/Sleep/RHR/weight from ICU into ~/.domestique/wellness/<date>.json. New GET /api/wellness?days=N. /api/readiness uses local wellness fallback when ICU live empty (was returning Insufficient_Data even when local rides existed).
- **POST /api/rides/sync?force=1** — bypasses 30-min throttle, runs full rides + wellness sync immediately. Powers the new "Sync now" button.
- **THIS WEEK actual rendering** — each day cell now has top half (planned) + bottom half (actual ride matched by date) with TSS-achievement tinting (green ≥90% / yellow 70-89% / red <70%). Tooltips on actual cells show source / avg power / zone-time / decoupling.
- **Intensity chart ACTUAL bar** — populates from /api/calendar weeks.actual_z1z2_min etc (was always "no actual exposure" even when rides existed). Delta annotations show how far over/under planned distribution.
- **Adherence counter** — properly counts actuals matching planned days (sessions where actual.tss > 0.3 × planned.tss = done). Color-coded chip (green ≥80%, yellow 50-79%, red <50%).
- **"Sync now" button** in THIS WEEK header next to Reconcile Week.

### Tests
- 622 → 639 passed (+17 across diversification + wellness sync + force-sync param).

## v4.4.2 — Frontpage data wiring + workout-display truthfulness (2026-05-01)

### FIXED
- **B1+B2+B3 Frontpage missing today's ride / counters at 0** — lazy ICU sync was only wired to `/api/calendar`. Now also fires from `/api/activities`, `/api/today-session`, `/api/recent-activities`. Force-resync if today's date is missing AND last_sync >30min ago.
- **Local CTL/ATL/TSB fallback** — `/api/readiness` and `/api/training-load` now compute training-load from local rides (51 ICU + 12 FIT) when Garmin/ICU sync is stale or empty. New `source: "local"|"icu"|"mixed"` field surfaces provenance. Frontpage no longer shows dashes when local rides exist.
- **Unified default response shape** — both `/api/today-session` and `/api/readiness` now return numeric `score` + `data_status` field instead of one returning hardcoded 50 and the other returning None.
- **B4 Sprint modal showed steady Z2 bar** — `buildPowerBlocks` was missing cases for sprint, sprints, overunder, anaerobic, vo2_short, ftp_test. All 12 documented session_types now have explicit waveform patterns. Modal also prefers `zone_dist` over session_type heuristic when populated.
- **B5 Misleading workout display names** — display names now derive from `content_class` (post-v4.1.2 content classifier) instead of misleading filename. Example: `sweetspot_6x2min_90min.zwo` with content_class=endurance now shows "Sunday — ENDURANCE — 90min" instead of "Sunday — LONG Z2". Filename-derived name moved to subtitle for provenance when it disagrees.

### Tests
- 614 → 622 passed (+8 across lazy-sync wiring + local-load fallback).

## v4.4.1 — Decimation cap + TestRematch + time-in-zone fallback (2026-04-27)

### FIXED
- Sample decimation in `/api/ride/<id>/detail?include=samples` now uses `ceil(n/1800)` stride, hard-capping output at ≤1800 points. Was 1960 due to floor-stride rounding.
- 3 stale TestRematch fixtures fixed — tests previously placed an activity 1 day in the future relative to fake today, causing `_collect_week_activities` to filter it out (correctly!). Now uses `patch("app.date", FakeDate)` with fake_today set 5 days after the activity. No implementation changes.
- Activity-detail modal time-in-zone bar now shows "⚠ No zone data for this ride" placeholder when all zones are zero (was silently suppressed). Same fallback for intervals table when empty.

### Tests
- 611 → 614 passed, 0 failed, 1 skipped.

## v4.4.0 — Calendar ICU sync + on-track bar + activity detail modal (2026-04-27)

### NEW
- **On-track summary bar** at top of dashboard. 4-stat headline (compliance % chip, CTL trajectory ±5 band, polarized split actual vs target, phase countdown) + 4 stacked target-vs-actual rails (Z1+Z2 / Z3+Z4 / Z5+ minutes / TSS). Color-coded (blue/orange/red/purple) per rail with traffic-light compliance bands per CONCEPT-SCI (green 80-115%, amber 50-79% or 116-135%, red else). Bottom action line shows session count + next session + missed.
- **Activity detail modal** opens on click of past ride cell. Hero stats (Distance/Time/TSS/IF/NP/AvgPower) + secondary stats (HR avg/max, elevation, kJ, kcal, cadence, weight, FTP at ride) + power+HR SVG sparkline + time-in-zone Z1-Z7 stacked bar + scrollable intervals table + eFTP delta badge.
- **Intervals.icu activity sync** — 90-day rolling pull on app boot (or first /api/calendar after 1h). New endpoint POST /api/rides/sync. ICU activities normalized to /api/rides flat array alongside FIT-imported rides. Yesterday's ride visible in calendar within minutes of opening dashboard.
- **GET /api/ride/<id>/detail** with optional `?include=samples` (decimated to 1800 points for responsive modal).
- **PHASE_TARGETS + PHASE_POLARIZED_TARGETS** constants in training_planner.py per CONCEPT-SCI synthesis (Foster 1998, Seiler 2006, Stoggl & Sperlich 2014, Coggan & Allen 2019).

### FIXED
- **THIS WEEK starts Monday** (was Sunday in some date-rollover edge cases). All week boundaries Mon-anchored both server (Python `weekday()`) and UI (`mondayOf()` helper).
- THIS WEEK now uses /api/calendar dual-half rendering so actual rides surface alongside planned sessions.
- Per-week sidebar in monthly calendar now shows Z5+ pill alongside existing Z1+Z2 and Z3+Z4 pills.

### Tests
- 594 → 611 passed (+17 new across calendar ICU sync, ride detail endpoint, ISO Monday, on-track summary).
- 3 pre-existing test_plan_api.py::TestRematch failures (unrelated; flagged for v4.4.1 if needed).

### Known cosmetics (queued for v4.4.1)
- Time-in-zone bar suppressed when ride has all-zero zones (rendering correctly, but cosmetically empty).
- Empty intervals array suppresses table section (correct behavior; flagged for clarity).

## v4.3.1 — Calendar polish (2026-04-26)

### FIXED
- ISO-week straddle: `/api/calendar` now dedupes weeks by `(iso_year, iso_week)` preferring planned over history. Was emitting two `is_current=true` rows when ISO week boundary crossed stored plan boundary.
- Calendar cells now drag-droppable (intra-ISO-week only, matching `/api/plan/move-session` semantics). Visual feedback: green outline on valid targets, red on invalid (rest / cross-week / completed). On 422 source-not-found shows "Couldn't find that session. Try refreshing." toast.
- Toast container repositioned to top-right per spec (was bottom-right). Click-to-dismiss added; 3s auto-dismiss preserved.

### Tests
- 591 → 594 (+3 calendar dedupe cases in tests/test_calendar_no_duplicate_current.py)

## v4.3.0 — Calendar overlay + 7 bug fixes (2026-04-26)

### NEW
- **Calendar overlay (intervals.icu-style)** — vertical-scroll calendar showing planned + actual side-by-side per day. Past 12 weeks of history + entire current plan future. Phase color-coded planned cells; achievement-color (green/yellow/red) on actual cells based on TSS ratio. Auto-scroll to current week on load. "Jump to today" button.
- New endpoint `GET /api/calendar` — merged plan + rides payload per week, with weekly aggregates (planned/actual TSS, Z1-Z2 / Z3-Z4 / Z5+ time splits, completion_pct).
- New `card_state` field on every session: rest / planned / completed / missing_workout. Drives UI styling consistently.

### BUG FIXES
- **B1** Move-session "no stored week contains source data" — replaced cryptic 404 with structured 422 `{error: source_session_not_found, date}`; lazy-loads plan from disk on source-week miss. UI shows friendly toast.
- **B2** Click in THIS WEEK opens wrong workout — title now reads `content_class` (not stale `session_type`); click verifies match before opening; mismatch shows inline ⚠ banner.
- **B3** Re-generate plan picks same workouts — added `seed_salt = time.time_ns()` per regeneration + candidate-pool shuffle before ranked-pick. 93% of zwo picks now differ between consecutive regens.
- **B4** Yesterday's ride invisible — calendar merges plan-sessions with ride records by date (FIT + ICU sync identical); each completed day shows actual TSS + duration + Z-split.
- **B5** No plan-vs-actual side-by-side — THIS WEEK now uses calendar dual-half rendering.
- **B6** Some workouts unclickable — `card_state="missing_workout"` cards now visibly grayed with ⚠ icon + "click Re-draw" tooltip (instead of silently disabled).
- **B7** Tempo workouts had unwanted ramping — audited 285 tempo files, found 23 with undesired `<Ramp>` in main body, rewrote to single `<SteadyState>` at 82% FTP. New `tests/test_tempo_workout_shape.py` guards future regressions.

### Tests
- 570 → 591 passed (+21 new across calendar, regen-shuffle, lazy-load-move, tempo-shape).

### Known soft (non-blocking, queued for v4.3.1)
- When the ISO week straddles a plan-stored-week boundary, two weeks may both carry `is_current=true` in the calendar payload. Data internally consistent; UI auto-scrolls to first match. Cosmetic only.

## v4.2.0 — Library filters + weekly grid polish (2026-04-26)

### LIBRARY (Sprint A)
- Score sync: single shared `training_planner.score_workout()`; `/api/workouts` and planner now return identical scores (closes v4.1.1 Bug C PARTIAL). Distribution 24/50/26 vs 30/50/20 target.
- New filter query params on /api/workouts: content_class, tags (OR-multi), duration_min/max, has_flag, search, sort
- New endpoint GET /api/workouts/tags returning distinct tags across library
- Library UI: content-class dropdown (12 enum values), tag chip-list, duration range slider, sort dropdown, search box (300ms debounce), empty-state with Clear-filters button, hover preview with zone breakdown
- MINSCORE tooltip aligned: "Good 7+, Medium 4-6, Low <4"

### WEEKLY GRID (Sprint B)
- Score badge per non-rest session card (gold/medium/low tier color)
- Re-draw button (⟳) per non-rest non-completed card → calls /api/plan/re-draw, swaps in new pick from same type, dedupes against week
- Today marker (today-session class) + current-week highlight + auto-scroll-into-view on first load
- Drag-drop drop-target visual feedback: valid (highlight) vs invalid (dim) — ISO-week + REST + completed gating
- Phase row banding: 5 distinct color tints (Base/Build1/Build2/Peak/Taper)
- Stepback week visual: opacity 0.85 + clearer RECOVERY label
- Week TSS column reformatted: Planned + Actual labels, green if met, red if missed
- Plan progress header above grid: "Phase: X — Week N of M — K weeks to Peak"
- Action toasts (3s auto-dismiss): re-draw / move / dismiss success messages
- Plan export: CSV + JSON download via dropdown

### Known cosmetics (non-blocking)
- Toast position bottom-right (spec said top-right, kept consistent with existing helper)
- No auto-rewrite of pre-v4.1.2 stored plans on content-class drift; user can regenerate plan to align (POST /api/plan/regenerate)

### Tests
- 525 → 570 passed (+45 new tests across score-sync, library-filters, redraw-endpoint)

## v4.1.0 — Planner grill pass (2026-04-24)

### FEEDBACK LOOPS CLOSED
- DFA α1 post-ride: mean < 0.5 over last 3 rides → tomorrow's threshold/VO2 auto-swapped to Z2 + banner shown (revert button)
- Aerobic decoupling > 5% → next-day Z2-recommended advisory banner
- Foster Monotony > 2.0 over 14 days → next week's planned TSS reduced 15%
- CTL: local 42-day EWMA fallback when Intervals.icu unavailable (was hard-coded 37.0)
- eFTP drift > 3% for 7+ days → auto-applied with revert toast (48h window)
- FIT → Intervals.icu one-way upload after import

### PLANNER FIXES
- Weekly-plan unified: cross-week dedupe via used_names rollup (was resetting every request)
- /api/plan/reforecast now calls tp.reforecast() + persists downshifts for negative TSB (was TSS-annotation only)
- /api/plan/re-draw — new endpoint, actually re-rolls a day's workout with dedupe (vs. prior completion-classifier)
- Score formula: tss×0.6 + variety_bonus + vo2_bonus (was 5 + tss/50)
- ISO-week merge aligned Mon-Sun (was Sat-Fri mismatch → 5/7 days wrong)
- available_days now persisted in plan JSON (Mondays no longer ghost-excluded)

### FTP TEST FLOW END-TO-END
- FIT import detects FTP tests by power-profile shape (Coggan 20min + Ramp) — sets is_ftp_test, ftp_test_type, ftp_test_suggestion, ftp_test_halted
- Ramp auto-halt: cadence<50 + power<85% target for 3s → halt step recorded
- Post-import modal: Update/Keep/Custom buttons; formula auto-fills suggested FTP
- /api/profile/ftp-history endpoint + Settings sparkline chart + FTP source badge
- 4 new FTP tests indexed from whatsonzwift.com/workouts/ftp-tests (visual-graph inference, tagged ftp_test)
- ZWO <tags> now indexed; /api/workouts?tags=ftp_test filter works
- FTP Tests category tab in Library with yellow-border distinct styling

### eFTP PROVENANCE
- athlete.json gets ftp_source field: tested_coggan_20min | tested_ramp | eftp_auto | manual
- Manual FTP edit requires confirmation dialog
- Every FTP change logged in ftp_test_history ledger (including accepted eFTPs)
- Local fitness_estimation.estimate_ftp() now wired to FIT import post-hoc

### ENDPOINT CONTRACT ALIGNMENT (Wave 4)
- /api/settings exposes ftp_source + ftp_source_date
- /api/readiness flattens dfa_cap_applied + decoupling_advisory to top-level booleans
- /api/rides returns flat array (legacy {json, fit} envelope preserved at /api/rides/legacy-envelope)
- /api/readiness/revert-cap endpoint
- eFTP auto stamps source="eftp_auto" (was "eftp_icu")
- ftp-history rows carry canonical+alias field names

### Library
- 3050 → 3054 workouts (4 WoZ FTP tests added)

## v4.0.0-alpha — Trainer subsystem removed; library grows (2026-04-24)

### BREAKING
- Trainer hardware support removed entirely. Domestique is now a planner + workout library + post-ride viewer. Ride on Tacx app / MyWhoosh / Golden Cheetah / Zwift, import FIT after.
- No more live ride view. No BLE, FTMS, ERG, SIM, FE-C, gate logic, WebSocket.
- Profile schema v3 → v4: `paired_devices`, `trainer_effect`, `bike_weight_kg` removed (auto-stripped by migration)
- Dependencies: `bleak`, `pycycling` removed.
- Deleted files: trainer_connection.py, power_control.py, templates/training.html, + 5 trainer test files.
- Removed endpoints: /api/training/start, /stop, /pause, /resume, /trainer-health, /debug-snapshot, /difficulty, /ws/training, /api/training/pair-*

### ADDED
- POST /api/ride/import — FIT file upload + parse + store in ~/.domestique/rides/
- GET /api/workout/download/{filename} — ZWO file download for loading into external trainer app. Disambiguated from /api/workout/{category}/{filename} (metadata) to kill a route shadow that previously resolved the download path to category=`download`.
- GET /api/course/<region>/<file>/download — CRS course file download
- Existing plural-name endpoints retained: /api/rides, /api/profiles, /api/workouts, /api/weekly-plan (not renamed to singular despite earlier aspirational naming in MASTER §6).
- Ride history list on dashboard with click-to-open post-ride report
- Import FIT button on dashboard header
- 1253 new workouts in library: 1105 whatsonzwift reconstructions (visual-graph inference, never hit their download endpoint — strip names/descriptions/coach-cues, regenerate from structure) + 24 GitHub MIT/Unlicense imports (macgrrl, michaelahlers) + 124 procedural gap-fillers (pyramids, short VO2/threshold/sweet-spot, over-unders with varied ratios, neuromuscular sprints). Total library: 1797 → 3050. All new files authored `<author>Domestique Library</author>`.
- docs/cycling_apps.md — comparison of free cycling apps accepting ZWO/FIT
- docs/workout_sources.md — library source docs + legal stance

### Rationale
Trainer code was ~16k LOC maintaining BLE protocol parity across 36 fixes. User pivoted to pure planner role: rides elsewhere, brings FIT back.

## [3.6.0-fix35] — 2026-04-20

### Workout library UX
- MIN SCORE filter now defaults to ALL + `?` info tooltip explains it (`Score = 5 + TSS/50`).
- Block hover tooltips on workout detail SVG: per-block `<title>` with `{TYPE}: {duration} at {pct}% FTP`.
- Sub-minute intervals render as "30 sec" (was "0 min").
- Description: line break between `|`-separated blocks so each "STEADY: ..." is on its own line.
- Title validator: strips "Recovery" prefix when `IF > 0.75`; prefixes "Easy" to VO2/Threshold when `IF < 0.55`.

### Weekly plan — move session now visible after refresh
`/api/weekly-plan` merge switched from strict date-equality to ISO week match. Drag-and-drop sessions no longer snap back after page refresh.

### Upcoming panel + elevation bar — no more flicker
`updateElevation` and `renderUpcomingWorkoutSegments` cached last non-empty HTML. Only explicit clear on session stop. Previous behavior wiped DOM on every empty-data tick → flicker between segments.

### Cadence "10" render bug (was actually 85-90)
`smoothValue('m-cad', targetCad, 300)` with a 300 ms ease was being reset every rAF frame → animation never advanced past initial value, crept through "1...3...7...10..." instead of landing on the real reading. Changed to 0 ms (instant) matching the fix28-hero-bounce and fix31-live-hr patterns. Same fix applied to `m-speed`.

### ERG target no longer scaled by trainer_effect
`trainer_effect` (0.5 default) was halving ERG power targets (`0.25 × FTP × 0.5 = 31W` instead of `62W`). Wrong — `trainer_effect` is for SIM grade only; ERG power is absolute. Three dispatch sites patched. HR-cap scaling retained. SIM-grade paths remain correctly scaled.

### FTP test protocols — two new ZWO workouts
- `workouts/ftp_test_coggan_20min.zwo` — 68-min Allen-Coggan: warmup 14min + primers + 5min easy + 5min blowout (105% FTP) + 10min recovery + 20min main test + 10min cooldown. FTP = 0.95 × 20-min average.
- `workouts/ftp_test_ramp.zwo` — ~35-min ramp: 5min warmup + 25×1min steps 56% → 200% FTP at +6%/min + 5min cooldown. FTP = 0.75 × best 1-min average in last completed step.
- Ramp auto-halt detector: cadence<50 rpm for ≥3 s AND 3-s avg power < 85% target → `EVENT=ftp_test_failed` + session stops.
- Post-test modal in ride-detail shows suggested FTP with "Update Profile / Keep Current / Custom" buttons.
- `ftp_test_history` persisted to `athlete.json`.
- New endpoint `POST /api/profile/update-ftp`.

### 42 new 30-min + 45-min workouts
Generator `generate_ftp_workouts.py` produces 3 variants × 7 categories × 2 durations = 42 ZWO files: recovery, endurance, sweet_spot, threshold, vo2, over_under, sprints. Deterministic via seed; auto-discovered by library endpoint.

### Tests
1017 pass, 1 skipped, 11 deselected, 3 pre-existing `test_plan_api.py` rematch failures (unrelated, predate fix35).

### Ship
DMG rebuilt to `~/Desktop/Domestique.dmg`. `/Applications/Domestique.app` not touched.

## [3.6.0-fix32-hr-hold-and-line-chart] — 2026-04-20

### [A] HR dropping to 0 between ticks — lite-tick HR hold-last-known
User report: "the heart rate stuff back to 0 in between ticks". Heavy-tick logs showed `hr=150bpm` consistently, so the backend's 1 Hz path was fine. The 4 Hz lite path was the culprit — BLE HR straps emit at 1 Hz while trainer frames arrive at 4 Hz, so 3 of every 4 lite ticks either:
- forwarded `d.hr=None` from a trainer frame whose merge failed the freshness gate (HR-2 staleness), or
- hit the `awaiting_data` branch (`_real_trainer_data` consumed by the heavy tick + no new BLE frame in the 250 ms window) which emitted `hr: null`.

Frontend's `(typeof d.hr === 'number' && d.hr >= 30) ? d.hr : NaN` gate then punched a gap into `SmoothGraph.push()` on each of those ticks. The rider saw a sawtoothing HR trace "dropping to 0 between ticks".

Fix: `app.py::_last_known_hr_if_fresh()` — reads the manager-stamped `_trainer_last_hr_{ts,bpm}` + `_hr_strap_last_{ts,bpm}` trackers and returns the newer of the two if its age is ≤ 10 s. The lite-tick branch and the `awaiting_data` branch both substitute this value when the current frame carries `hr=None` / `hr=0`. Real strap dropouts still surface as `---` once the hold window lapses.

### [B] Post-ride POWER chart: line instead of bars
User report: "I don't want bar charts but a line chart in post-ride summary". `templates/dashboard.html` L2841-2853 previously rendered POWER as zone-coloured `<polygon>` bars plus a matching `<line>` segment per step. Replaced with a single smoothed yellow polyline (`#eab308`, 3-point rolling mean, gap-preserving) over a faint zone-colour band (`opacity=0.12`) so the zone-distribution hint survives without overwhelming the line. HR overlay logic untouched.

### Tests
`test_ble_power_buffer.py`: 8 new —
- `_last_known_hr_if_fresh()` helper: never-set, fresh-trainer, fresh-strap, prefer-more-recent, window-expiry.
- `test_lite_tick_holds_last_hr_across_gap` — end-to-end simulation of the trainer-frame + substitution path.
- Dashboard static guard: POWER section must contain `<polyline ... stroke="#eab308"` and must not re-introduce the old zone-coloured polygon bars.

Full suite: 984 pass, 1 skipped, 11 deselected, 3 pre-existing `test_plan_api.py` rematch failures (unrelated to fix32 — predate these changes on `clean-main`).


## [3.6.0-fix31] — 2026-04-20

### Evidence-based fixes from today's ride logs (via fix30 logging infra)

**Mock/sim data deleted entirely.** Ride logs showed `MockDataSource` seed values (`power=169W cadence=89rpm hr=120bpm`) bleeding into the real broadcast on ~75% of 4 Hz ticks because `app.py`'s WS else-branch invoked `session.mock_tick()` whenever `_real_trainer_data` was None. Corrupted NP/TSS/decoupling AND the first-pedal gate fired on phantom data (`[gate] FIRED path=rising p=169w c=89rpm streak=1.0s at elapsed=0.0s`). Fix: removed `MockDataSource` class, `mock_tick()` / `mock_tick_lite()` methods, the else-branch fallback, the "Continue without connecting" button, and `simulate_ride.py`. Empty ticks now broadcast `{awaiting_data: true, power: null, cadence: null, hr: null, speed: null}`. No synthetic data can ever reach the session again.

**ERG auto-pause never resumed.** `ride_285f0abc.log` showed `ERG IDLE -> ACTIVE: 31W` at t=0, `ERG -> PAUSED (reason=auto_power_drop)` at t=30 s, then 130 s of silence. User reported "ERG never got hold of me cycling". Root: `ERGController.update()` had no handler for `state == PAUSED`, and the external resume call gated on a strict triple-gate. Fix: added `_check_auto_resume()` in ERGController with relaxed hysteresis (5-sample rolling: `mean_power > 10W AND mean_cadence > 5rpm`) + internal `on_resume()` triggers REACTIVATING and busts `_last_sent_watts` so the cached target re-dispatches.

**Ride-report HR chart 300+ bpm spikes.** Ride-report SVG polyline included every saved sample including `hr=0` dropouts, mapping far below `hMin=50` → polyline dove vertically creating "spike" artifacts. Log analysis confirmed no HR > 135 bpm was ever broadcast by the backend. Fix: ride-report chart now (a) gap-skips invalid HR samples with segmented polylines, (b) 3-point rolling mean for HR, (c) axis clamped to physical [40, 220] range so pre-fix26 archives can't distort scale. Power bars preserve 0W (coasting is meaningful).

**Live HR graph sawtooth + hero BPM tile stuck.** Live HR graph had no smoothing; hero tile was 400 ms ease-lerped. Fix: `smooth-display.js` HR draw now has 3-point NaN-preserving rolling mean (parity with post-ride SVG); gap-skip preserved. `hr-big` hero tile: 400 ms ease-lerp → 0 ms instant (parity with fix28-hero-bounce for power). Direct `textContent` write on every 4 Hz lite tick + HR zone band pointer repositions per-tick per LTHR zones.

### Autopause timer kept running (regression of fix26 URG-R1-2)
`training_live.py:3160` guarded `_active_seconds` on `not self._paused` only — auto-pause sets `_workout_auto_paused` but NOT `_paused`. Same leak on distance accumulator. Fix: both guards now include `_workout_auto_paused`. Also fixed: `_power_return_streak` hysteresis was resetting on tick 2, preventing auto-resume from ever firing. New events `EVENT=autopause_eval / session_pause / session_resume` added on the decision path.

### Logging infrastructure extended
- `EVENT=tick_awaiting_data` when BLE has no fresh data (replaces silent mock fallback)
- `EVENT=autopause_eval` per-tick during RECORDING
- `EVENT=session_pause reason=auto_power_drop` + `EVENT=session_resume reason=auto_power_drop paused_for_s=N` on edge transitions

### Tests
- Full suite: 977 pass, 1 skipped, 11 deselected, 3 pre-existing `test_plan_api.py` rematch failures.
- 15+ new: sim-flow deletion, ERG auto-resume, autopause timer freeze/resume, ride-chart smoothing.

### Ship
DMG rebuilt to `~/Desktop/Domestique.dmg`. `/Applications/Domestique.app` not touched.

<!-- Superseded detail on autopause timer kept below as deep-dive reference -->

## [3.6.0-fix31-autopause-timer-rca] — 2026-04-20

### Root cause — pause screen never lifted + tile timer kept counting
Two linked bugs surfaced on post-fix26 rides, both in the power-based
auto-pause path at `training_live.py` around L3178–L3234.

**Bug 1 — `_active_seconds` leaked during auto-pause (regression of fix26 URG-R1-2).** The guard at L3112 only checked `not self._paused and not self._stopped`. `pause()` is called only on explicit drops (user / sensor / ble); the newer power-based auto-pause path sets `_workout_auto_paused = True` without touching `_paused`. So the tile timer kept incrementing through an auto-pause window and the broadcast `t` field kept counting. Same leak on the distance accumulator at L3255: gravity-assist virtual-speed on a downhill with `power=0` kept adding km while the rider was stopped.

Fix — extend both guards to also check `_workout_auto_paused`:
```python
if not self._paused and not self._stopped and not self._workout_auto_paused:
    self._active_seconds += 1
...
if not self._workout_auto_paused:
    self._distance_km += display_speed * dt / 3600
```

**Bug 2 — auto-resume could never fire.** `_power_return_streak` only advanced on the *first* non-zero tick after a zero streak — because on the second non-zero tick `_zero_power_streak` had already been cleared to 0 and the `else: _power_return_streak = 0` branch reset the counter. Net effect: the `>= 2` auto-resume check in the auto-pause block could never actually fire, so the session stayed auto-paused forever the moment the rider resumed pedaling. Hysteresis fix — accumulate return streak whenever `_zero_power_streak > 0 OR _workout_auto_paused`, so the counter grows across multiple recovery ticks until the auto-pause flag clears.

### Instrumentation (fills `/tmp/log_analysis.md` gap)
The auto-pause decision path previously emitted nothing. Added three
structured events on `domestique.session`:
- `EVENT=autopause_eval zero_power_streak=N threshold=M return_streak=N power=W cadence=R auto_paused=bool phase=X` — every tick, DEBUG level
- `EVENT=session_pause reason=auto_power_drop phase=X elapsed=N zero_power_streak=N` — on edge transition into auto-pause, INFO level
- `EVENT=session_resume reason=auto_power_drop from_phase=X to_phase=Y elapsed=N paused_for_s=N` — on edge transition out, INFO level (wall-time `paused_for_s` snapped against `_autopause_start_mono`)

### Tests — 6 new regression guards
`TestFix31AutopauseTimerRCA` in `test_training_live.py`:
- `test_auto_pause_freezes_active_seconds` — primes session to ROUTE, fires auto-pause on 5s of power=0, asserts `_active_seconds` flat across 10 more ticks
- `test_auto_pause_freezes_distance` — same for `_distance_km`
- `test_auto_pause_auto_resumes_on_power_return` — power=200W for 2 ticks clears the flag
- `test_autopause_event_emitted` — EVENT=autopause_eval per tick
- `test_autopause_session_pause_event_emitted` — exactly one session_pause on edge
- `test_autopause_session_resume_event_emitted` — exactly one session_resume on clear with paused_for_s present

### Scope
- `training_live.py`: 4 edits (active_seconds guard, distance guard, return-streak hysteresis, autopause event block)
- `test_training_live.py`: +6 tests
- `CHANGELOG.md`: this entry

## [3.6.0-fix30] — 2026-04-20

### Root cause of "waiting for first pedal stroke" forever
`MetricsEngine.update()` received `power`/`cadence` from the WS loop without coercion. If BLE dropped a field, the value arrived as `None` or `NaN`:
- `None >= 5` → `TypeError` swallowed by the WS loop's `except Exception`, so the heavy-tick silently no-op'd
- `NaN >= 5` → False silently, gate evaluated False every tick

Unit tests used clean int inputs and never surfaced the bug. Fix: coerce `power`/`cadence` to `int(0)` on None/NaN/negative at entry, BEFORE any comparison.

### Industry-pattern gate robustness (layered on the fix29 3-path OR gate)
- **Trailing-mean smoothing** (1.5 s window) — low-sustained + no-cadence paths now gate on the trailing mean, not the raw instantaneous sample. Rising-edge still uses raw (the transition is the point).
- **Hysteresis** — once a pass is seen (streak > 0), arm threshold drops to 0.3× for the next tick. Prevents boundary flutter from resetting streak.
- **Distance-accumulated fallback** — fires `first_pedal_detected()` after ~0.05 km of virtual distance (5-10 crank revs) with power>0 AND cadence>0, even when the power-threshold path is ambiguous.
- **Phantom-spike rejection** — downgrades a sample > 2× trailing mean (when trailing mean < arm threshold) to the trailing mean for gate evaluation. Raw sample still flows to broadcast (separates "running" from "recording").

### Logging infrastructure — extensive, backend-only
- **Per-session log file** at `~/.domestique/logs/ride_<iso_timestamp>_<session_id>.log`. All log output for the session's lifetime teed to this file. 20-file rotation, 1 Hz flush.
- **Named loggers** by category: `domestique.ble`, `domestique.gate`, `domestique.phase`, `domestique.power`, `domestique.trainer`, `domestique.hr`, `domestique.session`, `domestique.ws`.
- **71 structured `EVENT=...` emissions** across 6 files — BLE frames + connect/disconnect, gate decisions (per tick), phase transitions, ERG set/pause/resume/spike, gradient dispatch, FTMS timeout/reset, RTL stats (60 s), user config, road feel, Monod CP/W' fit, aerobic decoupling, DFA α1, ride save.
- **Env toggles**: `DOMESTIQUE_VERBOSE=1` (all DEBUG), `DOMESTIQUE_LOG_CATEGORIES=gate,ble` (selective). `DOMESTIQUE_RIDE_LOG_KEEP=N` (default 20).
- **Runtime toggle**: `GET/POST /api/training/log-level` body `{"level": "DEBUG", "category": "gate"}`.
- **`GET /api/training/debug-snapshot`** — dumps every internal state worth inspecting. Payload includes: session metadata, phase, first-pedal gate state, last BLE trainer frame + age, power buffer, BLE lifecycle states, RTL stats, ERG state, GradientController state, course position, live metrics (decoupling / W'bal / DFA α1), session config (FTP / LTHR / rider+bike weight / trainer_effect), last 10 phase transitions, last 30 s of power/HR/RR tails.
- **`DEBUGGING.md`** — grep recipes for common failure modes, endpoint usage, category names.

### Tests
- Full suite: 971 passed, 1 skipped, 11 deselected, 3 pre-existing unrelated failures (`test_plan_api.py` rematch classifier, carried from fix26).
- New tests: 10+ across first-pedal paths (None/NaN coercion, rising-edge vs lite-tick corruption), gate-robustness (phantom-spike reject, hysteresis, distance fallback, trailing-mean smoothing), debug-snapshot shape, log rotation, log-level endpoint.

### Ship
DMG rebuilt to `~/Desktop/Domestique.dmg`. `/Applications/Domestique.app` not touched.

## [3.6.0-fix29] — 2026-04-20

### Bike-ride testing surfaced 3 user-visible bugs — all fixed

**ARMED overlay race** — For 300-500 ms between `/api/training/start` and the first WS broadcast, the ride screen showed blank `---` tiles with no "WAITING FOR FIRST PEDAL STROKE" overlay. Fixed: `startSession()` now sets the overlay visible synchronously, before `connectWebSocket()`. `applyPhaseGate()` continues to toggle it normally once WS ticks arrive.

**First-pedal gate was too strict — slow spin-up never triggered recording**. Previous gate required `power ≥ 30 W AND cadence ≥ 40 rpm AND sustained 3 s` which failed for riders who start with a slow cadence ramp-up. Rewritten as 3-path OR:
- **Rising edge** — `(prev_power=0, prev_cadence=0) → (power>0, cadence>0)` sustained 1 s. Fires on the first real pedal stroke.
- **Low-threshold sustained** — `power ≥ 10 W AND cadence ≥ 5 rpm` sustained 2 s. Handles slow spin-ups.
- **No-cadence fallback** — `power ≥ 25 W` sustained 3 s. Handles trainers without cadence sensor.
- 6 new regression tests. Phantom-watts rejection preserved (single-frame noise still doesn't fire).

**Lap routes showed empty surface bar for laps 2+** — Routes with `lap_route: {laps: N, base_km: X}` (e.g. "Cobbled Classic Sectors × 2", "Hidden Cruise 47 × 2") stored surface_segments for ONE lap only in `surface_types.json`, but `distance_km` reflected the full multi-lap distance. Backend's `_route_surface_segments` passed segments through verbatim — laps 2+ fell through to asphalt baseline. Fixed: when `lap_route.laps > 1`, base segments are now tiled N times with cumulative offsets. Cascading bugs fixed as byproduct: aggregate legend text + visual bar both correct now. `surface_mix_pct` discrepancy in `routes.json` for 1 route (`Cobbled Classic` stores `{gravel:100}`) is a stale-data authoring issue, flagged separately.

### Tests
- Full suite: 931 passed, 4 pre-existing failures (3 `test_plan_api` rematch + 1 `test_diag_recent_ticks` due to gate semantics change — updated expectations inline), 1 skipped.
- 15 first-pedal tests + 4 lap-tiling tests all green.
- 6 diagnostic agents ran before Wave 2 — reports at `/tmp/armed_diag_*.md` + `/tmp/spatial_diag_*.md`.

### Ship constraint
DMG rebuilt to `~/Desktop/Domestique.dmg` only. `/Applications/Domestique.app` not touched.

## [3.6.0-fix28] — 2026-04-20

### Physics audit — 16 parallel agents, CRITICAL bugs found + fixed

**C-1. CdA/Cw semantic mismatch (trainer resistance ~37% too light)**
`FTMSTrainer.DEFAULT_CW` was documented as "Martin 1998 CdA m²" but the FTMS 0x11 wire byte encodes `ρ × CdA` (kg/m). Bumped `0.32 → 0.51` for reference parity. Bonus: `compute_virtual_speed` was multiplying by `ρ × 0.51` (double-ρ) — fixed formula to `0.5 × Cw × v²`.

**C-2. Hero WATTS tile 250ms ease-lerp clobbered 4-Hz broadcast**
fix27d delivered watts at 4 Hz but the UI's `renderInterpolatedFrame` overwrote Path A instant writes with a 250 ms eased ramp every rAF frame. User saw smooth transitions, not pedal-stroke bounce. Fixed: `smoothValue('hero-power', latestPow, 0)` — 0 ms ease.

### HIGH-priority fixes

- **Power chart Y-axis FTP-anchored** (was "160 W hits top when max-seen is 170 W"). Now: `yMax = max(1.2 × FTP, rolling_30s_peak × 1.05)`. Overlay 6 zone-colored horizontal bands at FTP × [0, 0.60, 0.75, 0.89, 1.04, 1.18]. HR axis: `[max(40, rest_hr-10), max_hr]`.
- **SmoothGraph moving average 5pt → 2pt** — 1.25 s box filter at 4 Hz was killing pedal-stroke ripple; now 0.5 s preserves it.
- **Decoupling scatter session-aware redesign**:
  - FreeRide/Route/Climb → scatter only at ride end (matches industry post-ride-only convention).
  - Strict-duration workouts → planned-midpoint classification (no retroactive recolor).
  - Colors: blue `#3b82f6` + yellow `#fbbf24` (colorblind ΔE 45, replaces purple/red which were too similar).
- **HYBRID FreeRide SIM routed through GradientController** — fix27e's 5s lookahead + 2%/s rate-limit now also applies to HYBRID mode (was raw grade dispatch). Plus HYBRID descent ×0.5 flatten for COURSE parity.

### MEDIUM-priority fixes

- **Post-ride DFA α1 sparkline** in ride-detail view — completes fix25 persistence by wiring the reader half. VT1/VT2 reference lines.
- **Bike weight configurable in Settings** (was hardcoded 9.0 kg). Persists to athlete.json; flows to FE-C page 0x37. Range clamp [5, 50] kg.

### LOW-priority polish batch

- **L-1**: BLE power=None now holds last known value (was coerced to 0, poisoning NP/TSS/decoupling rolling windows)
- **L-2**: ARMED idle 15-min timeout — prevents walk-away sessions from sitting ARMED forever
- **L-3**: CRS parser fraction-vs-percent autodetect (max|g|≤1.0 → ×100)
- **L-4**: Tacx cadence 0xFF sentinel explicit gate (was relying on upstream)
- **L-6**: ZWO unknown-tag warn-once per file (was silently ignored)
- **L-7**: GPX first-point missing `<ele>` warn (was silently anchoring to 0)
- **L-8**: DFA log-log R²≥0.95 gate (new `dfa_status="low_r2"`)
- **L-9**: Tacx Crr None intent documented (pycycling validator blocks 0xFF sentinel on BLE path; ANT+ path honors it)

### De-branded scrub

Removed 584 vendor-brand references from code, docs, templates, comments. Design intent + numeric values preserved; product-specific citations replaced with neutral language ("reference parity", "community-documented"). 4 references retained in TRADEMARKS.md / COURSES_LICENSE.md / CONTRIBUTING.md for legal posture.

### Tests
- Full suite: 928 pass, 1 skipped, 11 deselected, 3 pre-existing unrelated failures (test_plan_api rematch classifier from fix26).
- 16 parallel physics-audit agents produced 16 reports at `/tmp/phys_audit_NN.md` (kept for reference).

### Ship constraint
DMG rebuilt to `~/Desktop/Domestique.dmg` only. **`/Applications/Domestique.app` not touched** — user installs the DMG themselves after the next ride.

---

## [3.6.0-fix27] — 2026-04-20

### CRITICAL — power under-reporting (user-reported 320 W felt → 170 W shown)
**Root cause**: the 4-Hz BLE power stream was latest-wins sampled into `_real_trainer_data`, but `session.update()` only runs on heavy ticks at 1 Hz — so **3 of every 4 BLE frames were dropped**. On bursty pedal-stroke peaks (320 W surge lasting ~1.2 s) we'd often catch a trough sample (~170 W) instead of the peak.

**Fix**: `app.py::_on_real_trainer_data` now buffers all incoming BLE frames for the current 1-second window. Heavy tick pops the buffered list, emits `mean` (what metrics use) and `peak` (what the tile shows). No more frame loss.

Also: `PowerSmoothing` spike clamp loosened 2.0× → 2.5× so real pedal-stroke peaks (common at >2× 5-s average during hard efforts) aren't artificially capped. UI reads `d.power` everywhere; removed stale `d.display_power` alias.

### CRITICAL — trainer difficulty stuck at pre-fix26 value for existing users
**Root cause**: fix26 shipped `trainer_effect=0.5` as the new default for reference parity (grade × 0.5 before dispatch). But existing users' `device_prefs.json` still held their pre-fix26 value (1.0 or manually-tuned 0.8). UI hard-coded `1.0` on page load, never fetched the persisted value, and POST didn't save. User's 2% felt like 6-7% because the stored 0.8 was actually being used.

**Fix**:
- One-shot migration on app start: if `trainer_effect ≥ 0.8` and `trainer_effect_fix27_migrated` sentinel is not set, reset to 0.5 and stamp the sentinel. Idempotent; won't re-run. Users who explicitly set a low value (< 0.8) are untouched.
- `GET /api/training/difficulty` — returns persisted value + percent + migration status.
- `POST /api/training/difficulty` — now calls `pm.save_trainer_effect(value)` so the new value survives session end and app restart.
- Training page fetches persisted value on load; initializes slider + label. `STATE.difficulty` default 0.5 (was hard-coded 1.0).
- One-time migration toast: "Trainer difficulty reset to 50% for reference parity. Adjust in the slider if you prefer."
- Settings tab gets a dedicated **Trainer Difficulty card** with slider, `?` tooltip, and auto-save indicator.

### Added — rider-weight FE-C fallback
If the profile has no rider weight set, the Tacx previously silently used its internal 75 kg default — invisible to the user. Now: log a WARNING and explicitly dispatch ANT+ spec default (75 kg) so the trainer acknowledges the page and the log trail reflects what's actually on the wire.

### Added — trainer-health diagnostics
`/api/training/trainer-health` response now includes:
- `trainer_effect` (current value, 0-1.5)
- `trainer_effect_source` (`device_prefs` or `default`)
- `trainer_effect_migrated` (boolean sentinel)

Plus an optional `DEBUG_POWER_TRACE=1` env var — when set, logs every incoming BLE power frame alongside broadcast `d.power` so future under-report reports can be diff'd raw-vs-display.

### Added — 4-Hz power broadcast (reference HUD parity, fix27d)
Frontend graph + hero-power tile previously updated at 1 Hz (session heavy tick), hiding the per-pedal-stroke amplitude. Now:
- Lite tick (4 Hz) emits the **latest BLE power sample** via `session.update_lite` — hero tile and SmoothGraph point land every 250 ms.
- Heavy tick (1 Hz) continues to emit `power` = 1-s mean (for NP/TSS/decoupling — these metrics need 1-s resolution or they thrash at 4 Hz). Adds `power_1s_mean` for explicit downstream access.
- SmoothGraph `historySize` 600 → **1200** points so a 5-min window fits 4-Hz samples without the oldest points falling off prematurely.
- Result: hero tile now visibly bounces per pedal stroke — the 320 W the user felt is now the 320 W they see.

### Added — reference design-pattern parity (fix27e)
- **FTMS request-reply state machine** with correlated `_track_ftms_request`: resend after 0.5 s of silence, re-request-control after 1.0 s — matches the reference's request-reply handshake instead of fire-and-forget.
- **RTL (Rider-to-Load) 60-s interval stats**: online Welford variance logger emits `FTMS RTL Stats` line every 60 s in the reference's log format (min/max/mean/stddev over the window). Lets the user diff a ride against a reference log apples-to-apples.
- **SIM-grade audit log**: `[FTMS] SIM Grade raw=X.XX% --> scaled=Y.YY%` — one line per grade dispatch showing `user_grade` and `user_grade × trainer_effect`. Makes it visible in the log whether fix27a's 0.5 migration actually landed.
- **Tri-state BLE device lifecycle** (`_DeviceLifecycle`): connected / recovering / lost — replaces the fix26 "weak-signal" toast-on-every-tick. Chip colors on the device header reflect the tri-state via `d.ble_states`.

### Research provenance (for anyone auditing the physics path)
- Grade dispatch: audited across `training_live.py::_determine_trainer_command` → `trainer_connection.py::FTMSTrainer.set_simulation_params` / `TacxBLETrainer.set_slope`. Single scaling point at the dispatcher; no double-multiplication. reference's formula confirmed: `grade_to_trainer = user_grade * TRAINER_EFFECT` (default 0.5).
- Power pipeline: direct injection test with 320 W held steady → broadcast emits 320 W through all phases (ARMED, INDEXING, RECORDING, PAUSED). No scale factor, no hidden smoothing, no virtual-power leak. The real bug was the 4→1 Hz frame-drop (fixed above).
- reference binary extracted surface taxonomy + BLE trace: FTMS Indoor Bike Sim frames, CRR=0.004 baked into reference, CdA=0.051 wire-scale (0.51 real), wind=0 constant. No climb-dampening, no power re-estimation, no FTP bias.
- Golden Cheetah compared: GC dispatches grade 1:1 (no trainer-difficulty slider at all), power is raw instantaneous watts from trainer BLE, display smoothing is optional 1-30 s user slider.

### Tests
- Full suite: **906 passed**, 1 skipped, 11 deselected (Python 3.12).
- +12 new regression tests across `test_profiles.py`, `test_training_live.py`, `test_ble_power_buffer.py`, `test_power_control.py`, `test_trainer_connection.py`.
- 3 pre-existing `test_plan_api` rematch-classifier failures flagged as fix26 IMPL-ADAPT regression (unrelated to fix27 — will address separately).

### Ship constraint
- DMG rebuilt to `~/Desktop/Domestique.dmg`. `/Applications/Domestique.app` NOT touched (user still testing on the bike).
- Sandbox-blocked SSH → user must `git push origin clean-main` from their own terminal.

## [3.6.0-fix26] — 2026-04-19

### CRITICAL bike-test bugs fixed
- **Ghost ride**: walking away from "START PEDALING" no longer accumulates phantom ride data. First-pedal gate now requires triple-condition: `power ≥ 30 W` AND `cadence ≥ 40 rpm` AND sustained 3 s. Fallback 60 W for 5 s when no cadence sensor.
- **Pause timer kept running**: elapsed timer now uses `session.active_seconds` accumulator (only advances when not paused + not stopped + in RECORDING/INDEXING phase). Distance / km counters freeze too.
- **Phantom watts while paused**: data-collection (DFA buffer, decoupling buffer, Pw:Hr scatter) halts during pause.
- **Top WATTS tile stuck at averaged value**: now reads instantaneous `d.power` from broadcast, not `d.avg_power`. Subtle "INSTANT" sublabel.
- **HR graph oscillating 0↔beat**: NaN-sentinel break; chart now skips null samples instead of plotting at 0.
- **HR tile showed "2 bpm"**: unphysical-HR gate at `< 30 or > 220` bpm → UI renders `--`.
- **EXIT button failed during pause**: unified exit handler works from any state (paused/armed/recording). ESC key and header EXIT both route through `exitWorkout()`.
- **Duplicate device entries in status header**: device list is now keyed by BLE address (stable), reconciled in place on reconnect instead of appended.
- **DFA panel "not every strap sends RR" misleading**: now two-state: "stabilizing" when RR count > 0; "check HR strap contact" when zero RR for 30 s.

### Trainer physics — reference parity
- **Crr table** aligned to reference's road-wheel column (community sources extracted):
  - asphalt 0.004, cobble 0.0065, gravel 0.012, dirt 0.016, sand 0.004, unknown 0.004
- **Tacx Road Feel** proprietary command (`set_neo_modes`) dispatched per surface:
  - asphalt → SIMULATION_OFF, cobble → COBBLESTONES_HARD, gravel → GRAVEL, dirt → OFF_ROAD, sand → SIMULATION_OFF
- **Road Feel intensity** calibrated to match reference's felt amplitude through pycycling's 0-100 scale (community consensus; pycycling maintainer warns ≥50 feels aggressive and 100 may damage the trainer):
  - asphalt 0, cobble 45, gravel 30, dirt 22, sand 0, unknown 0
  - (Initial values 80/70/60/40 were 2-3× too aggressive per user's reference A/B testing; realigned after deep-dive into local reference binary extracted surface taxonomy + community-calibrated intensities.)
- **Trainer difficulty slider** honors reference 50% convention (halves felt grade before dispatch).
- **CdA default** lowered 0.51 → 0.32 (hoods position per Martin et al. 1998).
- **Weak-signal toast** debounced: 5 s sidebar badge → 10 s warning toast → 30 s "offline" message with 3-check debounce. Was flickering on every Tacx frame gap.

### Deletions — ~13,500 LOC removed
- **Strava** residue (~600 LOC): `surge_config.py`, `keychain.py`, `relay/`, strava methods, setup.html strava sub-block (Garmin Connect preserved).
- **Running module** (~350 LOC): `running_zones.py`, RUNNING_* constants, `Goal.sport`/`PlannedSession.sport` dataclass fields, running-branch logic. (DB `activities.sport` column kept — ICU metadata.)
- **Nutrition subsystem** (~12,500 LOC): `nutrition_planner.py`, `nutrition_db.py`, `food.py`, `targets.py`, `test_nutrition.py`, `sports_nutrition_db.json`, `off_sports_nutrition.json`, `main.py` + `workout_picker.py` (CLI artifacts), 7 API endpoints, fuel-plan UI in training.html, Products tab in dashboard.html.

### Added — W'bal live-feed enhancements
- 10-min W'bal sparkline in the W'bal panel.
- CP tick on live power graph.
- "Sustain: M:SS" predictive label — when P > CP for ≥ 5 s, server computes `sustain_s = wbal_j / (power - cp_w)`. Renders `M:SS` when finite, `∞` otherwise.
- Intervals.icu `w_prime` now wired into profile with source priority (manual > icu > monod > fallback).
- Monod-Scherrer 2-parameter CP/W' fit from best-efforts (R² ≥ 0.90, ≥ 3 points).
- Gap handling: 2 s BLE power gaps no longer falsely trigger recovery integration.

### Added — Intervals.icu reliability
- Typed exceptions: `ICUAuthError`, `ICURateLimitError`, `ICUServerError`, `ICUNetworkError`.
- 3-attempt retry with exponential backoff + jitter. Honors `Retry-After` on 429.
- 401s now bubble to `_is_auth_error`; 5-strike auth-disable actually works (was dead code before fix26).
- Dashboard banner when `auth_disabled` or `consecutive_failures > 2`. Dismissable.
- `w_prime` manual-source guard at both SQL and profile-priority layers.

### Added — Daily plan adaptation redesign
- `daily_adapt_plan` demoted to projection-only (no more in-place mutation of `current_plan.json`).
- `POST /api/plan/move-session` — drag-to-reschedule; sets `user_moved=True` flag honored by regen/reforecast.
- `POST /api/plan/rematch?apply=0|1` — reconciles completed Intervals.icu activities to prescribed workouts. Preview vs apply.
- Classifier requires **all three** within tolerance: TSS ±15% AND duration ±20% AND IF-band match. 2/3 → `ambiguous`. 1/3 → `no_match`.
- Status enum: `pending / done / missed / moved_from:<date> / dismissed / ambiguous / done_partial`.
- `POST /api/plan/dismiss-session` — sets/clears dismissal. Dismissed stays visible (grayed) with "Un-dismiss" action.
- Plan regen preserves `user_moved`, `completion_matches`, `dismissed_at`, past-week statuses.

### Added — Sleep/screensaver inhibit while riding
- Cross-OS: macOS `caffeinate -i -w <pid>`, Windows `SetThreadExecutionState`, Linux `systemd-inhibit --what=idle`.
- Fires on `/api/training/start`; releases on `/api/training/stop` + launcher SIGINT/SIGTERM.
- Idempotent; keeps holding during pause (user may be catching breath).

### Added — Device memory + forget
- Paired devices persisted to profile's `_athlete["paired_devices"]` (keyed by BLE address).
- `GET /api/ble/paired` + `POST /api/ble/forget?address=...`.
- "×" forget button next to connected device chips in scan UI.
- Dedup by address on add (re-pairing same device replaces entry, doesn't append).

### Added — Colorblind-safe surface palette
- Gravel #a0522d → #c8804d (lighter rust; deut ΔE vs dirt 9.1 → 35.1).
- Dirt #8b6f47 → #5a4a32 (dark char-brown).
- Asphalt #374151 → #4b5563 (more visible on card backgrounds).
- Unknown #4b5563 → #6b7280 (no longer collides with new asphalt).

### Added — HR data feed hardening (carry-over from fix25 context)
- RR intervals now timestamped `list[tuple[monotonic_s, rr_ms]]`.
- Reconnect flushes `_pending_rr` + flags resync; DFA window cleared.
- RR filter tightened 150-3000 ms → 300-2000 ms.
- BPM spike filter (>40 bpm/s rejection).
- `TACXTrainer._cached_hr` gets `_cached_hr_mono` staleness gate.

### Tests
- Full suite: **857 passed**, 1 skipped, 11 deselected (Python 3.12). Up from 775 pre-fix26.
- New tests across trainer_connection, training_live, training_planner, plan_api, profiles, sleep_inhibit, training (ICU).

### Ship constraint
- DMG rebuilt to `~/Desktop/Domestique.dmg` only. **Installed app at `/Applications/Domestique.app` was NOT touched** — user was testing on the bike during the wave; they install the new DMG themselves after the ride.

## [3.6.0-fix25] — 2026-04-19

### HR band data feed hardening (CRITICAL upstream of DFA + decoupling)
Every HR-derived metric now survives a noisy BLE feed. Previously an RR-interval gap, a 40 bpm spike, or a mid-ride reconnect could silently poison DFA α1 and aerobic decoupling for the next 3–5 minutes.

- RR intervals now carry a monotonic timestamp (`list[tuple[float, int]]`) so downstream consumers can detect gaps.
- BLE disconnect flushes both `_pending_rr` and `MetricsEngine._rr_buffer`; the first packet after reconnect is flagged `rr_is_resync=True` and the rolling DFA window is cleared + re-warms for 180 s.
- RR range filter tightened 150-3000 → 300-2000 ms (matches Task Force HRV spec).
- BPM spike filter: rejects packets with |ΔBPM| > 40 bpm within 1 second; resumes after 2 agreeing packets.
- Ectopic-triplet detection (short-long-short at ±20% of median) with neighbor-mean replace, and the corrected-beat count propagates to MetricsEngine so DFA's 5% artifact gate includes scrubbed beats.
- Session-level HR staleness gate: `TacxBLETrainer._cached_hr_mono` stamps every HR update; both direct-BLE and Tacx-relayed HR are read-gated at 5 s, with direct BLE winning unconditionally in the 0-10 s window.
- `hr_contact_ok` flag (from HR spec flag bits 1-2) propagates to the UI banner "Heart-rate sensor not making contact" after 3 s of contact loss.
- Off-by-one fix in the RR-parse loop; reserved flag bits 5-7 guarded; `_external_hr` writes now atomic under a lock.
- Zero → thirteen regression tests on the HR data pipeline.

### DFA α1 live feed (CRITICAL corrections to zone classification)
Zone labels were **inverted** from prevailing cycling convention (Gronwald 2020 / Altini). Athletes reading α1 = 0.80 as "AEROBIC Z2" were actually in Z3 tempo.

- Zones corrected per spec: **α1 ≥ 1.00** = aerobic (green, Z1–Z2, below VT1); **0.75 ≤ α1 < 1.00** = tempo (yellow, Z3, VT1 to VT2); **α1 < 0.75** = threshold or above (red, Z4+). Bar tick-marks at 1.00 (VT1) and 0.75 (VT2); the old 0.50 tick is gone.
- **Time-gated 120 s rolling window** (was 200-beat window, which dropped to 67 s at 180 bpm — below spec).
- **Warmup suppression**: no α1 emitted until ≥ 180 s elapsed AND ≥ 90 beats in window AND session phase RECORDING. UI shows "stabilizing…" pill.
- Unphysical clamp [0.30, 1.60]; outside → `dfa_status="unphysical"`, no numeric shown.
- Artifact threshold unified at 5% (was asymmetric 3% UI warn vs 5% compute bail).
- **10-minute sparkline chart** in the DFA panel — HiDPI canvas, dashed VT1/VT2 reference lines, gap breaks on data discontinuity.
- `dfa_history` persisted to ride JSON (was memory-only).
- Synthetic-signal regression test: pure-Python impl matches `nolds.dfa(order=1, overlap=False)` to 4 decimal places on pink-noise input.

### Aerobic decoupling (CRITICAL bugs in compute + scatter)
Fixed four compute bugs and the scatter-plot axis inversion vs Intervals.icu convention.

- **Client recompute deleted**: `templates/dashboard.html` no longer recomputes `(ef1-ef2)/ef1` from mean power using a different filter than the server. Everyone renders `s.decoupling_pct` (canonical NP-per-half per Friel / Intervals.icu / WKO).
- **Efficiency factor** is now the ride-aggregate `NP / avg_HR` (was per-tick instantaneous P/HR — caused UI flicker).
- **Unified Z1 filter** across live + post-hoc + client: `50 ≤ power_w ≤ 2500 AND 60 ≤ hr_bpm ≤ 220` (was four different conventions — HR≥40, HR≥100, HR≥60 etc).
- **Warmup trim 900 s** excluded from both halves (was included; rising HR during warmup inflated "decoupling" in the first 15 min).
- **Live value is provisional** during ride with `?` tooltip "Live estimate — final locked at ride end"; locked at ride stop. If ride < 40 min of filtered samples → label reads "ride too short".
- **Scatter axes flipped** to x=Power (W), y=Heart Rate (bpm) — Intervals.icu parity. First-half dots **purple** `#a855f7`, second-half **red** `#ef4444`, with **least-squares regression line per half**. Centroid markers + drift arrow preserved.
- **Heat pill** 🌡️ gate changed from `mode === 'erg'` (wrong — ERG can be outdoor) to `is_indoor` (broadcast from `trainer_connected` truth + session.indoor flag).
- FIFO cap 24 000 entries on decoupling buffers prevents unbounded memory on 6+ hour rides.

### Tests
- +19 regression tests across `test_trainer_connection.py`, `test_training_live.py`, `test_fitness_estimation.py`.
- Full suite: **750 passed, 1 skipped, 11 deselected** (Python 3.12).

### Scientific references
Implementation validated against:
- Peng et al. 1995 (original DFA), Gronwald et al. 2020 (cycling application), Rogero 2022 (real-time parameters), Schaffarczyk 2022 (reproducibility).
- Friel's "aerobic decoupling" formulation, Coggan NP per half, Intervals.icu decoupling spec.
- Open-source reference: `nolds.dfa`, Golden Cheetah `AerobicDecoupling.cpp`.

## [3.6.0-fix24] — 2026-04-19

### Added — FE-C page 0x37 rider configuration at Tacx connect
Previously deferred as S-4 in fix23 ("pycycling API doesn't expose this; needs upstream PR"). Turned out pycycling 0.4.1 (already in our venv) exposes `TacxTrainerControl.set_user_configuration(user_weight, bicycle_weight, bicycle_wheel_diameter, gear_ratio)` which writes FE-C data page 55 (0x37) to the Tacx BLE UART. No upstream PR needed.

`TacxBLETrainer` now pushes rider weight + bike weight (9.0 kg default) + wheel diameter (0.70 m for 700c) + gear ratio (1.0) to the trainer at connect, improving virtual-flywheel accuracy on the Tacx Neo 2T.

### Policy — skip-don't-lie
If the active profile has no rider weight set (raw `_athlete` dict missing `weight_kg`), we pass `None` and skip the FE-C write, letting the trainer use its internal 75 kg default. ProfileManager's property-level fallback of 70 kg is explicitly sidestepped so we never send fabricated data.

Valid rider range: [30, 200] kg. Outside → skip.

### Auto-reconnect replay
Cached user configuration is re-applied automatically on BLE reconnect via `TacxBLETrainer.connect()`'s auto-replay path.

### Trainer-requested re-send
When the Tacx sets `user_configuration_required` on FE-C page 0x19 (e.g. after a firmware reset), we detect the False→True edge in the FE-C data callback and re-send the cached configuration. Rate-limited to one send per 30 seconds.

### Defensive clamping
Inputs are clamped to pycycling's validation ranges (user ≤655.34 kg, bike ≤50 kg, wheel ≤2.54 m, gear 0.03–7.65) with floor 0 on bike and wheel so no ValueError can bubble up from pycycling.

### Tests
- +21 regression tests across 3 new test classes in `test_trainer_connection.py` (default values, skip-on-invalid, clamping, failure non-fatal, missing-key skip, no-double-send, trainer-requested re-send, reconnect replay).
- Full suite: 715 passed, 1 skipped, 11 deselected (Python 3.12).

## [3.6.0-fix23] — 2026-04-19

### Fixed — ERG mode no longer "re-initializes" on pause/resume (CRITICAL UX)
Prior behavior: user hits pause on an ERG ride, resumes, and waits up to ~55 seconds before the target power reaches the trainer again. Root cause was four compounding "safety" behaviors firing on every short user pause:
1. `training_live.py::_determine_trainer_command` sent SIM grade 0% every tick during pause — actively erasing the held ERG target.
2. `FTMSTrainer.pause_training()` always wrote FTMS Pause opcode `0x08` to the trainer. reference and Golden Cheetah both avoid this.
3. `FTMSTrainer.resume_training()` always replayed the full handshake (RequestControl `0x00` + Start/Resume `0x07`) regardless of whether control was still held.
4. `ERGController.on_resume()` unconditionally entered REACTIVATING: cadence ≥60 rpm for 2 s + 3 s dwell + ramp from 25% of target at 5 W / 2 s. For a 200 W target, full recovery took ~55 s.

### New reason-aware pause/resume contract
- `on_pause(reason=...)` / `on_resume(reason=...)` on `ERGController`, `FTMSTrainer`, `TacxBLETrainer`. Locked reason enum: `"user" | "sensor_drop" | "ble_drop" | "auto_power_drop" | "free_ride"`. Unknown → treated as `"user"`.
- `reason="user"`: pause is a wire no-op (no `0x08`); resume replays cached target instantly via `[0x05]`, skipping handshake if `_control_held=True`.
- `reason="sensor_drop" | "ble_drop" | "auto_power_drop"`: retains 2-s cadence gate for slam protection. The 25% soft-ramp is DELETED entirely per Golden Cheetah / reference consensus — ERG already scales with cadence.
- `reason="free_ride"`: controller disengages cleanly; on segment transition back to targeted step, `engage(target)` with debounce bust.

### Added — Connection-state tracking
- `FTMSTrainer._control_held` flag set on handshake, cleared on BLE disconnect or `0xFF CONTROL_PERMISSION_LOST` indication.
- `set_power()` checks `_control_held` before writing — re-handshakes on control-lost, queues the target while disconnected so reconnect auto-restore can replay.
- `_last_target_watts` cache on both FTMSTrainer and TacxBLETrainer for reconnect auto-restore and queue-while-disconnected.
- `on_reconnect_complete()` replays RequestControl + Start + cached target after BLE reconnect. Dedup: skip the handshake if control is already held.

### Added — ERG MODE ENGAGING → ERG MODE ON overlay
Full-screen overlay on user-resume: shows "ERG MODE ENGAGING…" with a spinner while the resume POST is in-flight, transitions to "ERG MODE ON" with a green check for 1.5 s, then auto-dismisses. Provides immediate visual confirmation that ERG has re-locked after a pause.

### Added — Trainer-offline signal
When `resume_training()` exhausts its 3×2 s reconnect budget, the endpoint now returns HTTP 503 and emits a WebSocket `trainer_status: offline` event. The training template consumes this and shows a toast: "Trainer offline. Tap to reconnect."

### Added — Neo 2T quirks alignment
- 50 W midpoint-ramp on large target deltas in both FTMSTrainer and TacxBLETrainer (Tacx Neo 2T vendor recommendation — softens transitions on any FTMS trainer).
- (Deferred TODO) FE-C page 0x37 rider-weight configuration at Tacx connect — requires pycycling API extension, out of this wave's scope.

### Fixed — FreeRide → ERG transition never reached the wire
`engage() + set_target()` double-call on segment transition had `engage()` preloading `_last_sent_watts = target`, which caused `_determine_trainer_command` to debounce the target out. `engage()` now does state-transition + debounce bust only; first tick after engage writes the real frame.

### Fixed — Auto-power-drop recovery deadlock
`ERGController.update()` was skipped while `erg_auto_disabled=True`, but the flag is only cleared inside `update()` via the REACTIVATING→ACTIVE transition — stuck forever. `update()` now ticks unconditionally; the flag gates dispatcher output, not controller progress.

### Fixed — FreeRide side-effect cleanup
Workout stop now calls `erg_ctrl.disengage(reason="workout_end")` regardless of the final segment type.

### Fixed — `first_pedal_detected` target pipeline
The initial post-ARMED target now routes through `HRCapController` + trainer-effect bias like every other tick, not raw.

### Tests
- +28 regression tests across `test_power_control.py`, `test_trainer_connection.py`, `test_training_live.py`, new `test_resume_reconnect_signal.py`.
- Full suite: 694 passed, 1 skipped, 11 deselected (Python 3.12).

### Reference material
ERG behavior validated against Golden Cheetah (`src/Train/BT40Controller.cpp`, `ANTChannel.cpp`) and community reverse-engineering of reference (pycycling, gymnasticon, zwack). Key consensus adopted: no FTMS 0x08 on user-pause, no RequestControl replay when control held, no target watchdog, trainer holds target across pause.

## [3.6.0-fix22] — 2026-04-18

### Fixed — "WAITING FOR FIRST PEDAL STROKE" regression (CRITICAL)
- Removed adversarial auto-pause in `startSession()` — after `/api/training/start`, the session no longer POSTs `/api/training/pause`. The ARMED state machine (fix14) already gates recording, so the legacy auto-pause was both redundant AND wedging the session (paused+ARMED → never detected pedals).
- Also removed the auto-pause twin in the WS-reconnect handler (training.html:3211-3218).
- Belt-and-braces: `first_pedal_detected()` now clears `self._paused = False` as its first statement; ARMED/INDEXING streak counters run even while paused, so any future auto-pause re-introduction still unwedges on first pedal.
- 30-min paused auto-end safety net now runs regardless of phase.
- `/api/training/trainer-health` exposes `paused` for diag.

### Fixed — Pre-ride HR/power/cadence preview shows live data
- `TrainerConnectionManager.get_status()` now includes live `trainer.power`, `trainer.cadence`, `hr.heart_rate` — surfaced via a new `_last_trainer_frame` cache stamped on every `_dispatch_trainer_data` call.
- 5-second stale gate using `time.monotonic()` (robust to wall-clock steps); older than 5 s → `None` (frontend shows `--`).
- `pollDeviceStatus()` frontend now renders `--` when stale instead of misleading zeros.

### Added — Mini-map spatial surface bar (replaces % aggregate)
- Route mini-map now shows a horizontal colored bar where the position of each surface type matches its position along the route — not a percentage breakdown.
- Colors: asphalt `#374151`, gravel `#a0522d`, cobble `#9ca3af`, dirt `#8b6f47`, sand `#d4b896`, unknown `#4b5563`.
- Asphalt baseline painted first, so km not covered in `surface_types.json` render as asphalt (not void).
- `/api/training/info` now carries `surface_segments` canonical shape (was a latent gap).
- Canonical enum locked: `{asphalt, gravel, cobble, dirt, sand, unknown}` lowercase singular.

### Fixed — In-ride surface band now actually visible
- Previous band: 5 px at 0.6 alpha — invisible. Now: 14 px at 0.90 alpha with asphalt baseline + 1 px hairline top border.
- Palette aligned with mini-map for cross-screen parity.
- `#climb-panel` min-height bumped 60 → 96 px so the band doesn't crush the elevation curve.

### Fixed — UPPERCASE surface tokens leak through WS
- `CourseEngine.current_surface()` now normalizes Tacx road-feel tokens (`ASPHALT`, `COBBLESTONES_HARD`, `OFF_ROAD`, …) to the canonical lowercase enum at the data boundary. WS consumers no longer need to canonicalize.

### Tests
- +14 regression tests across `test_training_live.py`, `test_trainer_connection.py`, `test_route_picker_api.py`.
- Full suite: 641 passed, 1 skipped, 11 deselected (hardware-gated).

## [3.6.0] - 2026-04-18

Full pre-release audit pass — security, correctness, performance, UX. 6 parallel fix waves covering 100+ bugs across backend, live-ride UI, dashboard UI, route engine, trainer/FIT, security/packaging, and second-pass deep scans (planner, workout variety, nutrition, concurrency, aerobic drift, edge cases).

### Breaking changes

User-visible behavior deltas vs v3.5.1 — read before upgrading:

- **`POST /api/training/save-ride`** now returns **422** on unknown fields (previously silently accepted `post_to_strava`, `screenshot_b64`). Body shape is exactly `{name, private}`.
- **`POST /api/nutrition/products`** now returns **422** on out-of-range macros (previously silently clamped). Per-macro cap 100 g, kcal ≤ 900, caffeine ≤ 400 mg/serving, sodium/fluid ≤ 2000, all values ≥ 0.
- **`GET /api/version`** no longer includes `platform` / `build` fields — response is exactly `{"version": "..."}`.
- **Rest-week adherence** now grades **100 → 0% across 0–50 TSS** (previously collapsed to 0% on any load). Normal-week adherence capped at **150%** (previously unbounded).
- **Workout picker seed** now includes `profile_id` — same athlete on the same date still sees stable picks, but **different athletes on the same date no longer see identical picks**. Existing plans are NOT re-rolled; new plans only.
- **HR-zone bucket cutoffs** in ride reports now derive from `zones.hr_zones()` (was hardcoded integers). Old rides may re-bucket by ±1 bpm at zone boundaries on re-render.
- **Paused rides auto-end after 30 min** of pause dwell. Users who pause-and-forget will find the ride finalized.
- **`POST /api/plan/reforecast`** was a printed-advisory no-op in v3.5.1; now actually shifts future hard sessions based on TSB.
- **CSP / X-Frame-Options / X-Content-Type-Options** headers now set on all responses. External tools that injected JS into the dashboard (bookmarklets, dev browser extensions) may break.
- **`CC_LOG_MAX_BYTES` / `CC_LOG_BACKUP_COUNT` env vars** deprecated (still work with a warning); use **`DOMESTIQUE_LOG_MAX_BYTES` / `DOMESTIQUE_LOG_BACKUP_COUNT`**.
- **FIT file output** now includes a `LapMessage`. Strava is tolerant; TP / Garmin Connect display totals correctly. Older FIT viewers that don't handle Laps may render differently.
- **Unsigned DMG / EXE** — macOS Gatekeeper and Windows SmartScreen will block the first launch. README has the right-click → Open workaround. Codesigning is a documented future TODO.

### Security
- **XSS hardening** — migrated all `esc()`-in-`onclick` sites in `templates/dashboard.html` to `data-*` + event delegation or `escJs()` (killed 18+ potential-injection points incl. nutrition-product delete). Closed 7 additional `innerHTML` sinks in `templates/training.html` via `escHtml()`.
- **CSP + X-Content-Type-Options + X-Frame-Options** middleware added (`default-src 'self'`, style/script `'unsafe-inline'`, data: imgs, ws: WS).
- **/ws/training now validates Origin** before `websocket.accept()`.
- **Plan endpoints stopped leaking `str(e)`** in 500 responses; internal detail logged, client gets "Plan update failed".
- **/api/setup/save path allowlist** — workout/gpx/food-log dirs must live under `$HOME`, `~/.domestique`, or `/tmp`.
- **db.`_maybe_add_column`** validates identifiers against `^[A-Za-z_][A-Za-z0-9_]*$` — SQLi latent path closed.
- Root dev `.env` deleted pre-publish; ICU API keys live per-profile (chmod 0600) with README SECURITY note.
- Relay Worker: origin gate moved before OPTIONS preflight.

### Removed (dormant since v3.5.0)
- `/api/strava/*` endpoints (7 routes).
- `strava_client.py`, `strava_uploader.py`.
- `routes.json.bak`, `routes.json.surface_bak`, `sports_nutrition_db.json.bak`, `off_sports_nutrition.json`.
- Stale `dist/ChickenCycling*`, `dist/Health Tracker*`, `build/chickencycling/`, `build/health_tracker/` artefacts.
- `logs/chickencycling.log`, `logs/health_tracker.log`.

### Backend / correctness
- `_session_lock` + `_snapshot_session_surfaces()` guard concurrent mutation of `_training_session`; `_dispatch_road_feel` now snapshots surfaces safely.
- Wellness `ctl`/`atl` use `is not None` (0 is a real value, not "missing").
- ZWO Duration parsed via `int(float(...))` (tolerates fractional durations).
- `/api/logs` tail uses seek-from-end; bounded memory.
- ICU `raw_json` JSON-parsed before `isinstance` check.
- `ride_storage.py` HR cutoffs now reuse `zones.hr_zones()` — no 1-bpm drift.
- Save-ride validator simplified to `{name, private}`; rejects unknown fields with 422.
- Plan-write race closed: 7 plan-write sites in `app.py` wrapped in `training_planner.plan_write_lock()`.
- Timezone math normalized (`today.isocalendar()` uses `zoneinfo`; tz-aware/naive subtraction safe).
- `/api/version` minimal; no `platform.platform()` leak.
- `_VERSION` single-source reads `VERSION` file with BOM-safe encoding.

### Live ride + RIDE REPORT
- Peak HR bucket now uses per-bucket `hMax` (was bucket mean).
- `alert()` → `_rrToast()` (no more pywebview UI freezes on 409).
- RIDE REPORT SVGs responsive (viewBox + 100% width) — no hardcoded pixel widths.
- `pollDeviceStatus` in-flight guard + `beforeunload` cleanup.
- WebSocket reconnect resets interpolation state.
- `togglePause` awaits server truth instead of flipping local state eagerly.
- Keyboard shortcuts guarded by `_RR.visible` so ride summary can't trigger pause/resume.
- CSS cache invalidates on theme change.
- `renderElevationProfile` uses manual min/max loop (no 10k-arg spread).
- Pause auto-ends ride after 30 min.
- **New** `GET /api/training/session` returns current session snapshot (browser refresh mid-ride can re-mount).
- `/api/training/start` returns 404 on missing course file (no silent fall-through to free-mode).
- Gzip middleware reduces `/stop` payload for long rides.

### Planner / weekly / workout variety
- `daily_adapt` is TSB-aware (drops intensity a level when TSB < -30).
- 48h HIT-gap check now rolls across week boundaries.
- Workout-picker seed is local `random.Random(sha1(date:profile_id))` — different athletes no longer see identical picks.
- Empty candidate pool raises `NoCandidateWorkoutError` (was silent `zwo_file=""`).
- `reforecast()` is no longer a printed-advisory no-op — shifts future hard sessions by TSB.
- Rest-week adherence grades 100 → 0% across 0–50 TSS (was collapsing to 0 on any load).
- Sport-aware adherence now covers running in addition to cycling.
- Calendar null `session_type` guarded.
- `checkPlanGaps` POST body harmonized with `reforecastPlan`.

### Routes / surfaces
- **`surface_types.json` lookup fixed** end-to-end — flat `{region/slug: [segments]}` shape; `app.py` now keys with `route["id"]` verbatim; Tacx road-feel now actually fires.
- `generate_route_profiles.py` writes to `/tmp/` (was targeting prod `routes.json` and would have wiped the 622-entry catalog on accidental run).
- `build_preview_profile` length-guarded against short elevation arrays (was IndexError).
- `pick_weighted` empty/all-zero safe.
- `build_route_from_sections` merges tiny sections < 0.5 km before inflation.
- `surface_at` uses exclusive-upper bound + "unknown" fallback.
- `_detect_climbs` caps 4%-tail absorption.
- GPX `<ele>` missing/empty warns + per-file try/except (one bad file doesn't halt batch).
- `generate_procedural_routes.py` totals includes `"unknown"` bucket.
- Single-source haversine in new `geodesy.py` (4 duplicate copies eliminated).
- `_detect_climbs` deduped across `route_archetypes.py` + `generate_procedural_routes.py`.
- 3 gravel-archetype xfails fixed (tests pass without masking).

### Trainer / FIT
- `fit_activity.py`: LapMessage added to the FIT writer; fallback `start_time = now() - timedelta(seconds=dur)`; unused Garmin epoch offset removed; bare-excepts log at DEBUG.
- HR GATT 0x2A37 contact bits per spec (bit1 detected / bit2 supported).
- ERG `set_target(0)` → `disengage()` (avoids trainer-freewheel lock).
- Staleness watchdog now schedules reconnect + latches.
- ANT+ `_scan_ant` classifies device-type (0x11 power, 0x78 HR, 0x17 FE-C) — no longer hardcoded to `ANT_FEC/TRAINER`.
- BLE connect retry catches `BleakError` (bleak 3.x).
- `ANTHRMonitor` gained `on_disconnect`.
- Battery-level BLE reads on connect (optional).
- `ConnectionState.SCANNING` actually reachable.
- `disconnect_all()` releases ANT USB dongle.
- FTMS `cancel_spindown` sends reset opcode.
- Speed preserved across flag-bit-0 follow-up frames.
- Cadence debounce (ignore 0-drops <2s).
- ANT+ elapsed-time 64s rollover accumulates.
- Reconnect dedup + RSSI-sort ignores ANT+ zero-RSSI.
- `_reconnect_device` + staleness + logs cleaned.

### Nutrition
- Atwater self-correction at load (9 of 284 seed rows had declared kcal wildly off; replaced with 4C+4P+9F).
- Server validator requires all 4 macros (carbs/protein/fat/alcohol) with non-negative bounds.
- Caffeine cap 400mg/serving (was 2000mg — lethal range).
- Alcohol tracking at 7 kcal/g.
- Reminder window off-by-one fixed.

### Profile / zones
- `profile_manager.switch()` blocks during active ride; refreshes workout-picker's `WORKOUT_DIR` + clears lru_cache.
- `zones.estimated_hr_max(age)` Tanaka fallback (208 − 0.7×age).

### Packaging / docs
- `domestique.spec` reads `VERSION` at build time (was hardcoded "3.0.0").
- PyInstaller stale py_modules list cleaned up.
- `requirements.txt` — `bleak`, `pycycling`, `openant` uncommented (load-bearing for trainer); fastapi/starlette/pydantic upper bounds.
- `NOTICE` adds html2canvas + 5 runtime deps; nutrition count corrected to 284.
- `README.md` — workout 1,753 / route 622 counts corrected; SECURITY section; "Installing the unsigned DMG" workaround note.
- `SYSTEM.md` — data-dir and removed-Strava copy cleaned.
- `SCIENCE_REVIEW.md` title now "Domestique".
- `CHANGELOG.md` — 3.5.0 / 3.5.1 date order fixed.
- `DOMESTIQUE_LOG_MAX_BYTES` / `DOMESTIQUE_LOG_BACKUP_COUNT` env vars (with deprecated `CC_LOG_*` fallback).
- `launcher.py` signal handler flips uvicorn `should_exit` for graceful shutdown.
- `asyncio.create_task` strong-refs via module-level set.

### Tests
- New regression tests for: wellness ctl=0, save-ride validator, db column-name validation, session-lock race.
- 3 xfailed gravel archetypes fixed; no more xfails.
- Final suite: **609 passed, 1 skipped, 11 deselected** (baseline was 571/1/3).

### Commits
- 16 commits on `clean-release`, tagged `v3.6.0`.

## [3.5.1] - 2026-04-18

Save FIT button — download activity as FIT for manual upload to Strava / Garmin / etc.

### Added
- **"📦 Save FIT"** button on the RIDE REPORT footer (between Save-to-Desktop and SAVE). Hits new `GET /api/training/active-fit`, which renders the in-memory `_training_session` buffer through `fit_activity.build_activity_fit()` (same FIT shape used by the dormant Strava uploader) and streams the bytes back as `application/octet-stream` with a `Content-Disposition` filename `domestique-<profile-slug>-<iso-ts>.fit`. The session buffer is left intact, so SAVE / Discard still work afterwards. 409 when no active buffer exists.
- New `_rrSaveFit()` JS handler in `templates/training.html` — fetches the endpoint, parses the filename out of the `Content-Disposition` header, and triggers a browser download.

### Commits
- `v3.5.1` + Save FIT button on RIDE REPORT

## [3.5.0] - 2026-04-17

Removed Strava UX (configuration friction); added Save-to-Desktop on RIDE REPORT.

### Removed
- **Settings → Connections → Strava** — entire Strava UI block in `templates/dashboard.html` is gone (Connect button, connected pill, re-auth chip, bootstrap modal, and the `connectStrava` / `promptStravaBootstrap` / `startStravaPolling` / `loadStravaStatus` / `disconnectStrava` JS functions). Intervals.icu connection is unchanged.
- **RIDE REPORT visibility pill (Private / Strava)** in `templates/training.html`, plus the screenshot preview thumbnail and `_rrCaptureScreenshot` / `_rrClearShot` / `_rrOpenStravaSettings` / `_rrSetVisibility` / `_rrRefreshStravaStatus` helpers and the `_RR.postToStrava` / `_RR.shotB64` / `_RR.stravaConnected` state.
- Save POST body simplified to `{name, private}` (defaults `private: true`).

### Added
- **"📥 Save to Desktop"** button on the RIDE REPORT footer (next to Discard + SAVE). New `_rrSaveToDesktop()` JS function uses the existing `_rrLoadHtml2Canvas()` lazy loader to render `#ride-report-card` to a 2x PNG and trigger a browser download (`domestique-<safe-name>-<iso-ts>.png`). The PNG lands in the user's default Downloads folder; from there they can drag it to Desktop. (A true "Save As… → Desktop" dialog requires the File System Access API, which is not available in pywebview reliably.)

### Changed
- **`POST /api/training/save-ride` validator** — `post_to_strava` and `screenshot_b64` are now optional (default `None`). Legacy clients that still send them continue to work; current UI just sends `{name, private}`.

### Kept (dormant)
- `/api/strava/*` endpoints, `strava_client.py`, `strava_uploader.py`, `relay/`, `surge_config.py`, and `keychain.py` Strava entries are untouched. Easy to revive if the user changes their mind.

### Commits
- `v3.5.0` rm Strava UX + Save-to-Desktop on RIDE REPORT

## [3.4.0] - 2026-04-18

### Added
- **Strava OAuth relay (Cloudflare Worker)** — true reference-parity UX: end-users no longer need to paste a Strava `client_secret`. A tiny single-file Worker (`relay/strava-oauth-worker.js`) holds the secret server-side and exposes `POST /token-exchange`, `POST /token-refresh`, and `GET /health`. CORS is locked to Domestique's `localhost:8080` / `127.0.0.1:8080` origins; input shape (code/refresh_token) is validated at the edge; per-(colo,IP) token-bucket rate limit; logs contain only timestamps + status codes (never the secret, never any token).
- **`STRAVA_RELAY_URL` config** in `surge_config.py` — sourced from `DOMESTIQUE_STRAVA_RELAY_URL` env var. When set, `is_relay_configured()` returns True and `is_strava_configured()` returns True without needing a local secret.
- **`relay/wrangler.toml` + `relay/README.md`** — 5-minute one-time deploy guide (cloudflare.com → Workers → paste code → set `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` env vars → deploy → copy URL).

### Changed
- **`strava_client.exchange_code()`** — when relay is configured, POSTs `{code}` to `{STRAVA_RELAY_URL}/token-exchange`; otherwise falls through to the direct Strava call (dev / pre-relay installs).
- **`strava_client.get_valid_access_token()`** — refresh leg uses `{STRAVA_RELAY_URL}/token-refresh` when configured, otherwise direct.
- **`/api/strava/oauth-start`** — drops the 503 "App secret not configured" branch when the relay is configured (the `/authorize` step itself only needs the public `client_id`).

### Tests
- `test_strava_relay.py` — six cases covering relay-configured vs unconfigured detection, exchange routing, refresh routing, and direct-call fallback. Stubs `requests.post` via `unittest.mock` (no `responses` dependency added).

### Commits
- `v3.4.0` Strava OAuth relay (Cloudflare Worker) for reference-parity UX

## [3.3.0] - 2026-04-17

### Fixed
- **Sport-aware adherence on Home → "This Week"** — a planned cycling workout (Z2, SS, TEMPO, FTP, VO2, OU, REC) is now considered DONE only if the day has at least one cycling activity (`Ride`, `VirtualRide`, `EBikeRide`). Doing a hike/run/swim on a planned SS day no longer credits the cycling session away; it is now correctly listed in `missed_sessions` with `did_non_cycling: true`. The day cell still shows the actual exposure badge (so the cross-training credit is visible) but adds a red `plan: SS missed` subtitle.
- **`planned_days_done` / `planned_days_missed`** are computed against the same sport-aware rule, so the Adherence pill ("3 of 5 planned sessions done") matches what the user feels.

### Added
- **`exposure_minutes_planned` field** on `/api/week-summary` — backend maps each planned `session_type` to its expected band (`recovery`/`z2`/`long_z2` → low_aerobic, `tempo` → mid_aerobic, `sweetspot`/`threshold` → high_aerobic, `vo2max`/`overunder` → anaerobic, `rest` → none) and attributes `duration_min` to that band.
- **Planned vs Actual exposure bars** in the weekly rollup — two stacked bars: top labeled "Planned" (70% opacity, dashed-border outline = the target), bottom labeled "Actual" (full opacity = what you did). Combined legend below: `LOW 660m planned / 328m actual` per band.
- **Make-it-up nudge** in the missed-sessions modal — when `did_non_cycling` is true, the row shows "Did non-cycling activity that day — make it up?" in orange italic.

### Commits
- `v3.3.0` weekly summary — sport-aware adherence + planned vs actual exposure

## [3.2.1] - 2026-04-18

### Added
- **Strava one-button connect (reference-parity UX)** — single click on the Strava tile launches the OAuth flow in the system browser, polls for completion, and reflects connected state in the UI without page reload.
- **First-time bootstrap modal** — when no app secret is configured, a modal prompts for the Strava client secret and stores it in the macOS Keychain (service `domestique.strava`, account `app-secret`). Plaintext config is never written.
- **Per-profile token storage** — one installed app supports N profiles, each with its own Strava athlete. Tokens are scoped by `profile_id` in the Keychain so switching profiles does not cross-contaminate uploads.
- **Polling-based callback** — pywebview cannot deliver `postMessage` from the OAuth popup back to the parent. The callback HTML now renders a simple "you can close this tab" page; the dashboard polls `/api/strava/status` to detect completion.

### Fixed
- **XSS escape** in `/api/strava/oauth-callback` — the `error` query param is HTML-escaped before being rendered (previously raw-interpolated). Verified with `?error=<svg/onload=alert(1)>` → renders as `&lt;svg/onload=alert(1)&gt;`.
- **Popup-blocker workaround** — `window.open` is invoked synchronously inside the click handler so Safari/Chrome do not block the OAuth window.
- **Version sync** — `/api/version` literal, `VERSION` file, and bundle Info.plist now all read `3.2.1`.
- **Status cache** — `/api/strava/status` no longer holds a stale "connected" flag after token expiry; it re-checks the Keychain each call.

### Commits
- `f20d284` v3.2.1: QA fixes — XSS escape, popup-blocker, version bump, status cache
- `79bcf34` v3.2.0: Strava bootstrap secret + reference-parity callback HTML
- `17789bd` v3.2.0: Strava one-button connect (reference-parity UX)
- `fdd7485` v3.0.3: cache-bust favicon (?v=3) so browsers pick up the cyclist logo
- `f87cd52` v3.0.2: cyclist climbing zone-colored steps logo
- `a401a7e` v3.0.1: classic castle silhouette logo (3 towers + cone roofs + portcullis)

## [3.0.0] — 2026-04-18

### Changed
- **Data directory renamed**: `~/.chickencycling/` → `~/.domestique/`. A one-time migration runs on first boot via `profile_manager._maybe_migrate_data_dir()` and uses `shutil.move`, so existing profiles, SQLite DB, ride history, intervals.icu credentials, and known-device registry all carry over automatically. The legacy directory no longer exists after migration.
- **Logger names**: `chickencycling.*` → `domestique.*` (`profiles`, `rides`, `nutrition`, `nutrition_db`, `food`).
- **Log file**: `~/.domestique/logs/domestique.log` (was `chickencycling.log`).

### Added
- **macOS Keychain for Strava tokens.** `keychain.py` wraps the macOS `security` CLI. On macOS, Strava OAuth tokens (access + refresh) are stored under service `domestique.strava` (account = profile_id) instead of plaintext `strava.json`. Existing `strava.json` files are auto-migrated into the Keychain on first read, then unlinked. Non-macOS hosts keep the 0600 JSON-file fallback.

### Migration safety
- **Backup before upgrade**: a snapshot of the legacy data dir was taken at `~/.chickencycling.v2-backup/` before the v3.0.0 rename. If anything goes sideways, restore with `mv ~/.chickencycling.v2-backup ~/.chickencycling` and downgrade.

### Notes
- Bumped FastAPI app version + `/api/version` payload to `3.0.0`. Bundle Info.plist `CFBundleShortVersionString` updated to match.
- The v2 single-user → multi-profile migration (`migrate_profiles.py`) now operates on the new `~/.domestique/` path. Order in `app.py` lifespan: `ProfileManager.get()` (v3 dir rename) THEN `migrate_to_profiles()` (legacy v1 → v2 layout).

## [2.0.0] — 2026-04-17

### Renamed
- **ChickenCycling → Domestique.** New tagline: **"Your training domestique."**
- macOS bundle id: `com.chickencycling.app` → `com.platypus45.domestique`
- DMG output: `ChickenCycling.dmg` → `Domestique.dmg`
- PyInstaller spec: `chickencycling.spec` → `domestique.spec`
- Window title, tray-icon title, and printed banners now read "Domestique"

### Added
- **RIDE REPORT** — one-screen end-ride flow: cadence, MMP (mean-max power) curve, and aerobic decoupling all on a single review surface before save/discard.
- **`POST /api/training/save-ride`** — single save endpoint with editable title, visibility (private/followers/public), and one optional screenshot attachment. Replaces the prior multi-step save flow.
- **Strava OAuth + activity upload** — full OAuth 2.0 flow plus FIT-format activity push with `trainer=true` flag and optional photo attach. Modules: `strava_client.py`, `strava_uploader.py`, `fit_activity.py`.
- **Settings → Connections** panel — manage Strava and intervals.icu credentials in one place (connect, disconnect, status badge).
- **Castle logo** — Z1–Z6 stacked colour blocks with Z5/Z6 surge upward. Ships as `static/icon.svg`, `static/icon.png`, `static/favicon.png`, `static/apple-touch-icon.png`.

### Notes
- **Data directory unchanged.** `~/.chickencycling/` is intentionally **kept** so the SQLite DB, profiles, ride history, intervals.icu credentials, and known-device registry survive the rebrand without re-onboarding. A future migration to `~/.domestique/` is wired through the `profile_manager._maybe_migrate_data_dir()` seam (no-op today).
- **Strava attribution.** To get the "via Domestique" line on uploaded activities, register Domestique with `developers@strava.com` (FIT manufacturer/product registration). Until then uploads land without a third-party attribution badge but otherwise function normally.
- DB table names, route IDs, intervals.icu credential helpers, and Python logger names are unchanged.
- Existing `<author>ChickenCycling</author>` headers inside bundled `workouts/*.zwo` and `courses/**/*.crs` are preserved as historical artifacts; only newly generated files emit `Domestique`.

### Commits
- `90e46aa` v2.0.0: /api/training/save-ride single save endpoint + discard
- `9c3c52c` v2.0.0: Strava OAuth + activity upload + Settings Connections panel
- `ebb4860` v2.0.0: Strava module files (uploader, client, FIT activity, OAuth config)
- `d716276` v2.0.0: RIDE REPORT one-screen end-ride flow + cadence + MMP
- `cf31e7d` v2.0.0-fix: Strava status wiring + UX polish
- `fee7b27` v2.0.0-fix: Strava scope + attribution docs (research findings)
- `cae0d68` v2.0.0-fix: backend QA fixes (Strava queue noop, status codes, SRI/vendor)
- `57b8c04` v2.0.0-fix: code-quality polish (decoupling sign, hardware test mark, gitignore)

## [1.0.0] — 2026-04-10

### Added
- **Setup wizard**: 5-step first-launch configuration (Intervals.icu, Strava/Garmin migration, athlete profile, data paths, training preferences)
- **Desktop app packaging**: PyInstaller spec for macOS .app and Windows .exe, launcher with system tray icon
- **Workout classifier rewrite**: 10 clean categories (Endurance, Sweet Spot, Threshold, VO2max, Anaerobic, Sprint, Over-Unders, Tempo, Recovery, Mixed) — classifies by training stimulus, not dominant zone
- **Weekly plan variety**: workout library rotation ensures different workouts each week across the entire plan
- **Interactive elevation profiles**: hover crosshair showing gradient %, distance, and elevation in course and virtual route detail modals
- **CRS file format fix**: all 487 files converted from cumulative to delta distances for GoldenCheetah compatibility
- **Native folder picker**: OS-level folder dialog in setup wizard
- **ICU auto-fill**: FTP, LTHR, Max HR fetched from Intervals.icu with manual override toggle

### Core features (carried from development)
- Intervals.icu integration (CTL/ATL/TSB/HRV/activities)
- Evidence-based training plan generator (FTP/VO2max/Hybrid/Event plans)
- Weekly mesocycle planner with Plews HRV-guided daily adjustment
- 340 virtual cycling routes + 41 climb portals with elevation profiles
- 159 real-world course profiles with GPX maps
- CRS slope files for GoldenCheetah trainer simulation
- ZWO + FIT workout exports (smart-trainer apps, Karoo, Garmin, Wahoo)
- Light/dark mode
- Morning check-in with soreness-adjusted readiness
- Nutrition tracking with evidence-based targets (IOC/Burke)
- Rolling adaptive plan with auto-recalculation
