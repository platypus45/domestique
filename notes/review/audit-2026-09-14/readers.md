# Week-view readers audit — Domestique @ a95447e8 (2026-09-14)

Lens: every reader of the week plan and of today's session; the session-identity
writers; the form cards. Independent agent, driver under `with TestClient(app)` with
the conftest sandbox, today frozen Thu 2026-09-10 (ISO week 09-07..09-13), rides Tue 80
+ Wed 70 TSS seeded identically into ICU archive, SQLite and metrics; network cut at
the socket; two runs, plus two by an independent reviewer agent (library picks are
random; the relationships held in all four). Kept verbatim; the synthesis is in
../architecture-audit-2026-09-14.md.

## §1 Verdicts

| Finding | Verdict | Deciding numbers (my run 3; reviewer's run in brackets) |
|---|---|---|
| HTTP-2 second-planner target | **HELD** | Stored week containing today (Thu–Sun stub) `tss_target` **234**, Σ stored ISO sessions **190** [227]; `/api/calendar` planned **190** [227]; `/api/weekly-plan` `tss_target` (home badge + rollup) **418** [418]; Σ of the 7 home cards **324** [361]. Sensitivity: doubling every stored `tss_target` → 466 (moves, not doubles: `generate_weekly_plan` now seeds its budget from prorated stored targets, tp 15637-15653, then re-plans and emits `round(Σ regenerated sessions)` at tp 15922-15929); doubling every stored `tss_estimate` → 418 (no effect). Phantoms present: Tue "sweetspot 100", Wed "z2 34" before the plan starts. `?week_offset=-1` ignores the offset inside tp (`monday = date.today()` 15623-15624): "Last week" gets **418** with phase "general". |
| HTTP-3 planned zone bar | **HELD** | week-summary planned bar low/mid/high/an = **81/0/19/0 %**; served files of the same cards (tp `_row_zone_minutes`) = 82/14/4/0; stored ISO sessions' files = 93/7/1/0. The 19 % "high" is the phantom Tue slot's 75 min filed by type. |
| HTTP-7 opposite verdicts | **HELD** | week-summary 150/418 = **36 %** "Behind plan", `planned_days_done` **2** (the two phantom days, credited by "any cycling activity"); calendar `completion_pct_to_date` **2.0** (150 vs 23 to-date), `compliance_pct_this_week` 200, band **red**, on-track red. Calendar history row 08-31: planned **0**; "Last week" card (week-summary −1): target **418**, `planned_days_missed` **2**, done 0 — this is the owner's "history 0 vs feedback 332" pair; the 2 "missed" are this week's phantoms graded against last week's rides. |
| HTTP-12 calendar zone split never sees the file | **HELD** | `zone_dist` persisted in store: **False** (only on `/api/plan` responses). Over 12 plan weeks, 12/12 calendar rows differ from the zone_dist-based split, e.g. week 09-21 calendar z12/z34/z5+ = 282.5/17.9/62.6 vs 245.4/71.3/46.5; current week 271/0/0 vs 250.6/20.3/0. |
| HTTP-13 four maps | **HELD** | `app._SESSION_TYPE_TO_BAND` (9633) / `tp.SESSION_TYPE_TO_BAND` (15272) / `tp._SESSION_TYPE_TO_BAND` (2664) / JS (dashboard 17513): ftp_test none/high/none/none; neuromuscular anaerobic/none/sprint/none; overunder anaerobic/anaerobic/threshold/anaerobic; sweetspot high/high/tempo_ss/high. |
| HTTP-14 per-endpoint week/done | **HELD** | Week boundary: week-summary Mon (TZ `datetime.now`, 9759), calendar Mon (15438), weekly-plan Mon (tp 15624), stored plan Thu 09-10 (daily-adapt reads it: `weekly_target` 234, `total_actual` **0** — its window starts Thu, rides were Tue/Wed), programme window from 09-10 (`actual_tss` **0** for base while 150 TSS ridden this ISO week; planned = `weekly_tss × weeks` = 850). "Done": week-summary any cycling ride that day (9934); calendar `has_actual` (15578) → 2 "completed" days with no plan; programme 0.3× rule (JS 20641). Activity source: switching metrics → archive changed the actual bar from 90/30/10/0 to **0/130/0/0** (signal `hr`, ids `icu_…`): HTTP-4 still present. |

Not verified: HTTP-1/5/6/8 (out of scope; note the only push-hook firing was generate's own — no GET wrote the plan in this run because auto-recalc was not called).

## §2 Reader matrix (D = derived by the endpoint itself, S = read from the stored plan)

| Endpoint → card(s) | label/type | served file | planned TSS (week) | actual TSS | week boundary | done |
|---|---|---|---|---|---|---|
| `/api/plan` → Plan tab (`loadPlan` 15309; Planned col 13142) | S | S; `zone_dist`/`content_class`/`card_state` **D** 14498-14563 (response only) | JS Σ `tss_estimate` **D** 13142 | persisted `actual_tss` or `/api/activities` | stored week | `card_state` **D** 14575 |
| `/api/weekly-plan` → badge `week-phase-badge` 17366, cards `weekly-calendar` 17347 (then overwritten by calendar, see below) | stored overlay by day 9402, else regenerated **D** | stored 9394, else regenerated **D** (a stored REST day keeps the regenerated file: 09-13 "rest" + `endurance_5x1min…180min.zwo`) | `week.tss_target` = Σ regenerated **D** 9444 / tp 15922 | — | Mon–Sun, offset ignored **D** tp 15623 | status S |
| `/api/week-summary` (0 and −1) → rollup `_renderWeeklyRollup` 17610-17660; "Last week" card `last-week-feedback-content` 7903 | from weekly-plan | from weekly-plan | `plan.tss_target` 9786 (= HTTP-2) | `api_activities()` 9772 (3-source ladder 4604) | ISO Mon, TZ **D** 9759 | `_matches_planned` any cycling ride **D** 9934 |
| `/api/today-session` → `today-card` 1614, label `planned.session_type` JS 17902 | stored 10697 (S), regen fallback | stored `zwo_file` 10874 | yesterday's planned from regenerated week **D** 10798 | SQLite 3 d | — | — |
| `/api/calendar` → THIS WEEK `renderThisWeekFromCalendar` 14821 (paints the same `weekly-calendar` element after weekly-plan, 7197-7198), on-track bar, Home rails 20630 | S `session_type`; cell **name = `zwo_name`** 15543 | S; `content_class` from library (always "" — see §3) | Σ stored `tss_estimate` per ISO week **D** 15565; to-date **D** | `load_all_rides`+`list_rides` 15509 | ISO Mon normalised **D** 15438; synthetic history rows `tss_target` 0 | `has_actual` **D** 15578 |
| `/api/programme/summary` → programme modal 2528 | — | — | `weekly_tss × weeks` **D** 20102 | `load_all_rides` in plan window **D** 20107 | phase windows from plan start | JS 0.3× **D** 20641 |
| `/api/plan/preview` → wizard 11788/12014 | — | — | `generate_phases` with thin Goal **D** 11320-11365 | — | — | — |
| `/api/readiness` → gauge, header `header-stats` 7068, `loadPlanMetrics` 12496 | — | — | — | — | — | — (form, §4) |
| `/api/plan/daily-adapt` → home chip 21314 | S | S | stored `tss_target` 13644 | SQLite+`list_rides` from stored week start **D** 13600-13616 | stored week (Thu) | — |
| `/api/plan/missed-suggestions` (auto, 3893) | S | S | — | — | ISO week **D** 13850 | stored `status=="missed"` 13914 |

## §3 Identity

Mechanism: `match_zwo` picks by `_TYPE_TO_FALLBACK_CLASSES` (tp 5094-5104: sweetspot→{sweet_spot, threshold, tempo}; threshold→{…, sweet_spot, over_under}; overunder→{over_under, threshold}) and writes `zwo_file`/`zwo_name` only (5586, 5716); `session_type` is never re-derived from the served content (`_session_type_from_row` 7107 is used only by `_make_session_from_row` and the coherence pass). `_classify_card_state` sanctions it: `accepted_by_type["sweetspot"]` includes threshold (14620), and its check is dead anyway — the loaded library rows carry `ContentClass` (4234/4307) but the code reads `meta.get("content_class")` (0 rows) → calendar `content_class` is "" on every cell and a sweetspot slot with `threshold_3x15min…` returns "planned" (verified; with a synthetic lowercase key it returns "missing_workout").

Writers (assignment sites; scan + hand check):

| file | `zwo_file`←real file, no `session_type` re-derive | `zwo_file`←"" (forces rematch) | `session_type` set without touching file |
|---|---|---|---|
| training_planner.py | 6: 642, 5586, 5716, 11391, 11573, 12807 (12804 pairs it with a restamp — the only coherent one) | 27 | 4: 8492, 11540, 12174, 12267 (12174/12267 call `_rematch_eased` → match_zwo → back to the fallback table) |
| app.py | 6: 4109, 4385, 16264, 16484, 16691, 16810 | 6 | 1 handler-set type then match: 4080, 4347, 16456 |
| week_plan.py | 0 | 3 | 2 (435 rest, 505 demote) |

Reproduced through HTTP (`POST /api/plan/save-availability`, one day grown ≥15 % → tp 12026-12100 rescale + re-match, description keeps `s.session_type`): in my run 3 of 6 grown days changed family — 10-20 `overunder 55 min over_under_5x2x2min` → `overunder (88min) — availability adjusted` with `threshold_3x20min-3min_100pct_84min.zwo`; with today frozen to 10-20 the today card reads **OVERUNDER / "overunder (88min)"** and the THIS WEEK cell reads **"Threshold 3x20min (84min)"**, `card_state` "planned", stored duration 88 vs file 84 (the owner's 79/76 pattern). Also 09-15 tempo→sweet_spot file, 11-05 threshold→over_under file. **Caveat:** the reviewer's two runs drew same-family files for the same slots, so the path is deterministic in code but hits only when the random draw lands in the fallback class. At generate, 7/60 filed sessions already differ from served content (all `long_z2` vs `z2`, benign); hard slots also draw files whose *name* misleads the cell (`threshold` slot serving `anaerobic_6x2min…`, `sweetspot` serving `recovery_2x18min…`) — `zwo_name` is the third identity axis.

## §4 Form cards

| Card | Source | Line |
|---|---|---|
| Readiness score input TSB | ICU only (`training.get("tsb")`) | 3803 |
| Header CTL / `loadPlanMetrics` | `_merge_training_load`: ICU else local | 3823, 3701 |
| Today-card readiness | same, but None promoted to **50** when local load exists | 10758-10761 |
| Sparkline "CTL 32 · TSB −12" | `/api/wellness` records (ICU → local wellness file → SQLite) | 4844-4887, JS 5499 |
| Plan tab drift chip | plan `ctl_snapshot` vs merged live | 11236-11246 |
| Calendar on-track band | plan-projected `ctl_planned_today` | summary block 15150 |
| eFTP | `rs.load_recent_wellness` sportInfo | 3084 |
| Local fallback | `compute_local_ctl`/`_compute_local_atl` read **`list_rides()` (legacy `ride_*.json`) not `load_all_rides()`** | ride_storage 1655, app 3652 |

Probe: with ICU metrics failing, `readiness.training` = all None (`source: none`) while the archive holds two TSS-bearing ICU rides (reviewer: 1 ICU ride → `list_rides()` 0, `load_all_rides()` 1, `compute_local_ctl()` None). Simultaneously `/api/readiness` score = **None**, today card readiness = **50**, plan chip "assumes CTL 45", calendar band "planned CTL 36.7", sparkline "—". Four cards, four answers.

## §5 One-object builder must preserve

- Rest-week grading (`tss_target == 0` → 100 %) — `test_v208_rest_weeks.py`, `test_v176_week_offset_overreach.py`.
- Today can be done, never missed; strictly-past missed; `planned_days_total` 7 — `test_v176_week_offset_overreach.py`.
- Cross-sport does not satisfy a cycling day — `test_353_sport_gate_sync_theme.py`.
- Today label from the stored plan, click opens the same day — `test_343_today_identity.py`, `test_homepage_today_consistency.py`.
- Merge stored fields by date overlap, not ISO key (`user_moved`/status/dismissed round-trip) — `test_plan_api.py`, `test_session_payload_display_name.py`.
- Monday anchoring with a short opening week — `test_iso_week_monday.py`, `test_plan_entry_api.py`.
- Calendar dedupe by ISO key, one `is_current`, secondary rides count — `test_calendar_endpoint.py`, `test_this_week_calendar.py`, `test_343_cal_jump.py`.
- TZ-pinned "today" in week-summary — `test_calendar_endpoint.py`, `test_homepage_today_consistency.py`.
- save-availability changed-days-only; generate explicit-blocks-only — `test_v136_availability_restore.py`, `test_availability_reforecast.py`.
- Race-day immutability — `test_issue7_race_calendar.py`, `test_event_fixes_w1/w2.py`.
- auto-adjust `dry_run` no-write — `test_v180_auto_adjust.py`; recalc honours `user_moved` — `test_recalc_preserves_state.py`.
- Push hook once per logical write, active-profile guard — `test_icu_push.py`; ICU stub refusal — `test_ride_detail_zones.py`/`test_hrtss_ingestion.py`.
- Week-offset −1 overreach flags — `test_v176_week_offset_overreach.py` (currently pinned to the phantom target; must be re-pinned to the stored week).

Could not verify: HTTP-1/5/6/8 (not in brief); the exact live 332/79-min record (the owner's profile was not read); `dashboard.html` was read, not rendered — element wiring is from source.
