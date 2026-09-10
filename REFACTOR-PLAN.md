# Backend decomposition plan

Working notes for `refactor/backend-architecture`. Written as the analysis
lands so the branch is reviewable even if the work stops partway.

Scope: backend only. `src/*.py`. Nothing under `src/templates/`.

## Where this started

`src/app.py` is 22,273 lines and `src/training_planner.py` 14,051. The concrete
trigger was that the weekly TSS budget is re-derived independently in four
places, so a plan prescribed 2.5–3.7× its own target and an unload week came out
heavier than the load weeks it was recovering from. Fixing it site by site did
not hold: three were patched and a fourth (the sampler,
`training_planner.py:11218`) still overwrote the result.

That is the argument for structure rather than more clamps.

## Instruments built first

- `tests/characterize_planner.py` — 57 fingerprints across both week builders,
  covering goals, availabilities, targets, stepback, completed load and week
  position. Verified as an instrument: mutating `TSS_PER_HOUR["z2"]` 45→46 is
  caught, restoring it goes clean. `--bless` exists for intentional changes; a
  refactor that needs it is not a refactor.
- Two facts that fell out of building it:
  - **The planner is not reproducible across processes.** The HIT shuffle is
    seeded (`training_planner.py:3496`) but the candidate list arrives in
    set-iteration order, so 43 of 57 cases differed run to run until
    `PYTHONHASHSEED` was pinned. Two identical regenerations give different
    plans. Fix: sort candidates before seeding.
  - **Stale `.pyc` files silently serve old code.** A restored file reported 39
    changed cases. The harness now sets `PYTHONDONTWRITEBYTECODE`.

## What the coupling analysis found

Four read-only reviews, each required to cite `file:line`. Verdicts:

| Chunk | Lines | Verdict |
|---|---|---|
| Workout library + courses + routes + downloads + FIT export | 3,853 | **EXTRACTABLE-AFTER** — 3 names / 10 call sites outbound, 0 escaping mutable state, 0 threads |
| Planning domain | 7,706 | **EXTRACTABLE-AFTER** — 6 names / 10 call sites inbound, no `global`s, no threading primitives in the chunk |
| Ride import + history | ~3,730 | **ENTANGLED** — two-way cycle with the planner; ride history lives in four places |
| Profile + settings + setup | 4,235 | **ENTANGLED** — owns the app-wide cache, `lifespan` wiring, 6 threads, profile switching |

**All three of the completed reviews independently named the same first
blocker: the cache.** `_cache` / `_cache_ts` / `cached` / `clear_cache`
(`app.py:1603–1679`) live inside the setup-wizard section, are read by 32 call
sites outside every candidate chunk, and are mutated *directly* from outside the
API at `app.py:4569` and `:4604`. `clear_cache()` is a global flush: the ride
code calls it 6× and owns one key, destroying `"training"`, `"wellness_7"`,
`"sleep"` and the rest.

### Corrections to my own briefs

Both reviews pushed back on the section boundaries I gave them, correctly:

- The ride chunk I described as two sections is five. Two of them are not ride
  ingest at all — `IMPL-HRV-PROMPT` (202 lines) is wellness UI, and
  `PROGRAMME-SUMMARY` (534 lines) takes a *plan* and belongs to the planning
  domain.
- `ROLLING PLAN AUTO-RECALCULATION` does not end where its next banner suggests.
  Splitting at 15868 would cut a bidirectional seam: 8 of 14 outbound sites
  cross it and `_enrich_plan_for_response` (`:16099`) is called 3× from above.

### A claim that did not survive checking

The ride review reported a live data-loss hazard: four raw writes into the ICU
rides directory (`app.py:20207, 20257, 20293, 21416`) said to bypass
`persist_icu_activity`'s carry-forward of DFA keys, rider input and PRs.

Checked directly: all four are **read-modify-write of the loaded record** — the
existing JSON is read, one field is set, and it is written back. Every other key
survives. Carry-forward matters when a *fresh* record replaces an old one, which
is not what these do. The structural criticism stands (four sites write into a
directory another module owns, and the FIT parser exists three times), but it is
a design problem, not data loss.

## The substrate review overturned the ordering

The fourth review asked a different question — not "can this chunk move" but
"what does the whole file sit on" — and its answer changes the plan:

> A split cannot start with a feature area.

**755 cross-section references across 89 edges**, and **ten section pairs that
depend on each other in both directions**. Cutting anywhere along a banner today
produces an import cycle. Every one of the fifteen heaviest edges points into
the prelude, and their substance is a short repeating list: `_plan_dir`,
`_get_json_body`, `_log_error`, `_log`, `cached`, `clear_cache`, `WORKOUT_DIR`,
`DATA_DIR`, `app`.

Three of its sharpest claims, verified directly rather than taken on trust:

- **`app.PLAN_DIR` (`:498`) is a dead alias.** `_plan_dir()` (`:171`) reads
  `tp.PLAN_DIR` dynamically; the module constant never updates. Any module that
  imports it gets a value that is wrong after the first profile switch. Delete
  it rather than move it.
- **`_cache` / `_cache_ts` have no lock at all** and are mutated from four
  sections. The single-worker assertion at `:22263` is standing in for
  synchronisation.
- **`_dfa_backfill_lock` is acquired at `:2495` and released at `:2475`** — in a
  different function, on a different thread.

The last two are behaviour questions, not moves. They get their own commits
*after* the extraction, so the characterization harness can tell a relocation
from a change.

## Order of work

Dependency-ordered. Each step is independently revertable and gated on: upstream
pytest green, the nine stack suites green, and the characterization harness
clean.

## Status

**Substrate is done.** `app.py` 22,273 → 22,134; four leaf modules out, each of
which imports standalone without pulling in `fastapi`, `app` or
`training_planner` — that property, not the line count, is what makes a feature
extraction possible next.

| module | lines | holds |
|---|---|---|
| `obs.py` | 95 | loggers, the diagnostic ring, `_log_error` |
| `cache.py` | 101 | `cached`, `clear_cache`, the clearer registry |
| `paths.py` | 71 | `DATA_DIR`, `COURSE_DIR`, `ROUTE_*`, `_plan_dir`, `_safe_path`, `_rides_fit_dir` |
| `http_util.py` | 46 | `_get_json_body`, `_icu_verify`, `_diag_local_only` |

Three behaviour changes came with it, each its own commit and each with a
failing-first test:

- `cached()` returns defensive copies. It used to hand every caller the object
  it was holding, and one caller mutated it — app.py's readiness endpoint wrote
  three keys straight into the cached dict. Shallow, not deep, because the ride
  archive behind `"all_rides"` is 17 MB read 22 times per request; that limit is
  pinned by a test asserting nested values *are* shared.
- The dead `PLAN_DIR` alias is gone.
- Workout and GPX directories are read through `active_workout_dir()` /
  `active_gpx_dir()`, with a test that scans `src/` for
  `from ... import WORKOUT_DIR` and fails on any hit.

**What the gate caught, which is the case for having one.** The accessor change
broke 9 tests in `test_calendar_push_workout.py` via a name collision
(`workout_dir = Path(workout_dir())` is an `UnboundLocalError`), and only a run
against `clean-main` — which passes all 20 — showed they were regressions rather
than inherited. Separately, one careless test of mine built the `ProfileManager`
singleton in the shared sandbox and cost 22 unrelated failures.

**Substrate first — no endpoint moves until all four are done.** Each is a pure
relocation: the harness must stay clean, and `app.py` re-exports every moved
name so the 60 test files that reach `app.X` keep working.

1. **`obs.py`** — `_log_error` (`:77`), `_DIAG_RING` + its lock (`:73-74`),
   `_diag_ring_snapshot` (`:124`), the loggers (`:56-63`). Smallest, fewest
   edges; proves the extraction pattern and the gate before anything riskier.
2. **`cache.py`** — `cached` (`:1639`), `clear_cache` (`:1670`), `_cache` /
   `_cache_ts` (`:1603-1604`), the fatigue-resistance lock map (`:1610-1620`).
   Relocation only. The missing lock and per-key invalidation follow as separate
   commits, because they change behaviour and should be visible as such.
3. **`paths.py`** — `_plan_dir` (`:171`), `_safe_path` (`:8350`),
   `_rides_fit_dir` (`:18760`), `DATA_DIR`, `COURSE_DIR`, `ROUTE_*`, and
   **accessors `workout_dir()` / `gpx_dir()`** replacing the rebound globals.
   Delete `PLAN_DIR` (`:498`) outright.
4. **`http_util.py`** — `_get_json_body` (`:813`, used from 12 sections),
   `_icu_verify` (`:47`), `_diag_local_only` (`:22026`).

Only then:

5. **Extract the workout library + routes chunk** — the best first feature
   extraction on every measure: smallest interface, no escaping state, no
   threads, 3 test files touching internals.
6. **`size_session` as the single owner of session sizing** — the four known
   layers migrate to it. Note the blast radius is wider than those four:
   `TYPE_CEILING`, `TSS_PER_HOUR`, `_INTENSITY_LADDER`, `_deescalated_load`,
   `apply_week_tier_down` and others are used from `app.py` outside the planning
   chunk.
7. **Monday week anchoring** — `regenerate_from_today` builds weeks starting on
   whatever weekday it runs, so a Wednesday regenerate produces Wed–Tue weeks
   while every rollup aggregates Mon–Sun. Requested explicitly.
8. **Sorted candidates before seeding** — makes regeneration reproducible.
9. **Planning domain extraction** — only after 1 and 2.

Not scheduled: the ride chunk (needs `_maybe_auto_reforecast` inverted into a
hook first) and the profile chunk (entangled with the whole file by design).

## Hard rules for this branch

- `app.py` must re-export anything moved: 60 test files reach 67 planning names
  and 21 reach profile internals via `app.X`.
- Never `from app import WORKOUT_DIR`. All 24 reads are runtime module-global
  reads and a `from`-import snapshots the boot value, silently breaking profile
  switching with no error.
- Do not touch the live checkout, the running service, or plan data. This is a
  worktree for that reason.
