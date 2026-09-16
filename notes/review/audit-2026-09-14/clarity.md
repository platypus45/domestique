# Clarity and branching audit — a95447e8 (2026-09-14)

Lens: complexity, cascades, decision keys, flags, dead code, duplication, comments, error handling.
Independent agent, measured with radon 6 / vulture / pyflakes / AST scripts in a scratch worktree.
Kept verbatim; the synthesis is in ../architecture-audit-2026-09-14.md.

## Headline numbers

| Measure | app.py | training_planner.py |
|---|---|---|
| radon MI | **0.00 (C)** | **0.00 (C)** (also 0.00: ride_storage, route_archetypes, structure_fidelity, training_live) |
| total CC / functions / CC≥11 | 5036 / 394 / **148** | 3649 / 250 / **81** |
| `if` / `elif` / `try` lines | 1539 / 84 / **619** | 1279 / 68 / 100 |
| `.get("` / ` or {}/[]/0/""` | 1621 / 674 | 380 / 420 |
| comment lines / docstring lines | 14.8% / 11.1% | **25.6%** / 11.5% (code ≈ 55%) |
| comments citing a version/ticket | 433 | 444 |
| broad `except Exception` / silent | **395 / 182** | 53 / 35 |

Across src/: 210 functions with CC≥20, 110 with CC≥30, 37 with CC≥50; 41 functions >200 lines. `_USE_WEEK_SOLVER`, `_USE_TRAINING_WEEK`, `_GENERATE_WHEN_LIBRARY_MISSES`, `_REPAIR_MAX_MOVES=0` all disable their branch in production; the "second builder" is 55 dead lines behind a flag plus a 95-line dead loop.

## 1. Complexity — worst 25 (CC, file:line, length)

```
272 training_planner.py:7354  sample_week_workouts     1079 lines, 20 returns, 174 locals, 5 nested defs
161 structure_fidelity.py:931 score_blocks              515
143 app.py:19798  _build_programme_summary              467 (14 returns, 105 locals)
128 training_planner.py:11811 reforecast                658 (nesting depth 11 — deepest in repo)
122 training_planner.py:5197  _match_zwo_unclamped      523 (0 direct tests)
121 training_planner.py:13166 regenerate_from_today     511
117 training_planner.py:8601  generate_plan             704
105 app.py:15254  merge_plan_with_rides                 431
103 ride_storage.py:179 _normalize_icu_activity         328
102 app.py:3033   api_rider_stats                       297
101 training_planner.py:14655 refit_remaining_week      409
100 training_planner.py:13787 recalculate_plan          487
 93 training_planner.py:9655  _enforce_build2_peak_hard_floor 356
 91 training_planner.py:15590 generate_weekly_plan      342
 90 app.py:1280   setup_save                            312
 78 app.py:6757   api_routes_suggest                    218
 73 app.py:9737   api_week_summary                      283
 71 app.py:4183   api_plan_auto_adjust                  299
 70 app.py:11583  api_plan_generate                     344
 68 training_planner.py:14280 extend_continuous_plan    337
 68 training_planner.py:3390  generate_phases           395
 68 app.py:12598  _apply_plan_update                    268
 61 fit_activity.py:431 build_activity_fit              212
 59 training_planner.py:4135  _pick_session             337
 59 app.py:8565   update_settings                       195
```

**dashboard.html** (4 inline `<script>` blocks, 18,381 JS lines, 578 functions, 32 >100 lines): openRideDetail 693 (html:8651), renderActivityModal 349 (:20885), renderPlanJSON 342 (:12839), openDayWorkout 324 (:18880), renderOnTrackBar 308 (:20497), loadWeeklyCalendar 284 (:17162), loadHome 268 (:6982), renderCalDay 207, workoutProfileSVG 206, loadTodaySession 178, runPlanOpenSequence 177, _renderWeeklyRollup 173, buildPowerBlocks 171, renderThisWeekFromCalendar 163. Max brace depth 14 (html:6251, Chart.js options); 2,597 lines sit at depth ≥8. 1,836 `if(`, 1,104 `?`, only 3 `switch`, 2 else-if chains ≥4. The JS problem is length and nesting, not cascades.

## 2. Cascades and decision keys

`elif` chains ≥4 branches: **15 in training_planner.py, 14 in app.py** — few. The owner's "huge amount of branching" is not elif ladders; it is **1,279 + 1,539 nested `if` guards** re-inspecting the same session/goal/phase state, e.g. reforecast's 125-line 4-way block at tp:11987 (`hours<=0` / `session_type=="rest"` / `user_swapped` / else) inside 11 levels.

Longest chains: goal_to_dict tp:12635 (8× `f.name ==` — a dataclass field switch); api_workouts app:5880 (8× `sort ==`); plan_target_ctl tp:3349 (5× goal_type); generate_phases tp:3551 (101 lines, 3× goal_type); budget tp:2890 (phase_name); generate_weekly_plan tp:15853 (phase_name); ZWO `tag` cascade **7 copies** (tp:4859, app:5432, 6032, 6201, 13374, training_live:1818, structure_fidelity:254); zone thresholds `<56/76/91/106/121` **2 copies** (tp:4819, app:5416); build_fit_workout_bytes app:13098 (st_norm 5-way).

| Decision key | Occurrences | Replacement |
|---|---|---|
| `session_type ==/in` | 103 tp, 9 wp, 5 pi, 4 app; **49 are `== "rest"`** | `SessionType` enum + `is_rest/is_hard` properties on PlannedSession |
| hard/easy type literal sets | 10 inline tuples + `_HARD_SESSION_TYPES` (tp:11439), `HARD_TYPES` (pi:37), `hard_types` (app:10174), `_SWAP_TYPES` (app:16420) | one table `SESSION_KIND[type]` |
| ownership: `_protect_race` / `user_swapped` / `user_moved` / `is_opener` | 39 sites tp+app+wp (23 via `getattr(s,"user_…")`) | `session.is_owned` — one predicate (DUP-23) |
| `goal_type ==/in` | 27 tp, 7 app; 9 literal sets, `"continuous"` 31× | `GoalType` enum + strategy object per goal type (target rule, phases, emitter) |
| `phase_name`/`.name` | 17 + 15 tp; `"taper"` 36 tp; 23 literal sets | `Phase.kind` enum; per-phase table for budget/floor/hit-cap |
| unload/stepback | 46 conditional sites tp, 7 pi | one `week.is_unload` (DUP-3) |
| `plan_mode` | 11 tp, 5 app | strategy per mode |
| ZWO `tag` | 7 cascades | one `parse_zwo_segments()` |
| `isinstance(` | 157 app, 38 tp | typed DTOs at the boundary |
| `x or {}` / `.get("`  | 1094 / 2001 | typed plan/session models (dict-shaped plan is the root) |

## 3. Feature flags and dead branches (production values)

| Flag | Def | Value | Sites | Dead lines |
|---|---|---|---|---|
| `_USE_TRAINING_WEEK` | tp:6439 | False | tp:8831-8855, tp:13426-13457 | **55** (the "one owner" builder never runs; week_plan.TrainingWeek unreachable from generate/regenerate) |
| `_USE_WEEK_SOLVER` | tp:6630 | False | tp:8273-8275 | 2 + `_solve_week_assignment` tp:6641 (≈120 lines) |
| `_REPAIR_MAX_MOVES` | tp:6588 | **0** | tp:6837 `for _ in range(0)` | 95-line loop body dead; `_repair_week` still called unconditionally under **`if True:` tp:8276** (vulture: redundant condition) |
| `_GENERATE_WHEN_LIBRARY_MISSES` | tp:6440 | False | tp:7880-7924 | 44 |
| `STRICT_SEAL` | wp:177 | False | wp:196 | seal only logs |
| `BLOCK_EVAL_SURFACED` | app:15850 | True | app:15830 | else dead |
| `TAPER_AUTO_LOCK` | config:88 | True | — | unread by planner |

No env toggles reach the planner (0 `os.environ` in tp/wp). Sixty-plus lines of comment at tp:6420-6440 and 6580-6630 explain why each flag is off — the decision is recorded in prose instead of removed.

## 4. Dead code

vulture 80%: 12 hits, all real (unused imports `asyncio`, `Form` app:40/59, `asdict` tp:100; unused vars app:1626, wp:528/532; redundant `if True` tp:8276). pyflakes: 17 assigned-never-used locals (tp:4538, 11963, 13728, 13821, 13873, 14723, 15635, 15740, 16251; app:2036, 6472, 7266, 15430), 2 unused `global` decls, 35 unused imports. Caller map (1,266 top-level defs, route handlers excluded): **30 functions with zero non-test callers, 589 lines**, 13 with no test either: `render_ride_report_png` (93), `_enforce_hard_day_spacing` tp:6764 (51), `schedule_double_threshold_pair` tp:11614 (52), `required_weekly_tss` tp:2781, `_taper_anchor` tp:1189, `check_acwr` pi:440, `projected_ctl` pi:471, `classify_acwr/tsb/ramp/monotony` training.py:901-937, `compute_sleep_score`, `_predict_ftp_for_date`, `_plan_rest_frac`.

## 5. Duplication

Exact (normalised) ≥12-line blocks between distinct functions: 13 pairs, 204 lines. Fuzzy (≥6-line runs) among the 70 longest: **regenerate_from_today ↔ recalculate_plan share 77 lines in 7 runs** (tp:13511/14085, 13545/14125, 13571/14145, 13632/14218); recalculate ↔ extend_continuous 47 lines (14039/14455, 14229/14576); api_plan_auto_adjust ↔ api_readiness_apply_tier_down 58 lines (app:4127/4420); `_accept_redraw_apply` ↔ `_swap_session_type_apply` 21 (app:16301/16530); `_apply_move_session` ↔ `api_plan_move_session` 16 (13691/14080); `_np_fraction_from_samples` **copied verbatim across files** (app:5366 = tp:4686); `_recent_dfa_*` 19 (app:3473/3561); `_build_fit_workout` ↔ `_from_zwo` 12 (13235/13449). The five emitters (generate 704, regenerate 511, recalculate 487, extend 337, refit 409 = 2,448 lines) are the real duplicate; the post-pass chain is 12 `_enforce_*/_space_*` passes with 31 call sites.

## 6. Naming and comments

877 archaeology comments (`v2.5.0` 65×, `G3/G4` 28×, `QA-EDGE`, `W2B`); tp is a quarter comment by line. Of the 22 worst-CC functions sampled, 9 docstrings open with a version tag instead of a purpose (`_enforce_build2_peak_hard_floor`: "v4.6.1 PLANNER-VARIETY+RONNESTAD —"; `_apply_plan_update`: "v1.8.24 — THE single…"; `merge_plan_with_rides`, `refit_remaining_week`, `_normalize_icu_activity`, `api_plan_auto_adjust`, `api_rider_stats`). Names that no longer match: `sample_week_workouts` (also generates, repairs, rematches, solves); `reforecast` ("shift hard sessions one level" — also owns availability rebuild, the 9138110d cap, rest restore); `recalculate_plan`/`regenerate_from_today`/`extend_continuous_plan` (three names, one walk); `_enforce_weekly_volume_ceiling` (called twice with different `taper_only`); `api_readiness_apply_tier_down` (rewrites the plan). String enums: session_type 15 literals / 17 sets; goal_type 9/9 (`event` vs `event_preparation`, `ftp_vo2max` vs `hybrid` aliases); phase names 23 sets.

## 7. Error handling

1,199 handlers in src/, **641 broad, 319 silent** (pass/return/continue/debug-log). Ten that hide the most: app:9370 (stored-plan merge into weekly view dropped → sessions vanish from the week tile); app:136 (`_maybe_restore_plan_from_backup` returns None on any error, including a failed `tmp.replace` → half-written plan); app:687 (lifespan restore); app:18784 (plan-load alert always False); app:11226/11253 (`/api/plan` 86-line block: 3-D mirrors and ctl_drift silently absent); tp:662 (`rewrite_stale_plan_classifications` 110-line body, debug-only); tp:11580 (tier-down rematch fails → stale ZWO kept, reported `rematched`); tp:15771 (stepback detection → never unload); tp:15570 (eFTP write fails, then logs "applied"); app:16063 (day index resolve → 400 with wrong message).

## Ranked findings

**Critical** — (1) Five emitter walks, 2,448 lines, sharing 77+47 verbatim lines and diverging elsewhere (dupes DUP-1/3 are consequences). (2) Both alternative builders are dead in prod (`_USE_TRAINING_WEEK`, `_REPAIR_MAX_MOVES=0`), so 12 post-passes re-decide every week; the owner's cleanup target is not yet wired. **High** — (3) sample_week_workouts CC 272; (4) reforecast depth 11, CC 128, 0 typed state; (5) 182 silent broad excepts in app.py on plan-read/write paths; (6) 39 ownership checks, 49 `== "rest"`, 10 hard-type sets. **Medium** — (7) 7 ZWO-tag cascades, 2 zone tables; (8) 30 uncalled functions/589 lines; (9) 877 version comments. **Low** — flake/vulture hygiene (12+17+35 items).

## Ten refactors, best clarity-per-risk

1. Delete dead flag branches + `if True:` + `_solve_week_assignment` + `_repair_week` loop (tp:6439-6440, 6588-6630, 6641-6932, 7880-7924, 8273-8285, 8831-8855, 13426-13457): ≈400 lines; pinned by test_tid_plan_properties, gate.
2. One `parse_zwo_segments()` replacing 7 tag cascades (+2 zone tables): ≈250 lines; test_scan_zwo*, structure_fidelity tests.
3. `session.is_owned` predicate for 39 sites (DUP-23): ≈60 lines; 40 reforecast test files.
4. `SessionType`/`GoalType`/`PhaseKind` enums + one `SESSION_KIND` table replacing 10 literal sets: ≈100 lines; 66 generate_plan files.
5. Extract the shared prologue/epilogue of regenerate/recalculate/extend (tp:13511-13656 ≙ 14085-14241 ≙ 14455-14589) into one `_walk_future_weeks()`: ≈150 lines; 23+14+5 test files.
6. `goal_to_dict` field switch → `dataclasses.fields` with a converter map (tp:12635): 16→4 lines.
7. `api_workouts` sort ladder → key table (app:5880): 23→8 lines; 1 test.
8. Remove 30 uncalled functions: 589 lines; 13 have no test at all.
9. Split `reforecast` availability block (tp:11987-12112) into its own function with an explicit `WeekBudget` arg: 0 net, depth 11→6; 40 test files.
10. Delete `_np_fraction_from_samples` copy in app.py:5366 → import from tp: 12 lines; 4 tests.

## Not measured

JS function bounds are brace-counted after string/comment stripping but not regex-literal stripping, so one artefact (`_hrvPromptDeviceRows` reported as 18k lines) is wrong; the rest are plausible but unverified. Silent-except "data loss" ranking is keyword-scored, then hand-read for the top 10 only. Fuzzy duplication was computed only across the 70 longest functions. No runtime coverage: "which tests pin it" is grep of test files naming the function, not executed coverage. dupes.md's DUP items were not re-verified.
