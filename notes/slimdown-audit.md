# Slimming audit — measured, not guessed

Last measured **2026-09-16** at `e6d52b7d`. Everything below is a number an
instrument produced; re-run the instruments before trusting any of it again.

## The instruments (`tools/arch/`)

```sh
python3 tools/arch/astmap.py src > /tmp/src.json          # symbols, refs, imports, endpoints, clones
python3 tools/arch/report.py /tmp/src.json                # the numbers a slimming plan needs
python3 tools/arch/endpoints.py /tmp/src.json src/templates ~/Documents/cycling-stack/bin
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

| | 2026-09-15 (`3a75d688`) | today (`e6d52b7d`) |
|---|---|---|
| modules | 77 | 56 |
| symbols | 1,946 | 1,418 |
| src LOC | 80,102 | 66,035 |
| endpoints | 150 | 150 |

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

`endpoints.py` lists 20; three of those are LIVE and must stay:

- `/api/plan/rematch/{day}` — the dashboard calls `/api/plan/rematch?apply=1`;
  the `{day}` variant is a different function, 104 LOC, and nothing calls it.
  (The *base* endpoint is live; the day variant is a real candidate.)
- `/api/course/{region}/{filename}/download` — LIVE, built dynamically in
  `dashboard.html` with `encodeURIComponent`, which the regex matcher missed.
- `/oauth/icu/callback` — 280 LOC, unreachable on a from-source install
  (no client secret) but part of the packaged build. KEEP.

The rest, with the lines exclusive to each (`reach.py`), total ~560 LOC:

| endpoint | LOC | notes |
|---|---|---|
| `/api/climb-zwo/{region}/{filename}` | 91 | climb/GPX cluster |
| `/api/climb-workout` | 83 | climb/GPX cluster |
| `/api/gpx/{region}/{filename}` | 77 | climb/GPX cluster |
| `/api/setup/test-icu` | 63 | also named by `packaging/tls_intercept_probe_win.py` |
| `/api/workout/download/{filename}` | 40 | UI uses `/api/download/zwo/...` instead |
| `/api/setup/icu-hr` | 32 | setup.html does not call it |
| `/api/ride/{id}/prs/recompute` | 32 | |
| `/api/rides/legacy-envelope` | 31 | |
| `/api/plan/missed-suggestions` | 27 | |
| `/api/blood-markers` GET+POST | 31 | no UI at all |
| `/api/routes/surfaces` | 13 | |
| `/api/gc/status` | 9 | |
| `/api/download/crs/{region}/{filename}` | 8 | climb/GPX cluster |
| `/api/download/gpx/{region}/{filename}` | 5 | climb/GPX cluster |
| `/api/metrics/latest` | 3 | |
| `/api/setup/status` | 2 | |

Deleting the climb/GPX cluster also retires `gpx_to_gc.py` (391 LOC) and its
test. `src/workout_analysis.csv` (428 KB) is named only in two comments that
say it no longer exists — an orphan.

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
