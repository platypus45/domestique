# Handoff — one week view: Wave 1 done (2026-09-14, night)

For whoever picks this up. Branch `refactor/backend-architecture`, tip named at
the bottom, pushed to the owner's fork.

**Deployed on 2026-09-14 at the owner's request** (a43f5d1c, then the CTL owner
below): production runs the branch, detached in
`~/Documents/cycling-stack/domestique`. The six `ftp_test_*.zwo` repairs are
still working-tree edits there. The pre-deploy plan is kept in the session
scratchpad only; git history has 03237915 to roll back to.

**Owner decisions of 2026-09-14 (night), all done:**
- Deploy Wave 1.
- The morning adapter adapts the plan: `cycling-stack` commit 1d12ba2 applies
  the D8 patch (auto-recalc as POST) and adds step 2b, `POST
  /api/rides/sync?force=1`, in both modes. Checked against a fake Domestique
  that records requests and refuses GETs on write endpoints.
- CTL values from intervals.icu (P7): `src/fitness.py`, commit 070ad407;
  see "After Wave 1" below.

## Read first, in this order

1. `notes/overhaul-plan.md` — the plan of record; D1–D8 and the owner's
   2026-09-14 decisions are not to be relitigated without new evidence.
2. `notes/week-view-contract.md` — P1–P10, A1–A14; its status block says
   which assertions are met.
3. `notes/review/architecture-audit-2026-09-14.md` — five root causes, five
   waves; Waves 0 and 1 are done. The six reports under
   `notes/review/audit-2026-09-14/` carry the file:line evidence.
4. This file.

## What Wave 1 did (every card that shows the week reads one object)

Each step was gated against a same-day clean-main baseline (8 known failures,
0 new), the characterization compared unchanged (217 cases, 209 after step 7
removed the dead builder's 8) and its self-test passed; three independent
adversarial reviews drove the API and the dashboard, and every finding was
answered with a test that fails on the code it found.

| step | commit | what |
|---|---|---|
| 1 | b859b825 | `/api/today-session` reads the week view (the served file's type, not the slot label); one plan read; Monday's "yesterday" is last week's Sunday |
| 2 | ca1b6cda | `/api/calendar` cell type from the view; a week with no plan on record is `planned_tss: null`, not graded (adherence `None`, no overreach); dashboard draws "—" and "no plan on record" |
| review | c81505ff | calendar `content_class` stays `""` (filling it refused 624 of 4,307 files in the dashboard's own gate); `long_z2` kept for a long slot on endurance content; `has_plan` on a day with no session |
| 3 | d6b59ab1 | one type→exposure-band table, `week_view.TYPE_BAND`; the week summary's planned bands are the view's |
| 4 | 7300d0a5 | Generate keeps what was prescribed before it, in `plan["replaced_weeks"]` (read by the view and the calendar only, never a Regenerate's `past_weeks`) |
| 5 | b1ccb8be | the dashboard reads one object: `getResource` shares in-flight reads; tiles from `/api/week-summary`; client reconstructions deleted; one This Week painter; Plan-tab Actual from the calendar |
| 6 | 82d3505c | no GET writes: the lazy sync never adapts; POST `/api/rides/sync` adapts once and runs the deload advance; auto-recalc is a POST; eFTP auto-apply after the 30-min sync loop; Home POSTs the sync; a completed write forgets the shared reads |
| 7 | 95528598 | `generate_weekly_plan` deleted (345 lines) |
| review | 34072406 | skipped/moved days in the strip with undo; stub-row Plan-tab Actual; calendar dedupe prefers the current plan's row; `zwo_name` on today's planned |
| review | (the commit carrying this note) | rides the lazy sync stored adapt on the next POST (`adapted_ride_total`); a stamp-only write is not `plan_adapted`; a stored row beats a history shell in the calendar dedupe; the eFTP apply fetches only when opted in, and is tested in the sync loop |

## After Wave 1: one fitness owner (070ad407)

`fitness.state()` is the only CTL/ATL/TSB source: intervals.icu live, else its
last values in the SQLite wellness table (dated, at most 7 days old), else
unknown. The readiness card, the composite readiness and training severity
(today's TSB), the calendar's actual CTL, the programme summary's CTL gain and
every planning call read it; the planned-CTL curve starts from the plan's
generation-time ICU anchor (`ctl_snapshot`). The local EWMA over the FIT-only
ride list is no fallback; the 30/37/50 constants are one
`_PLANNING_CTL_UNKNOWN` (37). Contract A8 is met by
`tests/test_one_fitness_state.py`. Still reading the local archive: the
planner's own fallback inside `generate_plan` when handed None (planner
internals, Wave 3), and `ride_storage.compute_local_ctl` itself.

## Instruments built this session (reuse them)

- `tests/test_reads_do_not_write.py` — every parameterless GET under /api/
  against a fixture that trips each known writer; records plan writes and the
  eFTP apply, hashes the plan. It named five GET writers on b1ccb8be.
- `tests/test_calendar_cells_open.py` — one stored session per library
  content class through `/api/calendar`, then the dashboard's own
  `calContentMatches`/`calContentCss` under node.
- `tests/test_dashboard_one_object.py` — the shared-read memo under node
  (coalesces, never serves a finished response, forgets after a write).
- `tests/test_generate_keeps_history.py`, `tests/test_week_view_agreement.py`
  (A1, A2, A6, P4), the strip and grid harnesses in `test_344_plan_grid_parity.py`.
- **The sandboxed dashboard over CDP** (scratchpad scripts, not in the repo;
  recreate from this description): boot the worktree's `src/launcher.py
  --server-only` with `HOME=<tmp>` and `DOMESTIQUE_PORT=<spare>`; wait until
  the port is actually bindable first (TIME_WAIT sockets from the previous run
  make the launcher exit, and a page then loads from nowhere); seed
  `<HOME>/.domestique/profiles/default/plans/current_plan.json`,
  `.domestique/.setup_complete` and ICU ride JSONs under
  `profiles/default/rides/icu/`; drive headless Chromium with
  `--remote-debugging-port` from a small `websockets` script that records
  `Network.requestWillBeSent` (Fetch/XHR), console errors, `document.body.innerText`
  and a `Runtime.evaluate` of any expression. This is how the fetch counts,
  the painter swap, the stale Plan-tab Actual and the skip state were seen.
  The launcher runs uvicorn at log level warning, so the server log has no
  access lines; count requests in the browser.

## Measured on the sandboxed dashboard

- Home-load requests: readiness 3→2, activities 2→1, settings 2→1,
  icu/connection 2→1. Repeats that remain start after the first response is
  fully in (sharing them would need a cache that could go stale after a write).
- Home page text identical before and after steps 5 and 6; 0 console errors.
- After step 6 a Home load issues `POST /api/rides/sync`; when it adapts, the
  Today card and the week repaint, and the Today read is a fresh request.

## Deployment notes

- D8: applied in `cycling-stack` 1d12ba2 with the deployment (the patch file
  stays in `notes/deploy/` as the record).
- Scheduled adaptation: the morning adapter's step 2b (1d12ba2). The server's
  30-min loop (`db.run_sync`) never adapts, and `cs-repair-strava-husks`
  POSTs `/api/rides/sync?force=1` only when it repaired a husk. (Step 6's
  commit message claimed a 30-min server-side adaptation; the third review
  showed it false.)
- Opening Home still adapts the plan, through a POST; the stack README says
  so since 1d12ba2.
- Deploy as before: discard the two workout caches, `git checkout --detach
  <sha>`, `systemctl --user restart domestique`, check `/api/diag/health`.
  The six `ftp_test_*.zwo` repairs are working-tree edits there; never reset
  them. `cs-update.timer` stays disabled.

## What is left

- **Wave 1 step 8, identity at write time (P9)** — Wave 3 territory: `match_zwo`
  picks from `_TYPE_TO_FALLBACK_CLASSES` and never re-derives `session_type`;
  about 130 write sites. The readers all derive from the served file now, so
  every card agrees; do not start the writer side before the planner package.
- **Contract assertions not yet met**: A1 across all six endpoints for done
  load, budget and boundary (planned load and today's label are covered);
  A2 on the write side; A3 (no GET invokes the sampler — implied for the known
  paths by A4, not asserted); A7, A14 (Wave 2). A8 is met (070ad407).
- **Wave 2** (one owner per fact): `notes/review/audit-2026-09-14/state.md`,
  "One owner per fact"; the fitness row is done; S-1 (FTP) next.
- Then Wave 3 (structure, Step 6 of the plan inside it), Wave 4 (branching),
  Wave 5 (performance, tests).
- GETs still write non-plan state (outside P8's plan file, seen by the third
  review's sweep of 431 GET URLs): `/api/ride/{id}/prs` rewrites the ride
  record, `/api/profile/power-curve` backfills ride records,
  `/api/profile/pr-toast-queue?drain=` writes `last_pr_toast.json`,
  `/api/update/check` its cache. Wave 2 (D8's spirit). The sweep script is
  worth rebuilding into a test: boot on a sandbox HOME, GET every openapi path
  with path and query variants, hash every file under HOME after each.
- `cs-preflight`'s comment still calls auto-recalc a GET (comment only).
- `tp._SESSION_TYPE_TO_BAND` (five user-facing intensity bands) disagrees with
  the exposure table on over-unders (threshold vs anaerobic): a science
  question for Wave 3's `science.py`.

## Hazards

- **The live checkout is production.** Agent `isolation: "worktree"` lands
  inside it; make scratch worktrees by hand (`git worktree add --detach
  <scratchpad>/<name> <sha>`, symlink `.venv`).
- Push by URL: `git -c credential.helper='!gh auth git-credential' push
  https://github.com/taladjidi/domestique.git refactor/backend-architecture`.
- The known-failures list is 8 lines. A ninth means something.
- Run tests under pytest only (the conftest sandboxes HOME); restore
  `src/workouts/.library_index.json` and `.workout_facts.json` afterwards.
- `test_pr_detection`'s real-envelope test skips when the owner's envelope is
  absent from the sandbox; that skip predates Wave 1.

## Open for the owner

- Whether the Last-week card should show the budget beside the prescribed load.
- The Today card's description still carries the slot's old words ("sweetspot
  (79min)") over a threshold file: the stored description, written by the
  planner. It goes with Step 8 (identity at write time), Wave 3.

## Tip at handover

See the commit that carries this note.
