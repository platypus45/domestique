# State-duplication audit — Domestique @ a95447e8 (2026-09-14)

Lens: the same fact answered by more than one derivation, everywhere outside the
week-plan readers (those are in readers.md). Independent agent; probes ran under the
conftest sandbox. Kept verbatim; the synthesis is in ../architecture-audit-2026-09-14.md.

## Findings, ranked

### S-1 · CRITICAL · FTP has three owners and one of them freezes on the first accepted test
**Derivations.** (a) `athlete.json` via `ProfileManager.ftp` (profile_manager.py:137, default 200); (b) `config.ATHLETE_FTP_W`, a module `__getattr__` proxy to (a) (config.py:113-130); (c) `ride["ftp_at_ride"]` stamped at import (app.py:19290, 19395). `app.py:1878` (`/api/profile/update-ftp`, applied=True) does `config.ATHLETE_FTP_W = new_ftp`. A real module attribute now shadows `__getattr__` for the life of the process, so every `config.ATHLETE_FTP_W` reader is frozen: `/api/settings` `ftp`/`w_per_kg` (8512, 8543), the eFTP plausibility guard (9547), the local eFTP suggestion (17361-17372), auto-recalc `prior_ftp` (17337), session HR targets (11048), every later `ftp_at_ride` (19290). Meanwhile `power_zones`, `ftp-history`, readiness power card read (a).
**Evidence (P1).**
```
control     : pm.ftp=250 settings.ftp=250 Z4=228-262 frozen=False
update-ftp 260 applied -> frozen=True
POST /api/settings ftp=300 -> settings.ftp=260  power_zones.Z4=273-315  w_per_kg=3.71  pm.ftp=300  ftp-history=300
switch to profile with ftp=180 -> settings.ftp=260  Z4=164-189  pm.ftp=180
```
**Consequence.** One settings response carries two FTPs. After an accepted ramp test, a later manual edit "does not take" in the FTP field but does in the zones; eFTP auto-apply is gated against a stale FTP; every ride until restart is stamped with it; a profile switch serves the previous rider's FTP to every config reader. The comment at 1871-1877 claims `/api/settings` "already does" this mirror — it does not (8564-8760 never assigns config), which is why only this path breaks.

### S-2 · HIGH · CTL has five derivations and the calendar grades the rider against a curve that starts at 37
**Derivations.** (1) ICU wellness via `cached("training", get_today_metrics)` (25 sites); (2) `cached("wellness_7")` in `_actual_ctl_today` (15002); (3) `ride_storage.compute_local_ctl()` over `list_rides()` = legacy `ride_*.json` only, **not** the ICU archive (ride_storage.py:1655; its own neighbour `chronic_weekly_tss` documents "a rider whose rides arrive through intervals.icu has an empty list_rides()"); (4) constants: `or 30` at 9 sites (9249, 12037, 12633, 12898, 12967, 16917, 16933…), `37.0` in the planner (tp:8722), `_planned_ctl_today` (14971), `_annotate_planned_ctl_eow` (14870), `or 50.0` (11514); (5) `plan["ctl_snapshot"]`. ATL has two implementations: `app._compute_local_atl` over `list_rides` (3641) vs `ride_storage.compute_local_atl(rides)` (1867).
**Evidence (P2, 80 ICU-archive rides, ICU says CTL 60).**
```
list_rides=0 compute_local_ctl=None  app._compute_local_atl=None  rs.compute_local_atl(load_all_rides)=51.4  chronic_weekly_tss=395
plan.ctl_snapshot=60 | readiness ctl=60 (icu) | calendar ctl_actual=60 ctl_planned_today=36.1 band 31-41 | planned_ctl_eow wk1=38.1
ICU down: readiness ctl=None source=none | weekly-plan handed current_ctl=30 | generate handed None -> planner 37 | calendar planned 36.1
```
**Consequence.** The calendar's planned-CTL band is built from 37 (FIT-only local CTL is None for an ICU rider) while "actual" is 60: the rider is permanently "above band" and the ramp score is meaningless. During an ICU blip the same rider is CTL 30 to weekly-plan/recalc (STA-6), 37 to generate, unknown to readiness.

### S-3 · HIGH · Two readiness cards on one page read TSB from two stores
`/api/readiness` (3778) takes TSB from live ICU (`training` cache, 30 s `{}` on failure, local FIT-only fallback). `/api/readiness/composite` (3969 → readiness_composite.py:105-115) reads SQLite `wellness`, filled by `db.sync_wellness` on the 30-min loop that STA-7 shows can die silently. `/api/wellness` (4844) reads ICU live with a SQLite fallback of its own (4870).
**Evidence (P4).** SQLite seeded CTL 50/ATL 60, live 60/40: `/api/readiness tsb=+20 (icu)`, `/api/readiness/composite components.tsb=-10`, `/api/wellness last tsb=+20`.
**Consequence.** "Fresh, TSB +20" beside "fatigued, TSB −10" on the home page; the composite's score, and the tier-down it drives, use a different fatigue than the readiness banner.

### S-4 · HIGH · A ride can exist in one activity source and not the others
Sources: ICU metrics `recent_activities` (7 d, no stub refusal, training.py:733-788); ICU archive `rides/icu/*.json` (refuses Strava stubs, ride_storage.py:195-222); FIT imports `rides/*.fit` (+sidecar); legacy `rides/ride_*.json` (`list_rides`); SQLite `activities` (planner's `_recent_activities_for_planner`, 14898; reforecast 12273); `load_all_rides` merges only archive+FIT. Selection is per endpoint with three id schemes (HTTP-4/14).
**Evidence (P3, Strava stub for today).** `get_today_metrics.recent_activities=[('i424242', today, tss 0, sport None)]`; `persist_icu_activity -> None`; `/api/activities today: [('i424242', 0)]`; calendar today: no ride.
**Consequence.** The home activity list shows a zero-TSS "ride" today that the calendar, completion and load never see; `_matches_planned` (9687) can mark the session done from it.

### S-5 · MEDIUM · Availability has six representations; generate reads a prefs file that no longer exists
(1) per-profile `user_prefs.json` (`pm.save_prefs`, setup 1567: `rest_days`, `available_days`, `hours_per_week`); (2) **root** `DATA_DIR/user_prefs.json` read by `api_plan_generate` (11590), a file `migrate_profiles` moved away; (3) `Goal.rest_days/available_days/daily_max_hours/max_weekday_hours/max_weekend_hours` (tp:1805-1881, keys re-int'ed by `_goal_from_plan_dict` 12679); (4) dense `plan["availability"]` rows (11837-11866); (5) sparse `availability_overrides` rebuilt at 8 sites (4149, 4442, 11731, 12085, 12236, 12827, 16321, 16550) — planner lets overrides beat the Goal (tp:9025-9048); (6) `_compute_missed_suggestions` derives days from `goal.available_days` (13784).
**Evidence (P5).** `save_prefs` wrote `…/profiles/default/user_prefs.json rest_days=[2,4]`; generate reads `…/.domestique/user_prefs.json exists=False` → default `[0]` (Monday). Latent while the UI always posts `rest_days`. HTTP-1 covers the scaler consequence.

### S-6 · MEDIUM · Athlete numbers: defaults and zone models differ by reader
LTHR default 170 in `pm.lthr`; `or 175` in week-summary (9775). `_hr_zones` honours the HRR model only when handed `pm` (8866); `_session_hr_target` calls it without (11048), so a rider on HRR gets LTHR zones in session targets. Week-summary calls `api_settings()` — an ICU wellness fetch — to read a `timezone` no writer sets (9755, "the UI setup has no tz field"); 79 `date.today()` sites elsewhere; ride dates use the server's `astimezone()` (14652). eFTP is extracted from `sportInfo[0].eftp` at nine hand-copied sites (1020, 3088, 4785, 8360, 8501, 8722, 9473, 9544, 16976). Weight has one owner (athlete.json); ICU weight only prefils the wizard.

### S-7 · MEDIUM · Caches: unscoped keys, a cache nothing clears, and a memo key missing its input
`cached()` keys are not profile-scoped (`training`, `wellness_7/14`, `sleep`, `all_rides`); the composite hard-codes `profile_id="default"` (3980). `_ENRICH_CACHE` (14415) is keyed on plan mtime+size+date, never registered with `register_clearer` (only the fatigue memo is, 1639), never cleared on sync or switch: card_state (done/missed) lags a ride by up to 300 s. `app.py` writes `_cache[...]` directly (1966, 2077, 2682) with 24 h TTLs, bypassing the module's failure policy. The fatigue-resistance entry key omits FTP (`cache_key` 2562) although the memo below it is FTP-keyed, so an FTP change serves the old curve for 24 h. Only week-summary's tz path is profile-aware; `localStorage` keys are profile-scoped only where `_profileLsKey` is used (`summary_shown_`, update banner are not).

### S-8 · MEDIUM · Front-end: no shared model; every card fetches, several twice
Static call graph (self-tested on a planted double fetch): on one home load `/api/readiness` is fetched **3×** (`loadHome`, `loadReadinessComposite`, `loadReadinessActionBanner`), `/api/activities` **2×** (`loadHome`, `loadWeeklyCalendar`), `/api/week-summary` **2×** (`loadWeeklyCalendar`, `loadLastWeekFeedback`), plus `/api/settings` from `loadWeeklyCalendar` (server-side ICU fetch + eFTP auto-apply, HTTP-6). Module state is nearly empty: `_wellnessCache` written by `loadHome` (days=90) and `loadFitnessChart`; `_planWeeksManualCache` declared, never written. Each card derives from its own response, so two fetches straddling the 30 s `{}` failure window render two different riders.

### S-9 · MEDIUM · Plan store (cross-ref HTTP-5, STA-2; not re-driven)
24 `atomic_write_plan` sites in app.py (3946…17022) + 2 out-of-band writers (boot restore 127-133, v3 adopt 338); 67 references to `current_plan.json`; **no** shared load→mutate→save helper exists; 4 sites hold `plan_write_lock` across RMW (338, 3929, 11949, 13005); no version/mtime check anywhere; `.bak…bak7` rotates on every write, so one Plan-tab open (rides/sync, rematch, auto-recalc GET, today-session GET deload) can rotate four generations; `post_write_callback` → ICU push fires once per write including the two GET writers (10582, 17022). STA-1's `_ACTIVE_DISTRIBUTION` globals are gone at this commit (no hits).

## One owner per fact

| fact | proposed owner |
|---|---|
| FTP / LTHR / max HR / weight / zone model | `ProfileManager` properties only; delete `config.__getattr__` proxy and the 1878 assignment; `ftp_at_ride` stamped from `pm.ftp` |
| CTL / ATL / TSB (+ age, source) | one `fitness_state()` in `training.py`: ICU → SQLite wellness → EWMA over `load_all_rides`; returns explicit unknown, no `30/37/50` |
| planned-vs-actual CTL curve | same `fitness_state()` as start; `plan.ctl_snapshot` as the plan's own anchor |
| eFTP extraction | one `wellness_eftp(record)` in `training.py` |
| "activities in window" | `ride_storage.load_all_rides` (archive+FIT, stub-refusing), with ICU `recent_activities` used only to refresh it |
| availability | `Goal` + `plan["availability"]` (explicit blocks only); one `availability_for(plan, date)`; delete root-prefs read |
| rest days / hours prefs | `pm.prefs` |
| athlete timezone | `pm.prefs["timezone"]`, one `_today()` |
| response caches | `cache.py` keyed `(profile_id, key)`, generation-bumped on clear; `_ENRICH_CACHE` registered as a clearer |
| plan file | one `PlanStore.load()/save(mutator)` under the lock, version-checked, single push hook |
| front-end home model | one `HomeModel` fetched once per load; cards render from it |

## Not verified
Front-end counts are static upper bounds (no browser run). Timezone disagreement not driven (single-tz deployment). The owner's live plan/profile values were not read. Concurrency findings (S-9) are cited from HTTP/STA, not re-run.
