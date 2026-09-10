# Domestique overhaul — consolidated review and plan

Written 2026-09-10 (evening). **Supersedes §3–§4 of `planner-cleanup-plan.md`**:
an adversarial review, done before any code changed, found that several of
that plan's premises do not hold (see "Handover claims that did not hold").

The evidence is in `notes/review/` — five independent reports, one per lens
(owner, dupes, state, http, gates). Every finding there was produced by
running code in a sandbox, with a negative control showing the instrument
fires. The probe scripts themselves lived in a session scratchpad and are not
kept; the ones that matter became permanent gates (Step 0).

## State at start

| | |
|---|---|
| branch | `refactor/backend-architecture`, fast-forwarded to `ca6091f9` (the handover commits were on `refactor/session-sizing`) |
| old characterization | 73/73 unchanged — **on this machine, on 2026-09-10 only** (GATE-2) |
| full suite | with `--with fitparse --with Pillow`: 16 failed, every one on clean-main's known list; without them, three modules fail to import |
| `_USE_TRAINING_WEEK` | False. 8 owner-only failures with it on (not 6, and not one defect) |

## The eight root causes

Each is "a concept with no owner". Everything the reviews found is an
instance of one of these. IDs point into `notes/review/<lens>.md`.

**R1 — Nothing owns "this week's budget".**
- OWN-1 (critical, blocks the flag): owner on, Thursday generate after a
  normal Mon–Wed prescribes 19 TSS for Thu–Sun (legacy 194). The stub week is
  prorated by span *and* the Mon–Wed rides are subtracted.
- DUP-2 (high): three derivations of "what is left" give 192 / 222 / 0 TSS
  for the same rider and day; the right answer is 117.
- DUP-1 (critical, **live**): regenerate and recalculate call
  `generate_phases` without `recent_weekly_tss`, so the ACWR cap is gone
  (peak prescribed load +36–53%, 17–18 of 22 weeks above the safe load), and
  regenerate replaces the CTL target of every non-event goal.
- DUP-14: the phase preview shows ~1.8× the load Generate then builds — the
  preview gives `generate_phases` no recent load either.
- DUP-3 (high, **live**): the stepback week is counted five ways; after a
  regenerate the athlete can get six consecutive load weeks. 0.72 is a bare
  literal in 7 places.
- HTTP-1 (critical, **live**): Update plan, Reforecast, readiness tier-down
  and Auto-adjust feed the dense 71-row availability calendar in as
  overrides, and the scaler treats hours as a prescription: a 230-TSS week
  becomes 496; a *tier-down* raised its session from 80 to 135 TSS.
- HTTP-2 (critical, **live**): the home page's "this week" target comes from
  a second planner (`generate_weekly_plan`) that never reads the stored plan
  — 463 whatever the plan says. This is the mechanism behind "579 planned vs
  293 on the home page".
- HTTP-9: `net_tss_target` / `hit_allowance` are never persisted.

**R2 — Nothing owns "what is this workout".**
- OWN-3 / DUP-6: at least five definitions of "hard". The sampler mints a
  session's type from the file **name** (`_session_type_from_row`), so most
  easy-labelled sessions serving hard content are born in
  `_make_session_from_row`, not in `match_zwo`. Precedence #1 is enforced on
  labels: 3 weeks over the 60% hard cap by content, 0 by label.
- DUP-7: three content-class resolvers disagree on 73 files;
  `icu_calendar_push` reads the classification file at the wrong nesting, so
  every pushed event loses its display name (**live**).
- HTTP-3 / HTTP-13 / DUP-9: planned zone bars are filed by slot type (27%
  "high" against 0% in the served files); four type→band maps; three zone
  "family" folds.
- HTTP-4 (**live**): when activities come from the local archive the
  time-in-zone join misses (id prefix), and the actual bar shows 0% low —
  the "62% mid / 38% high" the athlete reported.

**R3 — Nothing owns a session after it is built.**
- OWN-2: with the owner on, *more* functions write each session (generate
  15 → 18); legacy passes write after `finish()` sealed the week; the seal is
  never installed in the app (GATE-5); only generate and regenerate use the
  owner at all.
- OWN-4: the eight owner-only failures have five different causes; two tests
  pass under legacy only by accident (a 2× overload; a demotion triggered by
  a mismatch).
- DUP-23 (high, **live**): "does the athlete own this session?" is decided in
  six places that disagree. Reforecast's fatigue (TSB) loop rewrites sessions
  the athlete dragged or dismissed, and drops their workout file.
- HTTP-10: 20 functions edit prescriptions directly in plan dicts. Moving a
  session onto an occupied day deletes the long ride; a hard session next to
  a hard one is accepted.
- Owner internals: dismissed sessions block 48 h and cost budget, done load
  counted twice (OWN-5); race week loses its opener and all intensity
  (OWN-6); the easy-floor repair can lower the easy share (OWN-7); `replan()`
  reshuffles 6 of 7 slots with no new input (OWN-8); `_demote` overcharges by
  up to 59% (DUP-8).

**R4 — Nothing owns the plan file.**
- STA-2 (critical, **live**): 20 of 23 plan writers read-modify-write outside
  the lock. Dismissals are lost 3/3 under concurrency; a freshly generated
  plan for a new goal is replaced by a recalculated copy of the old one.
- STA-3: the two writers that *do* hold the lock make stale writes land last.
- HTTP-6: GETs write — auto-recalc rebuilds the plan, week-summary and
  today-session can rewrite the athlete's FTP.
- HTTP-8: derived display fields are persisted and go stale.

**R5 — Generation parameters are process globals.**
- STA-1 (critical, **live**): the distribution model is a module global set
  per request; a concurrent generate and auto-recalc build — and persist — a
  plan under the other request's model (54/54 foreign reads).
- STA-5: the microintervals-only flag leaks from one click into later ones.
- STA-6: a failed fetch is cached as `{}` and each caller invents a CTL (30
  or 37).

**R6 — Nothing owns (de)serialisation of Goal and plan.**
- DUP-5 / HTTP-11: `Goal` is rebuilt by hand in 10+ places. `target_ctl`,
  `target_ftp` and the endurance targets are never persisted, so they vanish
  on the first automatic regenerate. Seven `PlannedWeek` builders from JSON.
- DUP-22 (high, **live**): reforecast reads sessions through the lossy
  reader, which drops `adapted` (and 11 other fields); the guard that skips
  adapted sessions never fires, so the same session is downgraded on every
  sync — vo2max → threshold → over-under → sweet spot → tempo in four syncs.
- DUP-25: the generate endpoint has its own serializer that writes 11 of 27
  session fields (nutrition notes lost at birth), and every rebuild deletes
  the readiness undo stash (`pre_adapt`), so "revert" restores nothing.

**R7 — Every entry point hand-assembles the week builder.**
- DUP-4: the six builder call sites disagree on 17 of 24 parameters. Refit
  re-samples a fixed_core plan with the random sampler (one missed hard day
  loses both long rides and adds two hard sessions), drops the focus emphasis
  and always samples as week 0. OWN-9: owner-on regenerate drops the event
  climbing emphasis. This is what the 24-parameter signature costs.

**R8 — The gates cannot judge the overhaul.** Closed by Step 0, below.
- GATE-1: the parity probe's recalculate/extend/refit columns were
  generate's plan audited again (40/40 identical).
- GATE-2: the golden held on one machine on one day (`date.today()` anchor,
  RNG seeded from the ICU athlete id).
- GATE-3: no served file in the fingerprint.
- GATE-4 / GATE-6: continuous goals only; the long ride, race week, blueprint
  modes and recalculate's body were never executed.
- GATE-7: tests that assert source text, not behaviour (to replace, step by
  step, with the property they protect — gates.md R7).
- OWN-12 / DUP-10 / GATE-9: the auditor graded against the planner's own
  stamped number, judged hardness by label, could not see default day caps,
  and counted missed sessions as hard.

Not planner, recorded for later (Step 7): STA-4 (a cache fill in flight
survives invalidation), STA-7/8 (the sync thread stays dead after a reconnect
while status reports healthy), STA-9, STA-11 (non-atomic ride-record
writes), DUP-24 (xSS is never computed from a FIT: a keyword mismatch that the
tests' fakes copy), DUP-26 (five FIT stream walkers disagree on pauses and
sampling rate), DUP-15/16 (the ZWO scanner exists twice and its committed
index serves stale zone percentages), and the Pillow gap in the production venv.

## Handover claims that did not hold

| claim | measured |
|---|---|
| "All six owner-on failures are one defect; fix it where match_zwo chooses" | 8 failures, 5 causes; the mismatch is mostly minted by the sampler |
| "12, 5, 6, 6, 3 passes"; "12 functions write a session" | no definition gives those; 22 distinct writers observed |
| "TrainingWeek is the only writer; enforced by the seal" | seal never installed in the app; 240 sealed writes in-process |
| "73 cases all passing" / "a run in six months compares equal" | date- and machine-dependent (GATE-2) |
| "parity probe: no violations in any entry point" | three of five entry points were no-ops in the probe |
| "84 argument lines x=goal.x" | 68 literal (88 with `adjusted_goal`) |
| "scale_budget_to_week called from 4 places" | 6, with 5 distinct argument combinations |
| "141-line unreachable block deleted" | still there; it is the production path |
| "handlers reaching SQL directly: 5" | 28 |
| "the FIT parser exists three times" | nine decode sites, five stream walkers |

## Decisions taken this session

Recorded so they are not relitigated without new evidence.

- **D1** Constraint precedence unchanged: intensity budget > 48 h spacing >
  session count > variety.
- **D2** A week's budget is decided in one place, `TrainingWeek`. For an
  opening stub week: `min(gross × span/7, gross − ridden since that Monday)`.
  One subtraction, not two.
- **D3** Two predicates, named for their questions, and no other type sets in
  planning code: `is_hard` (costs recovery — spacing, HIT cap, hard share;
  judged on type *or* served content) and `has_intensity` (a tier-down or
  easing candidate; includes tempo).
- **D4** A session's type comes from its slot. A file is admissible for a
  slot only if its content class is allowed there; an endurance slot never
  holds hard content.
- **D5** Availability hours are a ceiling, never a prescription.
- **D6** A missed or dismissed session costs nothing: no load, no spacing
  block. A done session's load is counted once, from the ride.
- **D7** The auditor stays independent by what it reads (the served file,
  the goal's caps, the rides), not by keeping a copied type set.
- **D8** For the owner to decide: HTTP verb changes. GET auto-recalc writes,
  and `cs-domestique-adapt` depends on it.

## The plan

Each step ends with the gate green and a commit. Behaviour changes are
expected from Step 1 on: every fingerprint that moves is explained before
`--bless`, per trap 3 of the owner review. A test that pins source text is
replaced by a test of the property it protects, shown to pass on the old code
too, before the step that breaks it lands (gates.md R7).

**The gate, from Step 0 on.**
```
tests/characterize_planner.py [--self-test]        # what moved, load vs content
tests/probe_entry_point_parity.py 40 [--owner]      # invariants per entry point
<scratchpad>/gate.sh <worktree> <label> baseline2   # full suite, serial rerun of any new failure
```

### Step 0 — gates that can judge — DONE

- `tests/_gate_env.py`: hermetic (empty data home, no network, frozen today,
  pinned athlete id) and one driver per entry point, shared by both gates.
  Every editing driver is shown to change its base plan.
- `tests/characterize_planner.py`: 57 builder cases + 78 entry cases per
  owner mode, over continuous, event, blueprint and CTL goals, with Thursday
  anchors, rides carrying zones, owned sessions, a missed hard day, stepback
  weeks in extend and refit, a TSB crash, and the day/week adapters. The
  fingerprint includes the served file; the diff says load or content.
  Independent of machine and date (verified with an empty `HOME`).
  `--self-test` PASS: each of six entry points' outputs is observed (15/15,
  10/10, 2/2, 16/16, 4/4, 25/25 own cases move when it is perturbed, and no
  other entry's), and five planted faults are seen (TSS rate, match_zwo
  ignoring content, ridden load ignored, long ride ignoring the weekend cap,
  owner 48 h off-by-one).
- `src/plan_invariants.py`: content-aware, status-aware, with a budget
  derived from the week's own span and the rides, the goal's real caps, and
  four new rules — under-delivery, easy-slot content, hard share, stepback
  lightest. `tests/test_plan_invariants.py`: a negative control per rule.
- What the new auditor finds in today's plans (78 cases per mode):

  | | weekly_volume | hard_share | easy_slot_content | under_delivery | empty_week | spacing |
  |---|---|---|---|---|---|---|
  | owner off | 107 | 36 | 27 | 5 | 0 | 0 |
  | owner on | 21 | 9 | 22 | 16 | 10 | 3 |

  Owner-on under-delivery / empty weeks are OWN-1 (all on `generate-thu` /
  `regenerate@3`). Parity probe, 40 riders, owner off: easy-slot content on
  21/21 event regenerate, recalculate and reforecast runs; weekly volume over
  budget for 10–33 riders on every entry point.

### Step 1 — one workout identity (R2)

One content-class resolver, one `is_hard`, one `has_intensity`, one type→band
map. Sampled sessions take their slot's type; endurance pools exclude hard
content. *Verify:* `easy_slot_content` → 0 in both modes; hard share holds by
content; the characterization diff is content-heavy and explained.

### Step 2 — one serialisation and no generation globals (R5, R6)

`Goal.to_dict/from_dict` and `dataclasses.replace` for adjusted goals; one
`PlannedWeek`/`PlannedSession` codec driven by `dataclasses.fields()` that
carries unknown keys through (DUP-22, DUP-25) and keeps `net_tss_target`; the
distribution model and microinterval flag travel with the goal, and the
module setters are deleted. *Verify:* round-trip tests; the two-thread repro
from STA-1 shows 0 foreign reads; the reforecast ratchet (DUP-22) stops.

### Step 3 — one owner of the week's budget and one builder signature (R1, R7)

`TrainingWeek` derives the ceiling (D2); one stepback predicate on a
plan-wide week index; every entry point (and the phase preview) feeds
`generate_phases` the same athlete state (ACWR cap, `target_ctl`);
`sample_week_workouts(ctx, state, budget)` and `expand_blueprint_week(ctx,
budget)`, branched in one place. *Verify:* one answer per rider across entry
points for DUP-1/2/3; `under_delivery` and `weekly_volume` → 0 in the
characterization.

### Step 4 — one week pipeline (R3)

Every entry point builds weeks through `TrainingWeek`; plan-level policies
(FTP test, phase floors, race/taper, re-entry) run once, in one order, as
proposals; `finish()` is the last write. Post-passes the owner subsumes are
deleted; the coherence pass's class-preserving rematch moves into the owner.
`_USE_TRAINING_WEEK` is deleted. One `is_immutable` used by every mutating
pass (DUP-23). Owner fixes D6, easy-floor taper exemption, opener placement
on an available day, deterministic `replan`, long ride sized inside
`_commit`. *Verify:* STRICT_SEAL over every gate case → 0 trips; invariants
clean across all entry points; test_event_and_goal_focus 10/10.

### Step 5 — availability as a ceiling, one plan store (R1 HTTP-1, R4)

One derivation of per-date availability, consumed by the owner as day caps;
one load→mutate→save with a version check instead of 23 hand-rolled writers.
*Verify:* the HTTP-1 sandbox replay keeps the week at its target; the STA-2
concurrent repros keep both writers' updates.

### Step 6 — one week view for the HTTP layer (HTTP-2/3/4/7)

Home and calendar read the stored week and the served files;
`generate_weekly_plan` stops answering "what is planned". Response shapes
unchanged (backend only).

### Step 7 — runtime state and the non-planner findings

STA-4, 7, 8, 9, 11; DUP-24, 26, 15/16; the Pillow gap in the production venv.

## Deployment note

Production runs the live checkout at `~/Documents/cycling-stack/domestique`,
on `refactor/session-sizing`; the running process loaded `9f30cf93` at 16:24
on 2026-09-10. `cs-update` runs Sunday 03:59 with `TRACK=stable` and will move
that checkout to the newest stable tag. Nothing from this branch is deployed;
that is the owner's decision.
