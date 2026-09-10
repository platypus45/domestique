# Owner lens: who writes a session, and does the single owner own it?

Reviewer: owner-lens (1 of 5). Code: `ca6091f9` in the scratch worktree
`rev-owner`. Nothing was fixed or committed.

## 0. How this was measured (and why you can trust the instruments)

Every number below comes from running code in a sandbox: HOME points at a
temp dir, `DOMESTIQUE_NO_NET=1`, the live-metrics and ride-archive fetches are
stubbed (`review/tools/boot.py`), and the run asserts that `tp.PLAN_DIR` is
inside the sandbox. Production was never touched. The worktree has two
instrumentation edits: `_USE_TRAINING_WEEK` reads `REVIEW_OWNER`
(`training_planner.py:6091`), and an opt-in write census is added
(`src/census_hook.py`, active only when `CENSUS_OUT` is set or when a driver
installs it).

| instrument | what it does | negative control |
|---|---|---|
| `src/census_hook.py` + `tools/census_drive.py` | wraps `PlannedSession.__init__/__setattr__`; for every **changed** write it records entry (outermost `src/` frame), pass (the frame just under it) and writer (innermost `src/` frame), and whether the session was sealed. Also tracks each **day/slot**, so a pass that *replaces* a session object is still counted. Plan **dicts** are wrapped in a recording `dict` subclass. | `tools/census_control.py`: a known write by `TrainingWeek._as_rest` is recorded with the right writer, construction-time assignments and no-op writes are not counted, and a write to a sealed session is flagged → PASS. Stated blind spots: `object.__setattr__` bypasses it (confirmed), and so do dicts the app loads from disk. |
| `tools/stages.py` | snapshots every session before and after each post-pass that `generate_plan` runs, plus after each `TrainingWeek.finish()`, with the flag off and on in the same process | its "after week loop" snapshot reproduces the base-week HIT counts and B3 numbers the failing tests report (2 HIT, 167.6 vs 141.8) |
| `tools/domain.py`, `repro_stub.py`, `attacks.py`, `attacks2.py`, `content_share.py`, `probe_wrap.py` | domain metrics measured on the **served** `.zwo` zone split; targeted reproductions | each attack also runs the same input through the legacy path as a contrast |

Riders: the owner-like athlete (FTP 240 W, 12.5 h over 6 days with Friday
off, CTL 55; continuous, and an event 12 weeks out), plus the probe's
generator of plausible riders (10 for the census, 40 for the sweeps).
"Monday" means today frozen at 2026-09-07; "Thursday" is 2026-09-10 with
Mon–Wed rides of 62/96/48 TSS (206 in total).

---

## 1. Findings, most severe first

### OWN-1 · critical · With the owner on, a mid-week generate or regenerate gives the athlete almost nothing for the rest of the week

`week_plan.py:299-317` (`ceiling`), `week_plan.py:561-562` (clip before sizing).

**What is wrong.** `plan()` prorates the opening stub week (Thu–Sun, 4/7 of
the target), then `ceiling` subtracts **every ride since that Monday**. Each
step prescribes "what is left" on its own. Applied together they subtract the
elapsed days twice. The legacy loop subtracts the rides from the gross 7-day
target and prorates afterwards, which is roughly right.

**Evidence** (`tools/repro_stub.py`, and `attacks.py` §C for regenerate):
```
continuous owner=off  target(prorated)=234 net=None ridden=206 prescribed Thu-Sun=194  week total 400 (98%)
continuous owner=ON   target(prorated)=234 net=28.0 ridden=206 prescribed Thu-Sun=19   week total 225 (55%)
           [('Thu','rest'),('Fri','rest'),('Sat','rest'),('Sun','tempo',20)]   audit: clean
event      owner=off  target=200 ... prescribed=228  (124% -- legacy overshoots)       audit: clean
event      owner=ON   target=200 net=0.0 prescribed=0 [4 x rest]  (59%)                audit: clean
regenerate owner=off  wk1 target=234 prescribed=234 ; owner=ON net=28.0 prescribed=15
```
**Reachability.** This is live the moment the flag is turned on. The app
passes the last 30 days of rides into both entry points: `app.py:11878`
(`activities=_recent_activities_for_planner()` into `generate_plan`) and
`app.py:12552-12557` (`activities=` into `regenerate_from_today`), and
`_completed_tss_in` reads both the `date`/`start_date_local` and the
`tss`/`icu_training_load` shapes. With the flag off (production today) it does
not occur, so it blocks Step 5 rather than being a bug in production now.

**Consequence.** An athlete who has ridden a normal Mon–Wed and presses
generate or regenerate on Thursday gets four rest days (event) or one 20-minute
ride (continuous). The auditor calls both **clean**: `weekly_volume` grades
against the `net_tss_target` the planner stamped (28 or 0), and `empty_week`
only fires when that net is at least 50 (`plan_invariants.py:164`). The
ceiling's own docstring cites "a Thursday regenerate" as the case it fixes.

**Root cause: structural.** "What is left of this week" is derived twice: by
span (`_clip_week_to_phase`) and by rides (`ceiling`), and nothing owns it.
The auditor then reads the planner's own stamped number, so it can only check
the planner against itself (see OWN-12).

### OWN-2 · critical · `TrainingWeek` is not the owner. With the flag on, more functions write each session, not fewer

`training_planner.py:8423-8459, 8642-8953` (generate) and `12776-12810, 12954-13084` (regenerate); `week_plan.py:25, 173-196, 516-531`.

**What is wrong.** With the flag on, `plan()` replaces only the week loop.
The whole legacy chain (18 pass functions in generate, 9 in regenerate) still
runs on its output. `finish()` then re-commits, and **after the seal** the
legacy passes write again. The seal does nothing in production:
`_install_seal` is called only from a test and a probe, `amend()` has no
production caller, and a plan crosses requests as JSON, so every downstream
entry point receives fresh, unsealed objects. The app edits plans as
**dicts**, a second representation that no owner or seal can see.

**Evidence** (`census_drive.py`, 12 riders, all entry points, flag off and on):
```
distinct innermost writer functions      generate  off 15  on 18   regenerate off 9  on 12
functions that built/changed a delivered slot (median/p90/max)
                   generate      off 3/7/8   on 4/7/8
                   generate@thu  off 3/7/8   on 5/7/9
                   regenerate    off 4/6/8   on 4/6/8
new writers with the flag on: TrainingWeek._rescale (401 writes), _as_rest (213), _demote (70)
legacy writers still active with the flag on: _enforce_stepback_is_lightest 124, _space_hard_days 76,
  _inject_mid_cycle_ftp_tests 83, _enforce_slot_file_coherence 68, _enforce_weekly_volume_ceiling 20 ...
writes to SEALED sessions (flag on):  _match_zwo_unclamped 106, _enforce_slot_file_coherence 92,
  _space_hard_days_across_plan 42
TrainingWeek constructed from: {'generate_plan': 160, 'regenerate_from_today': 160} -- nothing else
```
Entry points that never use the owner: `recalculate_plan`, `extend_continuous_plan`,
`refit_remaining_week`, `reforecast`, `reforecast_dict`, `apply_week_tier_down`,
`generate_weekly_plan`, `plan_week` and `adjust_today_session`, plus the 10
functions that write plan dicts (`static_writers.py`): `_apply_reforecast_to_dict`,
`apply_week_tier_down` and `rewrite_stale_plan_classifications` in the planner;
`_accept_redraw_apply`, `_swap_session_type_apply`, `api_plan_auto_adjust`,
`api_plan_ftp_test_type`, `api_plan_rematch_day`, `api_readiness_apply_tier_down`
and `api_today_session_persist` in the app. `daily_adapt_plan` and `rematch_week`
made no writes, which holds up their "projection only" claims.

**Consequence.** This is the owner's own complaint ("computed, then 3–4
functions mess with it") in measured form. With the owner on, the median
generated slot passes through 4–5 functions instead of 3. For maintainers, a
rule added in `_commit` is not the last word: two legacy passes overrule it
after sealing, and most edit paths bypass it entirely.

**Root cause: structural.** The owner was added *next to* the pass chain
instead of *replacing* it. Ownership state lives in memory, but sessions live
in JSON on disk.

### OWN-3 · high · "Slot type vs served content" is minted by the sampler's filename classifier, not by `match_zwo`. The handover's Step 4 aims at the minority source

`training_planner.py:6757` (`_session_type_from_row`: filename prefix first), `6823` (`_make_session_from_row`), `9107-9126` (`_session_is_hit`), `week_plan.py:46`, `plan_invariants.py:21`.

**Evidence.** For every delivered session labelled easy but serving hard
content, the census records who attached the file:
```
owner OFF generate (7):  5 born in _make_session_from_row, 1 _make_opener_session, 1 set by match_zwo
owner OFF regenerate(16): 13 born in _make_session_from_row/_make_opener_session, 2 match_zwo
owner ON  generate (4):   3 _make_session_from_row, 1 match_zwo
owner ON  regenerate(11): 10 _make_session_from_row, 1 match_zwo
labels involved: tempo<-sweet_spot/tempo_intervals/threshold_ladder, recovery<-sweet_spot/threshold_ladder,
                 z2<-vo2_ladder/sweet_spot/anaerobic(opener)
```
`_session_type_from_row` derives the slot's type **from the file name**
(`tempo_*` → tempo, `recovery_*` → recovery). The content class comes from the
classifier. That makes at least five classifications of one workout:
filename (`_session_type_from_row`), content (`_content_class_for_row/_zwo`),
the union of the two (`_session_is_hit`, which counts sweet_spot and
tempo_intervals), `week_plan.HARD_TYPES` (label only; no `double_threshold`),
and `plan_invariants.HARD_TYPES` (label only).

Precedence #1, the intensity budget, is enforced **on labels**
(`content_share.py`, 40 riders with the flag on):
```
weeks=160  over 60% cap by LABEL=0  by CONTENT=3   worst: 163 TSS hard content vs cap 118 (1.38x), label says 112
```
Replanning surfaced `supra_threshold_2x30s-3min_109pct` on a z2 slot and
`tempo_4x4min…` on a long_z2 slot. Both came from the sampler.

**Consequence.** A predicate at `match_zwo` would leave most mismatches in
place. The budget that must never be exceeded is measured on the axis that
cannot see the breach. The auditor's `hard_day_spacing` check is label-only,
so it is structurally blind to the defect.

**Root cause: structural.** Nothing owns the question "what is this workout".
Each consumer re-derives it.

### OWN-4 · high · "All six remaining owner-on failures are one defect" did not hold. There are 8 owner-only failures and none is that defect

**Evidence.** Named set with the flag on: 5 fail, and
`TestBlockConcentration` **passes**, alone and in its file. Wider planner
subset (58 files, `-n 2`): flag off gives 620 passed and 0 failed; flag on
gives **8 failed**, all owner-only. Each was diagnosed from a stage trace
(`stages.py`, `stages_report.py`):

| failure | cause, from the trace |
|---|---|
| `fs1::test_blueprint_keeps_b5_and_b3` (deload 167.6 vs 141.8) | W1 is the 4-day Thursday stub. The owner sizes it to its prorated target (141.8 of 155), and the test compares the deload against it. **Legacy passes only by overloading:** W1 336 against 155 (2.17×), W2 501 against 272 (1.84×). |
| `TestFix2…test_multiple_tempos_and_one_hit_possible` | Two sources of truth: `Phase.hit_per_week=1` against `IntensityBudget(base).hit_count_max=2` (`hit_allowance=2`). The owner puts 2 labelled HIT in base W2, which is within budget. Legacy passed **by accident**: a hard-content easy slot raised the union count to 3, `_enforce_weekly_hit_cap` demoted a labelled HIT, spacing removed the other, and legacy base weeks ended with **0 HIT, below `hit_count_min=1`**. |
| `variety_bonus` ×3 (vo2_short 8→6, build2 vo2s 2→1, peak anaerobic 1→0) | `finish()` shrinks the sessions the floor pass installed. `_rescale` drops the file, and the rematch goes **by slot type**: wk13 `vo2_short_4x6min` 60m → `vo2max_2x5x1min` 40m; wk19 vo2_short → vo2max_short; wk23 anaerobic → z2 `tempo_3x1min`. This is the defect mirrored: the owner discards a correct content choice because it only knows the label. The coherence pass's rematch preserves the class and the owner's does not, so there are two rematch policies. |
| `event_and_goal_focus` ×2 (long ride 233 < 240) | One number, four writers: `plan()` sizes the ride at 170–189 → `_apply_long_ride_target` grows it to 300 → `_enforce_weekly_volume_ceiling` trims it to 223–265 → `finish()` trims again (wk10 241, then wk14 150–172). This is the `pin()` removal that was already measured. |
| `tid_plan_properties…polarized…less_middle` | POL stays below THR at every stage until `_enforce_slot_file_coherence`, which runs **after `finish()` sealed the weeks**. It moves THR 21.0→19.3 and leaves POL at 19.8. The result is decided by a pass that writes after the seal. |

**Consequence.** Step 4 as written would not unblock the flag. Two of the
failing tests encode legacy accidents (a 2× overload, and a demotion triggered
by a mismatch). Re-pinning them without this context would lock in the wrong
lesson in either direction.

**Root cause: structural** (OWN-2 and OWN-3): no single owner of the week's
budget, of workout identity, or of the last write.

### OWN-5 · high · Athlete-owned sessions: the owner, the legacy passes and the converters disagree about what "owned" means

`week_plan.py:107-138, 319-323, 423-428`; `training_planner.py:10526-10534, 11976-12041, 11357`.

**Evidence** (`attacks.py` §B, `attacks2.py` §G, plus two short scripts):
```
dismissed Sat VO2 (will not be ridden):  owner counts its 90 TSS AND eases Fri threshold -> z2
                                          ("eased: 48 h from the neighbouring hard day")
same pair through legacy _space_hard_days_across_plan: eased=[] (dismissed must not block, 10526-10532)
done Thu session + the Thu ride in `ridden`: ceiling=320, committed=80 -> room 240 (should be 320)
_plan_dict_to_planned_weeks(adapted=True, completion_matches, moved_from, execution)
    -> adapted False | completion_matches None | moved_from '' | execution None
    is_athlete_owned: True -> False
reforecast (object path) on an adapted=True vo2max, TSB -40  -> rewritten to threshold
```
**Consequence.** The athlete loses a hard day to a session they already
dismissed. A done session costs its load twice. Every `reforecast_dict` call
site (there are 7 in the app) sees adapted sessions as fair game, and
`reforecast` rewrites them anyway. This contradicts "it must never rewrite
one" (`week_plan.py:131-135`).

**Root cause: structural.** The ownership predicate and the definition of "a
session that costs recovery" are re-implemented in each pass and each
converter.

### OWN-6 · high · The race week loses its opener and all its intensity with the owner on. Legacy puts the opener on the athlete's rest day

`week_plan.py:780-804` (no taper exemption), `training_planner.py:10580` (`_apply_race_week_shape`).

**Evidence** (owner athlete, Friday unavailable, A race on Saturday 12-05):
```
owner=off race week: Tue sweetspot 63 | Fri z2 45 OPENER | Sat RACE
          audit: [rest_days] wk13: 2026-12-04 (Fri) has z2 but is not an available day
owner=ON  race week: Tue z2 67 "eased: the week had lost its easy majority" | Thu "Rest -- no room left"
          | Fri rest (opener deleted) | Sat RACE        (explain(): total 227 TSS, hard 0)
```
`_restore_the_easy_floor` has no taper exemption. Step 4 of `_commit` has one.
The race has no file, so it is invisible to `easy_share()`, which means a race
week is always "mostly hard" by served content. The owner has no way to
relocate a session: it can only cut, so an opener landing on a rest day
simply vanishes.

**Consequence.** Legacy asks the rider to train on a day they declared
unavailable. The owner removes the taper intensity (the precedence comment
cites Mujika against exactly that) and the opener.

**Root cause: structural.** Plan-level policies place sessions without
consulting availability, and the owner can only rescale, demote or rest.

### OWN-7 · medium · `_restore_the_easy_floor` can lower the easy share it exists to restore

`week_plan.py:791-793`: the candidates are `HARD_TYPES or session_type == DEMOTE_TO`, which means **every z2 ride**. Tempo is never a candidate.

**Evidence** (`attacks2.py` §H, using real library files):
```
before: Mon vo2max 60 | Tue tempo 90 | Thu tempo 90 | Sat z2 120 | Sun tempo 90   easy share 0.49
after:  Mon rest      | Tue tempo 90 | Thu tempo 90 | Sat rest   | Sun tempo 90   easy share 0.22
```
The result is a pure grey-zone week (the tempo file is 78% Z3): the
"moderate-intensity black hole" the module's own comment warns against. In
the 40-rider sweep it fired on 2 weeks and helped both times
(0.545→0.725, 0.503→0.625), so it is reachable but uncommon. It also caused
OWN-6. **Instance** of a repair loop running over labels.

### OWN-8 · medium · `replan()` is not a function of its inputs

`week_plan.py:534-657, 823-833`. `plan()` mutates `PlanState` (novelty,
pick counts, HIT rotation) on every call.

**Evidence** (`attacks.py` §F): the same context and rides, with `plan()`
then `replan()`, gave **6 of 7 slots different**, including the two mismatches
quoted in OWN-3. **Consequence:** the public verb meant for a Thursday re-plan
reshuffles the week even when nothing new is known. **Structural:** plan-level
bookkeeping is written by a per-week decision.

### OWN-9 · medium · With the owner on, regenerate drops event climbing specificity. The 24-argument drift is reproduced inside the owner

`training_planner.py:12785` against `8434-8440`; `week_plan.py:247` (`event_targets` is declared and never read).

**Evidence:** `sample_week_workouts` was spied on for an event with `climbing_bias=True`:
```
generate   off/on and regenerate off : build2:event_climb x3, peak:event_climb x4
regenerate owner=ON                  : build2:None x3,       peak:None x4
```
**Structural.** Each call site still assembles `WeekContext` by hand, which is
the drift mechanism the handover blames on the signature.

### OWN-10 · medium · The owner's own output needs a legacy pass to finish it

`week_plan.py:688-706` (`_clamp_to_limits` keeps the file) and `731-744`.

**Evidence** (`attacks.py` §A, 40 riders, flag on, invariants checked just
after `finish()` and again after coherence):
```
after finish()   file_len_vs_slot_gt25pct=40  easy_label_hard_content=18
after coherence  file_len_vs_slot_gt25pct=0   easy_label_hard_content=18
```
Credit where due: at `finish()` there were 0 weeks over the live ceiling,
0 over the (label) hard share, 0 over a day cap, 0 stale `net_tss_target`,
0 labelled or content-hard pairs under 48 h, and FTP tests delivered 40/40 in
both modes.

### OWN-11 · low · Small correctness notes on the owner's internals

- `_demote` uses a flat ×0.8 (`week_plan.py:502`). VO2 75 min at 105 TSS becomes z2 at 84, where the z2 rate gives 56; threshold 90 min goes 110 → 88 against 68. The phantom load is spent at commit time and the shrinks it causes are never undone, because re-commits only shrink. The ledger itself matched the served totals in all 80 weeks checked.
- `finish()` is nearly but not exactly idempotent: a second `finish()` changed 1 of 1,120 sessions (−1.1 TSS), and a third changed nothing.
- `is_immutable` swallows exceptions (`week_plan.py:118-122`). If `_protect_race` raises, `is_immutable(race)` returns False.
- Cost is fine: `_commit_all` median 8 calls per 4-week plan, match_zwo median 8, generate 16.1 s against 14.1 s for 40 riders.
- `ceiling` is recomputed on every access but did not drift: 0 stale nets in 160 weeks, including the taper weeks of an event plan.

### OWN-12 · medium (instrument) · The evidence base cannot see the failures above

- `plan_invariants._ceiling` reads the planner's stamped `net_tss_target`, so OWN-1 grades clean. It checks overshoot only (no under-delivery), and `empty_week` needs a ceiling of at least 50.
- Hardness is judged by label in the auditor (OWN-3).
- **The refit column of the parity probe is vacuous.** Rerun in the sandbox, it gave `refit actions: {'no_change': 40}` with the flag off *and* on, because the plans are generated "today" and contain no missed hard session. "No violations in any entry point" includes an entry point that never ran its logic.
- The probes pass no `recent_weekly_tss`. Run as documented (outside pytest, so without conftest's HOME sandbox), `generate_plan` (`training_planner.py:8306-8311`) and `regenerate_from_today` (`12988-12995`) call `ride_storage.recent_mean_weekly_tss()`. That reads the **active profile's** ride archive (`ride_storage.py:95-106`, `ProfileManager.get().active_dir`, under `domestique_home()`, which defaults to `~/.domestique`), and `_rides_dir()` calls `mkdir(parents=True)` there. If no profile is active, it raises, the error is swallowed, and the plan falls back to CTL×7. So the handover's numbers depend on which machine and profile ran them. In the sandbox, legacy `weekly_volume` came out 2/7/2/2/2 against the documented baseline of 1/7/1/1/1.

### OWN-13 · low (domain, both modes) · Delivered load against each week's own target

From `domain.py`, served content, owner athlete:

- Event peak weeks deliver **0.58–0.82** of target (500 TSS; 289–422 min of roughly 750 available) whether the owner is off or on.
- Legacy build1 overshoots (1.21–1.33×). The owner fits it (0.90–0.96).
- Stepback weeks are the lightest in their block in both modes for this athlete.
- There were no hard pairs under 48 h by label or by content.
- The served easy share stayed at or above 0.58 except in the degenerate owner-on Thursday stub (OWN-1).
- Taper: the owner zeroes race-week intensity (OWN-6).

---

## 2. Claims checked

| claim (source) | verdict | evidence |
|---|---|---|
| "12, 5, 6, 6 and 3 passes" (week_plan.py:7, plan_invariants.py:5, probe docstring) | **DID NOT HOLD** | Static distinct pass functions: now 18/9/12/7/6, and 17/8/11/6/5 at `9f30cf93` (before the owner). Passes that actually wrote at runtime (flag off): 16/8/11/0/3. No definition gives 12/5/6/6/3. |
| "12 functions can write to a session" | **DID NOT HOLD** | 22 distinct innermost writers observed at runtime. Statically, 24 planner and 4 week_plan functions assign session fields, plus 10 dict writers. |
| "median delivered session touched by 3" | **PARTLY** | Slot level: legacy generate median 3 (p90 7, max 8) and regenerate 4. With the owner on, generate is 4–5, so it got worse (OWN-2). |
| "TrainingWeek is the only thing that writes to a PlannedSession" / "enforced rather than documented" | **DID NOT HOLD** | OWN-2: 240 sealed writes in-process, and the seal is never installed in the app. |
| "the UI … edits go through amend()"; "Tests run strict" | **DID NOT HOLD** | 0 production callers of `amend()`. Only `TheSealCatchesLaterWriters` sets `STRICT_SEAL`. |
| "Constraints are consulted before a slot is committed, never applied as edits after" | **DID NOT HOLD** | OWN-2 and OWN-4 (the last word goes to coherence and spacing). |
| "All six remaining owner-on failures are one defect" | **DID NOT HOLD** | OWN-4: 8 owner-only failures, none of them that defect, and `TestBlockConcentration` passes. |
| Step 4: "fix it where match_zwo chooses" | **DID NOT HOLD as the locus** | OWN-3: the majority of mismatches come from `_make_session_from_row`. |
| "owner 0 spacing / 0 weekly-volume across 40×5" | **HELD for those invariants** | Sandbox rerun, flag on: "no violations anywhere". But refit is vacuous, and the invariants are blind to OWN-1, OWN-3 and OWN-6. |
| "legacy: 5 spacing breaches, 51 weekly-volume weeks" (architecture review) | **DID NOT HOLD now** | Legacy spacing is 0 in all 5 paths (cd161a0e added the pass everywhere). Volume 2/7/2/2/2 riders. |
| "Thursday regenerate … not prescribing on top of the rides" (ceiling docstring) | **OVERCORRECTS** | OWN-1. |
| "pin() worth 7 min; long ride 233 without it" | **HELD** | 233 reproduced; OWN-4 gives the four-writer mechanism. |
| "141-line unreachable block in regenerate … deleted" (architecture review) | **DID NOT HOLD** | The block is still there (`training_planner.py:12812-12952`) and it is the **default** (flag-off) production path. `architecture_report --section dead` now reports 0 unreachable blocks. |
| `_commit_all` "re-runnable by design"; `finish()` idempotent | **MOSTLY HELD** | 1 of 1,120 sessions changed on a second `finish()`. |
| "_restore_the_easy_floor … Bounded by the number of hard sessions" | **HELD (bound), logic wrong** | OWN-7 and OWN-6. |
| "`ceiling` recomputed on access can change mid-commit" (review brief) | **not observed** | 0 stale nets. |
| "FTP test outranks a sampled workout" with the owner on | **HELD** | 40/40 delivered in both modes. |
| "clearing the file when content is hard took failures from 6 to 12" | not re-tested | Consistent with OWN-3: most mismatches are not rematches. |

---

## 3. Traps, and what a fix must preserve

1. **Plans cross requests as JSON.** An in-memory seal or owner flag
   protects nothing downstream. Any ownership fact has to be persisted in the
   plan data, and `_plan_dict_to_planned_weeks` drops four of them today
   (OWN-5).
2. **Two representations.** Ten functions edit plan *dicts*. Routing only
   object writes through an owner leaves the app's edit paths outside it.
3. **"Owner-only failure" does not mean "owner regression".** fs1 and Fix2
   pass under legacy by accident (a 2× overload; a demotion triggered by a
   mismatch). Measure why a number moved before re-pinning a test, in both
   directions.
4. **Don't trust the auditor's "clean" for budgets or hardness.** It reads
   the planner's own `net_tss_target` and labels. It needs an independent
   derivation of the remaining budget and content-based hardness, or OWN-1 and
   OWN-3 stay invisible.
5. **Stub weeks.** Prescribe what is left exactly once: either prorate by
   span or subtract the rides, not both. Keep the legacy fix that stopped
   "369 TSS already ridden plus a full week".
6. **One classifier of workout identity.** Filename-first typing
   (`_session_type_from_row`) exists for a reason (the boot-time staleness
   rewriter). Changing it moves slot types plan-wide, so re-measure
   `characterize_planner.py` with that in mind.
7. **Removing legacy passes naively will ship incoherent files.** The owner
   currently relies on the post-seal coherence pass (40 of 160 weeks), and its
   rematch loses content class where the coherence pass preserves it.
8. **Preserve these behaviours**, all of which some pass implements today:
   race immutability (`_protect_race`); athlete-owned sessions;
   missed/dismissed **not** blocking 48 h and not counting as load (legacy
   rule); done sessions counted once; openers never flattened; FTP tests
   counted but never demoted; 48 h across the week boundary; the taper
   exempt from intensity cuts (only step 4 of `_commit` honours it today); the
   event climbing emphasis on every entry point.
9. **Probe hygiene.** Sandbox HOME before import (the probes read the real
   archive otherwise). Don't count the refit column until the base plan has a
   missed hard session before today. Running the planner rewrites the tracked
   `src/workouts/.library_index.json` (it shows up modified in the worktree
   after runs).
10. **Decided, not relitigated:** the constraint precedence. But note that
    #1 is currently enforced on labels (OWN-3). Honouring it means measuring
    it on served content.

## Artefacts
All under `review/owner-lens/` and `review/tools/`. The census JSON files are
`census3_owner{0,1}.json` and `_extra.json`; stage traces are `stages_*.json`;
the test logs are `subset_owner{0,1}.txt` and `named6.txt`; the probe runs are
`probe_parity_owner{0,1}.txt`; and the domain tables are `domain_owner{0,1}.txt`.
