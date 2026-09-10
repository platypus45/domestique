# Backend decomposition plan

Working notes for `refactor/backend-architecture`. Written as the analysis
lands so the branch is reviewable even if the work stops partway.

Scope: backend only. `src/*.py`. Nothing under `src/templates/`.

## Where this started

`src/app.py` is 22,201 lines and `src/training_planner.py` 14,051. The concrete
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
  - **The planner was not reproducible across processes.** 43 of 57 cases
    differed run to run until `PYTHONHASHSEED` was pinned, so two identical
    regenerations handed the rider different workouts.

    My first diagnosis here was wrong and is worth leaving on the record: I
    wrote that the HIT candidate list "arrives in set-iteration order". It does
    not — it is a literal list filtered by a list membership test, fully
    ordered. The actual cause was one component of the shuffle seed,
    `abs(hash(phase.name))`: Python randomises `str` hashing per process
    (PEP 456), so the seed changed on every restart. `zlib.crc32` instead.
    Measured with `tests/probe_plan_reproducibility.py`, which runs the matrix
    in separate child processes with no pin: **43 of 57 differing → 0 of 57**.
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

Pre-existing and left alone, flagged rather than fixed: `app.py:1296` declares
`global WORKOUT_DIR, GPX_DIR` in a function that assigns neither. A review
attributed this to the accessor commit on this branch; it is on `clean-main`
too (line 1303 there), so it is not ours to clean up.

The last two are behaviour questions, not moves. They get their own commits
*after* the extraction, so the characterization harness can tell a relocation
from a change.

## Order of work

Dependency-ordered. Each step is independently revertable and gated on: upstream
pytest green, the nine stack suites green, and the characterization harness
clean.

## Status

**Substrate done, first feature extractions done.** `app.py` 22,201 → 20,936.
Every module below imports standalone without pulling in `fastapi`, `app` or
`training_planner` (`download_lib` imports fastapi on purpose — building the
response is what it does). That property, not the line count, is what makes the
next extraction possible.

| module | lines | holds |
|---|---|---|
| `obs.py` | 95 | loggers, the diagnostic ring, `_log_error` |
| `cache.py` | 101 | `cached`, `clear_cache`, the clearer registry |
| `paths.py` | 71 | `DATA_DIR`, `COURSE_DIR`, `ROUTE_*`, `_plan_dir`, `_safe_path`, `_rides_fit_dir` |
| `http_util.py` | 46 | `_get_json_body`, `_icu_verify`, `_diag_local_only` |
| `hr.py` | 48 | `_prescription_hr_rows` and the target-mode gate — the single HR resolver |
| `routes_lib.py` | 808 | the virtual-route library: load, index, filter, score, profile, climb .zwo |
| `search_lib.py` | 394 | the `q=` grammar: synonyms, typos, durations, matcher, ranker |
| `download_lib.py` | 125 | capacity cap, outdoor wrapper, the shared `.zwo` response |

### The rule mutable state taught us

`routes_lib` and `search_lib` own module-level caches, and app.py does **not**
re-export them. `from routes_lib import _ROUTES_CACHE` binds the list object, so
rebinding `app.X` later leaves the module reading its own — silently.

Not theoretical: moving the routes caches without retargeting the monkeypatches
broke exactly three tests in `test_route_picker_api.py`. Functions are safe to
re-export (60 test files reach them through `app.X`); mutable state never is,
and `tests/test_routes_lib_boundary.py` fails on any attempt.

### Still blocked: the workout-library and FIT-export helpers

The remaining ~725 lines of that chunk (`_build_library_rows` and friends, both
FIT builders) all call `active_workout_dir()`, which reads `app.WORKOUT_DIR` —
a module global rebound on every profile switch, patched by 10 test sites across
20 files.

Worse, there are **two** of them: `app.WORKOUT_DIR` (rebound by
`app._apply_profile_paths`) and `training_planner.WORKOUT_DIR` (rebound by
`profile_manager._retarget_training_planner`). Two globals, two writers, one
concept. Unifying them is the real fix and it is a behaviour change, not a
relocation — it gets its own commit, not this one.

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
7. **Monday week anchoring** — DONE, on `refactor/monday-anchoring`.

   `tests/probe_week_anchors.py` measured the condition before anything was
   touched: **87 of 91 emitted week starts were not a Monday**, while every
   rollup in the app (week tile, adherence counter, TSS budget, ramp check)
   aggregates Mon–Sun. Now 0 after the opening week.

   The plan still starts today. Opening on the next Monday instead was two
   lines and made every week whole — and left a rider generating on a Thursday
   with nothing to ride until Monday, and on the regen path with the current
   week gone from the calendar. So the first week is short (today→Sunday) and
   every later week is Mon–Sun.

   Three things it needed that were not obvious:

   - The stub week **counts** as one of its phase's weeks rather than adding to
     it, or the continuous goal's "4-week rolling horizon" becomes five rows.
     One owner now, `_phase_end_for_weeks`; three sites derived it
     independently and the continuous one was missed first time round.
   - `_clip_week_to_phase` only ever clipped the phase *end*. A week that can
     open mid-week needs its own Sunday as a second ceiling, or the opening row
     spills into the next week and double-books those days.
   - **The taper is exempt.** It is laid backward from a fixed date, so the
     Monday grid leaves the race week only the days between the last Monday and
     the event — measured, that cut a race week from three rest days to zero.

   Two test findings worth keeping: the phase-week sum may now read 21 for a
   20-week runway, and **21 is the honest number** (on main `peak` reported 2
   while emitting 3); and `test_more_elapsed_weeks_ramps_higher` was passing on
   main by luck — the long-ride series is non-monotone on both branches because
   the test reads `duration_min` *after* `match_zwo` restamps a slot down to
   whatever the library holds.

8. **Reproducible regeneration** — DONE, and the fix was not the one this plan
   predicted (see the correction above): `zlib.crc32(phase.name)` in place of
   the builtin `hash`. 43 of 57 → 0 of 57 across separate processes.

   **It also produced the sharpest evidence yet for item 6.** Sampling eight
   hash seeds on the OLD code, the same week came out at five different loads:

   | case | target | totals across 8 process seeds |
   |---|---|---|
   | `every-day/1h/target150` | 150 | 139, 143, 150, 154, 158 — over target on 4 of 8 |
   | `every-day/1h/target272` | 272 | 252, 256, 260, 275, 279 |

   A rider on 1h/day was getting anywhere from 7% under to 5% over, decided by
   which process served the request. Determinism fixes the *variance*; it does
   not fix the *bias*, and the seed it settles on lands that first case at 166,
   10.7% over target, every time. Cause: HIT variants carry different TSS for
   the same duration (`threshold` 45min = 68 TSS, `sweetspot` 45min = 60), the
   z2 fill is sized from the residual BEFORE the variant is drawn, and when the
   daily-hours cap pins z2 it cannot absorb the difference. That is precisely
   what `size_session` as single owner has to fix, and it is now measurable
   rather than anecdotal.
9. **Planning domain extraction** — only after 1 and 2.

Not scheduled: the ride chunk (needs `_maybe_auto_reforecast` inverted into a
hook first) and the profile chunk (entangled with the whole file by design).

## How the weekly TSS budget is actually established

Traced end to end, because the answer was spread over four files and two
tables that disagree with each other.

```
current_ctl                        ICU, or estimated from ride history
  → safe_ramp_rate = clamp(5 × ctl/80, 3, 7)                    Couzens
  → target_ctl     = EVENT_CTL_TARGETS[event] × (0.94 + 0.12·difficulty)
  → max_achievable = ctl + ramp × (weeks − elapsed − 2)         feasibility cap
  → target         = min(target_ctl, max_achievable)
peak_weekly_tss    = target × 7
  → capped by  recent_weekly_tss × 1.3        Gabbett 2016 ACWR   ← with history
     or, with no history,  hours_per_week × 65                   ← flat rate
phase targets:  base   = ctl × 7 × 1.05  (clamped ≤ build1)
                build1 = peak × 0.70
                build2 = peak × 0.85
                peak   = peak × 1.00
                taper  = peak × 0.60
  → every 4th week × 0.72                     Issurin stepback
PlannedWeek.tss_target
  → plan_week sizes sessions to it
  → sample_week_workouts REPLACES them
```

That last step was where it came apart. The sampler verified its week against
`BUDGETS[phase].tss_per_week` — a **table constant**, 425/600/650 — not against
the athlete's own `PlannedWeek.tss_target`. A rider on a 287 TSS week had it
checked against 600, so the check could never fire.

### The table is authored for a 10 h/week rider

`BUDGETS` (`training_planner.py:1879`) states the Seiler distribution as
absolute minutes. Measured hard-minute allowance as a share of a rider's week:

| phase | hard min | 4 h/wk | 6 h/wk | 8 h/wk | 10 h/wk | 12 h/wk |
|---|---|---|---|---|---|---|
| base | 60 | 25% | 17% | 12% | 10% | 8% |
| build1 | 225 | **94%** | 62% | 47% | 38% | 31% |
| peak | 250 | **100%** | 69% | 52% | 42% | 35% |

So below about ten hours there was no intensity ceiling at all. And the rows
contradict their own `polarized_target`: build1's minutes are 65/19/16 against
a stated 78/6/16 — 13 points of extra grey zone, which is the one thing a
polarized model exists to avoid.

**The science in that table is the RATIO, not the minutes.** Seiler's
distribution is a share of training *time* and is scale-free. So
`PHASE_POLARIZED_TARGETS` is now the single source of the distribution and
`scale_budget_to_week` derives the week's minutes from the athlete's own
target, pricing each band from `TSS_PER_HOUR` so the two tables cannot drift.

### A hard slot has to be hard

`hit_count_min/max` counts *sessions*. Seiler and Rønnestad prescribe *time at
intensity* — Rønnestad's 3×13×30/15 is ~19.5 min at VO2max, Seiler's 4×8min
~32 min at threshold. Nothing checked that a workout admitted to a HIT slot
delivered any.

Measured on a peak week for a 2 h/day rider: the three HIT slots were a 41-min
neuromuscular file carrying **1.7 minutes** above Z2, a 49-min intervals file
on a *recovery* slot carrying none, and one real VO2 session — 41 hard minutes,
against build1's 72 in the same plan. **The peaking phase came out easier than
the build phase it exists to sharpen**, and peak missed its 24% Z4+ target by
11–18 points at every volume sampled.

`_hit_slot_hard_floor` makes it a slot contract, the pattern already used for
`_SPRINT_SLOT_IF_CEILING` and `_EASY_SLOT_IF_CEILING`: a candidate must carry at
least half of what the slot owes (remaining hard budget ÷ remaining hard slots),
scaled by the clamp it will actually get. An empty gated pool falls back rather
than emitting no session — a library gap must not make a week unplannable.

**It costs library coverage, measured rather than assumed**: 895 → 867 of 2,643
candidate files reached across 30 regenerations, 33.9% → 32.8%. Those 28 files
are not lost, they are no longer served *where they do not belong* — every one
stays reachable on an endurance slot. 3.1% relative, against a test written to
catch >10%, so `test_population_coverage_across_regenerations`'s floor moves to
0.32 with the measurement recorded rather than the sampler being loosened.

**Five sampler entry points, not four.** `generate_plan`,
`regenerate_from_today`, `recalculate_plan`, `extend_continuous_plan` and
`refit_remaining_week` each build a budget and call the sampler. Missing the
fifth left the refit sampling against the 10 h/week table while every other
path used the athlete's week, and the hard floor derived from that oversized
budget was high enough that a freed missed slot could not be re-owed —
`test_v207_missed_hard_refit` caught it at 49 of 60 seeds against a 55
threshold. That the count of these sites had to be discovered by a failing test
is itself the argument for the wider `size_session` work.

### What it moved

`tests/probe_budget_fidelity.py`, 20 full weeks across five athlete volumes,
scoring **time in zone** read from each matched workout's own profile:

| | before | after |
|---|---|---|
| mean \|TSS miss\| | 17.0% | 16.9% |
| mean easy-share gap | +3.4 pts | −1.3 pts |
| mean hard-share gap | −5.3 pts | −1.9 pts |
| weeks >8 pts short of hard | 7 of 20 | **2 of 20** |
| weeks >10 pts off easy | 1 of 20 | 4 of 20 |

The intensity deficit is largely closed and the easy share is now centred
rather than uniformly too easy. One metric got worse: the easy share scatters
more, four weeks landing >10 points off in either direction (base too easy at
1.5–3 h/day, build2 too hard). That is honest variance, not a fix.

### Two things the instrument taught me the hard way

- **Scoring session labels is not scoring zones.** The first version of the
  probe attributed a whole session to the band of its `session_type` and
  reported build weeks at "49% hard". A 60-minute VO2max session is a warm-up,
  some intervals, the recoveries between them and a cool-down — most of its
  minutes are Z1/Z2. The corrected probe reads each matched workout's own
  Z1%..Z6% and the same weeks are 12–21% hard.
- **The opening week is not a week.** Scoring the short Monday-anchored opening
  week against a seven-day phase shape reported a 56% "miss" that was nothing
  but the week being four days long.

### Still open

`hours_per_week × 65` (`:2603`, `:2723`) is the no-history fallback cap. 65
TSS/h implies riding at IF ≈ 0.8 all week; the phase model asks for 72–88% easy,
which yields 48–55 TSS/h. `scale_budget_to_week` already computes the
phase-correct rate — the cap should use it. Left alone here because it only
binds for a rider with no ride history, and it deserves its own before/after.

## Hard rules for this branch

- `app.py` must re-export anything moved: 60 test files reach 67 planning names
  and 21 reach profile internals via `app.X`.
- Never `from app import WORKOUT_DIR`. All 24 reads are runtime module-global
  reads and a `from`-import snapshots the boot value, silently breaking profile
  switching with no error.
- Do not touch the live checkout, the running service, or plan data. This is a
  worktree for that reason.
