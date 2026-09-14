# Architecture audit — Domestique @ a95447e8, 2026-09-14

Six independent lenses, each an agent in its own scratch worktree briefed to verify
by running and to treat comments and commit messages as claims. Their reports are
kept verbatim in `audit-2026-09-14/` (readers, state, architecture, clarity,
performance, tests). This file ranks across them, names the root causes, and
orders the fix. The owner's request: "all cards displaying things related to the
week plan should pull data from the same object; same for the form cards";
audit the whole codebase for that class, and for clarity, performance,
architecture and separation of concerns.

## The five root causes

**R1. There is no single source of truth, for anything.** The plan is a dict with
no model invariants (architecture §4). Readers re-derive: `/api/weekly-plan`
runs a second planner per GET and its target is served by the home badge, the
rollup and the "Last week" card, which ignores its own week offset (readers §1,
HTTP-2 held: stored 190 vs served 418). A session's identity is split across
`session_type`, `zwo_file` and `zwo_name`; `match_zwo` picks from a fallback
class table (sweetspot→threshold, overunder→threshold) and never re-derives the
label, and the calendar's content check reads a key that no library row carries,
so it is dead (readers §3; reproduced through save-availability). The rider's
FTP has three owners and one freezes for the process after an accepted test
(state S-1, reproduced). CTL has five derivations, one of which reads the
FIT-only archive and is None for an ICU rider, so the calendar grades the rider
against a curve that starts at 37 (S-2). TSB comes from two stores on one page
(S-3). A ride can exist in one of five activity sources and not the others
(S-4). Availability has six representations and Generate reads a prefs file
migration moved away (S-5). Caches are keyed without the profile (S-7). The
frontend has no model: 19 unconditional fetches per home load, readiness
fetched three times, each card rendering its own response (S-8, architecture §7).

**R2. The layers are inverted.** Half of app.py's function lines are handler
bodies; 21 handlers read and write the plan directly; 20 reach into 13 private
planner functions; load math and planning decisions live in handlers
(architecture #2). The import graph is acyclic only because 234 lazy imports
hide seven two-way cycles, including the planner importing the HTTP module for
the ZWO scanner (#1). GET handlers write the plan and start sync threads (#5,
performance #6). There is no plan store: 24 `atomic_write_plan` sites, no
version check, a backup rotation that one Plan-tab open can spin four times (S-9).

**R3. Branching is stringly-typed state re-inspected everywhere, plus dead
alternatives.** Not elif ladders: about 2,800 nested `if` guards test
`session_type`, `goal_type`, phase names, ownership flags and unload status
against 10 hard-type sets, 9 goal-type sets, 23 phase-name sets and 39
ownership checks (clarity §2). Two identity fields have about 130 write sites.
Four type→band maps and four power-zone tables disagree (clarity, architecture
#6). Four planner flags leave about 400 dead lines, including the "one owner"
builder the plan of record is built around (clarity §3). Five entry points
re-implement the week walk in 2,448 lines sharing 124 verbatim lines (clarity
§5). Both monoliths score a maintainability index of zero; `reforecast` nests
eleven deep; app.py swallows 182 broad exceptions silently on plan paths.

**R4. Work is repeated per request.** The shared cache has no single-flight
lock, so a cold home load parses the archive four times concurrently and every
one of those requests waits over four seconds (performance #1). Five endpoints
bypass the cache and re-read every ride file (#3). The home page starts a full
streams backfill that rewrites hundreds of ride files and, on a network
failure, marks every ride deleted (#2). Four planner rebuilds per home load
(#6). The planner is O(weeks × library × candidates) and recomputes per-row
facts from 4,307 rows on every call (#5).

**R5. The tests cannot judge the defect the owner reports.** No test asserts
that two endpoints agree on the same week (tests #2). 130 tests read the real
clock while the product may read a pinned one (#1). The characterization gate
reports invariant violations but never fails on them, and 93 violations are
blessed into the golden (#5), while its self-test fails today on two entry
cases that cannot see a fault (#4). Seven "known failures" are a real product
bug: FIT export scales step durations twice, so a 60-minute step decodes as
1,000 hours (#3). One test file is 36% of the suite's time (#7).

## Corrections to the record

- `notes/overhaul-plan.md` Step 0 says the characterization self-test passes.
  It fails at a95447e8 (tests #4). Until it passes, the gate's entry-point
  coverage is overstated, and every "characterization unchanged" claim since
  the self-test broke, including today's bless, rests on a driver that cannot
  see two of its own cases.
- Today's bless (4fcdbcf0) accepted 9 new invariant hits as "Step 5b/6
  classes". The gate never fails on invariants, so that acceptance was mine,
  not the gate's. The golden now carries 93 blessed violations.
- `KNOWN_FAILURES.md` calls the seven FIT failures environmental. They are a
  double scale in `app.py:13272,13474` against fit_tool 0.9.16.

## What holds

Order-independence of the planner suites (tests #12); the load-flake was not
reproducible (#11); SQLite access is fine (performance); `atomic_write_plan`
is cheap; the ICU stub refusal in the archive; `STEPBACK_LOAD_FACTOR` is the
only executable 0.72; module-level import time is small.

## The fix, in waves (suite green at every boundary)

**Wave 0 — instruments, before anything else.**
1. One autouse conftest fixture that freezes `app.date`, `tp.date` and both
   `datetime`s (tests #1: kills the 130-test class).
2. The cross-endpoint agreement test: generate on a weekday, then assert
   `/api/plan`, `/api/weekly-plan`, `/api/week-summary` (both offsets),
   `/api/calendar`, `/api/today-session` and `/api/programme/summary` agree on
   planned, done, budget, boundary and today's label. Fails today. This is
   contract assertion A1.
3. The characterization compare fails on new invariant hits; the self-test's
   two blind cases are fixed; per-rule counts replace per-case lines.
4. The seven FIT failures leave the known list: fixed, or strict xfails
   naming the double scale.
5. A single-flight lock in `cache.cached` (performance #1: one line class of
   fix, four seconds off a cold load).

**Wave 1 — the week-view contract** (`notes/week-view-contract.md`, revised
below). One `week_view.py` builds planned, budget, actual, done, planned and
actual zone minutes, boundary and today from the stored plan, the served
files and one activity source; the six endpoints serialise it; the second
planner stops answering reads; `serve_file()` is the only writer of a
session's identity and derives the type from the served content; Generate
keeps elapsed weeks; one band map; one `store.js` in the dashboard with one
fetch per resource per load. Retires readers §1–§3, HTTP-2/3/7/12/13/14,
S-8, performance #6, and the `lastWeekActs` and Plan-tab client derivations.

**Wave 2 — one owner per fact** (state's table). `ProfileManager` is the only
FTP/LTHR/weight owner and the `config` proxy shadow is deleted (S-1); one
`fitness_state()` returning CTL/ATL/TSB with source and age, no 30/37/50
constants (S-2/S-3); `load_all_rides` is the one activity source with ICU
`recent_activities` used only to refresh it (S-4); one `availability_for()`
and the dead prefs read gone (S-5); caches keyed by profile and cleared on
write (S-7); `plan_store.load()/update(mutator)` with a version check and one
push hook, replacing 24 writers (S-9); no GET writes or starts threads (D8,
architecture #5).

**Wave 3 — structure** (architecture's migration order 1–8): `science.py`
(one table for zones, bands, thresholds; the dashboard reads it through one
endpoint), `model.py` (dataclasses with invariants and the one codec),
`library.py` (the scanner leaves app.py; breaks the planner→app cycle),
`plan_store.py`, `loads.py`; then the planner package, which is Step 6 with
its five entry points as compositions of one week walk; then handler bodies
into services and `app.py` split into `routes/*`; then the frontend split by
tab with no framework.

**Wave 4 — branching** (clarity's ten): delete the dead flags and branches
(~400 lines); `SessionType`/`GoalType`/`PhaseKind` enums and one
`SESSION_KIND` table for the ten hard-type sets; `session.is_owned` for 39
sites; one `parse_zwo_segments()` for seven cascades; the shared prologue of
regenerate/recalculate/extend; remove 30 uncalled functions; the top silent
excepts made loud.

**Wave 5 — the rest of performance** (#2, #3, #4, #5, #7, #9) and the test
suite's cost (tests' eight changes: shared library fixture in `test_357`,
memoised plans, one client fixture, timing tests out of the parallel run).

## Wave 0 — done on 2026-09-14, the same day

1. `freeze_clock` and the one clock: the product reads `src/clock.py` alone
   (218 sites moved, grep 0 outside it; 04da2976); the conftest mirrors the
   thirty-one module-attribute pins into it until they migrate; the
   characterization harness freezes it directly.
2. The agreement tests, `tests/test_week_view_agreement.py` (3eadfdf8):
   three strict expected failures on the owner's own record, each failing
   for its stated reason (stored 234 vs served 441; sweetspot vs threshold;
   441 invented for a week with no record), plus a control that passes.
3. The compare fails on new invariant hits, by rule (e37f71b8), and the
   self-test passes: the reforecast@21 driver now exercises the
   availability block. One `daily_duration_cap` from that, for Step 7.
4. The seven FIT failures fixed at the cause (fc5f1edb): 106 FIT-path tests
   pass; the known-failures list is eight lines.
5. `cache.cached` is single-flight per key (c49d7796): four threads on a
   cold key run the function once, where the old module ran it four times.

## Sequencing against the plan of record

This programme absorbs Steps 7, 8 and 9 and the "second pass" of
`notes/overhaul-plan.md`, and pulls them ahead of Steps 5b and 6; Step 6 lands
inside Wave 3 as the planner package. Decisions D1–D8 stand. The owner chooses
whether Wave 1 starts before the deployment test week ends.

## Not verified across lenses

Runtime behaviour under two workers or two profiles (static only); the
owner's live profile and plan were not read by any lens; the frontend was
rendered only by the performance lens; production ICU latency; whether fit_tool
0.9.13 scaled differently.
