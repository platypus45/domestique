# One week view — the contract (DRAFT, 2026-09-14, revised after the audit)

Status (2026-09-14, night): Wave 1 implemented, gated and reviewed three
times (`notes/handoff-week-view.md` has the commits). The six-lens audit
(`notes/review/architecture-audit-2026-09-14.md`) verified every premise and
added the assertions marked *audit*. Assertions are what gets graded; the
implementer never grades its own work.

| assertion | state | where |
|---|---|---|
| A1 | planned load and today's label agree across weekly-plan, week-summary, calendar, today-session; done load, budget and boundary across all six not yet asserted | `test_week_view_agreement.py` |
| A2 | reader side met (every card derives the type from the served file); write side open (Step 8, Wave 3) | `test_week_view_agreement.py`, `test_calendar_cells_open.py` |
| A3 | open (implied for the known paths by A4, not asserted) | |
| A4 | met for every parameterless GET | `test_reads_do_not_write.py` |
| A5 | met (`replaced_weeks`) | `test_generate_keeps_history.py` |
| A6 | met; `tp._SESSION_TYPE_TO_BAND` (five intensity bands) kept, see the handoff | `test_training_planner.py::TestSessionTypeToBandLockedValues` |
| A7, A14 | open (Wave 2) | |
| A8 | met: one `fitness.state()`, intervals.icu (the owner's decision) | `test_one_fitness_state.py` |
| A9 | met at every step (0 new failures; characterization unchanged) | the handoff |
| A10 | met; residual sequential repeats measured | `test_dashboard_one_object.py` |
| A11–A13 | met in Wave 0 | |

## Why

On 2026-09-14 the live home page said, for the same Monday, "SWEETSPOT
(79min) — availability adjusted" in the Today card and "Threshold — 3×15min
@100%" in the This Week list; and for the same past week, "369 / 0" in the
calendar's history row and "369 / 332" in the Last-week card. The stored
record for that Monday carries `session_type="sweetspot"` beside
`zwo_file="threshold_3x15min-5min_100pct_76min.zwo"`. Each card reads its own
derivation, and the one object they share contradicts itself.

The adversarial HTTP review (`notes/review/http.md`, 2026-09-10) named the
class: HTTP-2 (a second planner answers "what is planned"), HTTP-3/12/13
(zone bars by label, four band maps), HTTP-7/14 ("done", "which week" and
"planned" defined per endpoint), HTTP-11 (21 hand-written dict↔domain
sites). The plan of record scheduled it as Step 8 and part of Step 7. The
owner's decision (2026-09-14): do it now, before Steps 5b and 6.

## Principles

- **P1 One store.** The persisted plan is the only source of "what is
  planned". No read endpoint runs a planner to answer. The second planner
  behind `/api/weekly-plan` (`generate_weekly_plan`) stops answering reads.
- **P2 One identity.** A session is its served file. Its type, label, band
  minutes and TSS are derived from that file's content classification by one
  function at the moment the file is served. No writer sets `zwo_file`
  without that derivation, and no writer sets `session_type` without serving
  a file that matches it. (R2, Step 1, extended to every writer.)
- **P3 One derivation per question,** in one module, `src/week_view.py`:
  the week boundary; planned load of a week (Σ stored sessions) and the
  week's budget (the ramp's `tss_target`), named apart; actual load; "done";
  planned zone minutes (from served files); actual zone minutes (from ride
  `time_in_zone`); today's session and its adaptation as derived state.
- **P4 The store remembers.** A Generate keeps the elapsed weeks of the plan
  it replaces (Regenerate already does). A week with no record reads as "no
  plan on record", never as 0 and never as a re-derived phase target.
- **P5 Endpoints are views.** `/api/plan`, `/api/weekly-plan`,
  `/api/week-summary`, `/api/calendar`, `/api/today-session`,
  `/api/programme/summary` serialise the same `WeekView` objects. Response
  shapes the dashboard depends on are kept; client-side re-derivations of
  planned or actual are deleted.
- **P6 One conversion.** One `PlannedWeek`/`PlannedSession` ↔ dict pair,
  round-tripping every field; `Goal` from a plan dict by one function.
- **P7 One form.** Header strip, Training Load card and readiness read the
  same CTL/ATL/TSB, from one named source; the plan's projected CTL is
  labelled a projection wherever shown.
- **P8 Reads do not write.** No GET changes the plan file. Auto-recalculate
  moves to POST and `cs-domestique-adapt` changes in the same deployment (D8).
- **P9 One identity writer.** `serve_file(session, row)` is the only code
  that sets `zwo_file`/`zwo_name`, and it sets `session_type` from the row's
  content class. The fallback class table may change the file; it may not
  leave the label behind (*audit*: readers §3, about 130 write sites today).
- **P10 One activity source and one fitness state.** `load_all_rides` is the
  only ride source readers see; `fitness_state()` is the only CTL/ATL/TSB
  source, with its origin and age, and no 30/37/50 constants (*audit*: state
  S-2/S-3/S-4).

## Assertions (each becomes a test that fails on today's code)

- **A1** For any plan and any date, the ISO week's planned TSS, done TSS,
  budget, boundary and today's label are equal across the six endpoints of
  P5. Driver: `with TestClient(app)`, a weekday-pinned today (a Monday pin
  hides half the disagreements), one goal, rides seeded identically in the
  ICU archive, SQLite and the metrics cache.
- **A2** For every stored session with a served file,
  `session_type == the type derived from that file`. Checked by
  `plan_invariants` on every write path and by the characterization's
  invariants over all 217 cases.
- **A3** No GET handler invokes the sampler: `match_zwo` patched to raise,
  every read endpoint still answers.
- **A4** No GET changes the plan file (hash before and after each read
  endpoint, auto-recalc included once it is POST).
- **A5** After Generate on a Thursday over an existing plan, the elapsed
  rows of the old plan are still in the store, the calendar's history row for
  that week shows their planned load, and a week with no record serialises
  `planned: null`.
- **A6** One type→band map exists; the three others are gone (grep is the
  test), and the home zone bars equal the served files' zone minutes.
- **A7** Every session field, including `user_moved`, `user_swapped`,
  `dismissed_at`, `completion_matches`, `nutrition_note`, survives a
  dict→domain→dict round trip through the one converter; the six inline
  builders in app.py are gone.
- **A8** Header CTL, Training Load CTL and the readiness TSB come from one
  call; a probe that changes the ICU value alone moves all three or none.
- **A9** Planner characterization unchanged (this is the HTTP layer); the
  gate shows 0 new failures against a same-day clean-main baseline.
- **A10** The dashboard's `lastWeekActs` tile, the Plan-tab actual
  derivation and the client-side week-summary reconstruction are deleted;
  every week or today card reads the one fetched object.
- **A11** (*audit*) The cross-endpoint agreement test of A1 exists and fails
  on today's code before Wave 1 starts; it is the first commit.
- **A12** (*audit*) The characterization compare fails on any new invariant
  hit, and its self-test passes, before the first bless of Wave 1.
- **A13** (*audit*) A date-pinned test never reads the real clock: one autouse
  fixture freezes `app.date`, `tp.date` and `datetime` in both modules.
- **A14** (*audit*) FTP has one owner: after an accepted FTP test, a manual
  FTP edit and a profile switch move every reader (settings, zones, session
  targets, `ftp_at_ride`) or none.

## What must be preserved (http.md §4 plus the audit's readers §5, each with its pinning test)

save-availability's changed-days-only and generate's explicit-blocks-only
semantics; FC3 race-day immutability in every mutator; `user_moved` /
`user_swapped` / `dismissed_at` round-tripping; week-summary's rest-week
grading and "today can be done but never missed"; the ICU push hook once per
logical change; auto-adjust's dry-run isolation; the ICU-stub refusal.

## Out of scope

The write side of the week (the builders, the post-passes, `TrainingWeek`)
is Step 6. This contract fixes what every reader sees and how the store is
kept; it does not change how a week is built.
