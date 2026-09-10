# Adversarial review — one concept, many implementations (branch head ca6091f9)

Lens: where one concept is implemented more than once, and whether the copies DISAGREE
when run on the same input. Every number below was produced by a probe in this directory, run at
ca6091f9 in the scratch worktree. `_USE_TRAINING_WEEK=False` (the production path) applies unless
a line says "owner". Each probe carries a self-test that plants the fault it looks for and must fire
before its "clean" is believed. An independent reviewer then re-ran the findings with its own riders,
seeds and code paths, and broke each probe's detector to confirm the assert trips
(`rv/review_of_dupes.md`). Its corrections are applied below and listed in §5.

Conventions: `R=/tmp/claude-1000/-home-aladjidi-Documents/bb02fc0a-851f-4ace-bcf4-5fc93d19911a/scratchpad/review`,
`WT=.../scratchpad/rev-dupes`. Every run is
`cd $WT && WT=$WT DOMESTIQUE_HOME=$R/dhome PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 .venv/bin/python $R/<probe>.py`.
The scratch `DOMESTIQUE_HOME` keeps the planner away from `~/.domestique`, and the sandbox has no
ride archive. "Today" is 2026-09-10, a Thursday, so every entry point opens with a Thu–Sun stub week.
Tp = `src/training_planner.py`, WP = `src/week_plan.py`, PI = `src/plan_invariants.py`.

| ID | severity | concept with more than one implementation | live disagreement? |
|---|---|---|---|
| DUP-1 | critical | the plan's weekly load ceiling and target CTL | yes: 17–18 of 22 weeks over the ACWR-safe load after regenerate/recalculate |
| DUP-2 | high | "what is left to ride this week" | yes: 192 / 222 / 0 TSS for the same stub week |
| DUP-3 | high | which weeks are unload weeks | yes: up to 6 consecutive load weeks after regenerate |
| DUP-4 | high | the week builder's inputs (24-arg threading) | yes: refit re-samples fixed plans and adds hard days |
| DUP-6 | high | "is this session hard" | narrow live (tier-down membership); spacing agrees today |
| DUP-14 | medium | phase targets shown vs built | yes: preview shows 1.8× the plan Generate builds |
| DUP-5 | medium | Goal (de)serialisation | yes in the code; latent through the UI |
| DUP-7…13 | medium–low | content class, easing load, zone families, day caps, taper start, tables, clamps | see each |

| DUP-22 | high | plan dict → PlannedSession (two readers) | yes: reforecast re-downgrades the same session every sync |
| DUP-23 | high | "the athlete owns this session" (six predicates) | yes: a dragged or dismissed session is rewritten by reforecast |
| DUP-24 | high | `compute_xss_components` signature (caller vs module vs test double) | yes: xSS never computed from any FIT; tests green |
| DUP-15/16 | high | ZWO scanner ×2 + committed index cache | copies agree today, but both guard tests are vacuous; the index serves stale Z% (≤2.8 pts) where mtimes match |
| DUP-17/18/19/25/26 | medium | classifier zones, ride TSS duration, `TSS_PER_HOUR`, serializers/undo stash, FIT stream walkers | yes, each measured |
| DUP-20/21/27 | low / out of lens | sub-30 s NP, facts IF, dead live engine, IF units, rematch feed, reforecast Goal, phase reader | see each |

---

## 1. Findings, most severe first

### DUP-1 — CRITICAL — Each entry point derives the plan's weekly load ceiling differently; regenerate and recalculate lose the ACWR safety cap and replace the goal's target rule

**One derivation, five callers fed different inputs.** `generate_phases` Tp:3033 is the only place
phase targets are computed. Its target rule is at Tp:3062-3081: an explicit `target_ctl` wins, then
the event table, then per-goal-type rules with the ramp capped at 12 weeks and ceilings of 90
(ftp), 85 (vo2max) and 95 (hybrid). Its load cap at Tp:3112-3117 is `recent_weekly_tss × 1.3` when
it is given, else `hours_per_week × 65`. Its five callers:
- `generate_plan` Tp:8328 — passes the recent load (fetched at Tp:8303-8320: archive, else CTL×7)
- `regenerate_from_today` Tp:12700 — passes none; the function has no such parameter
- `recalculate_plan` Tp:13369 — passes none
- the phase preview, app.py:11396 — passes none (DUP-14)
- the entry-scan hypothesis, Tp:3634 — passes none (effect not measured)

**Regenerate also overrides the output.** Tp:12631-12636 set
`original_target = target_ctl_for_event(...) if goal.goal_type == "event" else None` and
`adjusted_target = … else max_achievable`, and Tp:12659 writes the result into
`adjusted_goal.target_ctl`. For every non-event goal this discards `generate_phases`' per-type rule,
and it discards an explicit `target_ctl` as well.

**The late fetch cannot repair it.** Regenerate does fetch the recent load, at Tp:12987-12995, but
only after `generate_phases` has run, and feeds it only to `_enforce_weekly_volume_ceiling`
(Tp:12997, 13065). That pass starts its trim ceiling at `tss_target` and only ever *raises* it to
recent×1.3 (Tp:9811, 9848-9859), so against an uncapped target it caps nothing. Recalculate has
no volume-ceiling pass at all: the only call sites are Tp:8795, 8921, 12997 and 13065. The recent
load itself is fetched at five sites: app.py:11843, app.py:12621 (regenerate wrapper, used only for
the drift chip), Tp:8309, Tp:12991 (too late) and Tp:13825 (extend). Recalculate fetches it nowhere.
The two cap-rule copies at Tp:2993-2996 and Tp:3112-3117 agree. Tp:9811 is a different,
raise-only rule, not a third copy.

**Evidence, in prescribed load.** `probe_week_load.py` part 2: event goal, CTL 55, 10 h/wk, recent
load 250 TSS/wk passed to generate. Its self-test shows the gap is the ACWR ceiling and nothing else.
```
PRESCRIBED generate   : peak full week 327 TSS; weeks over 1.05 x ACWR ceiling (341): 0/22
PRESCRIBED regenerate : peak full week 501 TSS; weeks over 1.05 x ACWR ceiling (341): 17/22
PRESCRIBED recalculate: peak full week 466 TSS; weeks over 1.05 x ACWR ceiling (341): 18/22
(labels: tss_target peak 325 vs 595 vs 595)
```
With the archive patched to return 250 on every path, as production gets it, regenerate prescribes
446 rather than 501 (`rv/dup1_followup.py` A, rerun: 327 / 446 / 466; 0, 17 and 18 of 22 weeks over).
A second rider in `rv/dup1_acwr.py` (reviewer-run) prescribes +19% and +46%, with 14 and 12 of 27
weeks over. Its self-test shows that injecting the recent load into regenerate's `generate_phases`
call closes the gap. **The earlier draft's "+83%" and "2.1×" compared `tss_target` labels.** The
prescribed peak is +36% to +53% on regenerate and +43% on recalculate. The label still matters,
because `scale_budget_to_week` sizes the intensity budget from it.

**The non-event target rule is lost for every goal the UI can create.** `rv/dup1_followup.py` B,
rerun, with `target_ctl=None` as the UI sends it:
```
ftp      generate phase peak 610 (~CTL 87) | regenerate phase peak 910 adjusted_target_ctl=134 | peak prescribed gen 538 regen 617
ctl      generate phase peak 610 (~CTL 87) | regenerate phase peak 910 adjusted_target_ctl=134 | peak prescribed gen 525 regen 575
vo2max   generate phase peak 595 (~CTL 85) | regenerate phase peak 910 adjusted_target_ctl=134 | peak prescribed gen 543 regen 603
```
An explicit target is ignored too (latent: the UI never sends one; see DUP-5). In
`probe_ctl_regen.py`, regenerate's peak `tss_target` is 885 with `target_ctl` 60 and 885 with 90.
Generate gives 420 and 630. The prescribed peaks are 473 (generate) and 522 (regenerate).

**Reachability, with its gates.** Automatic regenerate runs `_maybe_auto_reforecast` (app:12084) →
`_apply_plan_update` (app:12892) → `_regenerate_plan_dict` (app:13035) → `tp.regenerate_from_today`
(app:12568). It fires only when `needs_regen_now = ctl_gap > 15 or ≥2 consecutive missed weeks`
(app:12993), outside the taper window (app:13003), once per episode. That means after an absence,
the classic ACWR-spike situation, and exactly when the cap matters. Automatic recalculate runs from
`GET /api/plan/auto-recalc` on dashboard load. It needs more than 7 days since the last one, and it
rebuilds only when readiness deviates by more than 8% (Tp:13266).

**STRUCTURAL.** `generate_phases` is already the single derivation, but its inputs are assembled
five ways and one caller overwrites its answer. **Home:** `generate_phases`, with one athlete-state
input (CTL, recent weekly TSS, goal) that every caller must supply. Regenerate's post-recovery ramp
should enter as a ceiling on the target, not replace the target rule.

---

### DUP-2 — HIGH — "What is left to ride this week" has three incompatible derivations (192, 222 or 0 TSS for the same stub week)

**Sites**
- legacy generate Tp:8477-8479 counts rides from the Monday. Tp:8506 then computes
  `_net_target = pw.tss_target - _done_tss` on the **un-prorated** 7-day target. After that
  `_clip_week_to_phase` (Tp:8175) prorates the stub week, 372 → 213, and Tp:8511 subtracts the
  ridden **zone minutes a second time**, from a budget already sized to the net TSS.
- legacy regenerate Tp:12812-12816 counts `completed_tss` from the cursor, so Mon–Wed rides are
  invisible. Tp:12848-12852 sizes the budget on the GROSS `pw.tss_target`, with `spent_zones` from
  `pw.start`.
- recalculate Tp:13446 (no `completed_tss`) and Tp:13510-13514 (gross); extend Tp:13910/13952 and
  refit Tp:14292 (gross, no `spent_zones`).
- owner WP:290-317: `ceiling` = clipped `tss_target` minus rides from the Monday. WP:553
  `plan_week(completed_tss=…)` and WP:579 `spent_zones` both count from `ctx.start`, so the
  owner uses two windows inside one object.
- auditor PI:45-54 grades against `net_tss_target`, which the legacy path never stamps (`None`
  → the clipped gross).

**Evidence** — `probe_week_load.py` part 1 (continuous goal, week target 372, 255 TSS ridden Mon–Wed):
```
owner=False generate   : tss_target=213 net=None prescribed=192 TSS, 1 hard (Sun vo2max 50m)
owner=False regenerate : tss_target=213 net=None prescribed=222 TSS, 2 hard (Thu threshold 85m 114, Sat tempo 60m)
owner=True  generate   : tss_target=213 net=0.0  prescribed=0 TSS
```
`probe_budget_sites.py` records each `scale_budget_to_week` call. Its self-test shows the recorder
sees `spent_zones` change when rides are added:
```
generate    generate_plan          target 117  spent 155/0/35/20 -> hit (1,1) mins 0/20/0/0
regenerate  regenerate_from_today  target 372  spent 0/0/0/0     -> hit (2,3) mins 344/52/26/13
owner       plan                   target   0                    -> hit (0,0)
```
In the reviewer's run (`rv/dup2_remainder.py`, another rider), 220 TSS ridden leaves generate at
141 and regenerate at 176, and the owner at 0. With 70 TSS ridden the generate budget is 245 against
a prorated stub of 180. `rv/dup2_audit.py` shows `plan_invariants` passes every one of these weeks.
Its self-test, the same stub week with every session's TSS tripled, is flagged.

**What the code intends is itself duplicated.** The legacy comment (Tp:8465-8476) nets the
whole week's rides against the week. The owner's comments (WP:555-560 "clip and prorate BEFORE
anything is sized"; WP:291-317 rides before the cursor are "part of the load the athlete is
carrying into it") intend prorate-then-net. That order double counts: proration has already removed
Mon–Wed's share, and the Mon–Wed rides are subtracted again, which gives 0 here. The legacy order
overfills the stub when rides are light (245 against 180). No path implements a rule that is right
for both, for example `min(clipped, gross − week_ridden)`, which gives 95 heavy, 180 light and
117 in this case.

**Consequence** — legacy regenerate prescribes 222 on top of 255 already ridden: 477 against a
372 weekly target (+28%), including an 85-minute threshold session, and the auditor passes it.
Scope is limited: only the opening stub week of a plan generated or regenerated mid-week after rides
exist that week. The owner's 0 is off the production path. The zone double-subtraction empties the
easy-minute budget, but what is served depends on how the sampler fills: the reviewer's rider got four
easy sessions. **STRUCTURAL.** **Home:** `TrainingWeek.ceiling`, as the only derivation, reading the
clipped `PlannedWeek` and one ridden window, with `net_tss_target` always stamped. This is a behaviour
change, not a relocation. Step 2 of the cleanup plan would import the owner's double count.

---

### DUP-3 — HIGH — Unload weeks are counted five different ways; regenerate moves them and gives up to six load weeks in a row

**Sites** — generate Tp:8420-8421 counts `global_week % 4` across phases. Regenerate
(Tp:12774-12775) and recalculate (Tp:13437-13438) count `phase_week % 4`, reset every phase.
Extend (Tp:13905) counts `week_num % 4`, with no taper exemption. `_entry_week_targets` (Tp:3509)
mirrors generate. `generate_weekly_plan` (Tp:15240-15243) counts weeks since plan start, else the
ISO week number % 4. Refit reads the stored flag. The factor 0.72 is a bare literal in seven places:
Tp:3512, 3701, 7187, 7950, 8092, 15246 and WP:310.

**Evidence** — `probe_stepback.py` (event goal, target 2027-02-21; its self-test reports a single
flipped week):
```
week start   generate           regenerate          recalculate
2026-11-23   build1 UNLOAD 252  build1 load 416     build1 load 416   <== differs
2026-12-07   build1 load 350    build1 UNLOAD 300   build1 load 416   <== differs
2027-01-18   peak UNLOAD 360    peak load 595       peak load 595     <== differs
2027-02-01   peak load 500      peak UNLOAD 428     peak load 595     <== differs
differ generate vs regenerate: 6 of 25 weeks; generate vs recalculate: 10 of 25
```
`rv/dup3_stepback.py` (reviewer's rider, rerun) gives phases identical in generate and regenerate,
so the counter alone causes the difference:
```
stepback differs gen/regen: 7 of 32, gen/recalc: 9 of 32
generate max consecutive load weeks = 3 ; regenerate = 6 ; recalculate = 6
```
**Consequence** — the 3:1 loading rhythm, the plan's recovery mechanism, depends on which button
was pressed. The longest run is (phase length mod 4) + 3: five weeks for my rider, six for the
reviewer's. Recalculate also shifts every phase boundary by a week. **STRUCTURAL.** **Home:** one
predicate in Tp over a plan-wide week index that regenerate and recalculate continue rather than
restart, with the factor as a named constant beside `STEP_BACK_EVERY`.

---

### DUP-4 — HIGH — The builder's 24 arguments are reassembled at six call sites; refit rebuilds the week differently, and re-samples fixed plans

**Sites** — `sample_week_workouts` Tp:7023 is called at Tp:8546, 12883, 13540, 13970, 14295 and
WP:609. `expand_blueprint_week` Tp:6951 is called at Tp:8534, 12872, 13529, 13959 and WP:597.
`plan_week` Tp:3671 has seven call sites, including app.py:10595 (`_advance_continuous_deload`,
with an `is_stepback=True` literal and no `completed_tss`).

**Evidence** — `ast_threading.py` maps every argument to its source expression at every call site
(its self-test reports a planted differing `seed_salt`). Eighteen of the 24 parameters are spelled
differently somewhere, mostly `x` versus `st.x`. **Six differ in meaning:**
```
phase            Phase(name=week.phase, …, weeks=1, focus='', session_types=[]) [refit] | phase | ctx.phase
week_in_phase    0 [refit] | week_num - 1 [extend] | week_in_phase [gen, recalc, regen] | ctx.week_in_phase
plan_total_weeks len(current_plan_weeks) [refit] | CONTINUOUS_HORIZON_WEEKS [extend] | three per-function locals
goal_type        getattr(goal,'goal_type','general') | 'continuous' [extend]
emphasis_profile <default None> [refit] | _emph [4 sites] | ctx.emphasis_profile
block_focus      _block_focus_for(week.phase, goal, week.is_stepback) [refit] | None [extend] | block_focus
scale_budget_to_week week_tss_target: 5 distinct expressions; spent_zones: 5 distinct windows (DUP-2)
```
Runtime: `probe_builder_args.py` spies on both builders; its self-test records focus "ftp" and "vo2"
as different emphasis. `rv/dup4_refit.py` (reviewer's, rerun) shows refit takes the sampler where
every other entry point takes the blueprint:
```
EVENT goal, plan_mode='fixed_core'   generate/regenerate/recalculate: blueprint ; refit: sampler, week_in_phase 0, focus '', session_types ()
  missed 09-16 ->  Thu 09-17  z2 75 endurance_steady_68pct_80min      -> overunder 62 over_under_3x5min_80pct_62min
                   Sat 09-19  long_z2 160 endurance_steady_74pct_170min -> z2 58
                   Sun 09-20  long_z2 160 endurance_10x30s-1min_95pct   -> sweetspot 120 over_under_4x3min_90pct_114min
CONTINUOUS goal, focus='ftp'         generate/regenerate emphasis ['ftp'] ; refit emphasis ['None']
```
**Consequence** — refit runs automatically after a missed hard session (app.py:13074 →
`_apply_refit_to_plan`). In a fixed_core week, one missed hard day loses both long rides and gains
two hard sessions. That breaks FS1's "a fixed plan stays fixed", which regenerate, recalculate and
extend all honour (`_bp_mode` at Tp:12868, 13525, 13886). `refit_remaining_week` never mentions
`plan_mode`. Refit also drops the focus emphasis, always samples as week 0, and builds a `Phase` by
hand because `PlannedWeek.phase` is only a string. This is the drift mechanism the notes describe,
now measured. **STRUCTURAL.** **Home:** `WeekContext` + `PlanState`. Both exist, and `WeekContext`
already carries `emphasis_profile`, `block_focus`, `plan_mode` and `week_in_phase`. The
blueprint/sampler branch should be taken once, in `TrainingWeek.plan` (WP:593).

---

### DUP-6 — HIGH — Five definitions of "hard session"; the owner and the auditor share one blind copy

**Sites** — the sets differ:
- `HARD_TYPES` at PI:21 and WP:46: the same 9 types
- `_HIT_SESSION_TYPES` Tp:9107 with `_session_is_hit` Tp:9119: type OR served content
- `_HARD_SESSION_TYPES` Tp:10991: includes tempo, excludes ftp_test
- local `hard_types` Tp:14636: includes tempo and sprint, excludes double_threshold and ftp_test
- local `hard_types` app.py:10211: excludes sprint
- `_sess_is_hard` app.py:14195: a hand mirror of `_session_is_hit` on dicts
- `_ease_for_recovery` Tp:707: `_HIT_SESSION_TYPES ∪ {tempo, sweetspot}`
- `_HIT_SLOT_CONTENT_CLASSES` Tp:5528

The 48 h rule is checked at five places: Tp:6416, 9210 and 10491 use the union definition; the
check inside `_commit` (WP:369, def WP:326) and PI:65 use type only.

**Evidence** — `probe_content_class.py` [5] (abridged):
```
                     anaerobic double_th ftp_test hill_rep race sprint tempo threshold vo2max
week_plan.HARD_TYPES     X         .        X        X      X     X      .      X        X
tp._HIT_SESSION_TYPES    .         X        X        .      .     X      .      X        X
tp._HARD_SESSION_TYPES   .         X        .        .      .     X      X      X        X
week_plan.HARD_TYPES members the planner never emits: ['anaerobic', 'hill_repeats', 'race']
```
`probe_hard_defs.py` runs 20 riders × 5 entry points. Its self-test: the type-only detectors fire on a
typed back-to-back pair and stay silent on a z2 slot serving a VO2 file, while the union detector
fires on both.
```
definitions disagree on 36 of 276 sessions (legacy), 38 of 307 (owner)
   ftp_test: owner=hard hit=hard reforecast=easy  x20 ;  tempo+sweet_spot file: owner=easy hit=hard reforecast=hard …
48 h breaches: 0 under the owner and union definitions, every entry point, both modes
```
`probe_content_class.py` [3]: in 520 of 4,307 library files, "the slot type is hard" and "the content
is hard" disagree.

**Consequence** — the live part is narrow. An FTP test is exempt from what `/api/plan/auto-adjust`
eases (`api_plan_auto_adjust` app.py:4183 → `apply_week_tier_down` :4408, own check :4341), from
`reforecast`'s TSB downshift (Tp:11646, 11720, 11804) and from `adjust_today_session` (Tp:15599,
15638). Tempo is tiered down by all of those but exempt from the 48 h rule. Spacing outputs agree
today, which is latent risk. The WP:42-45 comment says the set is "duplicated deliberately" so that
"a silent divergence surfaces as a failing audit". That did not hold: the two copies are identical
(`PI.HARD_TYPES == WP.HARD_TYPES`), so they share the blind spot the self-test shows. The divergence
that matters, with `_session_is_hit`, is checked by neither. **STRUCTURAL.** **Home:** one
`is_hard(session)` predicate (type OR served content) owned by `week_plan`. The auditor's
independence should come from what it reads, the served file, not from a copied set.

---

### DUP-14 — MEDIUM — The phase preview shows about 1.8× the load the Generate button then builds

app.py:11396 calls `tp.generate_phases(g, current_ctl)` with a five-field Goal and no recent load.
dashboard.html:12916 renders the result as `${p.weekly_tss} TSS/wk`. `rv/dup1_followup.py` C
(rerun), for the DUP-1 rider:
```
preview  TSS/wk: base 404, build1 416, build2 506, peak 595, taper 357
generate TSS/wk: base 227, build1 227, build2 276, peak 325, taper 195
```
The athlete is shown one plan and handed another. This is the same missing input as DUP-1, at a
fourth caller. **Home:** `generate_phases` with the shared athlete state.

---

### DUP-5 — MEDIUM — Goal is rebuilt by hand in 10 places, and the persisted goal cannot carry its own target (latent through the UI)

**Sites** — the writer at app.py:11884-11912 (`plan_dict["goal"]`) persists no `target_ctl`,
`target_ftp`, `target_distance_km`, `target_duration_h` or `target_weight_kg`. The reader
`_goal_from_plan_dict` (app.py:12706, Goal built at :12748) does not read them either, and it is used
by regenerate (app:12492), refit (app:12844), auto-recalculate (app:17327) and the continuous deload
(app:10555). There are field-by-field copies at Tp:12651 (regenerate `adjusted_goal`) and Tp:13297
(recalculate). Both drop `vo2_microintervals_only`, `longest_ride_h_90d` and `last_ftp_test_date`.

**Evidence** — `ast_goal_rebuild.py` lists the ten rebuilds (its self-test reports a planted missing
field). Two copy an existing Goal (Tp:12651, 13297); four are small preview or projection goals.
`rv/dup5_roundtrip.py` reads the real writer and reader with the AST, and both miss the same five
fields. `probe_misc.py` M1: a restored `target_ctl` of 60 becomes `None`, and the phase peak moves
from 420 to 671.

**Consequence — latent.** The UI never sends any `target_*` field (no match in `templates/` or
`static/`; dashboard.html:2377 and 12680 say "No target needed"). Only an API client is affected
today, and regenerate ignores `target_ctl` anyway (DUP-1). Dropping `longest_ride_h_90d` is inert:
its only planner reader, `_event_demand_targets` (Tp:2731), is called with the original goal
(Tp:12627, 13345). `vo2_microintervals_only` survives regenerate only through a module global (trap 3).
**STRUCTURAL.** **Home:** `Goal`, with one `to_dict`/`from_dict` pair and
`dataclasses.replace(goal, target_ctl=…)` for the adjusted copies.

---

### DUP-7 — MEDIUM — "What kind of workout is this file": three resolvers, four filename-prefix tables; the ICU push reads the classification file at the wrong nesting level

**Sites**
- the three resolvers:
  - `_content_class_for_row` Tp:5655: ContentClass, else the filename
  - `_content_class_for_zwo` Tp:10977: cache `primary`, with no fallback
  - `_session_type_from_row` Tp:6757: filename first
- the prefix tables: all three resolvers, plus `_SESSION_TYPE_PREFIXES` Tp:494 and
  `_classify_protocol` Tp:4253 (prefix chain at 4290). They disagree: `overunder_` appears only in
  the first, `pyramid_` only in the last.
- the readers of `.content_classification.json`:
  - `_load_content_classifications` Tp:226 returns `payload["classifications"]`
  - `icu_calendar_push._load_classifications` :171 returns the whole payload
  - app.py:8250-8256 handles both shapes

**Evidence** — `probe_content_class.py`:
```
[1] _content_class_for_row != _content_class_for_zwo: 73 files  (row='endurance' zwo='' n=49 …)
[4] push _display_name -> 'ZWO_NAME_FALLBACK'; cache display_name -> 'Anaerobic 64min — 30×15s/45s @ 121%';
    push handed the nested dict (control) -> 'Anaerobic 64min — 30×15s/45s @ 121%'
```
**Consequence** — live but low stakes: every event pushed to the ICU calendar (`icu_calendar_push`:292,
used at :227) loses the classifier's display name. Latent: `_session_is_hit`'s content axis is blank
for 73 files. **Home:** `_load_content_classifications` and `_content_class_for_row` in Tp.

### DUP-8 — MEDIUM — Easing a hard session: two load formulas, and the owner's overstates by up to 59%

`TrainingWeek._demote` (WP:494-507) keeps `tss × 0.8` and the duration. `_ease_for_recovery`
(Tp:707) with `_deescalated_load` (Tp:720) prices the ride from `TSS_PER_HOUR[z2]` and never raises
it. From `probe_ease_clamp.py`: vo2max 60m/75 → 60 vs 45; threshold 90m/135 → 108 vs 68;
over-under 75m/106 → 84.8 vs 56.

Inside the owner, a demoted session is charged 108 TSS against the ceiling for a ride `TSS_PER_HOUR`
prices at 68. Slots committed after it are shrunk to fit (WP:402-418) until the rematch replaces the
estimate. **Home:** `_deescalated_load`.

### DUP-9 — MEDIUM — Zone "families": the same label means different Coggan zones in two app features

`zones.THREE_ZONE_FROM_COGGAN` (zones.py:74) puts Z5 in the hard band. `app._exposure_split_from_tiz`
(app.py:9695, fold at 9726-9732) calls Z5 "anaerobic". `app._continuous_family_deficits`
(app.py:10389, fold at 10424-10444), following `continuous_policy.py`:33-36, calls it "high_aerobic"
and keeps anaerobic for Z6+. `probe_misc.py` M2, one ride of 30 min Z2 plus 10 min Z5:
```
exposure bars {'low_aerobic': 30.0, 'mid_aerobic': 0.0, 'high_aerobic': 0.0, 'anaerobic': 10.0}
family deficit {'low_aerobic': 30, 'high_aerobic': 10, 'anaerobic': 0}
```
The planner, `_completed_zones_in` and analytics all agree on the three-band fold. **Home:** a
family fold beside `zones.three_zone`, defined once from `continuous_policy`'s declared bands.

### DUP-10 — MEDIUM — The daily cap rule has five copies; the auditor's partial copy is blind to the default caps

`Goal.max_hours_for_day` (Tp:1756) is bypassed by four copies: `WP._day_cap_min` :463, the sampler's
`_max_min_for` Tp:7128, `expand_blueprint_week._cap_min` Tp:6958 and `week_available_minutes`
Tp:2397. `PI.check_daily_duration_cap` :114-128 reads only the per-day dict.

`probe_misc.py` M3: a 150-minute Tuesday session against `max_weekday_hours=1.0` gets a 60-minute cap
from every copy, but the auditor returns `[]`. The control, the same cap given per day, is flagged.
`probe_entry_point_parity` always sets the per-day dict, so it never exercises this path.

Empty `available_days` means "all days" in WP, PI and `week_available_minutes`, and "no days" in
`plan_week`, the sampler and the blueprint. That input is degenerate, so this part is latent.
**Home:** `Goal.max_hours_for_day`.

### DUP-11 — LOW — Taper start: the "one owner" is dead, and its docstring cites a test that asserts the opposite

`_taper_anchor` (Tp:1067) has no caller in src/ or tests/. Its docstring promises a Monday start "…
tests/test_week_anchoring.py checks all seven weekdays". The test at :63 is
`test_the_taper_is_deliberately_not_on_the_monday_grid`. In `probe_stepback.py` the taper starts on
Tuesday 2027-02-09 and leaves a one-day "peak" week (71 TSS).

### DUP-12 — LOW — An orphan intensity table, and comments naming tables that do not exist

`PHASE_TARGETS` (Tp:1947) is read by no module in src; only tests/test_planner_injury_gates.py:66
reads it. Comments at Tp:2161 and 2381, and REFACTOR-PLAN, name `PHASE_TID_TARGETS` or
`PHASE_POLARIZED_TARGETS` as the single source, and neither exists. The live sources are
`PHASE_TID_DOSE` Tp:2052, `tid_target_pct` Tp:2106 and `_PHASE_SHAPE` Tp:2163, via `BUDGETS_BY_MODEL`.

### DUP-13 — LOW — A length change drops the file in `_rescale` but keeps it in `_clamp_to_limits`

The two methods sit in the same class (WP:473-488, WP:688-706). The effect is measured and rare
(`probe_ease_clamp.py` part 2; its self-test counts a planted case). A file more than 10 minutes
longer than its prescription appears on 1 of 307 owner sessions and 2 of 276 legacy ones.

---

### NP / IF / TSS and the ZWO scanner (sub-investigation)

A sub-agent built these findings, and an independent reviewer of its own re-ran them. The full
report, with a census of 24 NP/IF/TSS/zone copies (A–X, every file:line), is at
`$R/np/dupes_np.md`; its scripts are in `$R/np/`. **Re-verified here by rerun:** DUP-15, DUP-16 and
the numbers under DUP-20 (F5 and F6). The rest is carried with the sub-agent's evidence. Library
scans ran on a copy of the 4,307 `.zwo` files, because the loaders write caches into `WORKOUT_DIR`.

#### DUP-15 — HIGH — The duplicated ZWO scanner's two guard tests are vacuous

The planner's inline scanner (Tp:4446-4590) and the app's `_scan_zwo_for_library`
(app.py:5380-5488) both turn a `.zwo` into IF, TSS and zone %. Each carries its own `_np_fraction_from_samples`
(Tp:4347, app.py:5359) and its own zone edges (Tp:4486-4494, app.py:5414-5421), and neither imports
`zones.py`. Tp:6104-6106 and app.py:5443-5445 cite `tests/test_zone_binning.py` as the guard.
- `test_the_two_scanners_produce_identical_numbers` (:146-162) compares keys `Z1%…Z6%`, `TSS`,
  `IF` and `Duration(min)`. The app scanner returns `if_val`, `tss`, `total_sec` and `z*_sec`
  (app.py:5480-5488), so every lookup is `None`, the loop `continue`s, and **zero values are
  compared**. Its 0.05 tolerance would also pass an IF error of +0.045.
- `test_the_planner_and_app_scanners_use_the_same_edges` (:93-101) builds the planner's source
  into `p` and then asserts only on the app's source `a`.

```
$ np/zone_test_plant.py          keys the test compares that the scan actually returns: []   RESULT: PASS
$ np/zone_test_plant.py --plant  (app NP x1.5, +600 s every file) if_val 0.8696 -> 1.3044 ...  RESULT: PASS
```
Today the copies agree: `np/scan_compare.py` finds 0 of 4,307 files differing, and its planted app
NP ×1.01 is caught on every file. So the consequence is latent risk with no working guard. The
cleanup plan's first step ("dedupe `_np_fraction_from_samples`, or a test that fails when they
diverge") assumes a test that does not exist in working form. **Home:** one per-file scanner in
`workout_facts.py` (a leaf training_planner already imports), called by both `load_workout_library`
and `app._build_library_rows`.

#### DUP-16 — HIGH (structural; per-file effect ≤ 2.8 points) — The committed `.library_index.json` is a stale copy of the scanner, served verbatim whenever its header matches

`_read_library_index` (Tp:290-331) validates on `schema_version`, `classifier_version`, `count` and
`max_mtime` (Tp:319-324). There is no parser version, so a change to how files are binned never
invalidates an index. The index was last committed on 2026-09-02 (`56e276f2`), before the 1-second
ramp-binning change (`7f2eefdc`, 2026-09-10).
```
$ np/stale_index_demo.py   (library copy with mtimes set to match the committed header)
planner rows=4307; index served verbatim (no re-parse): True
files where planner Z% (served index) != app scanner Z%: 3337/4307
  tempo_progression_9x30s_130pct_18min.zwo (planner, app): Z2% (0.7, 3.5)  Z3% (33.4, 30.7)
```
Wherever mtimes match the header (the machine that built it, or any mtime-preserving copy or package;
not checked against production), the planner's zone shares disagree with the library browser, the
classifier and the tests on 77% of the library. One file also carries a stale Score. On a fresh
checkout the header never matches, so the index is rebuilt and written back into `src/workouts`. That
is why the scratch tree here shows `M src/workouts/.library_index.json` after the probes ran.
**Home:** a cache keyed on the DUP-15 scanner's version or source hash, or not committed at all.

#### DUP-17 — MEDIUM — A third zone-% copy (the classifier's features) is read on the plan path

`_features_for_row` (Tp:5895-5911) feeds `variety_score`, which ranks candidates in the phase
class-floor swap (Tp:9375). It reads `classify_library_content.py`'s own 7-zone `ZONES_FTP` (:118-126),
which leaves FreeRide out of the denominator. From `np/scan_compare.py`: row versus classifier zone %
differ on 3,274 of 4,234 files, by up to 65 points. For example, `threshold_2x5x20s_150pct_47min.zwo`
has Z2% 65.0 in its row and 0.0 in the classifier. The committed features are stale for 3,132 files
(`np/cls_fresh.py`), and 73 files have no features. The effect is limited to tie-breaks. **Home:**
zone edges in `zones.py`, and `_features_for_row` reading the row's Z%.

#### DUP-18 — MEDIUM — Ride TSS mixes an NP over the recorded samples with the timer's duration

`compute_power_tss` (ride_storage.py:1036-1058) computes NP over the FIT records as they are (no 1 Hz
resampling), but takes the duration from `total_timer_time`, falling back to the record count
(fit_activity.py:372-397). Rerun of `np/np_synth.py`, same 3,600 s 4×4 session:
```
planned 78.80 | ride 1 Hz, timer 3600: 78.8 | smart recording 1 rec/4 s, timer 3600: 75.1 | same, no timer: 18.8
600 s stop recorded at 0 W: timer 3600 -> 72.8 ; no timer (dur = 4200 records) -> 84.9
```
A smart-recording ride feeds ATL/CTL about 5% below the plan's TSS for the same workout, and about
76% below without a timer field. The ride-detail record shows `tss=None` for a FIT with no embedded
TSS, while the load uses the sidecar's value (sub-agent's reviewer, through real FIT files).
**Home:** `ride_storage`, with NP on a 1 Hz resampled series (`_resample_series_1hz` exists) and one
duration definition.

#### DUP-19 — MEDIUM — Planned TSS has two models: `TSS_PER_HOUR` against what the library delivers

`TSS_PER_HOUR` (Tp:1204-1220) sizes every slot, through `_BAND_TSS_PER_HOUR`, `_deescalated_load` and
the match clamp at Tp:4818/4848-4863. Against the median library TSS per hour (`np/cls_fresh.py` (d)),
it runs 20% high for threshold (90 vs 71.5), sweet spot (80 vs 64.1) and over-unders (85 vs 71.6), and
low for sprint (57 vs 65.1) and recovery (30 vs 32.9). Threshold-type budgets therefore sit about 20%
above what the matched files deliver. That compounds DUP-8, whose "reference" pricing is itself this
table. The plan-level effect was not measured. **Home:** a per-type rate derived from the library rows.

#### DUP-20 — LOW — Smaller NP/IF copy disagreements

- **F5:** below 30 samples, three NP copies (Tp, app, classifier) return 0.0 while `ride_storage` returns
  the plain mean. From 30 samples up, all six copies agree to 4e-15 (rerun: `np/np_synth.py`).
- **F3:** workout-facts IF/TSS differ from the library row on 322 files (FreeRide and ramp-sampling
  rules). This contradicts workout_facts.py:66-67, but only a test-side audit reads those values.
- **F7:** the `MetricsEngine` "live" NP/IF/TSS engine (training_live.py:390-640) is constructed nowhere
  in src, yet ride_storage.py:1025-1031 cites it as the reference formula.
- **F8:** ICU IF is read as a fraction at Tp:14797 and app.py:1207-1208, and as a percent by
  ride_storage.py:272-280 (rerun: `{'icu_intensity': 72.3} -> anaerobic`).

#### DUP-21 — out of lens, side finding (sub-agent's reviewer; not re-verified here)

Activities from the database reach `classify_rematch` with `duration_min=0` and no IF (app.py:13943,
13958-13966; the `activities` table has `duration_sec`). Duration and IF band therefore can never pass,
and `done` is unreachable for such a ride. What `rematch_week` then does with `no_match` was not traced.

---

### Plan (de)serialisation and FIT decoding (sub-investigation)

A sub-agent built these findings, and an independent reviewer of its own confirmed every headline
claim. The full report has the converter census (S1–S4, D1–D4, G, G′: two serializers, two session
readers, seven hand-typed week readers, six dict→Goal builders) and the FIT census (nine decode sites).
It is at `$R/ser/dupes_ser_fit.md`; scripts are in `$R/ser/`, and every probe imports a network guard.
**Re-verified here:**
- by reading the code: DUP-22's cause (D1 has no `adapted`), DUP-23's guards and DUP-26's allow-list
- by a direct call: DUP-24
- by rerunning the reviewer's end-to-end script (`ser/reviewer/rE_tsb_ratchet.py`, output in
  `$R/out_verify_ser_rA.txt`):
  - DUP-22: the two readers differ on 13 fields from one dict, and the ratchet reproduces
    (shipped reader: 09-12 vo2max → threshold → overunder → sweetspot → tempo over four syncs;
    D1 plus `adapted` only: it stays vo2max, and sync 4 modifies nothing)
  - DUP-23: the dragged and the dismissed session are rewritten by the TSB loop, and their files
    cleared
  - DUP-25: after a real regenerate the revert restores nothing; with `pre_adapt` allow-listed it
    restores sprint/45
  - DUP-27: the reforecast Goal reads 2.0/3.5 h where the rider has 1.5 h every day

#### DUP-22 — HIGH — Two session readers disagree on 12 fields, and reforecast uses the lossy one: the same session is downgraded on every sync

`tp._plan_dict_to_planned_weeks` (D1, Tp:11976; session built at 12008-12030) is the only reader
`reforecast_dict` uses (Tp:12232). It never reads `adapted`, and it drops `nutrition_note`, `matched`,
`moved_from`, `completion_matches`, `hr_ceiling_pct`, the double-threshold pairing, `execution`,
`variation`, `adapted_reason`, `auto_moved` and `ftp_test_type`. `app._planned_session_from_json`
(D2, app.py:13841) carries all of them. G3 skips adapted sessions (Tp:11724), but D1 has just reset
`adapted` to False:
```
rA.py (reviewer), first future hard session saved with adapted=True, 4 syncs:
  shipped D1:         2026-09-12 vo2max -> threshold -> overunder -> sweetspot -> tempo
  D1 + adapted only:  2026-09-12 stays vo2max; each session drops at most once
```
`adapted=True` reaches a future day through tier-down (app.py:4083), `apply_week_tier_down`
(Tp:11100, via app.py:4408) and the reforecast write-back itself (Tp:12123). **Home:** one codec in
training_planner, `session_from_dict`/`session_to_dict` driven by `dataclasses.fields()` plus the extra
keys. D2 is then deleted.

#### DUP-23 — HIGH — "Does the athlete own this session?" is decided in at least six places, and they disagree

The six places:
- `week_plan.is_athlete_owned`/`is_immutable` (WP:107-138): `user_moved`, `adapted`, non-pending status, race
- `_refit_session_frozen` (Tp:14126): eight conditions, including `dismissed_at` and `completion_matches`
- `_demote_hit_window` (Tp:10271-10275): race, non-pending status, `dismissed_at`, `user_moved`
- the legacy splice loops (Tp:12906-12915, 13570-13575): adapted, user_moved, non-pending
- the reforecast TSB downshift (Tp:11644-11651): **race and `user_swapped` only**
- G3 (Tp:11722-11727): race, `adapted`, `user_swapped`

The sub-agent's `ser_probe.py` T4b, unpatched `reforecast_dict` at TSB −40:
```
[user_moved]  2026-09-12: vo2max/50min -> threshold/45min zwo='' | _refit_session_frozen owned=True
[dismissed]   2026-09-17: overunder/90min -> sweetspot/78min zwo='' status=dismissed | owned=True
```
A session the athlete dragged or dismissed is rewritten, and loses its workout file, on the next
reforecast (run on sync, Update plan, tier-down, accept-redraw and swap-type). The TSB loop has no
"already adapted" guard at all, so a persistent TSB below −25 ratchets the same session even with a
fixed codec (the reviewer's `rE_tsb_ratchet.py`). This is DUP-6's pattern for a different concept.
WP:126-138 already states the rule ("the planner may plan AROUND such a session … it must never
rewrite one"), and four of the other sites do not implement it. **Home:** `week_plan.is_immutable`,
called by every mutating pass, with an explicit rule for re-downshifting an adapted session.

#### DUP-24 — HIGH — xSS is never computed from a FIT: a keyword mismatch between two copies of one signature, hidden by test doubles

`ride_storage.py:1459-1461` calls `strain_score.compute_xss_components(power_series, cp=cp,
wprime_j=wprime_j, pmax=pmax)`. The real signature is `(power_trace, cp, w_prime, pmax, ...)`
(strain_score.py:176-179). The `TypeError` is caught, logged, and `{}` returned, on both callers:
FIT import (app.py:17818) and the 3D backfill (app.py:5090). Rerun here by direct call:
```
TypeError: compute_xss_components() got an unexpected keyword argument 'wprime_j'. Did you mean 'w_prime'?
w_prime= -> {'xss_total': 155.5, 'xss_cp': 136.25, ...}
```
The test doubles at tests/test_v136_3d_fitness_backfill.py:64 and tests/test_xss_per_ride.py:67 take
`wprime_j`, copying the wrong caller rather than the real module, so both files pass (5 and 6 tests).
No `xss_*` keys and no `ss_*_daily` rows come from FIT import or backfill. This is not on the path
that decides what the athlete rides, but a whole feature is silently dead while its tests are green.
**Home:** the real `strain_score` signature, and the tests should import it rather than mirror it.

#### DUP-25 — MEDIUM — Serialisation drops fields on first save and deletes the undo stash on every rebuild

- **The generate endpoint has its own serializer** (app.py:11951-11967 session, 11943 week, 11932
  phase). It writes 11 of the 27 session fields, where `_planned_session_to_json` (app.py:13862)
  writes all of them. So `nutrition_note`, `matched=False`, `hr_ceiling_pct`, the double-threshold
  pairing, `hit_allowance` and `net_tss_target` are lost at birth and would survive a later regenerate.
  The reviewer drove this through `POST /api/plan/generate` with the real planner.
- **`pre_adapt` is deleted.** It is written at app.py:10097 and read by the readiness revert at
  app.py:3935-3937, but it is not in `_PS_JSON_ONLY_KEYS` (app.py:13838). Every canonical rebuild
  (regenerate, auto-recalc) therefore deletes it. End to end (reviewer): `persist → revert` restores
  sprint/45; `persist → real _regenerate_plan_dict → revert` restores nothing; adding `pre_adapt` to
  the allow-list restores it.
- **Week fields the planner stamps are never persisted** (latent). `hit_allowance`, `net_tss_target`
  and `block_focus` read back as `-1`/`None`/`None` through all seven hand-typed week readers
  (app.py:10488, 10557, 12524, 12848, 12970, 13894, 17356). So the PlannedWeek comment that
  `net_tss_target` is "recorded so the auditor and the UI grade the week" is false for any plan read
  back from disk. It feeds into DUP-2's auditor blindness.

**Home:** the same codec, carrying unknown keys through by default instead of an allow-list.

#### DUP-26 — MEDIUM — The five FIT stream walkers disagree on pauses, missing power and sampling rate

Nine decode sites exist, and five walk the record stream, four of them in app.py:
- `fit_activity.parse_record_streams` :307
- `app._v136_extract_fit_power_series` :4942
- `app._parse_fit_stats` :17562
- `app._build_fit_samples` :19710
- `app._fit_power_series_1hz` :19993

On one fixture (1 Hz, a 300 s pause, 60 records without power), the load path collapses the pause,
the FTP-test and eFTP path holds for 3 s and then adds 297 zeros, and the ride chart erases the pause
from its time axis. The result is three average powers (225, 213.7, 171.3) and two NPs (235.3 vs
221.9). hrTSS (`compute_hr_tss`, ride_storage.py:1061) assumes 1 Hz, and `compute_fit_load` never
resamples, so a 2-second smart-recording HR-only ride scores 41.3 against 82.6 at 1 Hz. That affects
FIT-only rides, which `load_all_rides` uses when no intervals.icu copy exists (ride_storage.py:869, 957).
One ride-detail request decodes the same file five times. This is the same unresampled-series defect
as DUP-18, on the HR side. **Home:** `fit_activity.py`, with one `decode_activity(path)` walk and
`_resample_series_1hz` moved in as `to_1hz()`.

#### DUP-27 — LOW — Smaller serialisation copies

- **The reforecast Goal** (Tp:12209) rebuilds a Goal with default hours (2.0/3.5, no per-day dict), so
  `_mark_race_days` clamps an unmarked Saturday B race to **210 min / 192 TSS** instead of the
  rider's 90 min / 82 TSS. The value then sticks, because the write-back skips race days (Tp:12082).
  It is another instance of DUP-5.
- **The `/api/weekly-plan` phase reader** (app.py:9262) hard-codes `z2_pct=70` and `hit_per_week=2`.
- **Out of lens:** `reforecast_dict`'s `today_iso` parameter is never read, although four call sites
  pass it (app.py:4154, 4447, 12256, 13140).

---

## 2. Bypassed abstractions

| abstraction | reconstructed by hand at | evidence |
|---|---|---|
| `Phase` | refit builds `Phase(name=week.phase, weeks=1, focus="", session_types=[], …)` from budget fields (Tp:14296-14302) | ast_threading, rv/dup4_refit |
| `PlannedWeek.phase` is a `str` | consumers look tables up by name: `get_budget_for_phase(week.phase)` Tp:14287, `active_model_for_phase(week.phase)` 14294, `_block_focus_for(week.phase,…)` 14322 | read |
| `Goal` | 10 field-by-field rebuilds (DUP-5); `Goal.max_hours_for_day` bypassed by four copies (DUP-10) | ast_goal_rebuild, probe_misc |
| athlete state (CTL, recent load) | the recent load is fetched at five sites, and three of the five `generate_phases` callers go without it (DUP-1, DUP-14) | grep, probes |
| `WeekContext.emphasis_profile/block_focus` | generate computes the emphasis twice (Tp:8435 for the owner, 8519 for legacy) | read |
| `WeekContext.end` | the property `start + 6` (WP:260-262) is next Wednesday for a stub week, while the clipped `PlannedWeek.end` is Sunday; latent, since no future rides exist | read |
| `PlannedWeek.net_tss_target` | only the owner stamps it; the legacy path leaves `None` and the auditor re-derives (DUP-2) | probe_week_load |
| `PlannedSession` | app.py:14195 `_sess_is_hard` mirrors `_session_is_hit` on dicts | read |
| content classifications | `icu_calendar_push` re-reads the JSON instead of calling `_load_content_classifications` (DUP-7) | probe_content_class |

`x=goal.x` census (`ast_threading.py`; the reviewer's `rv/claims_counts.py` agrees): Tp has 68 keyword
arguments of the literal form `x=goal.x`, and 88 if `x=adjusted_goal.x` is included. WP has 10
of the form `x=ctx.goal.x`.

---

## 3. Claims checked

| claim (source) | verdict | evidence |
|---|---|---|
| "84 argument lines are literally x=goal.x" (cleanup plan §2, review) | **DID NOT HOLD**: 68 literal, 88 with `adjusted_goal`; none of six regex readings gives 84 | ast_threading, rv/claims_counts |
| "165 lines of threading across 9 call sites" | **HELD**: 124 + 41 in Tp | ast_threading |
| "24 parameters = 7 week + 6 athlete + 9 plan + 2 per-call" | **HELD as a count; the conclusion did not.** The "per-call" pair is already on `WeekContext` (WP:241-242), and the target signature differs between the plan and the review | read |
| "scale_budget_to_week is called from 4 places" (plan step 2) | **DID NOT HOLD**: 6 sites, with 5 distinct target/window combinations | ast_threading, probe_budget_sites |
| "Five sampler entry points" (REFACTOR-PLAN) | **HELD**: 5 entry points and 6 call sites; the sixth, WP:609, runs only with the flag on (corrected after review) | rv/claims_counts |
| Step 1 "changes no behaviour … characterize_planner must show 0 changed" | **the gate is blind to it.** The harness calls `plan_week`, `generate_plan` and `generate_weekly_plan` (:137), never regenerate, recalculate, extend or refit | grep |
| Tp:12984-12986 "the regen path runs the SAME volume ceiling as generate_plan" | **DID NOT HOLD in effect**: the same function, but a raise-only trim against an uncapped target (DUP-1) | read, rv/dup1_acwr |
| REFACTOR-PLAN "`hours_per_week × 65` … only binds for a rider with no ride history" | **DID NOT HOLD**: it binds on every regenerate and recalculate, history or not | probe_week_load, rv/dup1_followup |
| WP:42-45 "duplicated deliberately … a divergence should surface as a failing audit" | **DID NOT HOLD** (DUP-6) | probe_hard_defs self-test |
| "PHASE_POLARIZED_TARGETS is now the single source" | **DID NOT HOLD**: no such name (DUP-12) | grep |
| "THREE_ZONE_FROM_COGGAN is the single fold … planner, analytics, on-track" | **HELD for three bands**; app has two more folds that disagree (DUP-9) | probe_misc M2 |
| `_taper_anchor` "… tests/test_week_anchoring.py checks all seven weekdays" | **DID NOT HOLD** (DUP-11) | grep |
| Tp:8465-8476 "the Thursday generate … ridden from the Monday" | **HELD for legacy generate only**; regenerate counts from the cursor (DUP-2) | probe_week_load, rv/dup2_remainder |
| "a 40-rider sweep found back-to-back hard days in regenerate and nowhere else" | **not reproduced** at ca6091f9 (20 riders × 5 entry points, both modes, 0 breaches) | probe_hard_defs |
| `_USE_TRAINING_WEEK` gates generate and regenerate only | **HELD** (Tp:8423, 12776) | grep |

| cleanup plan §5: "`_np_fraction_from_samples` … the code bodies are identical" | **HELD**: 0 of 4,307 files differ between the two scanners, and all six NP copies agree for n ≥ 30 | np/scan_compare, np/np_synth (rerun) |
| cleanup plan §5 / app.py:5364: "must stay in lockstep … nothing checks it" | **HELD, and worse than stated**: a test claims to check it and compares zero values (DUP-15) | np/zone_test_plant (rerun), test source |
| REFACTOR-PLAN: "the FIT parser exists three times" | **DID NOT HOLD**: nine decode sites; five walk the ride stream, four of them in app.py (DUP-26) | ser census |
| Tp:4352-4359: NP "matches ride_storage.compute_power_tss windowing exactly" | **HELD for n ≥ 30; DID NOT HOLD below 30** (0.0 vs the plain mean) | np/np_synth (rerun) |
| workout_facts.py:66-67: "facts match the loaders and ride-side TSS" | **DID NOT HOLD**: 322 files differ | np/scan_compare |
| training_live.py:393-395: the FIT viewer uses `MetricsEngine`; ride_storage.py:1025-1031 cites it as the reference | **DID NOT HOLD**: constructed nowhere in src | grep |
| PlannedWeek comment Tp:1859-1863: `net_tss_target` "recorded so the auditor and the UI grade the week" | **DID NOT HOLD for any plan read back from disk**: no serializer writes it (DUP-25) | ser_probe |
| Tp:12020-12024: pinned moves are protected on the dict path | **HELD only for `_demote_hit_window`**; the TSB loop and G3 ignore `user_moved` (DUP-23) | ser_probe T4b, read |

---

## 4. Traps — what a fix must preserve

1. **The refactor gate cannot see the drift.** `characterize_planner.py` exercises `plan_week`,
   `generate_plan` and `generate_weekly_plan` only. Its "midweek" rides carry `tss` but no
   `time_in_zone`, so the zone double-subtraction (DUP-2) is invisible too. Collapsing the builder
   signatures onto `WeekContext` will *correctly* change refit (week_in_phase, emphasis, fixed_core,
   `Phase`) and extend (`goal_type`), and the gate will still report "0 changed". Before step 1, add
   regenerate, recalculate, extend and refit cases, with rides that carry zones.
2. **DUP-1, 2 and 3 are behaviour changes, not relocations.** None has a right copy to keep. Gate
   them with probe_week_load (prescribed), probe_budget_sites, probe_stepback and rv/dup1_followup,
   not with "0 changed".
3. **`vo2_microintervals_only` survives regenerate through a module global** (app.py:12513 →
   `tp.set_vo2_micro_only`), not through the goal: `adjusted_goal` at Tp:12651 drops it. Moving the
   sampler onto `ctx.goal`, or removing the global, silently loses the preference unless the Goal
   copy is fixed first.
4. **Fixing DUP-1 lowers every auto-regenerated plan.** Passing the recent load into regenerate's
   `generate_phases` closes the gap, per the reviewer's self-test. Regenerate runs after an absence,
   when recent load is low, so plans will get visibly lighter. The comeback ramp (`recovery_weeks`)
   needs re-checking against capped phases. The raise-only base fill in
   `_enforce_weekly_volume_ceiling` then needs a decision: does regenerate or recalculate want load
   above `tss_target` at all?
5. **Regenerate's CTL recomputation is deliberate for events** (post-recovery ramp, Tp:12615-12636).
   Restoring the per-goal target rule must keep that ramp as a *ceiling* on the target.
6. **Unifying the stepback counter moves unload weeks in plans that already exist.** Regenerate must
   continue the stored plan's global week index, or the first regenerate after the fix will itself
   shift recovery weeks.
7. **The auditor must stay independent in what it reads, not in which set it copies.** If
   `plan_invariants` imports the owner's predicate, a wrong predicate becomes invisible. It should
   judge the served file and `Goal.max_hours_for_day`.
8. **Stub-week proration order is a rule, not a detail.** `_clip_week_to_phase` is not idempotent
   (WP:555-560), and the current orders give 192, 222 or 0 TSS. Choose the rule explicitly, e.g.
   `min(clipped, gross − week_ridden)`, and test both heavy and light rides.

### Commands (from `$WT`, prefix as in the header)
```
probe_week_load.py            # DUP-1 part 2 (label + prescribed), DUP-2 part 1
probe_ctl_regen.py            # DUP-1: regenerate invariant to target_ctl (885 == 885); prescribed peaks
rv/dup1_followup.py           # DUP-1 A (archive 250), B (non-event target rule), C (DUP-14 preview)
rv/dup1_acwr.py               # DUP-1 second rider + injection self-test (reviewer-run)
probe_budget_sites.py         # DUP-2
rv/dup2_remainder.py, rv/dup2_audit.py   # DUP-2 heavy/light, auditor blindness (reviewer-run)
probe_stepback.py, rv/dup3_stepback.py   # DUP-3
ast_threading.py [names…]     # DUP-4, x=goal.x census
probe_builder_args.py, rv/dup4_refit.py  # DUP-4 runtime
probe_hard_defs.py 20 --selftest ; probe_hard_defs.py 20 --owner   # DUP-6
probe_content_class.py        # DUP-6 [3],[5], DUP-7
ast_goal_rebuild.py, rv/dup5_roundtrip.py # DUP-5
probe_ease_clamp.py           # DUP-8, DUP-13
probe_misc.py                 # DUP-5 M1, DUP-9 M2, DUP-10 M3
probe_owner_ceiling.py        # DUP-2 owner trace (a trace, not a detector; no self-test)
```

---

## 5. Changes after independent review

The reviewer confirmed DUP-1 to DUP-5 with its own riders and code paths. Of the 129 citations it
checked, 123 landed exactly; the other six were off by a line or two, and are fixed above. It broke
seven probes' detectors, and each assert tripped. Its corrections, each re-verified here by rerun or
by reading the source, are applied:
- DUP-1 is now stated in prescribed load (+36–53%, with 17–18 of 22 weeks over), not in labels
  (+83%, 2.1×). It adds the late fetch (Tp:12987), recalculate's missing volume-ceiling pass, the
  trigger gates, and the non-event target rule (CTL 134 vs 87). The explicit-`target_ctl` example
  is marked latent. Tp:9811 is no longer called a copy of the cap rule.
- DUP-2 is downgraded from critical to high. "117 is intended" was replaced by the two conflicting
  intents in the code's own comments, plus the light-ride overfill.
- DUP-3: the longest run of load weeks is up to 6, not 5. DUP-4: "17 of 24" became 18 spellings
  and 6 meanings, and it gains the fixed_core refit consequence.
- DUP-5 is downgraded from high to medium: the UI never sends a target. The claims table now says
  "five entry points" HELD, and notes the harness also calls `generate_weekly_plan`.
- DUP-14 (phase preview) is new. The recent-load fetch count is five sites, one more than the
  reviewer's four (app.py:12621).
- `probe_ctl_regen.py` now asserts regenerate's invariance to the target directly, and
  `probe_week_load.py` reports prescribed load.
