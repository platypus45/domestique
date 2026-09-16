# Gate review: can the gates judge the planner overhaul?

Reviewer lens: gates. Code: `ca6091f9` (detached worktree `rev-gates`). Nothing
was fixed or committed. Production, `domestique-refactor` and systemd were not
touched.

Every number below was produced by a command in this review, and the scripts
are kept in `review/gates-work/`. Commit messages, docstrings and notes are
treated as claims (section 3).

**How it was measured**

- *Hermetic env* (all mutation runs): `DOMESTIQUE_HOME=<empty dir>`,
  `DOMESTIQUE_NO_NET=1`, `PYTHONHASHSEED=0`, `PYTHONDONTWRITEBYTECODE=1`.
- Baselines were recorded in that same env, and each mutant was compared
  against them. The comparison is not against `tests/characterization.json`;
  why is in GATE-2.
- *Mutation harness*: `gates-work/mutharness.py`. It injects one fault, runs
  every gate, reverts with `git checkout -- src/`, and asserts the tree is
  clean before the next fault. `reset_clean: true` is recorded for every
  mutation.
- *Pytest runs* use the plain environment (hash seed 0, no bytecode), so
  conftest's per-process HOME sandbox and network block apply. Each mutant run
  is compared against the **same files, flag mode, environment and worker
  count at clean HEAD** (`gates-work/subset_baselines.py`).
- *A first pytest phase was discarded.* It passed `DOMESTIQUE_HOME`, which
  made every run share one data directory (GATE-8). Its outputs are kept as
  `mut_pytest_*.polluted.jsonl`.
- *Unaffected by that*: every characterization, parity and prototype result.
  Those runs finished by 21:46, and the shared directory's first file is from
  21:49.
- *Prototype gate*: `gates-work/proto_gate.py`. It is the reviewer's instrument
  for section 4, and each mutation was also scored against it.
- *Independent verification*: a separate agent, on its own fresh copy, re-ran
  GATE-1, GATE-2 (date and athlete id), GATE-3, GATE-5, C7, C15, the T05 and V02
  mutations and 22 citations with its own probes. Its scripts are in
  `gates-work/verifier/`. It confirmed those findings and corrected six
  statements, which are now fixed: the R6 date claim, T05's flag-on count,
  GATE-3's scope, R4.4, the athlete-id share (6 of 16), and two line numbers.
  The one row it could not re-check, by rule, is "copy of `~/.domestique`: 0
  changed", which needs a copy of the owner's credentials. That result rests on
  this review's `chain1.out` run alone.

## 1. Findings, most severe first

### GATE-1 · CRITICAL · The parity probe audits two entry points, not five
`tests/probe_entry_point_parity.py:56-60`, and in `src/training_planner.py`:
`:13217-13222` (a continuous goal is routed from recalculate to extend),
`:13796-13798` (extend returns `no_change("horizon_full")` on a plan built the
same day), and `:14196-14215` (refit returns `no_op` when nothing is missed).

**What is wrong.** Every rider the probe builds is `goal_type="continuous"`.
Each entry point is fed a copy of a plan generated moments earlier, with no
elapsed weeks, no missed sessions and no rides. Under those inputs:

- `recalculate_plan` hands the goal to `extend_continuous_plan`.
- `extend_continuous_plan` finds the horizon full and returns its input.
- `refit_remaining_week` finds nothing missed and returns its input.

The "recalculate", "extend" and "refit" columns are therefore generate's plan,
audited three more times.

**Evidence.**
```
$ gates-work/gate_parity.py (N=40), fingerprints compared per rider:
owner=0 recalculate  identical to generate output for 40/40 riders
owner=0 extend       identical to generate output for 40/40 riders
owner=0 refit        identical to generate output for 40/40 riders
owner=1 (flag on)    the same three lines, 40/40 each
coverage (N=4): recalculate_plan 3/176 lines, extend_continuous_plan 12/145, refit_remaining_week 9/155
```
In the mutation run, a fault planted in any one of those three entry points
(E06, E07, E08, E09) left the probe's tally unchanged in both flag modes
(section 2).

**Consequence.** Step 5 says to migrate `recalculate_plan`,
`extend_continuous_plan` and `refit_remaining_week` onto the owner, with
"`probe_entry_point_parity.py 40` showing no violations in any entry point" as
the check. That check would pass if all three were deleted. The notes' baseline
"1/7/1/1/1" is one number, generate's, shown four times, next to regenerate's
7. **STRUCTURAL.**

### GATE-2 · CRITICAL · The golden, and the parity baseline, are valid on one machine on one day
- `tests/characterize_planner.py:157-159`: `generate_plan` is called with no
  date, so it anchors on `date.today()` (`training_planner.py:8341`).
- `training_planner.py:5337-5356`: `match_zwo`'s RNG seed is
  `anchor_date:profile_id:week:day:type:salt`, where `profile_id` falls back to
  `config.ICU_ATHLETE_ID`. That value is loaded from
  `~/.domestique/profiles/default/.env`. The verifier set two different ids
  and 6 of the 16 `generate/*` cases moved, so the dependence is partial but
  real.

**Evidence** (`gates-work/../chain1.out`, `chain2.out`, `why_net.py`):
```
empty DOMESTIQUE_HOME                    2 changed (generate/every-day/3h/{fresh,midweek})
copy of ~/.domestique                    73 cases, all unchanged
copy minus profiles/default/.env only    DIFF
copy, network blocked (audit hook)       MATCH, 0 socket attempts
today = 2026-09-11                       16 changed   (every generate/* case)
today = 2026-09-17                        8 changed
today = 2026-10-12                       16 changed
parity N=40, hermetic, 09-10: weekly_volume 2/7/2/2/2
                        09-11:          4/6/4/4/4   (no code change)
```
The `midweek` cases date their rides `ANCHOR+0..2` (Sep 14-16), while the plan
starts at `today`. On 10-12 those rides fall before the plan, and all 8
`midweek` fingerprints are identical to their `fresh` twins (8/8). So the
behaviour those cases exist to pin, "a mid-week generate has to subtract it",
stops being tested without any signal.

**Consequence.**
- From 2026-09-11 the refactor gate reports 16 changed cases with no code
  change. They are exactly the 16 cases that run the pipeline the overhaul
  rewrites.
- The engineer can `--bless`, which captures that day's date and so hides a real
  change made the same day, or learn to ignore the gate. Either way the 16
  end-to-end cases stop judging anything.
- On any other machine, profile or CI, including the other engineer's worktree,
  the gate is red before any change.
- Run as documented, the gate reads the production data directory (the ICU
  credentials `.env`, `athlete.json`, prefs) outside pytest's HOME sandbox.
  **STRUCTURAL.**

### GATE-3 · CRITICAL for Step 4 · Neither gate can see which workout file is served
- `tests/characterize_planner.py:86-93` fingerprints `[session_type,
  duration_min, tss]`, with no `zwo_file`.
- `src/plan_invariants.py:131-151` (`check_slot_file_coherence`) flags only a
  *rest* slot that carries a file.

Step 4 changes what file a slot receives, and the §4 defect is "an endurance
slot served threshold content". Neither is observable unless durations or TSS
happen to move.

**Evidence.**
- Mutation T05 switches off both `file_admissible` content-class checks in
  `_match_zwo_unclamped` (`:4960`, `:5221`). Flag-off characterization:
  **0 changed**. Parity invariant tally: unchanged. Yet 32 of 40 generate
  outputs and 12 of 40 regenerate outputs changed. T12 disables
  `_enforce_slot_file_coherence` everywhere: characterization 0 changed in
  both modes, and 38 of 40 generate outputs changed.
- The §4 defect is present in a delivered plan today, flag off. From the
  prototype's `cont/wkend-long/regenerate` case:
  `2026-09-26 z2 90 min <- threshold_4x5min-2min_103pct_93min.zwo cc=threshold
  IF=0.855 Z4%=22.6`. `plan_invariants` passes it.
- Flag on, the prototype finds `z2<-anaerobic` and `z2<-sweet_spot` in event,
  blueprint and regenerate plans.
- Pytest has no judge either. The suite's only served-content test,
  `test_b5_easy_slot_content.py`, passes unchanged under T05 and T06. It
  catches only the removal of the easy-slot post-pass (E02), because that pass
  is what repairs `match_zwo`'s choice (section 4).

**Consequence.** For Step 4, the step that unblocks the flag, the existing
gates see only the part of a content-class change that also moves a session's
type, minutes or TSS. The verifier checked both extremes. With the content-class
admission bypassed at *every* call site, including the sampler's (`:6627`),
characterization moved 16 of 73 cases. With it bypassed in `match_zwo` only, it
moved 0, and 8 once `zwo_file` is in the fingerprint.

So *which file the matcher serves*, which is the §4 predicate itself, is
invisible. A fix that serves nothing looks the same as one that serves the right
file, and a regression that serves VO2 on z2 slots looks the same as no change.
**STRUCTURAL.**

### GATE-4 · HIGH · The matrix is continuous-only; Steps 3 and 5 run through code no gate executes
Every characterization and parity goal is `goal_type="continuous"`
(`characterize_planner.py:70-75`, `probe_entry_point_parity.py:36-43`).
Coverage in section 2 shows nothing ever enters these:

- `_apply_long_ride_target`, `_apply_race_week_shape`, the event/taper branch of
  `generate_phases`
- `expand_blueprint_week` (fixed_core and template)
- `recalculate_plan`'s own body (event goals only), and `refit` and `extend`
  beyond their early returns
- `reforecast`, `reforecast_dict`, `daily_adapt_plan`, `adjust_today_session`,
  `rematch_week`, `apply_week_tier_down`

In the mutation run, every V- and U-series fault survived both gates
(section 2).

The prototype gate (event, blueprint and every-entry-point cases) finds
`weekly_volume` breaches of **+17% to +30%** in a default 14-week granfondo plan
(weeks 1, 2, 6 and 7; flag off). That is the probe's own rule. The probe never
sees it because it has no event rider. **STRUCTURAL.**

### GATE-5 · HIGH · The owner's seal is not installed in the app, and in tests it depends on file order
`src/week_plan.py:173-196` defines `_install_seal`, and nothing in `src/` calls
it. `tests/test_week_plan_owner.py:137-140` installs it in `setUp` and never
removes it.

Evidence, from `gates-work/seal_probe.py`:
```
flag on, generate_plan, STRICT_SEAL=True, write session_type on a _sealed session
seal installed on PlannedSession: False
write ... ACCEPTED silently, trips = {}
```

**Consequence.**
- After the flag flips, a post-pass that overwrites the owner's decision
  (exactly what `finish()` was built to stop) is not caught by any seal in
  production.
- Under xdist, whether the guard exists in a worker depends on whether
  `test_week_plan_owner.py` happened to run there first. A flag-on test that
  relies on the seal is order-dependent. **STRUCTURAL.**

### GATE-6 · MEDIUM · 57 of 73 characterization cases pin a skeleton the generate path overwrites
The `week/*`, `stepback/*`, `completed/*` and `weeknum/*` cases call
`plan_week` directly. In `generate_plan` and `regenerate_from_today`, every
training slot `plan_week` lays out is overwritten by `sample_week_workouts`
(`training_planner.py:8583-8587`; regenerate: `:12915-12925`). Only `tss_target`, rest-day placement and
`ftp_test` slots survive into the delivered plan.

With the flag on, only the 16 `generate/*` cases move (flag-on baseline versus
golden: 16 changed). So for everything the owner does, the characterization is
16 cases, and those 16 are the date-dependent ones (GATE-2).

`plan_week` output is delivered as-is only through the deload splice
(`app.py:10595`), and `generate_weekly_plan` only through `/api/weekly-plan`
(`app.py:9242`). **INSTANCE** (the matrix composition).

### GATE-7 · MEDIUM · Pins that a correct Step 1 or 2 will break, and a broken one can keep green
Source-text assertions:

- `tests/test_planner_fixes.py:96-121`: `"tss_target * 0.72"` must appear in
  `plan_week`'s source, `"weekly_tss * 0.72"` in `generate_weekly_plan`'s, and
  every `hit_types = {...}` literal anywhere in the file must exclude tempo.
- `tests/test_budget_scaling.py:125-130,177-180`: `"not budget.week_scaled"`,
  `"1.0 if budget.week_scaled"`, `"if _gated:"`, `"feasible = _gated"` and
  `"falling back to the ungated pool"` must appear in
  `inspect.getsource(sample_week_workouts)`.
- `tests/test_planner_3d_v106.py:185-190`: `"glyco_stack_mult = 0.7"` must
  appear in `sample_week_workouts`.
- `tests/test_zone_binning.py:96-97`: compares `getsource` of two functions.

Signature pins: four test files, plus `week_plan.py`, call
`sample_week_workouts` or `expand_blueprint_week` with the 24 keyword arguments
(C7).

Step 1 deliberately rewrites the signature and the body layout of
`sample_week_workouts`. Step 2 moves the budget derivation, which is the
`week_scaled` logic, into `TrainingWeek`. Both steps fail these tests when done
correctly, and a version that keeps the strings in dead code passes them. The
property each one actually protects is listed in section 4. **STRUCTURAL**
(tests that assert text, not behaviour).

### GATE-8 · MEDIUM · Baseline stability
- **The documented command matters.** The briefed pytest command has no
  `--with fitparse --with Pillow`, and without them `test_ftp_test_freeride.py`
  fails to collect, which aborts the whole run ("Interrupted: 1 error during
  collection"). `KNOWN_FAILURES.md` gives the right command. Two different
  commands are in circulation.
- **The 15 IDs are deterministic in isolation.** Two serial runs of the four
  files gave the same 15 (C12).
- **Wall-clock assertions run in the parallel bulk.**
  - `tests/test_v181_reforecast_fastpath.py:128`: `assert elapsed < 0.5`, not
    marked `release_serial`, and in the planner set.
  - `release_serial` exists (`pytest.ini`) and marks only
    `test_v133_update_plan_perf.py` and `test_v133_frontpage_perf.py`, but the
    `KNOWN_FAILURES.md` command does not deselect them.
  - Timing-sensitive asserts also exist in `test_333_l4_ux.py`,
    `test_plan_entry_continuity.py` and `test_357_block_evaluation.py`.
  - On a loaded box these can invent a "new failure"; a timeout on a slower box
    can mask a real one.
- **Cross-test state.**
  - `conftest.py` guards `PLAN_DIR`, `WORKOUT_DIR`, `_ACTIVE_DISTRIBUTION`,
    `_ACTIVE_CUSTOM_BUDGETS`, `_VO2_MICRO_ONLY`, `db.DB_PATH` and the ICU sync
    singletons.
  - It does not guard the class-level `PlannedSession.__setattr__` seal
    (GATE-5), or `week_plan.SEAL_TRIPS`, which accumulates.
  - `_CONTENT_CLASSIFICATION_CACHE` is reset to `None` without restore in
    `tests/test_canaries_watertight.py:50`. That is harmless because it is a
    cache.
  - The `gen_*.zwo` sweep (`conftest.py:173-193`) is per-process but the
    workouts directory is shared by all xdist workers. One worker's teardown can
    delete a file another worker's test just generated. That is latent while
    `_GENERATE_WHEN_LIBRARY_MISSES = False`, and no test enables it.
- **`DOMESTIQUE_HOME` defeats conftest's sandbox.** Conftest swaps only
  `HOME` (`tests/conftest.py:59-78`), and `domestique_home()` prefers
  `DOMESTIQUE_HOME` (`src/user_home.py:23-28`). Run the suite with that
  variable set and every pytest process writes into the directory it names.
  - This review did exactly that by accident in its first pytest phase.
  - Each worker shared one directory: 390 files, 53 profiles, and
    `active_profile` switched to the fixture profile `pusher-31`.
  - Pointed at a live data directory, the same run would have rewritten the
    athlete's profile registry.
  - `tests/test_hermetic_home.py` caught it: it failed in 12 of 12 runs that
    included it, which is that test doing its job.
  - Fix: conftest should unset or override `DOMESTIQUE_HOME` as well as `HOME`.
- **Library ordering is deterministic.** `sorted(glob)` is used, and the
  "content_classification cache stale" warning (the worktree checkout changes
  mtimes) warns but still uses the cache.

**INSTANCE** (each point is small), with one structural note: the set of
failures is load-dependent by construction while wall-clock asserts share the
parallel run.

### GATE-9 · LOW · Invariants are not status-aware
`src/plan_invariants.py:65-82` counts a `missed`/`dismissed` hard session as
hard. Auditing a real refit or regenerate output, which carries missed sessions
by design, reports spacing violations the planner did not make. The prototype's
refit case trips on exactly the missed Tuesday it planted. Once GATE-1 is fixed
and those entry points are really audited, this becomes noise.
**INSTANCE.**


## 2. Coverage matrix

The table is statement coverage of each function's lines, measured with
coverage.py.

- `char` = `tests/characterize_planner.py`, all 73 cases.
- `parity` = `tests/probe_entry_point_parity.py` with N=4. N=40 calls the same
  functions with the same inputs, and only the riders differ.
- `-on` = the same gate run with `_USE_TRAINING_WEEK = True`.
- Command: `coverage run --source=src <gate>`, then `gates-work/../funcov.py`,
  which reports per function by AST range.
- "1 line" means only the `def` line ran: the function was never entered.

| function | char | parity | char-on | parity-on | pytest files that call it (baseline call trace) |
|---|---|---|---|---|---|
| generate_plan | 52% | 52% | 43% | 43% | **54** of 130: `event_api_w1`, `v161_planner_logging`, `pool_collapse_hardening`, `custom_distribution`… |
| regenerate_from_today | **0%** | 64% | **0%** | 52% | **19** of 130: `drift_chip`, `training_planner`, `execution_score`, `v1820_regen_preserves`… |
| recalculate_plan | **1 line** | 3/176 (routes to extend) | 1 line | 3/176 | **9** of 130: `332_tab_flatten`, `planner_recalc_mix`, `recalc_preserves_state`, `regen_goal_fidelity`… |
| extend_continuous_plan | **1 line** | 12/145 (returns `horizon_full`) | 1 line | 12/145 | **2** of 130: `340_continuous_w1`, `354_continuous_anaerobic_floor` |
| refit_remaining_week | **1 line** | 9/155 (returns `no_change`) | 1 line | 9/155 | **4** of 130: `execution_score`, `event_fixes_w2`, `v207_missed_hard_refit`, `event_matrix_w1` |
| daily_adapt_plan | 1 line | 1 line | 1 line | 1 line | **2** of 130: `training_planner`, `plan_api` |
| adjust_today_session | 1 line | 1 line | 1 line | 1 line | **7** of 130: `planner_injury_gates`, `362_g7_under_rider_rpe`, `v1816_downgrade_rules`, `injury_gates_integration`… |
| rematch_week | 1 line | 1 line | 1 line | 1 line | **6** of 130: `planner_acwr_feedback`, `training_planner`, `plan_api`, `event_fixes_w2`… |
| apply_week_tier_down | 1 line | 1 line | 1 line | 1 line | **3** of 130: `issue3_tier_down`, `v180_auto_adjust`, `event_fixes_w2` |
| reforecast / _apply_reforecast_to_dict / reforecast_dict | 1 line | 1 line | 1 line | 1 line | **34** of 130: `planner_acwr_feedback`, `v135_availability_rests_unavail_days`, `v131_availability_reflow`, `reflow_runs_without_new_rides`… |
| generate_weekly_plan | 58% | 1 line | 58% | 1 line | **7** of 130: `planner_acwr_feedback`, `session_payload_display_name`, `injury_gates_integration`, `340_continuous_w2`… |
| plan_week | 100% | 100% | 100% | 100% | **59** of 130: `drift_chip`, `pool_collapse_hardening`, `training_planner`, `340_continuous_w2`… |
| sample_week_workouts | 69% | 69% | 69% | 69% | **59** of 130: `slot_contracts`, `drift_chip`, `pool_collapse_hardening`, `training_planner`… |
| expand_blueprint_week (fixed_core **and** template) | **1 line** | 1 line | 1 line | 1 line | **5** of 130: `custom_distribution`, `fs1_regen_persistence`, `regen_goal_fidelity`, `event_fixes_w1`… |
| match_zwo / _match_zwo_unclamped | 60% / 76% | 60% / 75% | 60% / 66% | 60% / 66% | **86** of 130: `reentry_shape`, `session_payload_display_name`, `issue3_tier_down`, `v172_cap_rematches_zwo`… |
| scale_budget_to_week | 100% | 100% | 100% | 100% | **61** of 130: `executed_aware_budget`, `budget_scaling`, `drift_chip`, `pool_collapse_hardening`… |
| TrainingWeek.plan | **0** | **0** | 78% | 78% | **none** of 130 |
| TrainingWeek._commit | **0** | **0** | 93% | 88% | **1** of 130: `week_plan_owner` |
| TrainingWeek._commit_all | **0** | **0** | 91% | 91% | **none** of 130 |
| TrainingWeek.finish | **0** | **0** | 83% | 83% | **none** of 130 |
| `if _USE_TRAINING_WEEK:` in generate_plan (tp:8423) | 0 | 0 | 100% | 100% | |
| `if _USE_TRAINING_WEEK:` in regenerate (tp:12776) | 0 | 0 | **0** | 100% | |
| _enforce_weekly_hit_cap | 92% | 26% | 87% | 26% | **52** of 130: `pool_collapse_hardening`, `custom_distribution`, `hermetic_home`, `333_owner_fixes`… |
| _enforce_weekly_volume_ceiling | 76% | 76% | 66% | 68% | **58** of 130: `drift_chip`, `pool_collapse_hardening`, `training_planner`, `custom_distribution`… |
| _enforce_stepback_is_lightest | 92% | 92% | 96% | 92% | **52** of 130: `pool_collapse_hardening`, `custom_distribution`, `hermetic_home`, `333_owner_fixes`… |
| _enforce_easy_slot_content | 84% | 64% | 84% | 84% | **52** of 130: `pool_collapse_hardening`, `custom_distribution`, `hermetic_home`, `333_owner_fixes`… |
| _enforce_slot_file_coherence | 74% | 74% | 64% | 64% | **59** of 130: `drift_chip`, `pool_collapse_hardening`, `training_planner`, `custom_distribution`… |
| _enforce_build2_peak_hard_floor | 78% | 76% | 78% | 76% | **50** of 130: `pool_collapse_hardening`, `hermetic_home`, `333_owner_fixes`, `v1821_generate_availability`… |
| _enforce_ronnestad_floor | 91% | 91% | 91% | 91% | **50** of 130: `pool_collapse_hardening`, `hermetic_home`, `333_owner_fixes`, `v1821_generate_availability`… |
| _enforce_event_taper_eve | body not run (event only) | body not run | same | same | **67** of 130: `planner_acwr_feedback`, `v135_availability_rests_unavail_days`, `v131_availability_reflow`, `v150_reforecast_dict_signature`… |
| _enforce_hard_day_spacing / _repair_week / _solve_week_assignment | 4% / 11% / 1 line | same | same | same | (no caller reaches them) |
| **_apply_long_ride_target** (Step 3 deletes it) | **1 line** | 1 line | 1 line | 1 line | **13** of 130: `v208_taper_eve`, `v210_grill_fixes`, `planner_recalc_mix`, `v23_bc_races`… |
| _inject_mid_cycle_ftp_tests | 87% | 84% | 87% | 84% | **52** of 130: `pool_collapse_hardening`, `custom_distribution`, `hermetic_home`, `333_owner_fixes`… |
| _mark_race_days | 16% (early return) | 16% | 16% | 16% | **81** of 130: `planner_acwr_feedback`, `v135_availability_rests_unavail_days`, `v131_availability_reflow`, `v150_reforecast_dict_signature`… |
| _apply_race_week_shape | **1 line** | 1 line | 1 line | 1 line | **35** of 130: `involution`, `v208_taper_eve`, `planner_interval_variety`, `issue7_race_calendar`… |
| generate_phases (event/taper branch) | 5/172 | 5/172 | 5/172 | 5/172 | **61** of 130: `plan_config_endpoint`, `v161_planner_logging`, `entry_recognizer`, `drift_chip`… |

Reading of the matrix, against the overhaul steps:

- **Step 1** (collapse the 24-arg signature): 9 call sites, of which the gates
  reach 3 (generate ×1, regenerate ×1, plus the sampler through them). The
  sites in recalculate, extend (×2 modes) and refit, and the blueprint site in
  every entry point, are never called with the parameters they pass. A wrong
  argument there is invisible.
- **Step 2** (budget into TrainingWeek): `scale_budget_to_week` is 100% covered,
  but only through the generate and regenerate call sites. The extend, refit and
  recalculate sites are never called, and those are the "subtly different
  arguments" Step 2 is about.
- **Step 3** (long ride sized inside the owner): `_apply_long_ride_target`,
  `generate_phases`' event branch, the taper and the race week are all
  unexecuted by both gates. The one test named as the Step-3 check is
  `test_event_and_goal_focus.py`.
- **Step 4** (match_zwo content class): `match_zwo` runs, but the
  characterization fingerprint does not include the served file (GATE-4).
- **Step 5** (flag on, delete legacy bodies, migrate recalc/extend/refit): the
  owner is only exercised when the flag is on. Regenerate's owner block is
  reached only by the parity probe, which never judges it by output. The three
  entry points to be migrated are no-ops in the probe (GATE-1).

### Mutation table

37 faults, injected one at a time: 7 in the owner (W), 12 in shared builders
and constants (T), 9 in a single entry point (E), 5 in event or blueprint
machinery (V), and 4 in entry points no gate calls (U).

- `char off / on`: characterization cases changed, flag off / on, against a
  same-env baseline.
- `parity tally`: whether the probe's rule counts moved.
- `parity outputs changed`: rider × entry-point outputs that differ, flag off
  plus flag on, out of 400. This is what an output-diffing gate would see; the
  probe itself does not look at it.
- `targeted pytest`: tests that fail under the mutant but pass in the **same
  files, same flag mode, same environment and same worker count at clean
  HEAD**, plus the file count and wall time of the run.
- `verdict`: existing gates plus targeted pytest.

Times per mutation: characterization 7–15 s (flag off) and 3–8 s (flag on);
parity (N=40) 16–33 s and 18–57 s; owner unittest under 1 s; targeted pytest
8–70 s (one worker); prototype 7–13 s per mode.

| id | fault | where | char off / on (cases changed) | parity tally off / on | parity outputs changed (off+on, of 400) | owner unittest | targeted pytest: NEW failures (files) [files run, wall s] | prototype cases changed | verdict (existing gates + pytest) |
|---|---|---|---|---|---|---|---|---|---|
| W01 | owner _commit: 48h spacing off-by-one (only same-day blocked) | `week_plan.py:371` | 0 / 1 | - / - | 5 | **FAIL** | **2** `week_plan_owner` (flag on) [14 files, 17.5s] | 4 | caught |
| W02 | owner _commit: drop constraint 5 (week TSS total) | `week_plan.py:404` | 0 / 13 | - / **Y** | 126 | **FAIL** | **2** `week_plan_owner` (flag on) [14 files, 16.5s] | 18 +inv | caught |
| W03 | owner _commit: taper exemption removed (hard cap bites in taper) | `week_plan.py:384` | 0 / 0 | - / - | 0 | ok | 0 (flag on) [14 files, 17.3s] | 0 | SURVIVED |
| W04 | owner finish(): skip the easy-share floor | `week_plan.py:820` | 0 / 1 | - / - | 34 | ok | 0 (flag on) [14 files, 17.0s] | 5 | caught |
| W05 | owner: hard-share of ceiling 0.60 -> 0.75 (precedence #1 relaxed) | `week_plan.py:58` | 0 / 7 | - / - | 114 | ok | 0 (flag on) [14 files, 17.2s] | 10 +inv | caught |
| W06 | owner ceiling ignores work already ridden (Thursday-regenerate bug) | `week_plan.py:317` | 0 / 8 | - / - | 0 | **FAIL** | **2** `week_plan_owner` (flag on) [14 files, 17.3s] | 2 | caught |
| W07 | owner _processing_order: long ride loses its priority tier | `week_plan.py:683` | 0 / 6 | - / - | 106 | ok | 0 (flag on) [14 files, 17.1s] | 18 | caught |
| T01 | plan_week stepback factor 0.72 -> 0.85 | `training_planner.py:3701` | 20 / 20 | - / - | 400 | ok | **4** `340_continuous_w2`, `planner_fixes` [12 files, 24.0s] | 16 +inv | caught |
| T02 | TSS_PER_HOUR z2 45 -> 46 (the REFACTOR-PLAN self-test) | `training_planner.py:1206` | 53 / 54 | - / - | 386 | ok | **3** `availability_reforecast`, `v171_availability_per_day_cap` [12 files, 22.6s] | 22 +inv | caught |
| T03 | swap threshold/vo2max TSS-per-hour factors | `training_planner.py:1209` | 54 / 53 | **Y** / - | 356 | ok | **1** `362_g7_under_rider_rpe` [12 files, 22.3s] | 20 +inv | caught |
| T04 | scale_budget_to_week: HIT count no longer capped by affordability | `training_planner.py:2545` | 8 / 8 | - / - | 20 | ok | 0 [12 files, 16.7s] | 6 | caught |
| T05 | match_zwo ignores content class (both facts-gate admission checks off) | `training_planner.py:4960` | 0 / 4 | - / - | 279 | ok | 0 [12 files, 12.3s] | 16 | caught |
| T06 | match_zwo: easy-slot IF gate off (interval files on z2 slots) | `training_planner.py:4971` | 0 / 0 | - / - | 25 | ok | 0 [12 files, 12.2s] | 5 | SURVIVED |
| T07 | match_zwo budget clamp loosened 2x | `training_planner.py:4856` | 0 / 0 | - / - | 0 | ok | 0 [12 files, 12.1s] | 0 | SURVIVED |
| T08 | _space_hard_days_across_plan: 48h off-by-one | `training_planner.py:10536` | 5 / 2 | **Y** / - | 164 | ok | **1** `tid_plan_properties` [10 files, 54.1s] | 10 +inv | caught |
| T09 | _enforce_stepback_is_lightest: deload may be 10% heavier than builds | `training_planner.py:9977` | 6 / 1 | - / - | 68 | ok | 0 [12 files, 30.3s] | 6 | caught |
| T10 | _enforce_weekly_hit_cap disabled everywhere | `training_planner.py:9630` | 6 / 3 | **Y** / - | 56 | ok | **1** `facts_infra_failsafe` [3 files, 40.3s] | 11 +inv | caught |
| T11 | _inject_mid_cycle_ftp_tests: continuous test one week late | `training_planner.py:9032` | 16 / 16 | **Y** / - | 320 | ok | 0 [12 files, 27.4s] | 6 +inv | caught |
| T12 | _enforce_slot_file_coherence disabled everywhere | `training_planner.py:10134` | 0 / 0 | - / - | 278 | ok | **8** `r4r5_engine` [12 files, 40.1s] | 15 | pytest only |
| E01 | generate_plan skips _enforce_stepback_is_lightest | `training_planner.py:8928` | 12 / 16 | - / - | 244 | ok | 0 [12 files, 31.1s] | 12 +inv | caught |
| E02 | generate_plan skips _enforce_easy_slot_content | `training_planner.py:8901` | 7 / 2 | - / - | 44 | ok | **1** `b5_easy_slot_content` [12 files, 34.0s] | 9 +inv | caught |
| E03 | generate_plan: ONE sampler arg wrong (weekend cap <- weekday cap) | `training_planner.py:8555` | 0 / 0 | - / - | 0 | ok | 0 [1 files, 67.4s] | 8 +inv | SURVIVED |
| E04 | regenerate_from_today skips the final 48h spacing pass | `training_planner.py:13077` | 0 / 0 | **Y** / - | 28 | ok | 0 [6 files, 33.8s] | 3 +inv | caught |
| E05 | regenerate_from_today: ONE sampler arg wrong (weekend cap <- weekday cap) | `training_planner.py:12892` | 0 / 0 | - / - | 0 | ok | 0 [1 files, 66.0s] | 2 +inv | SURVIVED |
| E06 | extend_continuous_plan: sampler never told a week is a stepback | `training_planner.py:13980` | 0 / 0 | - / - | 0 | ok | 0 [2 files, 45.7s] | 0 | SURVIVED |
| E07 | extend_continuous_plan skips the weekly HIT cap | `training_planner.py:14062` | 0 / 0 | - / - | 0 | ok | 0 [3 files, 43.6s] | 0 | SURVIVED |
| E08 | refit_remaining_week: sampler never told a week is a stepback | `training_planner.py:14312` | 0 / 0 | - / - | 0 | ok | 0 [6 files, 50.1s] | 0 | SURVIVED |
| E09 | recalculate_plan (event path) skips the weekly HIT cap | `training_planner.py:13652` | 0 / 0 | - / - | 0 | ok | 0 [7 files, 70.1s] | 2 +inv | SURVIVED |
| V01 | _apply_long_ride_target: stepback discount dropped | `training_planner.py:8091` | 0 / 0 | - / - | 0 | ok | 0 [4 files, 48.4s] | 7 +inv | SURVIVED |
| V02 | _apply_long_ride_target ignores weekend availability | `training_planner.py:8090` | 0 / 0 | - / - | 0 | ok | 0 [1 files, 58.9s] | 3 +inv | SURVIVED |
| V03 | generate_plan skips the taper-eve guard | `training_planner.py:8799` | 0 / 0 | - / - | 0 | ok | 0 [12 files, 15.3s] | 2 | SURVIVED |
| V04 | generate_plan skips _apply_race_week_shape | `training_planner.py:8809` | 0 / 0 | - / - | 0 | ok | 0 [12 files, 19.6s] | 4 +inv | SURVIVED |
| V05 | expand_blueprint_week: stepback weeks get their HIT | `training_planner.py:6980` | 0 / 0 | - / - | 0 | ok | 0 [5 files, 25.2s] | 2 | SURVIVED |
| U01 | reforecast: ACWR week discount 0.85 -> 0.95 | `training_planner.py:11696` | 0 / 0 | - / - | 0 | ok | **1** `planner_acwr_feedback` [12 files, 7.9s] | 0 | pytest only |
| U02 | adjust_today_session: soreness gate threshold 6 -> 7 | `training_planner.py:15463` | 0 / 0 | - / - | 0 | ok | **1** `planner_injury_gates` [7 files, 18.6s] | 0 | pytest only |
| U03 | daily_adapt_plan: upscale clamp 1.25 -> 1.50 | `training_planner.py:14702` | 0 / 0 | - / - | 0 | ok | 0 [2 files, 11.8s] | 0 | SURVIVED |
| U04 | rematch_week: tie-break flipped | `training_planner.py:14923` | 0 / 0 | - / - | 0 | ok | 0 [7 files, 36.0s] | 0 | SURVIVED |

**Survivors.**

| of what | count | ids |
|---|---|---|
| the existing gates (characterization off and on, parity tally off and on, `test_week_plan_owner`) | **19 / 37** | W03, T06, T07, T12, E03, E05, E06, E07, E08, E09, V01, V02, V03, V04, V05, U01, U02, U03, U04 |
| … plus the targeted pytest files | **16 / 37** | W03, T06, T07, E03, E05, E06, E07, E08, E09, V01, V02, V03, V04, V05, U03, U04 |
| … plus the prototype (`proto_gate.py`) | **7 / 37** | W03, T07, E06, E07, E08, U03, U04 |

Two narrower counts matter for the overhaul:

- **Characterization as it runs today (flag off) catches 10 of 37**: T01, T02,
  T03, T04, T08, T09, T10, T11, E01, E02. Every one of them changes load (type,
  minutes or TSS) in `generate_plan` or `plan_week`.
- **With the flag on, the 13 end-to-end files that drive generate and
  regenerate caught none of the 7 owner mutations.** Only
  `test_week_plan_owner.py`, which calls `_commit` directly, caught W01, W02 and
  W06.

The seven that survive everything are informative in themselves:

- **W03, the taper exemption**, is invisible at plan level (R5).
- **T07, the `match_zwo` budget clamp loosened 2×**, never moved any output. It
  is equivalent for every input tried here.
- **E06, E07 and E08** are faults in extend and refit that need a stepback, or
  a HIT-cap overflow, inside the appended or refit week, and no case provides
  one.
- **U03 and U04** need a large deficit in daily adapt and tied candidates in
  rematch.

## 3. Claims checked

| # | claim (where) | verdict | evidence |
|---|---|---|---|
| C1 | "every date here is absolute rather than relative to today, so a run in six months compares equal to a run now" (`tests/characterize_planner.py:22-24`) | **DID NOT HOLD** | The same code with `date.today()` frozen, compared against the golden: 09-10 all 73 unchanged; **09-11: 16 changed**; 09-17: 8 changed; 10-12: 16 changed. The 16 are every `generate/*` case. `generate_plan` anchors on `date.today()` (`training_planner.py:8341`, `generate_phases`) and the cases pass no date. (`gates-work/../chain2.out`) |
| C2 | "73 cases, all passing" (`notes/planner-cleanup-plan.md` table) | **HELD only on the owner's machine, on 2026-09-10** | Empty data home: 2 changed (`generate/every-day/3h/{fresh,midweek}`). Copy of `~/.domestique`: 0 changed. The same copy with only `profiles/default/.env` removed: changed. Why: `match_zwo` seeds from `config.ICU_ATHLETE_ID` (`training_planner.py:5337-5356`); two different ids move 6 of the 16 `generate/*` cases (verifier). With the network blocked by an audit hook, the owner copy still matched, with 0 socket attempts, so this is the credential value, not a live call. |
| C3 | "The planner is deterministic for a fixed seed_salt (verified)" (`characterize_planner.py:22`) / "43 of 57 differing -> 0 of 57" (`REFACTOR-PLAN.md`) | **HELD** across processes; **not** across machines | `probe_plan_reproducibility.py --runs 3` (no hash pin): "0 of 73 cases differ". Across profiles it fails, per C2. |
| C4 | "mutating `TSS_PER_HOUR["z2"]` 45->46 is caught, restoring it goes clean" (`REFACTOR-PLAN.md`) | **HELD** | Mutation T02: characterization 53/73 changed; the revert was verified clean by the harness. |
| C5 | "Each of generate / regenerate / recalculate / extend / refit ... This probe asks whether that difference is visible in the output" (`tests/probe_entry_point_parity.py:3-8`) | **DID NOT HOLD** | recalculate, extend and refit outputs are byte-identical to generate's for **40/40 riders**, with the flag off and on (GATE-1). |
| C6 | "`weekly_volume` violations should not increase from the flag-off baseline of 1/7/1/1/1" (notes, Step 2) | **Not a usable criterion** | Hermetic env on 09-10: 2/7/2/2/2. Same env on 09-11: **4/6/4/4/4**. Columns 3-5 are generate's column copied (C5). |
| C7 | "Keep thin `**kwargs` shims only if a caller outside `training_planner.py` needs them -- at last count none did" (notes, Step 1) | **DID NOT HOLD** | `src/week_plan.py:597,609`; `tests/test_slot_contracts.py:214`, `test_event_and_goal_focus.py:285`, `test_availability_and_reshuffle_band.py:33`, `test_canaries_watertight.py:96` all call the builders with keyword arguments. |
| C8 | "Nothing outside this class writes to the sessions it produces. That is enforced rather than documented: `plan()` seals what it returns, and a later write raises under STRICT_SEAL" (`src/week_plan.py:274-277`) | **DID NOT HOLD in the app** | `git grep _install_seal -- src/` finds only the definition. The only callers are `tests/test_week_plan_owner.py:138` and `probe_owner_vs_legacy.py:45`. Probe: flag on, generate, `STRICT_SEAL=True`, then write to a `_sealed` session. Result: `seal installed on PlannedSession: False`, and the write was accepted silently with `trips = {}`. |
| C9 | "A clean invariant sweep is not proof of correctness ... It is a floor, not a proof." (notes §6) | **HELD, and it is worse than a floor** | Across 37 mutations the parity tally moved for **6** (W02 flag-on; T03, T08, T10, T11, E04 flag-off), while output fingerprints moved for **19** (section 2). |
| C10 | `plan_invariants` "states the rules once ... so a rule can be verified" (`src/plan_invariants.py:8-10`) | **PARTIAL** | Six rules. `slot_file_coherence` looks only at rest slots (`:145-151`). None is status-aware: a *missed* hard session counts as hard. The prototype's refit case trips `hard_day_spacing` on the missed Tuesday it planted, not on anything the planner did. Nothing covers hard share, stepback, taper or the long ride. |
| C11 | "Before trusting 'all unchanged', check the matrix calls the function you changed" (notes §6) | **Needed but not sufficient** | T05 (`match_zwo` ignores content class) and T06 run inside `match_zwo`, which the matrix does call, yet characterization reports 0 changed: the fingerprint omits `zwo_file`. |
| C12 | "15 failed ... diff of failing IDs empty" (`tests/KNOWN_FAILURES.md`) | **HELD** for the four files, serially | Two serial runs of the four files, `--with fitparse --with Pillow`: the same 15 IDs both times. Without `--with fitparse` the run aborts at collection ("Interrupted: 1 error during collection", `test_ftp_test_freeride.py:15`). |
| C13 | `PYTHONDONTWRITEBYTECODE` "never leave one" (`characterize_planner.py:43-47`) | **HELD** | `find src tests -name '*.pyc'` gives 0 in the worktree after all runs. |
| C14 | "84 argument lines are literally `x=goal.x`" (notes §2) | **Roughly held** | By my regex: 64 `=goal.<attr>,` lines + 23 `=adjusted_goal.<attr>,` = 87. |
| C15 | "With it [the flag] on: 6 failures", naming `TestFix2TempoNotHIT`, `test_planner_variety_bonus` ×3, `TestBlockConcentration`, `test_blueprint_keeps_b5_and_b3` (notes table, §4) | **5 of 6 reproduce** | The four named files at clean HEAD with `gates-work/flag_on_plugin.py`: "5 failed, 50 passed, 2 xfailed". `test_v22_block_periodization::TestBlockConcentration` **passes** with the flag on. The full-suite flag-on count was not measured (whole-suite runs were out of scope). |

## 4. Recommendation

These are the minimum additions that would let each overhaul step be judged.
Each one names the blindness it closes and the evidence for that blindness.
`gates-work/proto_gate.py` is a working prototype of R1–R3 plus two of R4's
invariants (22 cases, 12 s, deterministic across hash seeds: "changed: []"
with `PYTHONHASHSEED=123`). The last column of the mutation table shows what it
catches that the existing gates do not.

**Do R1 before anything else. Until it lands, "all unchanged" means "run on
2026-09-10 from the owner's profile".**

### R1 — Make the golden hermetic (closes GATE-2)
In `characterize_planner.py`:

- Freeze `training_planner.date` at `ANCHOR`, the way `conftest.FrozenPlannerDate`
  already does for pytest.
- Pin the `match_zwo` seed input: set `config.ICU_ATHLETE_ID` to a constant, or
  stamp `session.profile_id`.
- Pass `recent_weekly_tss` explicitly, so the ride archive is never read.
- In the re-exec, set `DOMESTIQUE_HOME` to an empty temp dir and
  `DOMESTIQUE_NO_NET=1`.
- Date the `midweek` rides inside the plan's opening week. Today they fall in
  week 2, and from next month before the plan starts (GATE-2).

Evidence: C1 and C2. The prototype applies these pins and reproduces
byte-for-byte across hash seeds. After R1, re-bless **once**, and record in the
commit that the only change is the pin.

### R2 — Put the served file in the fingerprint (closes GATE-3; required for Step 4)
Fingerprint `[type, minutes, TSS]` as the *load* and `zwo_file` as the
*content*, and have the diff say which one moved. Step 4 should move content
and nothing else, and a reviewer can check exactly that.

Evidence: with the flag off, T05 (content class ignored in `match_zwo`) and
T12 (coherence pass off) both gave **0 changed** in characterization, while
32–38 of 40 parity outputs moved. With the flag on, T05 moved 4 cases and T12
still 0.

### R3 — One real case per entry point, driven into its body (closes GATE-1 and GATE-4)
Add cases, fingerprinted per week, for:

| entry point | case that actually reaches its body | why (evidence) |
|---|---|---|
| generate | continuous **and** event (granfondo, weekend cap 5 h and 3 h), **and** `plan_mode` fixed_core and template | V01–V05 survived both gates; `expand_blueprint_week` has 0 coverage |
| generate / regenerate | a goal whose weekday hours ≠ weekend hours | E03/E05 (one sampler argument wrong) are invisible because both gates give every day the same hours (`characterize_planner.py:70-75`; the probe sets `max_weekday_hours = max_weekend_hours`) |
| regenerate | mid-week, with rides logged | W06 (owner ignores ridden) moved 0 parity outputs: the probe never passes rides |
| recalculate | an **event** goal, date advanced 3 weeks | a continuous goal is routed to extend (`:13217`); E09 was caught only by the prototype |
| extend | date advanced past the horizon so it appends, with a **stepback inside the appended window** | the probe's extend is `horizon_full`. E06/E07 survived even the prototype, whose appended week was not a stepback and did not exceed the HIT cap. The input must make the mutated branch matter |
| refit | a missed hard session in the current week, and a stepback current week | the probe's refit is `no_change`; E08 survived the prototype for the same reason as E06 |
| reforecast, reforecast_dict, daily_adapt_plan, adjust_today_session, rematch_week, apply_week_tier_down | one case each, with inputs chosen to reach the scaling / gate branch (ACWR spike; soreness ≥ 6; large deficit; tied candidates) | U01–U04 survived both gates **and the prototype**: generic inputs never reached the mutated line. Targeted pytest caught U01 (`test_planner_acwr_feedback`) and U02 (`test_planner_injury_gates`); U03 and U04 survived that too |

Make the matrix check itself: run it under coverage and fail when a named entry
point executes fewer than N statements. Evidence: recalculate, extend and refit
ran 3, 12 and 9 lines in the parity probe, and nobody noticed. That is the
notes' own trap ("check the matrix calls the function you changed"), automated.

### R4 — Invariants that match the stated precedence (closes GATE-3, C9, C10, GATE-9)
Add to `plan_invariants`, each checked **after** the file is attached:

1. **Served content**: an easy slot (`z2`/`long_z2`/`recovery`) must not carry a
   file whose content class is hard. This is §4's predicate. It fires today on
   a delivered plan: `z2 90 min <- threshold_4x5min...`.
2. **Hard share** ≤ `HARD_CEILING_SHARE` × ceiling outside taper (precedence #1).
   W05 (0.60 → 0.75) moved 114 parity outputs and 0 invariant tallies.
3. **Stepback is lighter** than the builds before it in its block. T01 (stepback
   factor 0.72 → 0.85) moved 400 of 400 parity outputs and 0 tallies.
4. **Long ride ≤ weekend cap.** This closes a gap the six checks leave, but
   it is *not* what catches V02. In the V02 mutant no weekend ride exceeds the
   cap, because generate's final per-day clamp still enforces it (verifier). The
   prototype catches V02 through its fingerprint and a `weekly_volume`
   violation.
5. **Spacing ignores missed/dismissed sessions**, so refit and regenerate audits
   are not noise (GATE-9).

Have the parity probe also diff *outputs* against a stored per-entry-point
fingerprint, not only tally rule counts. Across 37 mutations the tally moved 6
times and the outputs 19 (C9).

### R5 — Permanent negative controls (the notes' "verify an instrument fails when the fault is present")
Add a gate self-test that copies `src/` to a temp dir, applies each of a handful
of known faults, runs the gate, and asserts **"changed"**. Otherwise a harness
change can silently blind it again, as happened twice. `gates-work/mutharness.py`
is the prototype, at about 40 s per control. One control per blindness class,
taken from the mutation table:

- **T05**: content class ignored. Must move the content fingerprint.
- **E07**: fault in extend only. Must move the extend case.
- **E08**: fault in refit only.
- **E09**: fault in recalculate's event path.
- **V02**: long ride ignores the weekend cap.
- **E03**: one sampler argument wrong. Needs the weekday ≠ weekend goal.
- **W06**: owner ignores rides.
- **W03**: taper exemption. It cannot be seen at plan level: in every event
  plan built here, taper weeks carried 0–50% hard share against a 60% cap, while
  at unit level the exemption binds (3×80 TSS against 200 commits 200, not 120).
  Add a `TrainingWeek` taper case to `test_week_plan_owner.py`.

### R6 — Baseline hygiene (closes GATE-5, GATE-8)
- **One command.** The `KNOWN_FAILURES.md` command with `--with fitparse --with
  Pillow`, bulk as `-m "not release_serial"` and the perf tail serial. Mark
  `test_v181_reforecast_fastpath.py`'s `< 0.5 s` assert `release_serial`.
- **Pin dates in planner tests that read `date.today()` without
  `planner_pinned_env`.** Measured: `test_b3_stepback_lightest.py::…
  test_stepback_lighter_than_every_build_in_block` fails at clean HEAD **on
  2026-09-10**, and its outcome moves with the date. With the planner's today
  frozen it fails on 09-03, 09-10, 09-11, 09-12, 09-13 and 09-17, and passes on
  09-07 to 09-09 and 09-14 to 09-16 (my sweep plus the verifier's). Only the
  planner's date was frozen: the test's own `target_date` is the real
  `date.today() + weeks` (`:24`). Today the "no new failures" rule already has
  a 16th red test that no code change caused.
- **Order-dependence.** `test_v207_missed_hard_refit::…reaches_cap` and
  `test_v208_distribution_choice::…reuses_the_same_objects` failed in the
  combined 130-file `-n 2` run and pass alone. Judge a new failure against the
  **same file set and worker count** run at clean HEAD, never against the
  15-line list alone.
- **The seal.** Install it at import in `week_plan` (the app gets it), or make
  the test fixture uninstall it. Either way it must not be worker-order-
  dependent (GATE-5).

### R7 — Replace text pins with the property they protect, before Steps 1–2 (closes GATE-7)
The overhaul will legitimately move the files in the table below. A re-pin is
acceptable only if the property column is still asserted *behaviourally*
afterwards. The test to write is in the last column.

| file (what it pins) | property it actually protects | judge a re-pin by |
|---|---|---|
| `test_planner_fixes.py:96-136`: source text `"tss_target * 0.72"`, `"weekly_tss * 0.72"`, every `hit_types = {…}` literal | a stepback week is 0.72× its base week (Issurin 28%) in **both** builders; tempo is not HIT | the behavioural check already at `:90-93` (`plan_week(...stepback).tss_target == round(tss*0.72)`); add the same for `generate_weekly_plan`, and "tempo slots do not count toward `hit_count_max`" on a real week; delete the source greps |
| `test_budget_scaling.py:125-130,177-180`: `getsource(sample_week_workouts)` contains `week_scaled` / `_gated` strings | the budget is re-expressed per athlete, the stepback is applied **once** (deload = 72%, not 52%), an empty gated pool falls back instead of serving nothing | build a stepback week and assert its budget ≈ 0.72× the build, not 0.52×; a library with the gated pool emptied still fills the slot. Step 2 moves this logic into `TrainingWeek`, so the strings **will** go |
| `test_planner_3d_v106.py:185-190`: `"glyco_stack_mult = 0.7"` in the sampler source | a second glycolytic session within 48 h is down-weighted (×0.7), not banned | a two-glycolytic-slot week where the second is still possible but less likely, seeded |
| `test_zone_binning.py:96-97`: `getsource` of two scanners compared | the app and planner scanners bin zones identically | once deduplicated, assert both names are the same function object, or bin the same files identically |
| `test_week_plan_owner.py:92,99`: private `tw._hard_tss`, `tw._tss`, direct `tw._commit` | hard ≤ 60% of ceiling; week ≤ ceiling; 48 h eases to z2 (not tempo) and drops the file; rides come off the ceiling | the same assertions on `TrainingWeek.plan()` / `finish()` output, plus the taper case (R5) |
| `test_event_fixes_w2.py`: calls 12 private passes directly, including `_apply_long_ride_target`, which **Step 3 deletes** | long ride +25 min/week toward the event target, ×0.72 on stepback, capped by weekend hours, stops 3 weeks out | an event plan's weekend long rides: increasing, ≤ weekend cap, stepback shorter, none in the last 3 weeks. `test_long_ride_capped_by_weekend_hours` covers only the cap |
| `test_r4r5_engine.py:250-334`: internal counters `stats["trips"/"rematched"/"restamped_down"/"kept_narrated"]` of `_enforce_slot_file_coherence` | the coherence pass never lengthens a slot, and replaces a file that does not fit | if the pass is folded into the owner (Step 5): no session longer than its pre-pass length, and the served file within the slot's duration band |
| `test_slot_contracts.py:214`, `test_event_and_goal_focus.py:285`, `test_availability_and_reshuffle_band.py:33`, `test_canaries_watertight.py:96`, `src/week_plan.py:597,609`: the 24 keyword arguments | slot content contracts (facts gate), long ride vs availability, reshuffle band, watertight canaries | Step 1's diff to these files must be **call-site only**. Any assertion that changes is a behaviour change, not a re-pin |
| `test_planner_variety_bonus.py` ×3, `test_fs1_planner_modes::test_blueprint_keeps_b5_and_b3`, `test_planner_fixes::TestFix2…` (red with the flag on, C15) | vo2_short and per-phase floors; blueprint shape; weekly HIT cap | the notes' own trap: five quota tests that looked like re-pins were a bug. Judge them with R4.1 (served content) on the same plans, not by lowering the counts |

### What the targeted pytest runs showed (evidence for R3 and R5)

For each mutation I ran the pytest files a careful engineer would pick, chosen
in this order:

- the test the notes or the code name as that behaviour's judge;
- files that name the changed function;
- files that called it in a traced baseline run of the 130 planner test files.

Each run got up to about 180 s of baseline test time. With the flag on, the
owner mutations used `flag_on_plugin`. Selection is in `gates-work/build_sel.py`
and `pytest_sel.json`.

**The named judges run their entry point, but not with an input under which the
fault matters.** Every test below reaches the code in question; none of them
failed.

| fault | the test named as its judge, and how it reaches the code | outcome |
|---|---|---|
| E03, E05 (sampler gets the weekday cap for the weekend), V02 (long ride ignores the weekend cap) | `test_event_and_goal_focus.py`, the notes' Step-3 check: 4 event `generate_plan`, 1 `regenerate_from_today`, weekend caps 3, 4 and 5 h | **not caught** (E03 and V02 run alone, 47 s each). The prototype's continuous rider, with weekday ≠ weekend hours, caught E03 and E05. |
| V03 (generate skips the taper-eve guard) | `test_v208_taper_eve.py`: one event `generate_plan` | **not caught** |
| V01 (long-ride stepback discount dropped) | `test_b4_deload_long_ride.py`: 2 event `generate_plan` | **not caught**. Probably masked by the final `STEPBACK_LONG_RIDE_CAP_MIN` clamp; that mechanism is not verified. |
| V04 (generate skips the race-week shape) | `test_recalc_preserves_state.py`: 1 event generate, 1 recalculate | **not caught** |
| V05 (blueprint stepback weeks get a HIT) | `test_fs1_planner_modes.py`: 6 `generate_plan`, goal type `ftp` | **not caught** |
| E06, E07 (extend: no stepback / no HIT cap) | `test_340_continuous_w1.py`: 7 `extend_continuous_plan` calls, plus `test_354…` | **not caught** |
| E08 (refit: no stepback) | `test_v207_missed_hard_refit.py`: 11 `refit_remaining_week` calls, goal type `general` | **not caught** |
| E09 (recalculate's event path skips the HIT cap) | 7 files including `test_recalc_preserves_state.py` and `test_planner_recalc_mix.py` | **not caught** |

So the notes' trap ("check the matrix calls the function you changed") applies
to pytest as well. Every one of these files *executes* the mutated line's
function; none *judges* it. That is why R3 asks for inputs chosen so that the
mutated branch changes the output. R5's negative controls are how to know an
input qualifies.

**What the targeted pytest runs did catch.** The IDs below are the tests that
fail under the mutant and pass at clean HEAD in the same file set:

| mutation | caught by | what that test asserts |
|---|---|---|
| W01, W02, W06 (owner, flag on) | `test_week_plan_owner.py` only: `…too_close_to_another_is_eased_to_z2`, `…easing_drops_the_workout_file…`, `…week_total_stays_under_the_ceiling`, `…fully_ridden_prescribes_nothing_more`, `…work_already_ridden_comes_off_the_ceiling` | direct `_commit` unit tests. The 13 end-to-end files in the same run caught **none** of W01–W07 with the flag on. |
| T01 (stepback 0.72 → 0.85) | `test_planner_fixes.py::TestFix1StepbackReduction` (3 tests: 2 behavioural, 1 the source-text pin), `test_340_continuous_w2.py::…monotony_trip_converts_current_week` | the behavioural tests catch it on their own, so the text pin adds nothing (R7) |
| T02 (z2 TSS/h 45 → 46) | `test_availability_reforecast.py` ×2, `test_v171_availability_per_day_cap.py` | availability rescaling of TSS |
| T03 (threshold ↔ vo2max TSS/h) | `test_362_g7_under_rider_rpe.py::…load_is_held_by_trimming_time…` | |
| T08 (plan-wide 48 h off-by-one) | `test_tid_plan_properties.py::…hard_days_keep_48_hours_apart` | |
| T10 (HIT cap off) | `test_facts_infra_failsafe.py::test_ftp_test_is_hit_and_cap_never_demotes_it` | `test_planner_weekly_hit_cap.py` (1,547 s) was over the time budget |
| T12 (coherence pass off) | `test_r4r5_engine.py` ×8 | including the internal-counter tests of R7 |
| E02 (generate skips the easy-slot pass) | `test_b5_easy_slot_content.py::…no_easy_slot_carries_a_hard_file` | the one existing served-content test (see below) |
| U01, U02 | `test_planner_acwr_feedback.py::…acwr_over_threshold_scales_next_week`, `test_planner_injury_gates.py::test_g5_soreness_6_forces_recovery` | unit tests of the two ungated entry points |

**A red test that goes green is not progress.** The one test that fails at
clean HEAD with the flag on (`test_planner_fixes::TestFix2TempoNotHIT::
test_multiple_tempos_and_one_hit_possible`) **passes** under two of the owner
mutations:

- W02, which drops the week-total constraint;
- W07, which drops the long ride's priority tier.

During Step 5, "one fewer failure" can be produced by weakening a constraint,
and the run would look like progress.

**No existing pytest test judges the §4 predicate either.**
`test_b5_easy_slot_content.py` contains the suite's only served-content check,
`test_no_easy_slot_carries_a_hard_file`. I ran it directly against T05
(content class ignored in `match_zwo`) and T06 (easy-slot IF gate off). Both
gave "2 passed, 6 subtests passed", identical to clean HEAD.

It catches E02, the removal of the easy-slot *post-pass*, because that pass
repairs whatever `match_zwo` lets through. So the suite pins the repair and not
the selection: Step 4, which moves the rule into selection, has no pytest judge.
R2 and R4.1 are the judge.
