# Handoff — one week view, and the audit programme (2026-09-14, evening)

For whoever picks this up. Branch `refactor/backend-architecture`, tip named at
the bottom, pushed to the owner's fork. Production runs **03237915**, detached
in `~/Documents/cycling-stack/domestique`; the owner is testing that build on
their own rides this week before the rolling programme starts, and wants it to
stop "breaking every day".

## Read first, in this order

1. `notes/overhaul-plan.md` — the plan of record; decisions D1–D8 and the
   owner's 2026-09-14 decisions (in "The load the rider carries") are not to
   be relitigated without new evidence.
2. `notes/week-view-contract.md` — principles P1–P10, assertions A1–A14. The
   owner read it and said start; the contract is what gets graded.
3. `notes/review/architecture-audit-2026-09-14.md` — five root causes, five
   waves, and Wave 0 done; the six reports under `notes/review/audit-2026-09-14/`
   carry the file:line evidence for everything below.
4. This file.

## Where the code stands

Wave 0 is done and gated (8 known failures, 0 new against a same-day
clean-main baseline; characterization 217 cases unchanged; self-test PASS):

- **One clock** (`src/clock.py`, 04da2976). Every module reads
  `clock.today()/now(tz)/utcnow()`; nothing outside the module reads the
  datetime module for the current moment (grep 0). Tests pin with the
  `freeze_clock` fixture or `clock.freeze(date)`. Thirty-one suites still pin
  by replacing `tp.date` / `app.date` / `app.datetime`; `tests/conftest.py`
  mirrors the first such pin into the clock (test-side compatibility only;
  `clock.freeze` wins). `_gate_env` freezes the clock directly.
- **The agreement tests** (`tests/test_week_view_agreement.py`). A1 passes
  since 324cd038; A2 (today's label is the served file's) and P4 (last week
  has one answer) are strict expected failures that must be unmarked as you
  land steps 1 and 2 below; a control test proves the fixture.
- **The gate judges**: `tests/characterize_planner.py` fails on any new
  invariant hit and prints them by rule; `reforecast@21` exercises the
  availability block. `tools/gate.sh <worktree> <label> [baseline-label]`
  runs the full suite and diffs against a baseline; output dir `$GATE_OUT`
  (default `/tmp/domestique-gate`). Run the clean-main baseline **the same
  day** as the branch (`git worktree add --detach <dir> clean-main`, copy
  `tests/known-failures-clean-main.txt` into it, run the gate with a label,
  then the branch with that label as third argument).
- **FIT export** (fc5f1edb): durations through the seconds sub-field; the
  known-failures list is 8 lines, all environmental (pywebview, TLS fixture).
- **`cache.cached`** is single-flight per key (c49d7796).
- **First Wave 1 seam** (324cd038, gated: 8 known failures, 0 new against
  the same-day clean-main baseline): `src/week_view.py` and `/api/weekly-plan`
  reading it. `week_view.build(plan, monday, lib_by_file, tp._session_type_from_row,
  naming=...)` returns a `WeekView`: `planned_tss` (Σ stored sessions),
  `budget` (the row's `tss_target` when one row owns the week), `on_record`,
  seven `sessions` (stored dicts plus `slot_type` = stored label,
  `session_type` = the served file's content type, `zone_minutes` per band,
  `display_name`), `exposure_minutes_planned`. `as_weekly_plan()` keeps the
  old response keys and adds `budget`, `planned_tss`, `on_record`.
  `tss_target` in the response is the planned load.

## The remaining edits, in order (Wave 1: every card reads one object)

Each step: change, verify with the named tests, run `tools/gate.sh` against a
same-day baseline, commit with the measurement in the message. Never bless the
characterization without reading the diff; this wave is HTTP-layer only, so
it should not move at all.

1. **`/api/today-session` reads the view** (`src/app.py:10465`,
   `api_today_session`). Today it calls `api_weekly_plan(0)` then re-reads the
   plan file and builds `planned` from the stored dict's `session_type`
   (`planned_data = next(... _stored ...)`, ~10490-10510). Take
   `planned_data` from `week_data["sessions"]` (the view; `on_record` True),
   so the label is the served file's type; drop the second plan read; drop
   the "regen fallback" (there is no regenerated week any more). Yesterday's
   planned TSS (~10600, `week_data`) is already the stored week's. Then
   remove `@unittest.expectedFailure` from
   `test_todays_label_is_the_served_file`. Verify: `tests/test_343_today_identity.py`,
   `tests/test_homepage_today_consistency.py`, `tests/test_week_view_agreement.py`.
   Leave `_maybe_advance_continuous_deload` (a GET that writes, 10300) in
   place for step 6.
2. **`/api/calendar` reads the view** (`src/app.py:15522`, and
   `merge_plan_with_rides` 15087). (a) The cell's `session_type`
   (`planned_payload`, ~15380) becomes `week_view.derived_type(sess,
   lib_by_file, tp._session_type_from_row)`; its `content_class` reads
   `meta.get("content_class")` but library rows carry `ContentClass` (the
   readers report §3) — read the row's `ContentClass`. (b) History rows
   (`history_weeks`, 15153) carry `tss_target: 0`; the per-week summary
   (`"planned_tss": round(planned_tss, 1)`, 15468) must emit `None` when no
   stored row covers the week (`_synthetic_history` on every session).
   (c) `/api/week-summary?week_offset=-1` already gets `tss_target None`
   from the view for an unrecorded week; check `tss_adherence_pct` and the
   overreach block treat None as "no plan", not as 0 (`9567`, ~9620-9650).
   Then unmark `test_last_week_has_one_planned_answer`. Verify:
   `tests/test_calendar_endpoint.py`, `tests/test_this_week_calendar.py`,
   `tests/test_v176_week_offset_overreach.py`, `tests/test_v208_rest_weeks.py`.
   The dashboard's history-row pill (`dashboard.html` ~13884, `${aTss} / ${pTss}`)
   must render `—` for a null planned, and `loadLastWeekFeedback` (7903) must
   say "no plan on record" for a null target.
3. **One band map.** `/api/week-summary`'s planned exposure
   (`exposure_minutes_planned`, 9787) sums by `_SESSION_TYPE_TO_BAND` (9459);
   replace it with the view's `exposure_minutes_planned` (already in the
   weekly-plan response). Then delete `app._SESSION_TYPE_TO_BAND`,
   `tp.SESSION_TYPE_TO_BAND` (15273) and `tp._SESSION_TYPE_TO_BAND` (2665)
   where their only readers were these, and the dashboard's copy (~17513,
   inside the client-side reconstruction — see step 5). `tests/test_training_planner.py:367`
   pins one map against the dashboard text: re-pin it against
   `week_view.TYPE_BAND`. Contract A6.
4. **Generate keeps elapsed weeks** (`api_plan_generate`, 11413). Regenerate
   already keeps `past_weeks`; Generate replaces the file. Before writing the
   new plan, carry over the old plan's rows that end before today (mark them
   `carried_from_generate: <iso>`), so the calendar's history rows and the
   Last-week card read what was prescribed. Contract A5; write the test first
   (generate on a Thursday over a plan that had last week).
5. **The dashboard reads one object.** Delete the three client-side
   derivations: `lastWeekActs` (7160-7175, the Activities/Duration tile
   deltas come from `/api/week-summary?week_offset=-1`), the client-side
   week-summary reconstruction (17447-17560), the Plan-tab actual derivation
   (~13143-13155, use the calendar's `actual_tss`). Then one fetch per
   resource per load: `readiness` ×3, `activities` ×2, `week-summary` ×2,
   `settings` ×2 today (`loadHome` 6982, `loadWeeklyCalendar` 17162,
   `loadReadinessComposite`, `loadLastWeekFeedback`) — a `getResource(path)`
   memo cleared on each home load is enough; no framework. `renderThisWeekFromCalendar`
   (14821) and the weekly-plan painter both paint `#weekly-calendar`; keep one.
   Contract A10. Verify with a headless Chromium DOM dump (see "Instruments").
6. **Reads do not write** (P8, D8). `/api/today-session` →
   `_maybe_advance_continuous_deload` (10300) writes the plan on a GET; move
   it to the ride-sync adapt path or a POST. `/api/weekly-plan`'s eFTP
   auto-apply (`_guarded_check_and_auto_apply_eftp`, 9314) writes the profile
   and clears the cache on a GET; move it to the sync loop. `GET /api/plan/auto-recalc`
   (16716) → POST, and change `~/Documents/cycling-stack/bin/cs-domestique-adapt`
   in the same deployment (the owner's D8 decision). `_kick_lazy_icu_sync`
   (18471) from three GETs: keep the kick, but it must never write the plan.
   Contract A4: hash `current_plan.json` before and after every GET.
7. **Delete `generate_weekly_plan`** (`training_planner.py:15591`) once
   nothing reads it (grep; `api_today_session`'s fallback is the last).
8. **Identity at write time** (P9) is Step 6 territory: `match_zwo`
   (5154) picks from `_TYPE_TO_FALLBACK_CLASSES` (5095) and never re-derives
   `session_type`; ~130 write sites of the two identity fields. The view's
   reader-side derivation makes every card agree until the writers are one
   `serve_file()`; do not start the writer side before Wave 3's planner
   package, and do not "fix" the stored label piecemeal.

Then Wave 2 (one owner per fact: `notes/review/audit-2026-09-14/state.md`,
its "one owner per fact" table; S-1 FTP shadow first, it is a one-line
delete at `app.py:1878` plus the `config.__getattr__` proxy), Wave 3
(`architecture.md`'s migration order 1–8; Step 6 lives in it), Wave 4
(`clarity.md`'s ten refactors; the four dead flags first), Wave 5
(`performance.md`'s eight and `tests.md`'s eight).

## Instruments

- The live dashboard, headless: `chromium-browser --headless=new --disable-gpu
  --no-sandbox --virtual-time-budget=20000 --dump-dom http://127.0.0.1:22400/`
  then strip tags and grep the text (this is how both of the owner's
  reported cards were found). Only against the live service when the owner
  agrees; it triggers the lazy sync and the backfill (performance #2).
- Under pytest only. `tests/conftest.py` sandboxes HOME and bootstraps a
  profile; a plain `.venv/bin/python` probe reads the owner's live profile and
  builds different plans. Put probes under `tests/` as `test_zz_*.py`, run
  with `-s`, delete them.
- Run tests as: `PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 uv run --quiet
  --with pytest --with pytest-timeout --with pytest-xdist --with fitparse
  --with Pillow --python .venv/bin/python -m pytest -p no:cacheprovider ...`
  and afterwards `git checkout -- src/workouts/.library_index.json
  src/workouts/.workout_facts.json` (tests rewrite them).
- Adversarial review before handing anything back: an agent in a hand-made
  scratch worktree (`git worktree add --detach <scratchpad>/<name> <sha>`,
  symlink `.venv`), briefed that commit messages and comments are claims and
  to verify by running; it returns the report as its final message. Never
  `isolation: "worktree"` (it lands inside the live checkout).
- The cloud ultrareview only the owner can launch, from
  `~/Documents/domestique-refactor`, as `/ultrareview review-base` with a local
  branch placed so the diff is under 8,000 lines (a bless is 20k+). Its
  report lands as an untracked file under `notes/review/`.

## Hazards

- **The live checkout is production.** Deploy only what was measured on the
  owner's archive and only when they ask: discard the two workout caches,
  `git checkout --detach <sha>`, `systemctl --user restart domestique`, check
  `/api/diag/health`. The six `ftp_test_*.zwo` repairs are working-tree edits
  there; never reset them. `cs-update.timer` stays disabled.
- Push by URL, nothing else holds credentials:
  `git -c credential.helper='!gh auth git-credential' push https://github.com/taladjidi/domestique.git refactor/backend-architecture`.
- The known-failures list is 8 lines. A ninth means something.
- `test_tid_plan_properties`' intensity rail is a strict expected failure
  for Step 6; the easy floor there breaches 2 weeks in 288 at the pinned
  seed's load and passes by the choice the suite documents.
- The characterization golden holds 93 blessed invariant hits from before
  the compare asserted them; the compare fails only on new ones.
- The owner's chronic load decays while rides are unsynced (254 on 09-14
  against 281 the day before, newest ride 09-09); a "wrong" first-week budget
  may be a sync gap, not a planner bug.

## Owner decisions in force (2026-09-14)

Ship after the fixture fix (done); headroom is easy volume only (Step 6,
M6); L1 fresh runway, guarded; D8 to POST with the adapter; the audit
programme before Steps 5b and 6; no relitigating D1–D8.

## Open for the owner

- Which CTL is the real one for P7: local Banister over their archive, or
  intervals.icu's. Everything else in Wave 2 can proceed without it.
- Whether the Last-week card should show the budget beside the prescribed
  load once history is kept (step 4), or the prescribed load alone.

## Tip at handover

`b9391598` plus this note's commit, on the owner's fork. Production: 03237915.
