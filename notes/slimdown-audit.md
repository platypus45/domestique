# Slimming audit — measured, not guessed

Last measured **2026-09-16** at `e6d52b7d`. Everything below is a number an
instrument produced; re-run the instruments before trusting any of it again.

## The instruments (`tools/arch/`)

```sh
python3 tools/arch/astmap.py src > /tmp/src.json          # symbols, refs, imports, endpoints, clones
python3 tools/arch/report.py /tmp/src.json                # the numbers a slimming plan needs
python3 tools/arch/endpoints.py /tmp/src.json src/templates src/static ~/Documents/cycling-stack/bin
python3 tools/arch/endpoints_control.py /tmp/src.json     # negative control: must print 0 misidentified
python3 tools/arch/reach.py src /tmp/src.json src/templates ~/Documents/cycling-stack/bin
```

`astmap` reads function-LOCAL imports as well as module-level ones — this
codebase imports inside functions to break cycles, so a module-level graph
lies. `reach` builds the call graph by bare name, so it is over- rather than
under-connected: a false edge keeps code alive, which is the safe direction
for a deletion argument.

**Their output is a candidate list, never a verdict.** Every item below that
says LIVE was on the instrument's dead list and turned out to be reachable.
Confirm each candidate by a second method (grep for dynamically built URLs,
check the flag actually evaluates false in production) before deleting it.

## Where the tree stands

| | 2026-09-15 (`3a75d688`) | after wave 1 | after wave 2 |
|---|---|---|---|
| modules | 77 | 56 | 55 |
| src LOC | 80,102 | 66,035 | 64,827 |
| `/api` endpoints | 142 | 142 | 127 |

Two files hold 56 % of it: `app.py` 20,567 (403 defs, worst function 452
lines / complexity 189) and `training_planner.py` 16,350 (241 defs, worst
function 1,079 lines / complexity 282: `sample_week_workouts`).

## Wave 1 — done (`ed95eae2`, `d0441904`)

Deleted with no behaviour change (186 related tests green, planner
characterization byte-identical): `training_live.py` (every engine class
unreferenced), `route_archetypes.py` (2,857 of 4,258 LOC unreachable), their
tests, and 20 of 24 `src/scripts/*.py`. `RideSample`, `_is_valid_decoupling_sample`
and the two decoupling constants moved to `fitness_estimation.py`.
`generate_ronnestad_microintervals.py` was restored after deletion — it is how
the 30/15 family is produced, and all 21 files it emits are recognised by the
microinterval classifier.

## Wave 2 — endpoints no client reaches

**Re-audited 2026-09-16 with a rewritten instrument.** The first pass matched
each route's literal parts against the client text with a short wildcard
between them and got three of twenty wrong, because the dashboard builds

```js
`/api/course/${encodeURIComponent(region)}/${encodeURIComponent(name)}/download`
```

and 24 characters of `${encodeURIComponent(` blew past the gap. `endpoints.py`
now parses URLs instead of text: interpolation is collapsed to a wildcard
segment *before* strings are extracted (so `'/api/ride/' + id + '/detail'` is
read whole), a trailing slash implies a segment, and routes match segment by
segment.

**It is checked against a negative control**: every `/api` path that appears
inside a `fetch()` in the dashboard or the static JS — 105 of them — must come
back "reached". It does. Re-run that control after any change to the matcher;
a tool nobody has tried to break is not evidence.

Verdicts over the 142 `/api` routes: **122 reached, 3 ambiguous, 17 unreached.**

Ambiguous — a client URL lines up only through an interpolated segment
(`/api/routes/${id}` lines up with `/api/routes/surfaces`), so the source
cannot say either way. Read the call site before touching these:

- `GET /api/rides/legacy-envelope`
- `GET /api/routes/surfaces`
- `GET /api/workout/download/{filename}`

Unreached by any client:

| endpoint | note |
|---|---|
| `GET/POST /api/blood-markers` | no UI at all |
| `GET /api/climb-workout` | climb/GPX cluster |
| `GET /api/climb-zwo/{region}/{filename}` | climb/GPX cluster |
| `GET /api/gpx/{region}/{filename}` | climb/GPX cluster |
| `GET /api/download/crs/{region}/{filename}` | climb/GPX cluster |
| `GET /api/download/gpx/{region}/{filename}` | climb/GPX cluster |
| `GET /api/gc/status` | |
| `GET /api/metrics/latest` | |
| `GET /api/plan/missed-suggestions` | 1 test |
| `POST /api/plan/rematch/{day}` | the dashboard calls `/api/plan/rematch?apply=1`, a different route; 4 tests |
| `GET /api/power-curve` | dashboard names it only in a comment: "legacy loadPowerCurve(...)" |
| `GET /api/readiness/composite` | dashboard says "the deprecated /api/readiness/composite is no longer used" |
| `POST /api/ride/{ride_id}/prs/recompute` | nothing in the repo names it |
| `GET /api/setup/icu-hr` | setup.html does not call it |
| `GET /api/setup/status` | 1 test |
| `POST /api/setup/test-icu` | also named by `packaging/tls_intercept_probe_win.py` |

What the re-audit changed, and why it matters more than the list:

- **Wrongly called dead before, actually live:** `/api/course/.../download`,
  `/api/ride/{ride_id}/prs`, `POST /api/activity/{activity_id}/race`. All three
  are reached through interpolated URLs.
- **Wrongly called live before, actually dead:** `/api/power-curve` and
  `/api/readiness/composite`. Both appear in `dashboard.html` — in comments
  saying they are no longer used. A grep for the string said "referenced".
- `/oauth/icu/callback` is out of scope now (the instrument reports `/api`
  routes); it stays regardless, as part of the packaged build.

Deleting the climb/GPX cluster also retires `gpx_to_gc.py` (391 LOC) and its
test. `src/workout_analysis.csv` (428 KB) is named only in two comments that
say it no longer exists — an orphan.

## Wave 2 — done

1,305 lines of Python and a 2,460-line orphan CSV, with the characterization
byte-identical and the full gate green:

- 15 of the 17 unreached endpoints (550 lines of `app.py`).
- `gpx_to_gc.py` (390) — the climb/GPX endpoints were its only callers.
- `routes_lib._build_climb_zwo` + `_gradient_to_power_factor` (64), orphaned
  by those endpoints, and the `config` import they held up.
- Seven defs with no caller and no test: `ride_storage._build_summary_dict`
  (105), `training_planner._enforce_hard_day_spacing` (53),
  `classify_library_content._mask_cooldown` + `_audit_reason` (63),
  `oos_validation._predict_ftp_for_date` (28), `sleep.compute_sleep_score`
  (24), `structure_fidelity._plan_rest_frac` (21).
- `src/workout_analysis.csv` (428 KB), named only in two comments saying it
  no longer exists.

**Kept, with the reason, from the 17:**

- `POST /api/setup/test-icu` — `packaging/tls_intercept_probe_win.py` posts to
  it during the Windows TLS-interception probe. No client calls it; a build
  does.
- `GET /api/setup/status` — 3 lines, two tests, named in the packaging plan.

**Coverage was moved, not dropped.** Three deleted endpoints were thin
wrappers over live code, and their tests now call that code directly:
`_compute_missed_suggestions` (still behind `/api/plan`),
`power_curve.compute_ride_prs`, and — for `/api/readiness/composite`, which
the dashboard stopped calling in v2.2.5 — the two remaining readers in
`test_one_fitness_state`.

## Still to decide (measured, not deleted)

These are dead in production but have real test surfaces, so deleting them is
a decision about the capability, not a cleanup:

| def | lines | test hits | note |
|---|---|---|---|
| `plan_invariants.check_acwr` (+ `projected_ctl`) | 48 | 13 | **A bug, not dead code:** it is a written, tested ACWR safety check that `ALL_CHECKS` never runs, so `audit()` has never enforced it. Wire it in rather than delete it. |
| `ride_report_png.render_ride_report_png` | 93 | 0 | An entire PNG feature, unwired. The owner said to keep PNG export — this is a different, unreferenced one. |
| `fitness_estimation.aerobic_decoupling` | 100 | 10 | The product reads `decoupling_pct` from intervals.icu (`ride_storage:498`), never this local computation. |
| `ride_storage.recent_mean_weekly_tss` | 52 | 10 | Superseded by `chronic_weekly_tss`; its own module docstring says why. |
| `compute_fitness_signature`, `compute_local_atl`, `detect_wbal_overshoot`, `compute_sr_avg`, `count_weighted_markers`, `audit_labels`, `set_level`, `title_from_zwo`, `legacy_card_state`, `schedule_double_threshold_pair`, `_install_seal` | ~430 | 60+ | Utilities whose only exercise is their own unit tests. |

## Candidates the instruments flagged that are NOT dead

- **`week_solver.py` (220 LOC) and `workout_gen.py` (404 LOC) are live.**
  The "always-false flag" belief was wrong: `_solve_week_assignment` returns
  False only when scipy is missing, and scipy is installed in BOTH the dev
  venv and `cycling-stack/domestique/.venv`. `workout_gen` fires under
  `_GEN_MAX_PER_WEEK = 3`, not a disabled flag.
- **`error_codes.py`**: all 70 codes are referenced as `Codes.<NAME>`; nothing
  to trim.
- **`.overhaul_manifest.json`** lives in `src/workouts/`, not `src/`, and
  `test_library_consistency.py` reads it. `.osm_cache/` does not exist.
- `launcher.py`, `ride_report_png.py` are imported by nobody — they are entry
  points / desktop layer, which the owner has said to keep in full.

## The rest of the plan (unmeasured beyond this point)

1. `zwo_parse` extraction out of `training_planner.py`, then the app.py leaf
   modules, `plan_store`, the `training_planner` line-range splits, routers.
   The planner's band graph is a DAG but for two one-edge cycles, so it splits
   by line range; app.py's one 12-band SCC is held by ~10 misplaced helpers
   and collapses with 7 leaf extractions.
2. 14 clone groups, 355 duplicated LOC.
3. 76 top-level defs (3,035 LOC) are never named in src, templates or the
   stack jobs — biggest: `ride_storage._build_summary_dict` (103),
   `fitness_estimation.aerobic_decoupling` (100),
   `analytics.compute_dfa_threshold_analysis` (96).

## Rules for every wave

1. Move code unchanged; re-export moved names (207 test files reach `app.X`).
2. One wave per commit series; `tests/characterize_planner.py` must stay
   byte-identical unless the wave is explicitly a behaviour change, in which
   case the diff is reviewed case by case before `--bless`.
3. Anything with no test gets a characterization test written and shown green
   BEFORE it moves.
4. Nothing deploys until the full gate (`tools/gate.sh`) and the stack suite
   (`cycling-stack/tests/run-all`) pass.
5. Re-run the instruments after each wave and update the table above.
