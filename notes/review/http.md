# Domestique — adversarial review, lens: the HTTP layer re-deriving the domain

Commit `ca6091f9` (detached worktree `rev-http`). Reviewer: rev-http. Nothing fixed, nothing committed.

The short version: **the HTTP layer does not ask the planner what "this week" is.**
It asks a second planner (`generate_weekly_plan`), it re-sums sessions, it re-maps
session types to zones, it re-assembles the planner's inputs by hand in every
handler, and it writes the plan file from 24 places with no shared unit of work.
Every number the athlete compared two days ago comes from a different one of
those derivations. The worst consequence is that three buttons meant to reduce
or rebalance load (**Update plan, Reforecast, readiness tier-down**) roughly
**double** the week's prescription. All of it is reproduced below in a sandbox,
with the numbers.

---

## 0. The instrument, and proof it can see

All evidence comes from `review/drive_http.py` (driven through FastAPI's
`TestClient`, the way `tests/conftest.py` does), plus two static tools. Results
land in `review/drive_http_results_*.json`. There are three runs: A =
generate/regenerate/autoadjust; B = the full set; C = generate, genfields,
regenerate, verify2. Library picks are random per run, so absolute numbers
differ between runs. The *relationships* reproduce in every run.

Sandbox, asserted before any write:

- `HOME` is a fresh `mkdtemp` under `scratchpad/review/home-*`, set **before any
  project import**. The script asserts `tp.PLAN_DIR`, `app._plan_dir()`,
  `db.DB_PATH`, `pm.active_dir` and `app.DATA_DIR` all resolve inside it.
- The network is cut at the **socket layer** (`socket.connect`,
  `create_connection`, `getaddrinfo`), so the cut covers urllib, httpx and
  anything else. **Planted check:** before importing the app, the script makes a
  real `urllib` and a real `httpx` request to intervals.icu and aborts unless both
  are refused. In all runs `net_attempts` stayed `[]`.
- `tp.post_write_callback`, the hook that schedules the intervals.icu calendar
  push, is replaced by a **recorder**, so a push that *would* be scheduled is
  counted. `icu_calendar_push.reconcile` is replaced by a function that raises.
  The lazy ICU sync is disabled. `get_today_metrics`, `fetch_wellness` and
  `get_sleep_metrics` are stubbed.
- The same 34 rides (8 weeks of history plus Tue and Wed of this week) are seeded
  **identically into all three activity sources**: the ICU archive (via
  `ride_storage.persist_icu_activity`), SQLite `activities`, and the metrics
  `recent_activities`. Disagreements therefore come from derivation, not data.
- The planner's **own** `PlannedWeek` objects are captured by wrapping
  `tp.generate_plan`. Zone ground truth comes from **parsing every served
  `.zwo`** second by second.

Self-tests. Every one fired on its planted fault:

| instrument | planted fault | result |
|---|---|---|
| ZWO zone parser | synthetic file: 10 min @50 %, 20 min @95 %, 5 min @115 % | `{Z1:10, Z4:20, Z5:5}` ✔ |
| `plan_invariants.audit` wrapper | two adjacent vo2max days | `[hard_day_spacing] … 24h apart` ✔ |
| TIZ-signal probe | same rides via two sources | `signals: ["time_in_zone"]` vs `["hr"]` ✔ |
| `review/ctor_sites.py` | api_weekly_plan's `Goal()` omits `distribution` | reported ✔ |
| `review/dict_mutations.py` | tier-down assigns `target["session_type"]` | reported ✔ |
| network kill | real urllib + httpx GET | both refused ✔ |

The date was real: today = Thu 2026-09-10, ISO week Mon 09-07 to Sun 09-13. The
plan was generated today, so the stored plan's first week is **Thu to Sun**.
That is normal production behaviour (generate anchors on the day it runs), not a
sandbox artefact.

---

## 1. Findings, most severe first

### HTTP-1 · CRITICAL · "Update plan", Reforecast, readiness tier-down and Auto-adjust inflate the week to full availability, past its own budget

`app.py:12245-12249` (reforecast); `app.py:13133-13137` (`_apply_plan_update`,
used by **POST /api/plan/update**, the UI's one primary adaptation, and by the
ride-sync auto-adapt `_maybe_auto_reforecast`); `app.py:4147-4151` (tier-down);
`app.py:4440-4444` (auto-adjust).

**What is wrong.** Each of these handlers builds the planner's
`availability_overrides` itself, as **every row** of `plan["availability"]`. But
`api_plan_generate` writes that calendar **densely**: one row for every day of
the plan span, filled with the wizard defaults (`app.py:12001-12021`, 71 rows
here, types `available`/`rest` only). `reforecast()` treats each row as "the
rider has N hours today" and scales every session up to it. The planner does not
cap the result at the week's `tss_target`. Two other handlers learned the lesson
and derive the input differently:

- `generate` feeds back explicit blocks only (`app.py:11781-11806`, whose comment
  says the dense rows "scaled the new plan's sessions to the STALE hours").
- `save-availability` feeds changed days only (`app.py:12390-12403`, v1.7.3).

The other five handlers never got the fix, because the derivation lives in each
handler, not in the planner.

**Evidence.** Stored week containing today, target 230, run B:

```
before                                   sum 305  [z2 65/46, z2 53/40, long_z2 225/168, z2 70/51]
tp.reforecast_dict(no overrides)         sum 305  (unchanged)            <- isolates the cause
tp.reforecast_dict(all 71 avail rows)    sum 496  [z2 90/68, z2 90/68, long_z2 240/180, z2 240/180]
POST /api/plan/reforecast   -> 200 sessions_changed=64, persisted sum 496
POST /api/plan/update       -> 200 "Plan rebalanced to today's fitness.", persisted sum 496
plan_invariants.audit       -> [weekly_volume] wk1: 496 TSS prescribed vs 230 target (+116%)
```

Readiness tier-down on a hard session (run C, `POST /api/readiness/apply-tier-down`):

```
response : old vo2max 80 TSS -> new threshold 79 TSS, duration_min 53
persisted: 09-10 threshold 90 min / 135 TSS; week 253 -> 480 TSS (target 230)
```

Auto-adjust (run A, `{"scope":"week","severity":"tier_down"}`): the response
listed one action, `overunder 44min/54 → sweetspot 44min/53`. The persisted week
read `sweetspot 90/120, recovery 90/45, long_z2 240/180, z2 240/180`, which is
**261 → 525** against 230. In both cases the response disagrees with the file it
just wrote. `dry_run` is clean (sha unchanged, run C).

Control, the "correct" handler (run D, `POST /api/plan/save-availability`):

```
re-post the identical 71-row calendar  -> sessions_modified 0, week unchanged (296)   <- its diffing works
raise ONE day (09-11) 1.5 h -> 2.5 h   -> sessions_modified 1: z2 48 min/35 TSS -> 150 min/112 TSS
                                          week 296 -> 373 against target 230
```

So there are two layers. The handler derivation decides **how many days**
get scaled: every row in five handlers, only changed rows in save-availability.
The planner's availability scaler then treats hours as a **prescription**, not a
ceiling: one extra hour tripled that session. It scales with no reference to the
week's `tss_target`. The HTTP layer turns a latent planner behaviour into a
whole-week inflation. The planner never enforces its own rule #1 on this path.

**Consequence.** A readiness "tier-down" nearly doubles the week, and raises the
tiered-down session itself from 80 to 135 TSS. "Update plan" and "Reforecast" do
the same, and so can a plain ride sync (the auto-adapt path). The week's
`tss_target` does not move, and the UI says "rebalanced" or "tier-down
applied". This breaks precedence rule #1 in `notes/planner-cleanup-plan.md`
("Intensity budget. Nothing outranks it"). It is the most plausible source of a
week whose sessions sum to **579**.

**STRUCTURAL.** The structure: handlers assemble planner inputs by hand. The same
14-line `tsb_series` block and the same `avail = {…}` comprehension are pasted
into four handlers (4137-4151, 4430-4444, 12197-12249, 13123-13137), and a fifth
derivation lives in generate and a sixth in save-availability. The planner's
availability scaler also lacks a budget ceiling on this path, so nothing
downstream catches the wrong input.

---

### HTTP-2 · CRITICAL · The home page's "this week" target comes from a second planner that never reads the stored week (explains 579 vs 293)

`app.py:9345` → `tp.generate_weekly_plan` (`training_planner.py:15094-15401`).
`tss_target = round(sum of the REGENERATED sessions)` at 15389-15398. It is
served as `"tss_target": week.tss_target` (`app.py:9473`) and re-served by
`/api/week-summary` (`app.py:9810, 9823`).

**What is wrong.** `GET /api/weekly-plan` regenerates a Mon–Sun week on every
request with a separate "simple weekly planner": fixed 75-minute HIT slots,
`TSS_PER_HOUR` estimates, and its own step-back and monotony rules. It then
overlays the stored sessions' fields **by date** for display, but keeps the
**regenerated target**. Days before the stored plan starts are filled with
regenerated **phantom sessions**. The home page shows the target twice: the
badge "TSS target: N" (`dashboard.html:17366`) and the rollup "done / N"
(`dashboard.html:17654`). The Plan tab's "Planned" column is a client-side sum of
the stored week's `tss_estimate` (`dashboard.html:13142-13165`).

**Evidence.** The same week, every endpoint (run B):

| number for the week of 09-07 | after generate | after regenerate | after POST /update |
|---|---|---|---|
| planner `PlannedWeek.tss_target` (own) | **200** (`net_tss_target` None) | 230 | 230 |
| planner Σ `tss_estimate` (own) = Plan-tab "Planned" | **378** | 305 | 496 |
| `/api/calendar` `planned_tss` (ISO week) / to-date | 378 / 86 | 305 / 46 | – |
| `/api/weekly-plan` `tss_target` (home badge) | **463** | **463** | **463** |
| `/api/week-summary` `tss_target` (home rollup) | **463** | **463** | **463** |
| Σ of the 7 home day cards | 512 | 439 | – |

(Run C, another random library draw: planner 200, plan tab 281, home 463.)

Sensitivity probe (`target_source`). Doubling every stored session's TSS **and**
the stored week target left the home target at **463**. Doubling
`phases[].weekly_tss` also left it at **463**. The home target is a function of
availability hours and fixed HIT durations, independent of the plan. The home
cards carried a **Tue 09-08 sweetspot 75 min / 100 TSS** that the stored plan
does not contain.

**Consequence.** Mechanism for the reported **579 vs 293**, present at
`ca6091f9`. "Planned workouts prescribed 579" is the Plan tab's sum of stored
sessions, very likely availability-inflated per HTTP-1. "Home shows 293" is
`generate_weekly_plan`'s own week. They agree only by coincidence and move
independently. (I did not read the athlete's real plan file, so the exact values
are not replayed. Only the mechanism is.) The athlete is also credited with
sessions that do not exist (HTTP-7).

**STRUCTURAL.** The structure: the HTTP layer answers "what is planned this
week" with its own planner, not the persisted `PlannedWeek`.
`api_weekly_plan` also hand-builds a `Phase` (`app.py:9260-9268`, with
`z2_pct=70` and a default `session_types` list hard-coded) and a `Goal` (10 of 31
fields) to feed it.

---

### HTTP-3 · HIGH · The home zone bars are classified by slot TYPE, include phantom sessions, and contradict the served files

`app.py:9990-10002` (planned bar) and `_SESSION_TYPE_TO_BAND` at `app.py:9665`.

**What is wrong.** The planned "LOW/MID/HIGH/AN" bar files each session's
**entire duration** under one band chosen by `session_type`. It ignores the
workout actually served and includes the regenerated phantom days from HTTP-2.
The planner has the real per-file answer (`tp._row_zone_minutes`, the library
Z% columns), and it matches ground truth exactly.

**Evidence.** Planned minutes for the ISO week's sessions (run B):

| source | low | mid | high | an |
|---|---|---|---|---|
| home planned bar (`/api/week-summary`), after generate | **51 %** | **22 %** | **27 %** | 0 |
| served `.zwo` files, parsed (ground truth) | 81 % | 19 % | **0 %** | 0 |
| planner's own `_row_zone_minutes` on the same files | 81 % | 19 % | 0 % | 0 |
| home planned bar, after regenerate | 86 % | 0 | 14 % | 0 |
| ground truth, after regenerate | 99 % | 1 % | 0 | 0 |

After generate, the "27 % high" is the phantom Tue sweetspot plus a Thursday
sweetspot slot whose file carries **zero** Z4 minutes. In run A, an `overunder`
44-min slot was filed 100 % under "anaerobic" while its file holds 7 min of Z5.

**Consequence.** The "zones breakdown" is wrong in both directions. It is also
blind to the §4 slot/content defect in the cleanup notes, because it never looks
at content.

**STRUCTURAL** (map count in HTTP-13).

---

### HTTP-4 · HIGH · The actual zone bar silently becomes "0 % low" when the activity list comes from the local archive (the 62 % mid / 38 % high signature)

`app.py:9866-9883` (TIZ index keyed on `ride_id` with the `icu_` prefix
stripped) against `app.py:4646` (fallback activities carry `id = ride_id`,
**with** the prefix). Also `cache.py:61-72` (a failed metrics fetch caches `{}`
for 30 s).

**What is wrong.** `/api/week-summary` takes activities from `api_activities()`,
which uses one of three sources: ICU metrics `recent_activities` (ids `i…`),
else the local archive for 7 days (ids `icu_i…`/`fit_…`), else SQLite. The
time-in-zone join only matches the first id scheme. On a miss, every ride is
filed **whole** by its average HR/LTHR (0.75-0.88 counts as mid, 0.88-0.96 as
high), so a steady endurance ride never lands in low. The JS fallback
`_buildWeekSummaryFallback` (`dashboard.html:17483-17492`) is HR-only always.

**Evidence** (`tizmiss`: the same two rides, same process, only the source changes):

```
ICU-metrics path   ids [i7700033, i7700034]         low 83% mid 5%  high 4%  an 8%  signals ["time_in_zone"]
local fallback     ids [icu_i7700033, icu_i7700034] low 0%  mid 55% high 45% an 0%  signals ["hr"]
```

**Consequence.** Whenever the ICU metrics call fails (no credentials, a 429, the
30-second negative cache, or a restart during an outage), the home "Actual" bar
shows only MID and HIGH. That is the shape the athlete reported (62/38, no LOW).
FIT-imported rides (`fit_…`, no `time_in_zone`) always miss.

**STRUCTURAL.** The structure: "which activities happened this week" has five
sources with three id schemes, chosen per handler (table in HTTP-14).

---

### HTTP-5 · HIGH · Lost update: 20 of 24 plan writers do read-modify-write with no lock, and a GET erases the rider's drag

`atomic_write_plan` has 24 call sites in `app.py`. Only 4 hold
`tp.plan_write_lock()` across the read-modify-write (`app.py:338, 3929, 12104,
13311`). The lock inside `atomic_write_plan` covers only the rename
(`training_planner.py:448-453`). `GET /api/plan/auto-recalc` is a sync `def`, so
production runs it in the threadpool, concurrently with the event loop. The UI
fires it on Plan-tab load (`dashboard.html:15364`).

**Evidence** (`race` + `verify2`):

```
control      move alone                               -> persisted: true
sequential   move (200) then auto-recalc (recalculated) -> move survives: true   (recalc honours user_moved)
interleaved  auto-recalc reads plan; move 200, on disk: true; auto-recalc writes
             -> move on disk after recalc returned: false
```

The sequential control rules out "recalc drops user moves". What erases the move
is the unlocked read-modify-write.

**Consequence.** A drag-and-drop right after opening the Plan tab, or any edit
during a background ride-sync adapt, is silently reverted after returning 200.
The same holds for move, swap-type, dismiss, tier-down and availability.

**STRUCTURAL.** The structure: no plan repository or unit of work. Every handler
does `open → json.load → mutate → atomic_write_plan` on its own.

---

### HTTP-6 · HIGH · GET endpoints mutate state: they rebuild the plan, schedule the calendar push, and rewrite the athlete's FTP

- **GET `/api/plan/auto-recalc`** rewrote the plan (sha `5d4031e490 →
  50d0ea3735`) and fired the post-write push hook **once**
  (`push_callbacks_fired_by_GET: 1`). In production that hook is
  `_icu_push_schedule_debounced` (`app.py:714`), which pushes to intervals.icu
  about 30 s later when calendar sync is on.
- **GET `/api/week-summary`** calls `api_weekly_plan()` (`app.py:9810`), which
  runs `_guarded_check_and_auto_apply_eftp` and then `clear_cache()`
  (`app.py:9518-9525`). `GET /api/today-session` does the same via
  `app.py:10712`. **Evidence** (`eftp_get`, pref `eftp_auto_apply` on, 14 days
  of eFTP at +12 %): `ftp_before 250 → ftp_after_GET 280`, and `athlete.json`
  sha `37095b20ac → 92f63dd1b9`. The home page issues these GETs on every load.
- `GET /api/weekly-plan` runs `tp.match_zwo`, which can write `gen_*.zwo` into
  the active workout directory. Every library load rewrote
  `src/workouts/.library_index.json` in the worktree (seen in `git status` after
  each run).

**Consequence.** A page view can change the athlete's FTP, and with it every
zone and target, and can push the plan to their calendar. Caching or prefetching
any of these GETs becomes unsafe.

**STRUCTURAL.** The structure: read models and commands are the same functions,
and handlers call each other's route functions as library calls.

---

### HTTP-7 · HIGH · The same home page gives opposite verdicts for the same week

`/api/week-summary` (rollup) and `/api/calendar` (on-track bar and THIS WEEK),
both loaded by `loadHome` (`dashboard.html:7197-7198`).

**Evidence** (run B, after generate; rides Tue and Wed = 150 TSS):

```
week-summary : tss_done 150 / tss_target 463  -> 32 %  "Behind plan", planned_days_done 2 of 6
calendar     : actual 150 / planned_to_date 86 -> completion_pct_to_date 1.744, compliance 174 %, band "red"
```

Two causes: different weeks and different denominators. The calendar's "to date"
counts only stored-plan days (the plan starts Thursday) but counts rides from
Monday. The rollup divides by the second planner's target (HTTP-2). The rollup
also counted **2 sessions done** on Tue and Wed: sessions that exist only in the
regenerated week, credited because "any cycling activity that day" counts as
done (`_matches_planned`, `app.py:9687-9692`).

**Consequence.** "Behind plan: one solid session closes it" sits next to a red
"over plan" bar. The rider is credited for sessions they were never given.

**STRUCTURAL.** The structure: week boundaries and "done" are computed per
endpoint (HTTP-14).

---

### HTTP-8 · MEDIUM · Reforecast persists derived, time-varying display fields into the plan store

`app.py:12176-12185` annotates `actual_tss`, `is_past`, `is_current` and
`adherence_pct` on every week, then writes the plan (`12267`). The Plan tab
prefers the persisted `w.actual_tss` over live rides (`dashboard.html:13143`).

**Evidence** (`stale`): after reforecast, a new 55-TSS ride today was seeded.

```
/api/plan current week actual_tss (Plan tab "Actual"): 0
/api/calendar actual_tss: 205        /api/week-summary tss_done: 205
```

**Consequence.** The Plan tab's "Actual" freezes at whatever it was on the last
reforecast. It also sums over the stored Thu–Sun week, not the ISO week.
**INSTANCE** of HTTP-5's structure (the handler owns persistence).

---

### HTTP-9 · MEDIUM · The planner's own week accounting is dropped at the HTTP boundary, and its numbers already disagree at birth

`PlannedWeek.net_tss_target` (`training_planner.py:1859-1864`) is documented as
"recorded so the auditor and the UI grade the week against the number that
decided it, rather than re-deriving it and disagreeing". It is set only by
`week_plan.py:655` (TrainingWeek, **flag off**, `_USE_TRAINING_WEEK = False` at
`training_planner.py:6091`). It is written by **none** of the three week writers
(`app.py:11942-11971`, `12587-12596`, `17411-17416`). Neither is
`hit_allowance`, which the planner set on 11 weeks in run C, nor
`hit_per_week`, `auto_acwr_scaled` or `block_focus`. Persisted week keys:
`end, is_stepback, phase, start, tss_target, week_num`.

**Evidence.** The planner's own current week: `tss_target 200`,
`net_tss_target None`, `hit_allowance 2`, Σ`tss_estimate` **378** in run B and
**281** in run C. The sessions exceed the week's own target by 40-89 % at
generation, before any HTTP handler touches them.

**Consequence.** No endpoint can show "the number that decided the week", so
each one derives its own; HTTP-2 lists five. **STRUCTURAL**: there is no
domain-owned serializer.

---

### HTTP-10 · MEDIUM · Handlers edit the prescription with no planner in the loop

`review/dict_mutations.py` found **20 functions (13 route handlers)** assigning
`session_type`, `tss_estimate`, `duration_min`, `zwo_file`, `status` or
`tss_target` straight into the plan dict. Top of the list:
`api_plan_auto_adjust` (from :4306), `api_readiness_apply_tier_down` (:4080),
`api_today_session_persist` (:10106), `api_plan_ftp_test_type` (:17064),
`_swap_session_type_apply` (:16829), `_accept_redraw_apply` (:16637) and
`_apply_move_session` (:14094).

**Evidence** (`move`, `POST /api/plan/move-session`):

- **Onto an occupied day:** Fri z2 → Sat. Saturday's 240-min long ride is
  **deleted** (week 496 → 316 TSS). A same-week move replaces the occupant
  (`_apply_move_session`, `app.py:14101-14124`).
- **Hard next to hard:** audit before had no spacing violation; after,
  `[hard_day_spacing] wk1: 2026-09-11 vo2max -> 2026-09-12 threshold = 24h
  apart`. The handler returns 200 and nothing re-checks precedence rule #2.

**STRUCTURAL.** The structure: mutations are dict surgery in handlers. The
planner's constraint order (budget > spacing > count > variety) is never
consulted after a user or readiness edit.

---

### HTTP-11 · MEDIUM · dict ↔ domain conversion is hand-written at 21 sites that disagree

`review/ctor_sites.py`:

- `tp.Goal(` in `app.py`: **10 sites**, passing 2, 2, 4, 5, 9, 9, 9, 10, 26 and
  28 of 31 fields. `reforecast_dict`'s own Goal passes 12/31 (drops
  `max_weekday_hours`, `max_weekend_hours`, `daily_max_hours`, `distribution`,
  `custom_bands`, `plan_mode` …). `_goal_from_plan_dict` (26/31) exists and is
  used by only 3 of those sites.
- `tp.PlannedWeek(` built from JSON: **7 sites in app.py**, each 7/14 fields.
  `tp._plan_dict_to_planned_weeks` does 9/14; its docstring says it is the
  "single conversion site", and it drops `completion_matches`, `adapted`,
  `moved_from`, `execution` and `auto_moved`.
- Week → JSON: **3 writers**. `api_plan_generate` writes 11 session keys inline.
  `_regenerate_plan_dict` and auto-recalc use `_planned_session_to_json` (all
  fields).

**Evidence that the copies disagree** (`genfields`, run C). The planner set
`nutrition_note` on **58** sessions ("Train-low option: fasted or low-carb Z2…",
"Normally fueled (4g/kg carbs)"). The generate writer dropped all of them. The
regenerate writer keeps them, so fuelling notes appear only after the first
regenerate. `matched=False` on an unmatched ftp_test session was also dropped at
generate.

Checked and found **benign today**: the event-projection / readiness Goal
copies (`app.py:11554, 9486, 17293`, 9 fields) differ from the canonical Goal in
6 fields (`daily_max_hours`, `distribution`, `events`, `max_weekday_hours`,
`max_weekend_hours`, `plan_weeks`). Yet `_project_event_capability` and
`compute_event_readiness` returned identical output for both (review probe, zero
diffs), because those functions do not read the dropped fields. Not every copy
bites today. Nothing stops the next one from biting, since nothing checks.

**Consequence.** Every entry point sees a different plan. That is the same
mechanism the cleanup notes blame for planner drift, one layer up.
**STRUCTURAL.**

---

### HTTP-12 · MEDIUM · Planned zone minutes in `/api/calendar` never see the served file

`_planned_zone_split_minutes` (`app.py:14629`) says it "reads zone_dist when
present (set by /api/plan from the library)". `zone_dist` is only ever added to
**response objects** by `_enrich_plan_for_response_uncached`
(`app.py:14915-14919`). It is never persisted, so the calendar
(`merge_plan_with_rides`, `app.py:15934`, reading the stored sessions) always
takes the session-type heuristic. `recovery`, `z2` and `long_z2` count as 100 %
Z1+Z2, and the code also ignores Z7.

**Evidence** (`target_source`): calendar planned `[660, 0, 0]` min as stored.
After planting `zone_dist` into the stored sessions: `[644, 16, 0]`. The branch
is dead in production.

**Consequence.** The home on-track rails' "Z3+Z4 planned" is a guess.

---

### HTTP-13 · MEDIUM · Four session-type → band maps, none alike

| map | tempo | sweetspot | overunder | ftp_test | sprint | neuromuscular |
|---|---|---|---|---|---|---|
| `app._SESSION_TYPE_TO_BAND` (`app.py:9665`, week-summary) | mid | high | anaerobic | — (0 min) | anaerobic | anaerobic |
| `tp.SESSION_TYPE_TO_BAND` (`training_planner.py:14776`) | mid | high | anaerobic | **high** | anaerobic | — |
| `tp._SESSION_TYPE_TO_BAND` (`training_planner.py:2569`, realized_bands) | tempo_ss | tempo_ss | **threshold** | — | sprint | sprint |
| JS fallback (`dashboard.html:17508`) | mid | high | anaerobic | — | **—** | — |

Plus two more heuristics: `_planned_zone_split_minutes` and
`_classify_card_state`'s `accepted_by_type`. The app.py comment at 9674 claiming
the planner's map "has always carried" `neuromuscular`/`anaerobic` is false.
**STRUCTURAL**: the planner exposes no single "what zone time does this session
deliver" function to the API.

---

### HTTP-14 · LOW-MEDIUM · "Done", "spent" and "which week" are each defined per endpoint

Read from the code; the sandbox numbers above show the consequences.

| question | week-summary | calendar | generate | reforecast / update / regen | reconcile (status done) | Plan tab (JS) | programme summary |
|---|---|---|---|---|---|---|---|
| activity source | `api_activities`: ICU metrics, else archive 7 d, else SQLite 7 d | `load_all_rides` + legacy `list_rides` | SQLite 30 d | SQLite 120 d | SQLite + legacy `list_rides` | persisted `actual_tss`, else `/api/activities` | `load_all_rides` |
| week | ISO Mon–Sun, athlete TZ | ISO Mon–Sun, server TZ | from generate day | stored week (starts on generate day) | stored week | stored week | phase windows |
| "done" | any cycling activity that day | any ride that day (`card_state`) | — | — | `rematch_week` 3-axis classifier | — | actual TSS > 0.3 × planned (`dashboard.html:20641`) |
| planned load | 2nd-planner target | Σ sessions (ISO) | — | stored `tss_target` | — | Σ sessions (stored) | phase `weekly_tss` × weeks |

---

### HTTP-15 · LOW · Smaller instances

- `_enrich_plan_for_response`'s docstring says it replaced the duplicates in
  save-availability, reforecast and redraw-day. Its only callers are
  `api_plan`, `generate`, `update` and one sample route (`app.py:11283, 12058,
  13332, 20862`), so reforecast and save-availability responses are un-enriched.
- `realized_bands` (the "Realized mix" readout) is computed once in `generate`
  (`app.py:12025`) and then carried unchanged by `dict(plan)` in regenerate and
  recalc (read from code). It is correct at birth (run C: stored equals
  recomputed).
- `/api/today-session` labels today from the stored plan (the v3.2.1 fix), but it
  still reads **yesterday's planned TSS** from the regenerated week
  (`week_data`, `app.py:10849-10853`). On the day after a mid-week generate,
  "yesterday" is a phantom `generate_weekly_plan` session. The G1 gate
  (`yesterday_tss_ratio > 1.5 → Z2`) is therefore graded against a session the
  rider was never given. Read from code; not driven.
- Programme-summary "planned TSS" per phase is `weekly_tss × weeks`
  (`app.py:20494-20497`), yet another derivation. It showed base = 1050 for
  weeks whose targets were 200 + 350 + 350.

---

## 2. Handlers over ~80 lines: what each should call, and what it would keep

| handler / helper (lines) | should call (plain domain function) | handler keeps |
|---|---|---|
| `api_plan_generate` (432) | `planner.create_plan(goal_request, athlete, history) -> Plan` + `plan_store.save(plan)` | form → `Goal` parse, 400 on `ValueError` |
| `_build_programme_summary` (469) | `analytics.programme_recap(plan, rides, metrics)` | nothing (it is not HTTP) |
| `merge_plan_with_rides` (433) | `week_view(plan, rides, week)`: planned/actual/zones per day from the planner's own week | JSON shape |
| `api_plan_auto_adjust` (300) | `planner.apply_readiness(plan, severity, scope, dry_run)` | severity lookup, response |
| `api_weekly_plan` (288) | `plan_store.load().week_containing(date)`, **not** `generate_weekly_plan` | eFTP banner (read-only) |
| `api_week_summary` (284) | `week_view(...)` (same as calendar) + `activities.for_week(week)` | TZ, rollup shape |
| `_apply_plan_update` (280) | `planner.adapt(plan, activities, today)` (tiers inside the planner) | none |
| `_api_today_session_impl` (265) | `planner.today(plan, readiness)` | response |
| `_compute_missed_suggestions` (233) | `planner.missed_suggestions(plan, today)` | none |
| `api_plan_auto_recalc` (180) | `planner.recalculate(plan)` behind a POST, via the store | freshness check |
| `api_readiness_apply_tier_down` (178) | `planner.apply_readiness(plan, "tier_down", day)` | 404/400 |
| `_regenerate_plan_dict` (163) | `planner.regenerate(plan, today)` | none |
| `api_plan` (142) | `plan_store.load()` + `plan_view.enrich(plan)` | markdown attach |
| `api_plan_reforecast` (142) | `planner.adapt(plan, …)` (no display annotations persisted) | response |
| `api_plan_move_session` (129) | `planner.move_session(plan, src, dst)`, re-checked against the constraint order | 400/422 mapping |
| `api_event_projection` (128) | `tp._project_event_capability(plan.goal, athlete, fitness)` with the canonical Goal | response |
| `api_save_availability` (124) | `planner.set_availability(plan, changes)` | diffing input |
| `_swap_session_type_apply` (143), `_accept_redraw_apply` (103), `_pick_redraw_candidate` (128) | `planner.replace_session(plan, day, …)` | none |
| `api_plan_rematch_day` (104), `api_plan_re_draw` (86) | `planner.rematch(plan, day)` | response |

---

## 3. Claims checked

| claim (where) | verdict | evidence |
|---|---|---|
| notes §5: "handlers reaching SQL directly: 5" | **DID NOT HOLD** | `architecture_report.py --section routes --json`: **28** handlers touch SQL; `--section layers`: 7 fs+http+sql |
| notes §5: 148 handlers / 9,648 lines / worst `api_plan_generate` 432 | HELD | routes section output |
| notes: `_USE_TRAINING_WEEK` is False | HELD | `training_planner.py:6091`; captured `net_tss_target` = None |
| `PlannedWeek.net_tss_target`: "recorded so … the UI grade the week against the number that decided it" | **DID NOT HOLD** | never persisted or served (HTTP-9) |
| `_plan_dict_to_planned_weeks`: "single conversion site … drift can only happen here" | **DID NOT HOLD** | 7 more `PlannedWeek(` sites in app.py (HTTP-11) |
| `_enrich_plan_for_response`: replaced duplicates in save-availability / reforecast / redraw | **DID NOT HOLD** | callers at 11283, 12058, 13332, 20862 only |
| `api_week_summary`: "Reuse existing helpers — do NOT re-implement" | **DID NOT HOLD** | re-derives target, bands, done (HTTP-2/3/7) |
| `merge_plan_with_rides`: "ISO-week Monday-anchored boundary EVERYWHERE on server" | **DID NOT HOLD** | stored weeks start Thu; reforecast/gaps use them (HTTP-14) |
| `_planned_zone_split_minutes`: "reads zone_dist when present (set by /api/plan)" | **DID NOT HOLD** on the calendar path | planted `zone_dist` changes the output (HTTP-12) |
| app.py:9674: the planner's map "has always carried" sprint/neuromuscular/anaerobic | **DID NOT HOLD** | `tp.SESSION_TYPE_TO_BAND` lacks neuromuscular/anaerobic (HTTP-13) |
| tier-down, issue #3 (`app.py:4076`): "a tier-DOWN must never INCREASE load" | **DID NOT HOLD** | session 80 → 135 TSS persisted, week 253 → 480 (HTTP-1) |
| tier-down response `duration_min`: "The stored duration … reporting the old value made the response disagree with the plan it had just written" | **DID NOT HOLD** | response 53 min, stored 90 min (HTTP-1) |
| auto-adjust: "dry_run=True … never writes disk" | HELD | sha unchanged, 1 action reported (run C) |
| move-session: "regenerate_from_today honors user_moved" (and recalc) | HELD for sequential recalc | move survives a sequential auto-recalc; lost only under interleave (HTTP-5) |
| save-availability v1.7.3: only changed days feed the scaler | HELD (run) | identical 71-row re-post modified 0 sessions (run D). It is the control that convicts the other five. But the one day it does feed is scaled to its full hours (35 → 112 TSS, week 296 → 373 vs 230). The scaler has no budget cap (HTTP-1) |
| save-availability docstring: per-day overrides "rescale duration_min / tss_estimate" | HELD, and that is the defect | availability is used as the prescription, not as a ceiling |
| conftest: HOME sandbox makes every data path land in the sandbox | HELD for data paths; **not** for the bundled library | `.library_index.json` rewritten in the repo tree by a library load |
| `_icu_push_schedule_debounced`: only the active profile's `current_plan.json` schedules | HELD | the recorder fired only for the sandbox plan path; a GET triggered it (HTTP-6) |

---

## 4. Traps, and what a fix must preserve

Traps met while building the instrument:

- **TestClient without `with`** runs no lifespan, so `tp.post_write_callback` is
  *not* registered and ICU sync threads do not start. Assert and replace the
  callback anyway: tests that use `with TestClient(app)` get the real push hook.
  Blocking only `training.urlopen` (as conftest does) does not cover
  `icu_calendar_push` (its own urllib) or app.py's `httpx`. Cut at the socket.
- **Generating on a weekday** yields a first stored week that starts that day.
  Half of the disagreements above only appear then, so a fixture pinned to a
  Monday hides them. `conftest.PLANNER_PIN_ANCHOR` is a Monday.
- **One activity source** makes endpoints agree by accident. Seed all three
  (ICU archive, SQLite, metrics) with the same rides, then vary one at a time.
- **The dense availability calendar** is written by generate itself. A fixture
  that builds a plan without calling the HTTP generate has no availability rows
  and will never see HTTP-1.
- **`cached()`** keeps `{}` for 30 s after a failure, and `_ENRICH_CACHE` is keyed
  on plan mtime. Clear both between probes, or you measure the previous state.
- **Loading the workout library writes** `.library_index.json` into the bundled
  library, and generation writes `gen_*.zwo`. Reset the worktree afterwards.
- **`head` on the driver's stdout** breaks the pipe before the JSON is written.
  Read the results file instead.
- **async handlers are serialized on the event loop in production; sync `def`
  handlers are not.** The race needs a sync handler or a background thread.
  auto-recalc and the ride-sync adapt both qualify.

What a fix must preserve:

- save-availability's "changed days only" and generate's "explicit blocks only"
  semantics are **the correct ones**. Move them into the planner; do not delete
  them.
- The FC3 race-day immutability guards in every mutator (`is_race` checks in
  move, auto-adjust, reforecast write-back).
- `user_moved` / `user_swapped` / `dismissed_at` pins must round-trip through
  every conversion. `_planned_session_to_json` is the only complete one today.
  A sequential recalc does honour `user_moved` (verified), so keep that.
- The rest-week grading in week-summary (`tss_target == 0` means 100 %) and the
  "today can be done but never missed" rule. Both are tested behaviours.
- `_icu_push_schedule_debounced`'s active-profile and file-name guards. Any
  repository that writes the plan must still call the hook exactly once per
  logical change.
- auto-adjust's `dry_run` isolation (a deep copy, no disk write), which works
  today.
- The ICU-stub refusal in `_normalize_icu_activity`: a Strava-first ride must
  stay absent, not zero.
