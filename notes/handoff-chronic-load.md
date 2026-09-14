# Handoff — the load the rider carries (Step 5 part 3, Option 2)

2026-09-13, evening. Branch `refactor/backend-architecture`, tip **743c1eda**.

> **Superseded, 2026-09-14.** The work below was committed as ba872ebc that
> same evening, and this note with it. The next session answered §7 (the
> gate's own riders supply a load above CTL × 7; the old floor clipped it),
> ran the ultrareview, and the owner chose option D. The record is in
> `notes/overhaul-plan.md`, "The load the rider carries". What follows is
> kept as written.

**Nothing is committed. Nothing is deployed.** The work sits in the working
tree of `~/Documents/domestique-refactor`.

Read `notes/overhaul-plan.md` first for the plan of record; this is one
session's state on the piece the owner chose as "Option 2", plus a discovery
that turned out to matter more than the change itself.

---

## 1. The decision that is open

The owner was asked how to proceed and **did not answer** — they asked to
clarify first, then asked for this document instead. Do not assume a choice.

The three options put to them, verbatim in substance:

| | what it means |
|---|---|
| **A. Ship it, xfail the 6h rail** | Deploy the chronic fix now; record the two breaching 6h cases as strict xfails naming Step 6, with the seed sweep as evidence the rail already breaches at HEAD. Owner starts Monday at ~366 TSS. |
| **B. Hold until the rail is enforced** | No deploy until Step 6 makes the intensity budget a real constraint. Owner's week stays at 276 TSS and the planner keeps reading their load as 228. |
| **C. Enforce the rail now, then ship** | Make a hard slot refuse a file whose VO2 minutes blow the week's z5plus budget, inside the shared matcher so downstream rewrites cannot undo it. Relitigates a recorded decision; tonight's deploy slips. |

Two things the owner raised that were **not** answered, and a fourth option
that was never put to them:

- **Is the 6h rider real?** The failing fixture is
  `current_ctl=50.0, recent_weekly_tss=450.0` against a 6 h/week shape —
  450 TSS in 360 minutes is 75 TSS/h sustained, IF ≈ 0.87 every hour of every
  week. No such rider exists. Under the old floor `min(450, 350)` this was
  masked. **Option D: fix the fixture instead** (pair 6h with a coherent
  load). This is arguably right, and arguably the cardinal sin of editing a
  safety test to go green. It needs the owner's judgement, not a session's.
- **Is 366 TSS what they want on Monday?** They said they would happily ride
  "3H Z2 rides everyday" but know it "is not the most efficient way to
  train". The budget is TSS; **how it is spent is a separate question that
  has never been answered.** Whether the extra headroom becomes easy volume
  or intensity is exactly their stated number-one constraint.

---

## 2. The finding that matters most

**The planner has never seen the owner's rides.**

`athlete_weekly_load` asked `ride_storage.recent_mean_weekly_tss()`, which
reads `list_rides()` — the **FIT import archive only**. The owner's rides
arrive from intervals.icu and live in `rides/icu/` as normalized records.

```
~/.domestique/profiles/default/rides/      -> contains only the icu/ subdir
~/.domestique/profiles/default/rides/icu/  -> 36 records
list_rides()                               -> 0
recent_mean_weekly_tss()                   -> None
```

So every plan the owner has ever been given fell back to `round(CTL * 7)`.
Their CTL is 32.6, so the planner has been ramping from **228 TSS a week**
while they actually carry **281**. The `min(chronic, ctl*7)` floor inside
`LoadRamp.__init__` — the thing Option 2 was about removing — was never even
the binding constraint in production.

Archive spans 2026-06-09 → 2026-09-09, 32 ride-days. Recent weeks:

```
07-20: 196   07-27: 170   08-03: 126   08-10: 307
08-17:  94   08-24: 328   08-31: 215   09-07: 369
```

---

## 3. What changed (uncommitted)

```
 src/app.py                      |  4 +--
 src/ride_storage.py             | 79 +++++++++++++++
 src/training_planner.py         | 34 ++++++-----
 tests/test_333_l4_ux.py         |  2 +-
 tests/test_drift_chip.py        |  2 +-
 tests/test_event_fixes_w2.py    |  4 +--
 tests/test_one_athlete_state.py |  2 +-
 tests/test_week_budget.py       | 18 +++++-
 tests/test_chronic_load.py      | NEW
```

**`ride_storage.chronic_weekly_tss(rides=None, today=None)`** — new. A 28-day
EWMA of the rider's load in TSS a week, over `load_all_rides()` (ICU records
*and* FIT imports), on the convention the ramp and the auditor already share
(`plan_invariants.chronic_after`). Returns `None` — caller keeps its CTL × 7
fallback — when the archive holds no TSS, when it spans less than
`_CHRONIC_SETTLE_DAYS` (56: an EWMA seeded at zero needs ~8 weeks to be
within 2%), or when nothing was ridden inside `_CHRONIC_WINDOW_DAYS` (28: a
rider months off is described by their CTL, not by an EWMA decayed to
nothing).

It walks **whole weeks back from today**, not day by day. Both defects that
forced this were caught by the new tests' own controls, not by reasoning:

- day-by-day, a Mon/Wed/Fri rider at 300 TSS reads **278 on a Sunday** — the
  trough of its own within-week sawtooth. The answer would depend on which
  weekday the rider regenerated. The live archive reads **255.9** day-by-day
  and **281.2** by weeks; 281.2 is the number to trust.
- one record with an unparseable date became the archive's `max()` and voided
  the whole thing. Dates are now parsed when the per-day map is built.

**`LoadRamp.__init__`** — the floor is gone:

```python
start = float(chronic or 0.0) or self.ctl * 7     # was min(chronic, ctl*7)
```

The floor existed because the recent mean averaged only weeks with a ride and
so could not decay through a lay-off (Step 5 review, L1). `chronic_weekly_tss`
counts a day off as a zero, so the decay is in the number itself.

**`athlete_weekly_load`** now asks `chronic_weekly_tss()` instead of
`recent_mean_weekly_tss()`. **`app.py`** (2 sites) likewise, passing its
cached `_load_all_rides_safe()` so the archive is not re-parsed. One of those
sites also feeds `plan_ctl_snapshot` (the drift chip), which deliberately
shares the rebuild's number — they still share it.

Four test files had patches pointing at `recent_mean_weekly_tss`, which would
have become **silent no-ops** testing a CTL × 7 rider instead. Re-pointed.
`test_event_fixes_w2`'s was `lambda: 650.0`, which would now raise — app.py
passes the ride list positionally. `recent_mean_weekly_tss` itself is left in
place (still tested directly by `test_hrtss_ingestion`).

---

## 4. What was measured

Reproduce with `.venv/bin/python`, `sys.path.insert(0, "src")`.

**The owner, live archive:**

```
chronic_weekly_tss()      281.2 TSS/week     (CTL*7 = 228)
first build week          296/297  ->  366
danger line (1.5x)        422
six weeks, build1     [366, 409, 457, 329(unload), 478, 535]
```

Every budget is 1.3 × the load carried into it by construction.
`test_no_rider_crosses_the_danger_line[33-256]` **passes** — the pairing the
owner asked for holds on their own numbers.

**The intensity rail, swept over 8 seeds × 3 models × 288 weeks:**

| shape | chronic | breaches / 288 | worst z5+ share |
|---|---|---|---|
| 6h | old 350 | **1** | 20.8% |
| 6h | new 450 | **4** | 22.1% |
| 12h | old 350 | 0 | 16.4% |
| 12h | new 450 | 0 | **14.7%** |

The rail already breaches at HEAD (`s2/polarized/build1wk6: 21%` breaches
*identically* under both). The change re-rolls a sampling lottery; it does not
introduce the failure. **12h riders — the owner — strictly improve.**

---

## 5. The real blocker, and why it is not this change's to fix

`tests/test_tid_plan_properties.py::test_no_week_breaches_the_intensity_ceiling`
is **8 passed at HEAD, 1 failed with the change**, on seed 3:

```
('6h','pyramidal') peak wk13:   22.9% z5+
('6h','threshold') build2 wk10: 18.4% z5+      (rail: 18.0%)
```

The rail measures `_bands(w)[2]` = **z5+z6**, minutes above 106% FTP.

Measured on the two breaching weeks — and this is the important part:

```
pyramidal wk13 peak: budget z5plus = 20 min, SERVED 46 min of 215 = 21.5%
threshold wk10 build2: budget z5plus =  8 min, SERVED 58 min of 265 = 22.1%
```

**The budget is already correct and tiny. The builder does not honour it** —
seven times over in one case. Three VO2 files landed in a 265-minute week
whose z5plus budget was 8 minutes.

The reason is two switches, both off deliberately:

- `_USE_WEEK_SOLVER = False`
- `_REPAIR_MAX_MOVES = 0` — so `_repair_week`'s loop never executes at all

The comment above them is a recorded measurement and must not be
relitigated without new evidence:

> Both week optimisers are OFF, and the deciding measurement is that the
> UNOPTIMISED sampler passes every safety rail while both optimisers breach
> one. […] the week changes after the optimiser sees it — match_zwo, the R4a
> coherence pass and clamp-then-rematch all still run downstream. Optimising
> an artifact that is subsequently rewritten is the real problem, and no
> amount of tuning either optimiser addresses it.

with a 20-week table (greedy: 3 of 20 weeks off, BREACH; solver: BREACH).
scipy 1.18.1 is installed and `week_solver` imports fine — availability is not
the constraint. `notes/overhaul-plan.md:805` already schedules `hard_share`,
8 cases, for **Step 6**.

So: nothing enforces the intensity budget at build time, for any rider, today.
That is the owner's stated number-one constraint, and it is a Step 6 thread.

---

## 6. Test status, as it stands

| suite | result |
|---|---|
| `test_chronic_load.py` + `test_week_budget.py` | **36 passed, 4 xfailed** |
| `test_drift_chip` + `test_333_l4_ux` + `test_event_fixes_w2` | **66 passed** |
| 9 planner-adjacent suites | **119 passed, 1 xfailed, 1 FAILED** (the tid rail) |
| `test_tid_plan_properties` at HEAD (src reverted) | **8 passed** |

New in `test_week_budget.py`: `test_the_ramp_starts_from_the_load_the_rider_carries`
(333 / 297 / 234 — restoring the floor fails the first line, removing the
ceiling fails the last), and `(33, 256)` added to both ACWR sweeps. It is a
**strict xfail in the sweet-spot sweep** (week 6 reads 1.45×, the same Step 6
class as the existing 300 and 490 rows) and **passes the danger-line sweep**.

`tests/test_chronic_load.py` is new: 8 tests including three malformed-record
controls and three weekday parameters.

**Not run against the branch:** the full gate. A clean-main baseline from the
same day is ready and must be compared against, since several suites are
date-anchored:

```
/tmp/claude-1000/-home-aladjidi-Documents/bb02fc0a-851f-4ace-bcf4-5fc93d19911a/scratchpad/gate/cleanmain-0913.*
15 failed, 3357 passed, 9 skipped, 15 xfailed, 12 xpassed  (663 s)
```

All 15 are environmental (`test_download_pywebview_bridge`, `test_fit_hr_mode`,
`test_ftp_test_freeride`, `test_tls_trust`); none planner. `.alone.txt` empty.
Run the branch with
`scratchpad/gate.sh <worktree> <label> cleanmain-0913`.

**Characterization: moved, NOT blessed.**

```
53+ fingerprint changes; invariants: 8 new, 1 resolved
cont/every-day/3h/extend@30:   new [hard_share:5, stepback_lightest:8, weekly_volume:8]
cont/three-day/3h/generate:    new [under_delivery:2]
cont/three-day/3h/refit@3:     new [under_delivery:2]
event/wkend3h/refit@3:         new [under_delivery:1]
event/wkend5h/recalculate@21:  new [hard_share:13]
event/wkend5h/regenerate@17:   new [under_delivery:13]
cont/every-day/3h/refit@3:     resolved [hard_share:1]
```

Example: `cont/every-day/3h/generate` week 0 head `410.0 -> 416.0`.

---

## 7. Open question the next session should answer first

**Why did the characterization move at all?**

It is *not* index drift (two runs, one with a clean
`src/workouts/.library_index.json`, are byte-identical) and it is *not* the
new archive read leaking live data. That was checked empirically:

```
DOMESTIQUE_HOME=$(mktemp -d) -> load_all_rides 0, chronic_weekly_tss None,
                                athlete_weekly_load(40.0) = 280 = CTL*7
```

`tests/_gate_env.py` sets `DOMESTIQUE_HOME`, `user_home.domestique_home()`
honours it, and `profile_manager` resolves off it. So the harness is hermetic
and the new function is inert inside it.

Which means the movement comes from **the floor removal acting on a chronic
that some caller supplies explicitly, above CTL × 7** — and that caller has
not been identified. `athlete_weekly_load` is the single derivation point and
returns CTL × 7 under an empty archive, so something upstream passes its own
number. Candidates: `_continuous_weekly_tss` / `_continuous_phases`
(`training_planner.py:3253`), and the `recalculate` path at ~13806 which takes
`recent_activities`. **Do not bless the characterization until this is named.**

---

## 8. Production state — untouched by any of this

```
live checkout  ~/Documents/cycling-stack/domestique  detached at 9138110d
domestique              active
domestique-auth         active
cs-update.timer         inactive   (owner disabled it; do not re-enable)
```

Current plan (`~/.domestique/profiles/default/plans/current_plan.json`):

```
wk1 2026-09-13..09-13  budget  42   prescribed   0   (one-day sliver)
wk2 2026-09-14..09-20  budget 276   prescribed 276
wk3 2026-09-21..09-27  budget 309   prescribed 322
wk4 2026-09-28..10-04  budget 222   prescribed 222
```

wk2 is the week the owner starts Monday. With the change deployed and the plan
regenerated it would be budgeted ≈366.

---

## 9. Hazards for the next session

- **Nothing is committed.** A pre-change copy of the three src files is at
  `scratchpad/prechange/` — used for the HEAD A/B, keep it until committed.
- **Tests rewrite `src/workouts/.library_index.json`.** Restore it before
  characterizing or committing (`git checkout -- src/workouts/.library_index.json`).
- **Date-anchored suites**: check the parent and clean-main *the same day*
  before attributing a failure. See `[[domestique-planner-gate-gap]]`.
- **Push to the owner's fork by URL** (nothing but `gh` holds credentials):
  `git -c credential.helper='!gh auth git-credential' push https://github.com/taladjidi/domestique.git refactor/backend-architecture`
- **Never use Agent `isolation: "worktree"`** here — it lands inside the live
  checkout. Make scratch worktrees by hand.
- The owner is **out of patience with re-inflating plans**. Do not deploy
  anything that has not been measured on their real archive first.
