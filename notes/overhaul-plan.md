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
- DUP-22 (high, **live**): the same session is downgraded on every sync —
  vo2max → threshold → over-under → sweet spot → tempo in four syncs.
  Reforecast reads sessions through a lossy reader that drops `adapted` (and
  11 other fields), so G3's "already adapted" guard never fires. That is not
  the whole cause (measured in Step 4): the TSB loop that does the
  downgrading has no such guard at all, and ratchets identically on either
  reader, because every sync hands it the same flat TSB projection.
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

Not planner, recorded for later (Step 9): STA-4 (a cache fill in flight
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
| DUP-22: "D1 plus `adapted` only: sync 4 modifies nothing" | with the reader carrying `adapted`, three syncs under TSB −40 still ease the same sessions a tier each sync: the TSB loop never reads `adapted` (Step 4) |

## Decisions taken this session

Recorded so they are not relitigated without new evidence. The project's
value is that its rules come from the training literature; each decision says
which rule it serves, so a later change can be argued on the same ground.

- **D1** Constraint precedence unchanged: intensity budget > 48 h spacing >
  session count > variety. *Why:* load spikes are the best-documented injury
  and overreaching risk (Gabbett 2016, ACWR); recovery from work above the
  first threshold is what caps hard sessions (Seiler 2010); the number and
  quality of hard sessions matters more than their length, and variety is a
  preference.
- **D2** A week's budget is decided in one place, `TrainingWeek`. For an
  opening stub week: `min(gross × span/7, gross − ridden since that Monday)`.
  One subtraction, not two. *Why:* the acute load the ACWR guards is the
  calendar week's, so what was ridden Mon–Wed counts against it (Gabbett 2016;
  Hulin 2016); proration keeps a week's load from being compressed into four
  days, which is itself a spike.
- **D3** One hardness predicate, `_session_is_hit` (the type prescribed *or*
  the served file's content), and the tier-down rungs derived from it
  (`_HARD_SESSION_TYPES` = hard types − the FTP test + tempo). No other type
  sets in planning code. *Why:* "hard" is a session above the first threshold,
  which delays autonomic recovery far more than work below it (Seiler, Haugen
  & Kuffel 2007) and counts against the session-goal budget of ~2–3 hard
  sessions a week (Seiler & Kjerland 2006) — whatever the slot is called.
  Tier-down sheds any intensity on a low-readiness day, tempo included
  (HRV-guided training, Kiviniemi 2007); a test is postponed, not tiered down.
- **D4** A session's type comes from its content, and an endurance slot never
  holds hard content — sweet spot included; tempo may stay. *Why:* an easy day
  that serves work above VT1 is an unplanned hard day that neither the weekly
  HIT count nor 48 h spacing saw, and distribution is judged by the time the
  athlete actually rides in each zone, not by labels (Rosenblat et al. 2025).
  Tempo is the moderate volume the pyramidal phases prescribe (Filipas 2022),
  budgeted by the week's zone minutes.
- **D5** Availability hours are a ceiling, never a prescription. *Why:* load
  is set by the athlete's chronic load and the phase (Gabbett 2016; Issurin
  2010); free time is not a training stimulus, and scaling a session up to it
  is how a tier-down came to raise load.
- **D6** A missed or dismissed session costs nothing: no load, no spacing
  block. A done session's load is counted once, from the ride. *Why:* fatigue
  comes from training performed, not planned (Banister impulse-response); an
  unridden session imposes no recovery cost.
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

### Step 1 — one workout identity (R2) — DONE

- One content-class resolver: `_content_class_for_zwo` falls back to the same
  filename rule as `_content_class_for_row` (they disagreed on 73 files).
- One hardness predicate, `_session_is_hit` (type or served content; takes a
  plan dict too). The owner's `week_plan.HARD_TYPES`, the app's hand mirror and
  daily-adapt's local set are gone; the tier-down rungs `_HARD_SESSION_TYPES`
  are derived from it (D3).
- Sessions are typed by content (`_session_type_from_row`), every content class
  maps, and endurance slots — pool, mix preference and emergency fallback —
  hold no hard content (D4). Sweet spot is out: it is above VT1 (Seiler,
  Haugen & Kuffel 2007) and has always been a hard type here.
- The z5plus "hard-kill" is a gate again. Once the budget fit became a
  multiplier, a file overshooting the week's time above 106% FTP by 20+ minutes
  kept two-thirds of its weight while novelty spans three orders of magnitude,
  so a threshold-model week drew three VO2 files and crossed the 18% rail.
- `icu_calendar_push` reads the classification entries, not the envelope
  (every pushed event had lost its display name).
- The type→band maps for the HTTP layer's zone bars are left for Step 6, which
  replaces labels with the served files' own zones.

Measured (characterization; planner change only, same auditor on both sides):

| | easy-slot content | 48 h spacing | hard share | under-delivery | weekly volume | stepback |
|---|---|---|---|---|---|---|
| owner off | 7 → 0 | 0 → 0 | 41 → 44 | 4 → 4 | 115 → 112 | 1 → 3 |
| owner on | 2 → 0 | 5 → 0 | 12 → 12 | 16 → 20 | 26 → 24 | 2 → 2 |

The other rules move by churn — new and resolved findings in every entry point
at similar rates: removing sweet spot from the endurance pool shifts every later
weighted draw, so session-level diffs are not attributable (the two new stepback
findings are both legacy regenerate, which has no "stepback is lightest" pass).
Time above 106% FTP per week over tid_plan_properties' matrix: mean 5.6 → 5.5 %,
p90 12.1 → 10.5 %, max 16.4 → 16.6 %, none over the 18 % rail (the identity half
alone reached 18.6 %; the gate brought it back). Delivered/target median
unchanged (0.97 off, 0.95 on). "tempo" labels fell from 205 to 59 and
labelled-hard sessions rose from 587 to 650: files named tempo_* whose content is
endurance or sweet spot are now typed by what they contain.

Re-pinned: `test_planner_fixes::…test_multiple_tempos_and_one_hit_possible`
counted labels against `Phase.hit_per_week` (1), a field the sampler does not
read, and passed on weeks whose only hard work was a 97-min sweet-spot file on a
z2 slot. It now counts served content against the base budget (two quality days
from 7 h/week, Zapico 2007); the new assertion passes on the Step 0 code too.

### Found on the way — SCI-1: the sampler does not honour its own science table

The mix preference is a science table (base: sweet spot and threshold, no VO2),
but the sampler draws per FILE with per-file weights, so a class's odds scale
with how many files the library holds in it. The HIT pool holds 388 VO2max
files against 382 sweet-spot and 630 threshold; pool size × mix weight alone
gives VO2max ~14 % of base hard picks where the table says 0, and novelty (25×
for an unseen file, ~1000× across picks) swamps both the budget fit (1.5^±1)
and the class preference. Measured: a pyramidal base week for an 8 h rider drew
two VO2 files. The z5plus gate bounds the damage; the fix is to choose the
CLASS by the table and the week's budget, then the file within it by novelty —
a behaviour change that needs its own measurement. Step 5b.

### Step 2 — one builder signature (R7) — DONE

`sample_week_workouts(ctx, state, budget)` and `expand_blueprint_week(ctx,
budget)`, from 24 and 12 parameters. Done ahead of the rest because the steps
below need the goal inside the sampler, and threading a 25th argument into the
function being collapsed would have been the old pattern again. A pure
refactor: every call site builds its WeekContext from exactly the values it
passed — refit's week-in-phase 0 and extend's fixed horizon included — so the
characterization is unchanged in all 213 cases. The drift itself (DUP-4) is
fixed as a visible change when the contexts move into TrainingWeek (Step 5).
The four tests that call the sampler directly changed at the call site only;
they pass None for the plan-wide bookkeeping, as their old calls did by default.

More evidence for SCI-1, found here: test_goal_focus_shifts_mix holds only with
that bookkeeping switched off. With it on — as every real entry point runs the
sampler — a vo2max goal drew 19 VO2 picks against 21 for a general goal over
5 seeds × 12 weeks: the goal's emphasis is swamped by novelty and diversity.

### Step 3 — generation parameters travel with the goal (R5) — DONE

`get_budget_for_phase(phase, goal)`, `active_model_for_phase(phase, goal)`,
`budget_table(goal)` and `polarized_targets(goal)` read the model from a Goal
or from a persisted goal block (a block without the key predates J1, when every
plan was polarized — the restorer's existing default). `match_zwo`'s
microinterval preference is an argument from whoever holds the goal: every
planner entry point, the owner, the class-preserving coherence pass, the
re-entry reshape, tier-down, reforecast and the seven app handlers that rematch
a day. The swap-type endpoint passes its per-swap override instead of setting a
process flag (STA-5). `set_active_distribution`, `set_vo2_micro_only`, their
getters and the three module globals are deleted, and with them conftest's
guard that snapped them back after every test — the symptom it hid is gone.

Measured: characterization unchanged in all 213 cases (the riders use the
default model, which every path already honoured serially).
`tests/test_plan_is_a_function_of_its_goal.py` builds a polarized and a
threshold plan on two threads at once and compares each with the same goal
planned alone: on the Step 2 code 4 of 4 trials produced a plan its goal does
not determine; now none. The tests that asserted "the global was re-pinned"
now assert the property it stood for — every budget lookup during a regenerate
or auto-recalc carries the plan's own model.

### Step 4 — one serialisation (R6) — DONE

One codec in the planner, next to the dataclasses and built from
`dataclasses.fields()`: `session_to_dict`/`session_from_dict`,
`week_to_dict`/`week_from_dict`, `goal_to_dict`/`goal_from_dict`. Every reader
of a stored plan goes through it: the seven app week builders,
`_plan_dict_to_planned_weeks`, reforecast's Goal and the five goal-block
readers. So do the generate, regenerate and auto-recalc writers. The goal block
keeps its historical key names, which the dashboard reads, and gains the
targets it never carried. Regenerate and recalculate copy the rider's goal
with `dataclasses.replace` instead of rebuilding it field by field.

One departure from the plan: the codec does not "carry unknown keys through".
`_enrich_plan_for_response` writes nine display keys into the dict it serves,
and the reforecast endpoint stamps weeks `is_current`/`is_past`. Carried
through a rebuild, a date-relative flag would outlive the day it was true on.
The codec carries the fields plus the five keys that are session state
without a field: `variation`, `adapted_reason`, `auto_moved`, `ftp_test_type`,
and `pre_adapt`, the undo stash the old allow-list forgot.

Measured:
- **Characterization** is unchanged in all 217 cases: 57 builders and 80 per
  owner mode. The commit message says 229, which is a miscount. This includes
  the new stored-plan reforecast driver, which was blessed on the previous
  code and seen by the self-test. The riders exercise none of the lost fields.
- **What that driver can see.** It fingerprints only what the rider is told to
  ride. State that changes no prescription, a dropped pin for instance, is
  invisible to it; the unit tests cover that. At the codec commit it did not
  move at all, because nothing it fingerprints read `adapted` (review M2).
- **Tests that fail on the previous code** show the fixed defects:
  - the reforecast reader keeps `adapted`, `completion_matches`, `moved_from`
    and `execution`, which is DUP-22's reader half;
  - after a real `_regenerate_plan_dict`, revert restores the original
    (DUP-25; on the old code it restores nothing);
  - the goal block round-trips `target_ctl` (DUP-5).

The ratchet did not stop, because its cause is not the reader (see "Handover
claims that did not hold"). That is Step 4b.

### Step 4b — reforecast eases a session once — DONE, superseded by 4c

*The review found this guard made the fatigue response one-shot for the
whole plan. Step 4c replaces it.*

The TSB downshift loop skips a session that is already `adapted`, as G3
always has. The rule it implements is one tier past TSB −25. Two syncs minutes
apart hand it the same reading, so applying the rule again per sync
compounded one reading into a tier per sync. The fatigue signal is not
re-counted, and the day-of adaptation (`adjust_today_session`, from actual
readiness) still eases the day itself further when the athlete arrives tired.
Measured:
- `test_reforecast_adapts_a_session_once_not_on_every_sync` fails on both
  readers before the guard and passes after it.
- The characterization changes only the two stored-plan cases, and only where
  a later sync had re-eased. After three syncs a session sits one tier down:
  over-under, not tempo; threshold, not sweet spot.
- The event rider's case now shows `hard_share` in weeks 5 and 7 and
  `weekly_volume` in weeks 2 and 10. These are the base plan's own findings:
  the legacy builder's generate has them, the owner's does not. The ratchet
  had hidden them by easing everything to tempo and z2, which cut week 7 from
  510 to 442 TSS. They belong to Steps 5 and 6.

### Step 4c — the review's fixes: fatigue easing, and three small ones — DONE

The independent review of Steps 4 and 4b (28 claims, 22 held) found that
Step 4b's guard made the fatigue response one-shot for the whole plan.
Production hands reforecast today's TSB for every future day. So a TSB −26 on
day 3 eased all 24 future hard sessions, and a real −60 crash on day 24 then
eased 0 of 20. The loop is now a function of the original prescription and
today's reading:
- **Rung.** `_ease_for_recovery` (z2), the code's rung for fatigue, replaces
  one rung down the ladder, which left a threshold day hard and kept its TSS.
- **Horizon.** Only the coming 7 days (`TSB_EASE_HORIZON_DAYS`). ATL, the
  fatigue term in TSB, is a 7-day exponentially weighted average (Banister),
  so today's reading says nothing about a session a month away.
- **Reversible.** The original is kept in `tsb_eased_from`, which the codec
  carries, and restored when the day's reading clears. An eased day is
  re-matched to a workout rather than left empty.

Also fixed:
- Readiness now needs an event date that parses; a corrupt one showed 100%.
- `week_from_dict` raised on a malformed session, where a rebuild used to
  drop it. *Reversed after the Step 5 review (M2): raising answered 500 on
  the ride-sync path. The codec skips and logs such a session again.*
- `_drop_intensity("sprint")` is now `z2`, not `vo2max`: a low-readiness
  tier-down added the stress it exists to shed.

Measured:
- **Tests.** `tests/test_reforecast_fatigue.py` fails four of five on Step 4b.
  The review's case fails on behaviour (a −60 crash eased 0 of 2); three fail
  for want of the record. All five pass here.
- **Characterization.** Only the two stored-plan cases move, and only in the
  coming week. An eased day is now z2 with a workout at about half the TSS
  (over-under 124 → z2 68, threshold 87 → z2 52), where it used to keep its
  load and lose its file. One `weekly_volume` finding resolves.
  The sprint rung moves one more case: a daily-adapt projection at TSB −35
  now de-loads a Thursday sprint to z2 at 34 TSS, where it projected VO2max
  at the same 50.
- **The swap pin.** `test_swap_type`'s pin test moved its two days into the
  coming week, and the pin holds.

### Found on the way — reforecast's fatigue response barely reduces fatigue

Two defects, measured on the stored-plan driver and present on either reader.
*Both are fixed for the fatigue loop in Step 4c. The one-rung ladder still
keeps the load where G3 and the tier-down endpoints use it.*
- **The tier drop keeps the load.** `_deescalated_load` keeps TSS: vo2max
  87 → threshold 87, 80 → 79, only the minutes shrink. The TSB model the loop
  invokes (Banister/Coggan: TSB = CTL − ATL, and ATL is driven by TSS) says
  fatigue falls only when stress does. An intensity swap at equal TSS answers
  a TSB −40 reading with the same training stress.
- **An eased session loses its workout file.** The loop clears `zwo_file`
  "to force a library re-match downstream". Neither `reforecast_dict` nor its
  seven app callers re-match, so the eased day is served with no workout.

### Found on the way — the Step 4 review (notes kept in the scratchpad's review/step4.md)

28 claims: 22 held, 3 did not, 1 held in part, 2 could not be checked. Its
findings, by where they are dealt with:
- **H1, Step 4b's own guard.** The fatigue response became one-shot for the
  whole plan. Fixed in Step 4c.
- **H2, pre-existing: every ride sync overwrites today's readiness
  adaptation.** The auto-reschedule moves yesterday's missed threshold onto
  today's readiness-eased ride and deletes the undo stash. It belongs to Step
  6, where one predicate says which sessions the athlete owns (DUP-23).
- **M1: a persisted `target_ctl` changes recalculate but not regenerate.**
  Regenerate ignored an explicit target, and Step 5 part 2 makes both honour
  it. Only plans created through the API carry one; the dashboard never
  sends it.
- **M2: the stored-plan driver's blind spot.** See Step 4.
- **M3, pre-existing: rebuilds still drop rider state.** Lost are the swap
  pin, `variation`, `ftp_test_type`, `auto_moved` and the current week's
  missed session. The codec carries them; the rebuild paths discard them. Step
  6 (DUP-23).
- **Low findings, fixed in Step 4c:**
  - a corrupt event date read as 100% readiness;
  - a malformed session was silently dropped on rebuild (reversed after the
    Step 5 review, M2: it is dropped again, and now logged);
  - the easing ladder turned a sprint into VO2max work under fatigue.

### Found on the way — the continuous floor lost its sessions to 48 h spacing

The gate's full run on 09-11 flagged `test_354`, a continuous plan's
anaerobic and neuromuscular floor. It passes on clean-main and fails on this
branch. Bisected to `cd161a0e` ("Enforce 48h spacing everywhere", from before
this session), which a Friday start exposed.

The floor for a missing class tried the lightest week first. When every slot
there sat within 48 h of a hard day it took one anyway, because spacing was
only a sort key. The spacing pass that runs afterwards then eased it. The
code's own note said ranking "keeps the floor met", and that did not hold.
Fixed: the floor searches the whole phase for a day the 48 h rule allows
before it takes a clashing one (D1: spacing outranks variety).

Measured, for continuous plans starting on each day of one week, and for
`test_tid_plan_properties`' matrix on 14 start dates:

| property | before | after |
|---|---|---|
| floor kept, 7 start days | 5 | 7 |
| no week above 18% time above threshold | 12/14 dates | 14/14 |
| polarized carries less middle than threshold | 41/42 | 36/42 |
| — at 12 h/week | 13/14 | 10/14 |

The separation cost is the variety floor's own. It imposes the same
anaerobic and neuromuscular sessions on every model. Once those stop being
eased away, they displace middle-zone work in threshold-model plans, and the
threshold model's middle share falls further than polarized's. That is a
variety rule overriding the distribution model, the SCI-1 problem: Step 5b's
remedy (class-first sampling that honours the model's table) has to cover the
floors too.

### Step 5 — one owner of the week's budget (R1) — parts 1, 2, 4 DONE; part 3 waits

*Status on 2026-09-11.* Three parts are done, each below:
- part 1, the stepback rhythm;
- part 2, one athlete state and one target rule;
- part 4, one week context.

Part 3 is D2's week ceiling, and it needs one thing decided first: which
number is a week's budget. That is the ramp target the auditor, the owner
and the UI read, or the ACWR base fill the week-total pass prescribes (see
the end of part 2). D2's `min(gross × span/7, gross − ridden)` is built on
that number. The verify line's `under_delivery` and `weekly_volume` → 0
depends on it too.

So does a low-load rider's hard share (M6 of the Step 5 review). When a
week's hard sessions alone exceed the ACWR ceiling, the easy-volume pass
cannot bring the week under it. The budget has to bound the hard sessions,
not only the week's total.

`TrainingWeek` derives the ceiling (D2); one stepback predicate on a plan-wide
week index; every entry point (and the phase preview) feeds `generate_phases`
the same athlete state (ACWR cap, `target_ctl`); the builder contexts are built
in one place, so refit's emphasis and week-in-phase stop drifting (DUP-4).
*Verify:* one answer per rider across entry points for DUP-1/2/3;
`under_delivery` and `weekly_volume` → 0 in the characterization.

#### Step 5, part 1 — one stepback rhythm (DUP-3) — DONE

`stepback_due(prior_weeks, phase)` counts the load weeks since the last unload
over the whole plan. An unload is a stepback, a taper, a regenerate's recovery
ramp (`recon`, `recovery_ramp`), or a deload the app advanced. Generate, the
entry scan, regenerate, recalculate and extend all use it. The 0.72 factor,
a bare literal in seven places, is `STEPBACK_LOAD_FACTOR`, beside
`STEP_BACK_EVERY`.

Measured:
- **Rebuild points.** On one 25-week event plan, rebuilt at every week 1–18:
  - the old counters gave regenerate 4 and 5 load weeks in a row at weeks 1–2;
  - recalculate got 6 at every fourth rebuild point, and 4–5 at most others;
  - the predicate gives 3 everywhere.
  - A plan built in one go is unchanged: stepbacks at weeks 4, 8 and 12.
- **Characterization.** 7 of 80 cases move per owner mode, all
  regenerate@17 and recalculate@21, each where a fourth load week became the
  stepback.
- **New auditor findings.** Each has an identified cause:
  - The FTP test is barred from stepback weeks, so it moves to the next row.
    Here that is a two-day build2 fragment (Mon–Tue) before a taper starting
    mid-week, which then fails `hard_share`.
  - The owner makes that same fragment its stepback (`under_delivery`).
  - A resampled taper week crosses `weekly_volume`: it was at +11% before.

Recorded, not decided here:
- **The rhythm counts plan rows**, as generate always has. A phase boundary
  that splits a calendar week makes a two-day fragment count as a week.
  Counting calendar weeks would be closer to the microcycle the 3:1 rule is
  about, but it changes generate too.
- **FTP tests are barred from stepback weeks.** Allen & Coggan's advice is to
  test rested, at the start of a block, which argues for the opposite. This
  is a question for the owner, not a refactor.

#### Step 5, part 2 — one athlete state, one target rule (DUP-1, DUP-14) — DONE

`athlete_weekly_load(ctl, recent)` — the recent mean from the ride archive,
else CTL × 7 — is filled in by `generate_phases` for every caller: the
preview, the entry scan, regenerate and recalculate got the ACWR ceiling
(Gabbett 2016: acute load past ~1.3× chronic) only by accident before.
`plan_target_ctl(goal, ctl, event_targets)` is the goal's own rule capped by
Couzens' safe ramp over the weeks the phases have. Regenerate's recovery ramp
is a ceiling on that rule, not a replacement for it: an FTP goal aimed at
CTL 99 and an explicit target of 60 at 113. Recalculate now runs the
week-total pass generate and regenerate run.

Measured on a rider with a chronic load of 250 TSS (ceiling 325):
| path | weeks over 1.05 × ceiling, before → after | peak prescribed |
|---|---|---|
| generate | 0/21 → 0/21 | 340 → 340 |
| regenerate | 12/17 → 0/17 | 514 → 336 |
| recalculate | 15/17 → 0/17 | 568 → 335 |

Capping the targets alone made recalculate *worse*, 564: its prescription
followed the rider's free time, not its targets, because it had no
week-total pass. Checking labels instead of prescribed load would have called
that fixed.

In the characterization, 7 of 80 cases move per owner mode, all
regenerate@17 and recalculate@21. Recalculate's unload week had been the
heaviest of its block: 465 TSS against a 265 label, now 259. For this rider
every rebuilt load week sits under the ACWR ceiling.

*That does not hold in general (the Step 5 review, M6).* Take a rider at
150 TSS a week (CTL 28, ceiling 195). Recalculate still has 5 of 12 weeks
over, up to 267 TSS (1.37× the ceiling). Generate has 5 of 14 with the
legacy builder (0 of 14 in owner mode). Every such week is all hard
sessions, and the week-total pass never shrinks a hard
session. That is the intensity budget, D1's first rule, and part 3's
question.

The auditor flags these weeks against their lowered ramp labels, as it
already flagged generate's. Part 2 also adds `hard_share` and
`under_delivery` findings on the rebuilt weeks. Both are the part 3
question below.

**The question part 3 has to settle.** The week-total pass lets easy volume
fill base, build and peak weeks up to the ACWR ceiling, above the phase's
ramp target ("polarized base fill", v2.1.1), while the auditor, the owner
and the UI read the ramp target. So a week has two budgets, and the
`weekly_volume` findings on generate's own plans are that disagreement. One
number has to win.

#### Step 5, part 4 — one week context (DUP-4, OWN-9) — DONE

`week_context(goal, phase, week, start, …)` builds the context every entry
point builds a week from, with one emphasis rule (`_week_emphasis`) and the
blueprint modes' block-focus exemption. `propose_week(ctx, state, budget)`
takes the blueprint-or-sampler branch for generate, regenerate, recalculate,
extend, refit and the owner. Refit reads its week-in-phase from the plan.

Measured. The sampler reads only the context's emphasis, block focus, goal,
phase, dates, seed and week indices, and the blueprint engine reads fewer
still. So only refit's plans and the owner's regenerate could change, and
the characterization moves only refit@3 cases (11 and 12 per owner mode):
- **Fixed plan.** With the legacy builder, a fixed_core plan's refit keeps
  its generated week: two long rides and one sweet spot, 456 TSS. The sampler
  had swapped Thursday's sweet spot for vo2max, cut Saturday's long ride to
  an hour of z2, and cut Sunday's from 150 to 120 minutes. That week
  totalled 339, counting the missed session.
  - *Corrected after the Step 5 review:* the first write-up said "two
    vo2max sessions and no long ride".
  - In owner mode, refit still bypasses the owner's sizing (L6, below).
- **Continuous plans.** Continuous refits carry their focus emphasis, and
  over 12 seeds the load they re-owe is unchanged: mean 127 → 132 TSS, hard
  86 → 85, and 3 of 12 under-deliver both before and after.
- **Owner's regenerate.** It now carries the climbing emphasis, but these
  riders' draws don't respond to it. That is SCI-1's diluted class
  preference (Step 5b).

`tests/test_one_week_context.py` fails all three of its checks on the
previous code.

### The Step 5 review — what was fixed, corrected and recorded

An independent reviewer ran Step 4c, the floor fix and Step 5 parts 1, 2
and 4. Of 47 claims, 38 held, 5 held in part and 4 did not. The report is
kept in the scratchpad's review/step5.md. It had no critical or high
findings.

**Fixed.** Each fix has a test that fails on the previous code, and the
characterization is unchanged (217 cases).
- **M1. No TSB reading counted as a cleared one.**
  - With ICU down, `get_today_metrics` answers `{}`. Every eased session was
    then restored, and eased again at the next sync with a reading.
  - No reading now leaves the day as it is, as Step 4b did. Through the app,
    an outage restored 2 of 2 eased days; now 0.
- **M3. A restore erased G3's flag.**
  - When the reading cleared in the same sync that G3 lowered the restored
    day, the write-back cleared `adapted`. G3 then lowered the day again the
    next time: two tiers for one breach.
  - The write-back now clears the flag only when nothing has adapted the day
    since.
- **M4. A restore ignored 48 h spacing.**
  - A rider who moved an eased day next to a hard one got the hard original
    back there: 35 of 37 moves in a probe, counting today's session. (The
    first count, 18, left today out.)
  - A hard original now goes back only where `_slot_breaks_hard_spacing`
    allows it (D1); otherwise the day stays eased. Now 0 of 37.
- **M2. The strict codec answered 500 on the ride-sync path.**
  - One session without a readable day, sitting in the current week, made
    update, rematch, regenerate and auto-recalc answer 500. It also made
    reforecast drop its whole week.
  - The codec skips and logs such a session, as before Step 4c. Every
    endpoint answers 200, and the week keeps its other sessions.
- **A refit replaced a scheduled FTP test.**
  - On the test's day, 16 of 16 fixed_core and template refits turned it
    into threshold. Refit redraws the week with a builder that never places
    a test.
  - A scheduled test is now frozen to the refit, like a race (Allen &
    Coggan: test on fresh legs). Now 16 of 16 kept.
- **L2. Refit's event emphasis never reached production.** The app's refit
  passed no athlete. The three inline copies of the athlete assembly are now
  one helper, `_planner_athlete`, which refit uses too.
- **L3. The home card unloaded one week late.** It unloaded in weeks 5, 9
  and 13 against the plan's 4, 8 and 12. It now reads the plan week that
  covers its week.

**Corrected above.**
- Part 2's recalculate peak: 568, not 532.
- Part 2's ACWR claim (M6).
- Part 4's fixed-plan refit (L5).
- Step 4c's strict codec (M2).

**Recorded, for the step that owns each.**
- **M5, for Step 6.**
  - A rebuild keeps a fatigue-eased day as the rider's own and plans the
    week around its easy load. The restore then puts the original back: up
    to 68 TSS on the week and 139 hard TSS over a never-eased rebuild
    (regenerate, legacy builder; corrected by the fix review, F5).
  - The 48 h check does not reduce it, because the rebuild never puts a
    hard day beside the eased one. The load is the ownership question
    (DUP-23). An adaptation the planner made is derived state, and a
    rebuild should re-derive it rather than own it.
- **G3 walks through the plan. Pre-existing; found while fixing M3.**
  - While one polarization breach persists, each reforecast lowers the next
    two hard sessions not yet adapted, so each run reaches further ahead.
    After 6 runs, 12 sessions were lowered, reaching 6 weeks out, in both
    owner modes.
  - Only `/api/plan/reforecast` passes the polarization inputs, and nothing
    in the dashboard calls it any more: its button went in v1.8.24, and
    `reforecastPlan()` is dead code. So G3 walks only on a direct API call.
    (Corrected by the fix review, F4: the first write-up said "once per
    click".)
  - It is the DUP-22 ratchet spread across sessions. Step 6 settles it with
    the fatigue loop's pattern: the coming week only, a record of the
    original, undone when the breach clears.
- **M6, for part 3.** See part 2 and the Step 5 status above.
- **L1, a decision for the owner.**
  - A goal without a target date, rebuilt after an absence, now aims higher
    than before. After a 3-week break, the CTL target went 65 → 86 and the
    phase peak 453 → 605.
  - Both versions lay a fresh 16-week runway after the ramp. The new target
    matches the phases actually built; the old one was sized to a plan end
    the rebuild overran.
  - The ACWR ceiling does not brake it, because the recent mean averages
    only weeks with a ride.
- **L4, for Step 6 (D6).** The rhythm reads labels, not ridden load. After
  one ridden load week and two fully missed ones, the next week is a
  stepback. D6 says a missed session costs nothing.
- **L6, for Step 6.** In owner mode, refit splices the raw blueprint week
  over a week `TrainingWeek` sized. Remaining-day load rose in 20 of 24
  cases, against 12 of 24 before part 4. Week totals stay under target.
- **A library file with impossible load, for Step 9.**
  `vo2_short_10x30s-30s_105pct_40min.zwo` is served at 117–128 TSS for 40
  minutes, which is 175–190 TSS an hour.

### The review of those fixes

A third independent review ran the fixes above. Of 22 claims, 19 held, 2
held in part and 1 did not. The report is kept in the scratchpad's
review/step5b.md.

The fixes held under the review's own probes:
- M1, through the app;
- M3, in both owner modes;
- M4, across week boundaries and double moves;
- M2, over 14 malformed session shapes and all 24 endpoint cases;
- the FTP test, in 48 of 48 refits;
- the athlete helper, over 20 profile states.

**Fixed.** Each fix has a test that fails on the previous code, and the
characterization is unchanged (217 cases).
- **F1 (medium, from Step 4c). A restore overrode the rider's
  availability.**
  - An eased day set to 0 h got its hard original back once the reading
    cleared. Through the app, that put threshold 63 minutes on a 0 h day.
  - The easing now records what it made of the day, and a restore undoes
    only that. A day another writer has changed since stays as it is (D5).
  - In the review's probe, 8 of 8 cut days keep their hours, and 860 of 860
    restores in unmoved plans still go through.
- **F2 (low). A dismissed neighbour blocked a restore.** D6 says a session
  not ridden costs nothing, so `_slot_breaks_hard_spacing` no longer counts
  a missed or dismissed one.
- **F3 (low). After the plan's end, the home card switched to ISO weeks.**
  That put up to 8 weeks between two unloads. The card now carries the
  plan's 3:1 on from the last week the plan unloads.

**Kept, with the reason.** Inside the plan, the card mirrors the week's own
stepback flag, not `_is_unload_week`. The review suggested the second, so
that taper weeks would show as unloads. But the card's load comes from the
phase's target, which in a taper is already reduced. A stepback cut on top
would cut it twice.

**Corrected above.**
- M4's before-count: 35 of 37 (F6).
- M5's size (F5).
- The G3 walk's reach: there is no button (F4).
- Part 4's fixed-plan refit: Sunday's long ride was cut too.
- Part 2's generate count: owner mode is 0 of 14.

**Recorded, for Step 8.** `/api/weekly-plan` ignores `week_offset`. The card
takes its Monday from today, so offsets 0, 1 and −1 all return this week
(F7, pre-existing).

### Step 5b — the sampler honours its science tables (SCI-1)

Choose the content CLASS by the phase's mix preference, the goal's emphasis and
the week's zone budget, then the file within it by novelty and quality. The
variety floors are in scope: they impose the same anaerobic and neuromuscular
sessions on every model, and in a threshold-model plan those displace the
middle-zone work the model is defined by (measured with the 48 h floor fix). *Verify:*
a base week's hard classes follow the base row; a vo2max goal draws more VO2 than
a general one with the bookkeeping on; the per-class minimums still fill.

### Step 6 — one week pipeline (R3)

Every entry point builds weeks through `TrainingWeek`; plan-level policies
(FTP test, phase floors, race/taper, re-entry) run once, in one order, as
proposals; `finish()` is the last write. Post-passes the owner subsumes are
deleted; the coherence pass's class-preserving rematch moves into the owner.
`_USE_TRAINING_WEEK` is deleted. One `is_immutable` used by every mutating
pass (DUP-23). Owner fixes D6, easy-floor taper exemption, opener placement
on an available day, deterministic `replan`, long ride sized inside
`_commit`.

Adaptations the planner makes (the fatigue easing, G3, the readiness
tier-down) become derived state with a record of the original. A rebuild
re-derives them instead of owning them. This covers the Step 5 review's M5,
the G3 walk, L4 and L6.

*Verify:* STRICT_SEAL over every gate case → 0 trips; invariants clean
across all entry points; test_event_and_goal_focus 10/10.

### Step 7 — availability as a ceiling, one plan store (R1 HTTP-1, R4)

One derivation of per-date availability, consumed by the owner as day caps;
one load→mutate→save with a version check instead of 23 hand-rolled writers.
*Verify:* the HTTP-1 sandbox replay keeps the week at its target; the STA-2
concurrent repros keep both writers' updates.

### Step 8 — one week view for the HTTP layer (HTTP-2/3/4/7)

Home and calendar read the stored week and the served files;
`generate_weekly_plan` stops answering "what is planned". Response shapes
unchanged (backend only).

### Step 9 — runtime state and the non-planner findings

STA-4, 7, 8, 9, 11; DUP-24, 26, 15/16; the Pillow gap in the production venv.

## Deployment note

Production runs the live checkout at `~/Documents/cycling-stack/domestique`,
on `refactor/session-sizing`; the running process loaded `9f30cf93` at 16:24
on 2026-09-10. `cs-update` runs Sunday 03:59 with `TRACK=stable` and will move
that checkout to the newest stable tag. Nothing from this branch is deployed;
that is the owner's decision.
