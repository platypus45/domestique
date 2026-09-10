# Planner cleanup — findings and plan

> **Superseded by `notes/overhaul-plan.md`** (2026-09-10, evening). An
> adversarial review done before any code changed (`notes/review/`) found that
> several premises below do not hold: the six owner-on failures are not one
> defect, Step 4 aims at the minority source of the slot/content mismatch,
> Step 2 as written would import the owner's double subtraction, and the gate
> this plan relies on was date- and machine-dependent and blind to three of the
> five entry points. Kept for the history and the decisions in §1, which stand.

Written 2026-09-10 at the end of a long session, for a **fresh session to
implement**. Everything here is measured; the tools that measured it are in the
repo and re-runnable. Nothing in this file is a guess.

State at handover:

| | |
|---|---|
| branch | `refactor/backend-architecture` |
| commits | `cd161a0e` (spacing + 6 planner bugs), `5dd953a9` (owner behaviours) |
| gate | 15/15 known baseline failures, none new |
| `_USE_TRAINING_WEEK` | **False**. With it on: 6 failures, all one root cause (§4) |
| characterization | 73 cases, all passing |

The companion architecture review — the same findings with the wider structural
survey, ranked, plus a recommended order of work:

- **In this repo:** `notes/architecture-review.html` (open it in a browser).
  This is the durable copy; it does not depend on any service.
- **Published page:** https://claude.ai/code/artifact/e2674ef2-d56d-47b7-999e-32bf3dafb5f0
  (private to the account that published it; also reachable from `/artifacts`
  in Claude Code, or the gallery at claude.ai/code/artifacts).

Neither is the source of truth. **The tool is** — every number in both is
regenerable, and if the code has moved since, the report is what to believe:

Re-run the measurements with:

```
scripts/architecture_report.py [--section size|arity|dead|layers|routes|duplication|state|coupling]
tests/probe_entry_point_parity.py 40      # invariants across all 5 entry points
.venv/bin/python tests/characterize_planner.py       # the refactor gate
```

---

## 1. Decisions already taken — do not relitigate

**Constraint precedence** (recorded in `src/week_plan.py`, above
`SealedSessionError`):

1. **Intensity budget.** Nothing outranks it. Exceeding the week's TSS ceiling
   or its hard-work share is how an athlete overtrains, and that is the one
   failure the planner must never cause.
2. **Recovery spacing.** 48 h between hard sessions (Seiler 2010, Gabbett 2016).
3. **Session count.** The right load over the right number of days beats the
   same load in fewer, bigger sessions.
4. **Variety quotas.** Per-class floors and TID separation. Real goals, and the
   first to yield.

**No escape hatch.** `week_plan.pin()` was built this session so a plan-level
policy could declare it must win. It had exactly one caller (the event
long-ride progression) and, measured, was worth **7 minutes** — the long ride
reached 233 min without it against a 240 min threshold. It is removed. A policy
that believes it outranks the order above belongs *in* the order, or its rule
belongs in `_commit`.

---

## 2. The headline finding: the module rebuilds what it already has

`sample_week_workouts` takes **24 parameters**. They are not 24 things. They are
four objects, unpacked:

| group | count | already lives on |
|---|---|---|
| the week | 7 | `WeekContext` + `IntensityBudget` |
| the athlete | 6 | `Goal` |
| the plan | 9 | `PlanState` |
| genuinely per-call | 2 | — |

`expand_blueprint_week` is the pure form of the anti-pattern: it takes `goal`
**and** five attributes of `goal` (`available_days`, `rest_days`,
`daily_max_hours`, `max_weekday_hours`, `max_weekend_hours`).

Measured across `training_planner.py`:

- **84 argument lines are literally `x=goal.x`**
- **165 lines of argument threading** across 9 call sites for those two builders

This is not cosmetic. It is the mechanism by which the five entry points drifted
apart: each one reassembles those 24 arguments by hand, and they stopped
agreeing. A 40-rider sweep found back-to-back hard days in `regenerate` and
nowhere else for exactly this reason. Passing a reference instead of unpacking
99% of an object's attributes removes the opportunity to get it wrong.

`WeekContext`, `PlanState`, `Goal` and `IntensityBudget` **all already exist**.
The target is `sample_week_workouts(ctx, state)` — 2 parameters, from 24.

---

## 3. The plan

Each step ends with the gate green. Do not batch them.

### Step 1 — collapse the builder signatures (half a day, low risk)

1. `sample_week_workouts(ctx: WeekContext, state: PlanState, budget)` — read
   everything else off `ctx`, `ctx.goal` and `state`.
2. Same for `expand_blueprint_week`, which already receives `goal`.
3. Update the 9 call sites. Five of them are the entry points; they already
   have or can trivially build a `WeekContext`.
4. Keep thin `**kwargs` shims **only** if a caller outside `training_planner.py`
   needs them — check first; at last count none did.

Verify: `characterize_planner.py` must show **0 changed** of 73. This step
changes no behaviour, so any fingerprint movement is a bug in the step.

### Step 2 — one derivation of the week's budget

`scale_budget_to_week` is called from 4 places with subtly different arguments
(net vs gross target, different `spent_zones` windows). Move it inside
`TrainingWeek`, which already knows the week, the athlete and what was ridden.
The entry points stop deriving budgets entirely.

Verify: `tests/probe_entry_point_parity.py 40` — `weekly_volume` violations
should not increase from the flag-off baseline of 1/7/1/1/1.

### Step 3 — the long ride is sized, not grown

`_apply_long_ride_target` runs as a post-pass and grows the weekend ride toward
the event's duration, after the owner has already sized the week. That ordering
is what `pin()` was papering over.

Instead: put the event's long-ride target on `WeekContext`
(`long_ride_target_min`) and have `_commit` size the weekend endurance slot to
it directly, bounded by weekend availability and the intensity budget (which
outranks it, per §1). Delete the post-pass.

Verify: `tests/test_event_and_goal_focus.py` — all 10, including
`test_long_ride_capped_by_weekend_hours`, which is the one that catches a fix
that ignores availability.

### Step 4 — slot type vs served content (the blocker, §4)

See below. This is the one that unblocks the owner flag.

### Step 5 — turn `_USE_TRAINING_WEEK` on

Only after step 4. Then delete the legacy loop bodies in `generate_plan` and
`regenerate_from_today` (the latter is ~141 lines that become unreachable), and
migrate `recalculate_plan`, `extend_continuous_plan` and `refit_remaining_week`
onto the owner too.

Verify: full gate, plus `probe_entry_point_parity.py 40` showing no violations
in any entry point.

---

## 4. The blocker: slot type and served content are different things

**All six remaining owner failures are this one defect.** The library can answer
an endurance slot with a file whose *content* is threshold or VO2. Then:

- the week is over its HIT cap with every slot-level rule satisfied
  (`test_planner_fixes::TestFix2TempoNotHIT`)
- `vo2_short` and per-phase floors miscount, because they count content classes
  over slots that were typed differently
  (`test_planner_variety_bonus` ×3)
- block-focus concentration is diluted
  (`test_v22_block_periodization::TestBlockConcentration`)
- a blueprint week's shape does not survive
  (`test_fs1_planner_modes::test_blueprint_keeps_b5_and_b3`)

It caused at least three separate failures earlier in the same session, in
different places, each time looking like a different bug.

**Do not patch it post-hoc.** That was tried and measured: clearing the file when
its content is hard but the slot is easy took the failures from **6 to 12** — an
unmatched slot is refilled elsewhere and the breach relocates. The note is in
`week_plan.py` at the site.

Fix it where `match_zwo` chooses: a slot must not be served a file whose content
class outranks the slot's type. That is one predicate, applied at selection.

---

## 5. Wider architecture findings

From `scripts/architecture_report.py`, over 79,050 lines of `src/`:

| finding | number |
|---|---|
| functions over 60 lines | 294 — **50% of `src/`** (39,856 lines) |
| functions over 200 lines | 38 |
| largest | `sample_week_workouts` 1,044 lines, 251 branches, depth 7 |
| deepest | `reforecast`, nesting depth **11** |
| `app.py` + `training_planner.py` | **47% of the codebase** (36,781 lines) |
| logic behind a URL | **9,648 lines**, 148 handlers, 68 over 40 lines |
| worst handler | `api_plan_generate`, 432 lines |
| handlers reaching SQL directly | 5 |
| import cycles | 7 |
| globals rebound at runtime | 40 (10 by more than one function) |
| unreferenced functions | 25 (reported, not deleted) |

Two more worth doing, both small:

**`_np_fraction_from_samples` is duplicated** in `app.py:5359` and
`training_planner.py:4347`. Verified by AST comparison with docstrings
stripped: **the code bodies are identical**. `app.py`'s docstring says they
"must stay in lockstep" and nothing checks it. Normalised power feeds TSS feeds
every budget in the planner. One definition in a leaf module both already
import — `zones.py` is the natural home. ~1 h, low risk, real correctness
consequence. Do this first; it is independent of everything else.

**`db.py`'s sync-thread globals** — `_sync_thread`, `_sync_stop`,
`_auth_disabled`, `_consecutive_failures`, `_last_sync_error` — are rebound from
three functions and touched from a background thread. They are one object's
fields.

---

## 6. Traps found the hard way this session

- **The characterization gate was blind.** Its 57 cases only ever called
  `plan_week`, never `generate_plan`, so it reported "all unchanged" while
  delivered plans changed materially. Now 73 cases including end-to-end
  generates. **Before trusting "all unchanged", check the matrix calls the
  function you changed.**
- **A clean invariant sweep is not proof of correctness.** Six invariants across
  40 riders came back clean while the owner was silently dropping two dozen
  behaviours. It is a floor, not a proof.
- **Implementation-pin tests are a comfortable place to hide a regression.**
  They do move legitimately, which is exactly why "the test is just a pin" must
  come *after* measuring why the number moved. Five quota tests looked like
  legitimate re-pins and were actually a bug: 26 slots were being turned to rest
  while the median week delivered 0.91 of its ceiling.
- **A rule about served content must be checked after the file is attached.**
  Violated three times in one session, in three different places.
- **Verify an instrument fails when the fault is present.** `plan_invariants`'
  first slot/file check iterated only training sessions, making it permanently
  blind to the rest-slot fault it exists to catch. `architecture_report.py` had
  four wrong checks on first run — it reported 162 dead functions because Flask
  registers handlers by decorator.
- **Tuning against a test set stops converging.** Failure counts went
  19 → 9 → 8 → 2 → 5 → 6 → 12 → 6. Past the first plateau, stop and reason about
  how the constraints compose. §1 exists so the next session does not have to.
