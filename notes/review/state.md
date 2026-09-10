# Domestique — runtime state, concurrency and persistence (lens STA)

Code: `ca6091f9` (scratch worktree `rev-state`). Every finding below was measured
in a sandbox. `review/sbx.py` points HOME and DOMESTIQUE_HOME at a fresh temp dir,
blocks the network, runs the first-boot sequence, and then **asserts** that
`tp.PLAN_DIR`, `paths._plan_dir()`, `paths.DATA_DIR`, `db.DB_PATH`, `pm.plan_dir`
and `pm.active_dir` resolve inside that dir and outside `~/.domestique`. The
repros are `review/r1…r7*.py`, with raw output in `review/r*.out`. To re-run one:

```
cd rev-state && PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 .venv/bin/python ../review/r1_distribution.py
```

Nothing in production was touched. The only change to the worktree is
`src/workouts/.library_index.json`, which the planner self-heals on first load.

## 0. How production actually runs requests (verified)

| fact | evidence |
|---|---|
| one process | the systemd unit runs `launcher.py --server-only`, which reaches `start_server()` (launcher.py:963) and builds `uvicorn.Config(app, host, port, log_level)` with no `workers` (launcher.py:440-444). `uvicorn.Config(...).workers` was printed as **1**. |
| the single-worker assert is not what enforces that | app.py:20944-20960 sits under `if __name__ == "__main__"` and never runs in production |
| one process is **not** one thread | 115 `def` handlers run in the AnyIO threadpool (`total_tokens` printed as **40**). 33 `async def` handlers run on the event-loop thread. app.py has **no** `run_in_threadpool` / `to_thread`, and none of the four middlewares (app.py:818-885) serialises requests. |
| so what overlaps | any async plan handler (generate, reforecast, rematch, dismiss, swap…) with any sync handler (GET auto-recalc, GET today-session, POST rides/sync, GET calendar…) and with the daemon threads: lazy ICU sync `domestique.icu_sync` → `_maybe_auto_reforecast`, db `sync`, the ICU push `Timer`, DFA backfill. Observed thread names in the repros: `asyncio-portal-…` (async) and `AnyIO worker thread` (sync). |
| automation (read from bin/) | 04:45 `cs-domestique-adapt` sends `POST /api/sync`. In **apply** mode it then sends `GET /api/plan/auto-recalc` (writes), `POST /api/plan/reforecast` and `POST /api/plan/auto-adjust`; in **observe** mode (current `stack.conf`) only `auto-adjust dry_run:true`. It finishes with `POST /api/icu/push`. `cs-repair-strava-husks` (every 30 min, 06:00–23:30, only when a husk is present) sends `POST /api/sync` and `POST /api/rides/sync?force=1`, and the latter writes the plan through `_maybe_auto_reforecast`. `cs-healthcheck` only reads. |
| the UI races itself | On every Plan-tab open, dashboard.html:15511 fires `POST /api/rides/sync` **without awaiting it**, then awaits `POST /api/plan/rematch?apply=1` at :15522. Both write the plan. |

---

## 1. Findings (most severe first)

### STA-1 · CRITICAL · The intensity model is a process global; a concurrent request changes it mid-plan, and the wrong plan is persisted

- **Where.** training_planner.py:2250-2258 (`_ACTIVE_DISTRIBUTION`, `_ACTIVE_CUSTOM_BUDGETS`, `_VO2_MICRO_ONLY`), the setters at :2293 and :2309, and the only readers, `active_model_for_phase` (:2337) and `_active_budget_table` (:2349, via `get_budget_for_phase` :2550).
- **Written once per request by:**
  - `generate_plan` :8271-8273
  - app.py:12231-12234 (reforecast)
  - :12513-12514 (`_regenerate_plan_dict`)
  - :12933-12935 (`_apply_plan_update`)
  - :16985 (swap-type, micro flag only)
  - :17346-17347 (auto-recalc)
- **Read but never set by:**
  - app.py:15575 (`_build_summary_block`)
  - :15996 (`merge_plan_with_rides` ← `GET /api/calendar`)
  - :10576 (`_advance_continuous_deload` ← `GET /api/today-session`, which writes the plan)
- **What is wrong.** State that belongs to one plan computation is stored process-wide and read implicitly ~50 times, deep in the call tree. The value in effect is whatever the most recent `set_*` call on any thread left there.
- **Evidence, function level (r1_distribution.py → r1.out).** Seeds were fixed, so plans are deterministic.
  ```
  baseline polarized fp=31600a209a14 hard_min=1593  repeat fp=31600a209a14  deterministic=True
  baseline threshold fp=9851d49cbc62 hard_min=1741  differs_from_polarized=True
  planted fault: foreign reads 49/53 seen=['threshold']  fp=e31a77772d2d changed=True   <- instrument fires
  clean run:     foreign reads 0/51  fp=31600a209a14 changed=False                   <- and is quiet
  CONCURRENT generate(polarized) || real api_plan_auto_recalc() on a threshold plan, 5 trials:
  trial 0..4: A fp=9ca4908e3a24 changed=True hard_min=1438 (base 1593) | A foreign 50/52 ['threshold']
  CONTROL concurrent, disk plan also polarized, 5 trials: fp unchanged, 0/51 foreign
  CONTROL same pair serialised, 3 trials:                 fp unchanged, 0/51 foreign
  ```
- **Evidence, HTTP level (r2_handlers.py → r2.out).** Two real TestClient requests on two threads.
  ```
  serial:      GENERATE 0/55 foreign, AUTORECALC 0/54 foreign
  auto-recalc issued while generate plans:  GENERATE 54/54, 53/55, 53/55 foreign ['threshold']
  generate issued while auto-recalc plans:  AUTORECALC 52/54, 55/55, 55/55 foreign ['polarized']
  ```
  In the reversed case, auto-recalc writes to disk a plan re-budgeted under the other request's model.
- **Consequence.** The athlete is served, and the disk keeps, a plan whose hard-minute budget came from a different distribution: 10% fewer hard minutes in the measured pair. Direction and size depend on the pair.
  - Exposure: any two overlapping computations with different models. The obvious one is a new goal generated while any background recalc, reforecast or auto-adapt runs on the old plan.
  - Legacy plans are also exposed. A plan without a `distribution` key gets `'auto'` from `Goal` (training_planner.py:1696) and `generate_plan` (:8272), but `'polarized'` from app.py:12233, :12766 and :12934. Two writers of the same plan therefore contaminate each other.
  - Display and deload readers show or persist the last-set model. After a restart that is `'auto'`, not necessarily the plan's model (from code; not separately reproduced).
- **Structural.** Structure: generation-scoped parameters are held as module globals and read implicitly. Owner: the computation's own context (`WeekContext` / `PlanState` / the `Goal` it is planning for), passed as a value. `set_active_distribution` and `set_vo2_micro_only` should cease to exist.

### STA-2 · CRITICAL · 20 of 23 plan writers read-modify-write outside the lock: edits and whole new plans are silently reverted

- **Where.** training_planner.py:430-459. `atomic_write_plan` takes `_plan_write_lock` for the rename only. Of the 23 functions that write `current_plan.json`, only 3 hold the lock across load→mutate→save: `api_readiness_revert_cap`, `_maybe_auto_reforecast` (app.py:12104) and `api_plan_update` (:13311). The full table is in `review/lock_table.txt` (AST scan: `python3 review/lock_table.py rev-state/src/app.py`). The other 20 load outside the lock and save through it, including:
  - `api_plan_auto_recalc` (sync GET; read :17265, write :17429)
  - `api_plan_generate` (:11796 / :12051)
  - reforecast (:12158 / :12267)
  - dismiss (:17222 / :17247)
  - rematch (:16364 / :16383)
  - move, swap-type, re-draw, add-race, save-availability, auto-adjust, today-session → `_advance_continuous_deload` (a GET that writes)
- **Evidence (r3_lost_update.py → r3.out).** Real handlers; the second request is released when the first enters its natural ~1 s compute, with no sleep planted inside code under test.
  ```
  R3a auto-recalc (sync) vs dismiss-session (async), target day 2026-09-12
  serial dismiss->recalc: D status on disk=['dismissed']
  serial recalc->dismiss: D status on disk=['dismissed']
  CONCURRENT trial 0..2:  dismiss={'ok': True, 'dismissed': True} ... D status on disk=['pending']   (3/3)

  R3c generate NEW goal (async) vs auto-recalc of the OLD plan (sync)
  serial generate->recalc: on disk NEW-plan ; serial recalc->generate: on disk NEW-plan
  recalc starts 0.0/0.3/0.6 s after generate entered the planner: ON DISK NEW-plan
  recalc starts 0.9 s: UI got (200, 'NEW-plan') ... ON DISK: OLD-plan
  recalc starts 1.2 s: UI got (200, 'NEW-plan') ... ON DISK: OLD-plan
  ```
- **Consequence.** In both cases the UI gets a success response while the file holds the other writer's stale snapshot:
  - A dismissal is lost.
  - A freshly generated plan for a new goal is replaced by a recalculated copy of the old one.

  The next page load shows the reverted state, and the 30 s debounced push then mirrors it to intervals.icu and on to the Bryton.
- **Structural.** Structure: the plan file has no owner. 23 functions each load, mutate and save it, and the lock guards the rename, not the transaction. Owner: one plan store whose save is either the whole load→mutate→save under one lock, or version-checked (reject or re-apply when the file changed since it was read).

### STA-3 · HIGH · The lock that does exist orders the stale write *after* the fresh one; the sync path's daily reconcile is thrown away

- **Where.** `_maybe_auto_reforecast` (app.py:12084-12146) and `api_plan_update` hold `plan_write_lock` for their whole read-modify-write, including seconds of `_apply_plan_update`. An unlocked writer that read earlier blocks inside `atomic_write_plan` on that same lock and, when it is released, writes its stale snapshot last.
- **Evidence, R3b** (r3.out, completion order re-checked in r7.out):
  ```
  serial reforecast->auto_adapt: reconcile_date on disk=2026-09-10
  serial auto_adapt->reforecast: reconcile_date on disk=2026-09-10
  CONCURRENT trial 0..2: reconcile_date on disk=None   (3/3)
  completion order: ('locked-writer', call+0.00s, done+0.00s, rec=True),
                    ('asyncio-portal', call-0.06s, done+0.00s, rec=False)   <- called first, blocked, landed last
  ```
- **Evidence, the dashboard's own sequence at natural timing** (r7_ui_selfrace.py → r7.out; rides/sync and rematch?apply=1 fired together as dashboard.html:15511/15522 does, no trigger):
  ```
  serial sync->rematch / rematch->sync: (reconcile_date today, last_rematch) = (True, True)
  CONCURRENT: LOST ONE WRITER'S UPDATE IN 1/8 TRIALS
  trial 6: rematch called write 0.38 s before the locked writer, completed after it -> reconcile_date lost
  ```
  This ran against an empty ride archive, where the rematch takes about 10 ms. A real archive widens the rematch's read→write window; that is not measured here.
- **Consequence.** The once-a-day reconcile (done/missed marks, gap latches, the reforecast) that the sync path computes is lost whenever a UI or timer write read the plan earlier. It then re-runs on the next sync, or not at all that day.
- **Instance of STA-2's structure.** It is called out separately because the "add the lock" fix is already present at these two sites and demonstrably does not work.

### STA-4 · MEDIUM · `clear_cache()` does not invalidate a fill that is in flight; pre-invalidation data is served for the whole TTL

- **Where.** cache.py:46-75. `cached()` stores whatever `fn()` returned even if `clear_cache()` (cache.py:90) ran while `fn()` was executing. There is no generation or epoch.
- **Invalidation points this defeats:**
  - the ride sync persisting new rides, then `clear_cache()` (app.py:18652; also :18599 on the prune path)
  - FIT import
  - the profile-switch callback (app.py:619)
- **Evidence (r4_misc.py → r4.out).** Real `app._load_all_rides_safe` with a planted 0.5 s loader:
  ```
  control (no read in flight): ['old-ride', 'NEW-ride']
  race    (read in flight):   ['old-ride']   <- served for the next 299s of the 300s TTL
  ```
- **Consequence.** A just-synced ride can be invisible for up to 5 min to everything that reads `all_rides`: week totals, completion, readiness and `_longest_ride_h_90d`, which feeds `Goal`. After a profile switch the same race serves the other profile's `training` / `wellness` / `all_rides`. There is a single profile in production, so that half is low exposure.
- **Structural.** Structure: invalidation is a global flush with no versioning. Owner: the data's owner. The ride store should memoise itself on its own version (count and max mtime, as `_WORKOUT_LIB_CACHE` already does), or `cached()` should carry a per-key generation that `clear` bumps and a stale fill cannot write.

### STA-5 · MEDIUM · The microintervals-only flag leaks from one swap into later rematches of other days

- **Where.** training_planner.py:2255, :2293-2302. `match_zwo` reads `micro_only or _VO2_MICRO_ONLY` (:5100), so even an explicit `False` from a caller cannot override it.
  - swap-type sets the flag from a **per-swap** body override (read at app.py:16968, set at :16985).
  - rematch, rematch/{day}, re-draw, accept-redraw, move-session and ftp-test-type never set it.
- **Evidence (r6_micro_leak.py → r6.out).** Plan preference is `False`. The rider swaps day X with "microintervals only" ticked, then rematches a different VO2 day Y.
  ```
  CONTROL (restart between clicks): rematch of Y saw _VO2_MICRO_ONLY=[False]; Y -> vo2_short_3x13x30s-15s_116pct_60min.zwo; flag after=False
  SAME PROCESS (as in production):  rematch of Y saw _VO2_MICRO_ONLY=[True];  Y -> vo2_short_4x2min-30s_100pct_62min.zwo; flag after=True
  ```
- **Consequence.** The workout served on a day depends on the process's click history. A one-off choice on another day persists until the next generate, recalc, reforecast or update resets the flag, and resets to `False` on restart.
- **Instance of STA-1's structure.** The same owner applies.

### STA-6 · MEDIUM · The cache stores failure as `{}`; each entry point then invents its own CTL (30 or 37)

- **Where.**
  - cache.py:50-72 caches `{}` for 30 s on exception.
  - Nine `training.get("ctl") or 30` sites in app.py (9249, 12192, 12927, 13204, 13273, …).
  - `generate_plan`'s own chain ends in `compute_local_ctl()` → `37.0` (training_planner.py:8289-8300).
- **Evidence (r4.out).**
  ```
  cache['training'] after the failed fetch = {}; auto-recalc planned with current_ctl=30 (action=recalculated)
  same state, generate_plan's own fallback chain: compute_local_ctl()=None -> then the constant (37.0)
  ```
- **Consequence.** An intervals.icu blip at recalc time rebuilds and persists the plan from a fabricated CTL of 30. The athlete's real CTL may be double that. The result stays on disk until the next weekly recalc. Generate would have used 37 for the same state.
- **Structural.** Structure: the cache cannot represent "unknown", so every caller picks a constant. Owner: one fitness-state provider that returns last-known-with-age or an explicit unknown, with one fallback policy.

### STA-7 · MEDIUM · Reconnecting clears the auth latch but never restarts the dead sync loop; status reports healthy

- **Where.**
  - `_sync_loop` **returns** on auth-disable (db.py:1147-1219).
  - app.py:1505 (`setup_save`) and :7596 (`_icu_oauth_reset_throttle`) write `db._auth_disabled = False` directly and do not restart.
  - `api_sync` restarts only if the flag is still `True` (app.py:8939-8940).
  - `get_sync_status` (db.py:895-908) has no liveness field.
- **Evidence (r5_sync_thread.py → r5.out).**
  ```
  after a 401:            thread alive=False  _auth_disabled=True
  after reconnect reset:  thread alive=False  _auth_disabled=False
  after POST /api/sync:   returned {'ok': True}  thread alive=False
  GET /api/sync/status reports auth_disabled=False consecutive_failures=0 last_error=None  (no liveness field)
  control (no reset, then POST /api/sync): thread alive=True
  ```
- **Consequence.** After any 401 followed by a reconnect, the 30-min background sync and the once-a-day calendar reconcile (`post_sync_callback`, app.py:715) stop until the process restarts. The status seen by the UI and `cs-healthcheck` says healthy. The 04:45 `POST /api/sync` still pulls data, so this means daytime staleness and no daily horizon roll, not total loss.
- **Structural.** Structure: five db module globals are written by three db functions, and two app functions reach in and write `db._auth_disabled` directly. Owner: a `SyncWorker` object that is the only writer of its fields and exposes `alive`.

### STA-8 · LOW (one profile) · `restart_sync` with a pass in flight refuses to start and leaves the stop flag set; every sync write gate then aborts

- **Where.** db.py:1246-1270 (5 s join, then `return` with `_sync_stop` still set); the write gate is at db.py:116-117.
- **Evidence (r5.out).**
  ```
  control (idle loop):    restart 0.00s  new thread=True  stop flag set=False  gate OK
  fault (pass in flight): restart 5.00s  new thread started=False  stop flag set=True  gate -> SyncAborted
  after the old pass ends: any sync thread alive=False  stop flag still set=True  gate -> SyncAborted
  ```
- **Consequence.** Sync is dead, and every later sync write (a manual sync included) aborts until something else restarts it. The only production caller that can hit this is the profile-switch callback. `api_profiles_switch` is `async` (app.py:1741), so that 5 s join also runs on the event-loop thread; that last point is from reading the code, not measured.
- **Instance of STA-7's structure.**

### STA-9 · LOW · `_dfa_backfill_lock` leaks when the worker never starts

- **Where.** Acquired in the request (app.py:2456), released only in the worker's `finally` (:2436). `t.start()` (:2473-2478) is outside any `try`.
- **Evidence (r4.out).**
  ```
  control: first=started  lock released after worker finished=True  second=started
  fault (Thread.start raises): lock still held=True; next call -> already_running, task state 'running' forever
  ```
- **Consequence.** Backfill is wedged until restart. Releasing across threads is legal for `threading.Lock`, so the leak on the failure path is the only hazard here.
- **Instance.** Structure: single-flight gate state spread across two functions. Owner: the task registry. Hold the lock in the worker, or release in the request's `except`.

### STA-10 · LOW · Profile switching and cached paths: two owners per concept, and in-flight requests straddle the switch (code reading)

- **What a switch rebinds:**
  - `tp.PLAN_DIR` and `tp.WORKOUT_DIR` (profile_manager.py:658, :682)
  - `app.WORKOUT_DIR` / `GPX_DIR` (app.py:468-518): a second owner of the same concept
  - `db.DB_PATH` (db.py:185)
  - `os.environ` ICU credentials (profile_manager.py:593-595)
  - the cache flush and the fatigue memo clearer
- **What a switch does not reset:** `_ACTIVE_DISTRIBUTION`, `_ACTIVE_CUSTOM_BUDGETS`, `_VO2_MICRO_ONLY`, `_icu_push_last_daily`, `week_plan.SEAL_TRIPS`.
- **The straddle.** A request in flight resolves these at different moments. For example, auto-recalc fixes `json_path` at entry (:17260) but reads `ProfileManager._athlete` (FTP and weight) at :17381, so a switch in between writes profile A's plan with profile B's FTP. This is from reading the code and was not reproduced; production has one profile.
- **Tests.** tests/conftest.py:150-215 snaps `PLAN_DIR`, `WORKOUT_DIR` and the three planner globals back after every test. That hides the leak rather than testing it. No test runs two plan writers concurrently: `conftest.py` is the only test file that mentions both threading and the plan lock.
- **Structural.** Structure: the "active profile" is re-read from five places per request. Owner: a request-scoped profile context captured once at entry.

### STA-11 · LOW · Rider-input and ride records are rewritten with a non-atomic `write_text` while other threads read them

- **Where.** ride_storage.py:778, 790, 862, 1757, and app.py:18435 (DFA augment, which runs in a background executor), :18918 (RPE), :18958 and :18993 (PRs), :20116 (FTP-test review). These truncate the file and then write it. `load_icu_rides` skips any file that fails to decode (ride_storage.py:805-809).
- **Evidence (r8_nonatomic.py).** The real `ride_storage.load_icu_rides()` in a loop, against a 419 KB record being rewritten in a loop on another thread:
  ```
  CONTROL atomic replace : 0/371 reads missed the ride (1649 rewrites)
  write_text in place    : 4026/4146 reads missed the ride (1925 rewrites)
  ```
  This is a stress rate. Production rewrites are occasional: persist then PR recompute, DFA augment, and RPE / PR / FTP-review edits. The per-read probability is small, but any overlapping read misses the ride.
- **Consequence.** A reader that hits the window drops the ride. If that read is the fill for `cached("all_rides")`, the ride stays missing for 300 s (see STA-4). A crash mid-write empties the record, which includes rider-entered RPE. By contrast, `profile_manager` writes are atomic (profile_manager.py:1343-1384).
- **Instance.** Owner: the ride store's own atomic-write helper, of the kind `profile_manager` already has.

### Inventory — module globals that planning or plan-serving reads

| global | written by / when | cross-request visible? |
|---|---|---|
| `tp._ACTIVE_DISTRIBUTION`, `_ACTIVE_CUSTOM_BUDGETS` | 6 sites, per request | **yes** (STA-1) |
| `tp._VO2_MICRO_ONLY` | 6 sites, per request (swap-type per click) | **yes** (STA-5) |
| `tp.PLAN_DIR`, `tp.WORKOUT_DIR` / `app.WORKOUT_DIR`, `GPX_DIR` | boot and switch; two owners | mid-request straddle (STA-10) |
| `tp._WORKOUT_LIB_CACHE` / `_FAST_VALIDATOR` | lazy, keyed by dir, mtime-validated, unlocked | benign (a duplicate parse at worst) |
| `tp._CONTENT_CLASSIFICATION_CACHE` | lazy, keyed by dir, **never revalidated in-process** | a library or classifier change needs a restart |
| `week_plan.SEAL_TRIPS` | incremented per plan | counts from concurrent plans mix (diagnostic only); `STRICT_SEAL` is a constant |
| `cache._cache`, `_cache_ts` | per request; keys not profile-scoped | STA-4 / STA-6 |
| `app._fatigue_resistance_memoised` (lru) | keyed without profile; clearer registered | STA-4 race applies |
| `app._ENRICH_CACHE` | keyed by plan stat and day, **not** by the rides it enriches from | a ride landing without a plan write leaves card state stale for ≤300 s (from code) |
| `app._LIBRARY_ROWS_CACHE` (dir, mtime, count) / `_LIBRARY_TAGS_CACHE` (max mtime only, no dir) | locked | tags can go stale across a dir switch with equal mtimes (negligible) |
| `routes_lib`, `search_lib`, `workout_facts` caches | locked or identity-keyed, bundled or dir-keyed | clean |
| `db._sync_thread`, `_sync_stop`, `_auth_disabled`, `_consecutive_failures`, `_last_sync_error`, `_sync_epoch` | sync thread, restart, two app writers | STA-7 / STA-8 |
| `app._icu_push_timer` / `_last_result` / `_last_daily` | timer thread and handlers | timer lock sound; `_last_daily` not per profile |

---

## 2. Claims checked

| claim (where) | verdict | evidence |
|---|---|---|
| "Endpoints use `with plan_write_lock()` around the tmp-write + rename. Without serialization, concurrent daily-adapt + auto-recalc can silently drop adaptations" (training_planner.py:359-366) | **DID NOT HOLD.** The lock covers the rename in 20 of 23 writers, and drops still happen. | R3a, R3b, R7 |
| "Single-process FastAPI worker → no thread race; plan writes are also serialized via tp.plan_write_lock()" (app.py:14784-14787) | **DID NOT HOLD** | a 40-thread pool; R3/R7 thread names |
| `_VO2_MICRO_ONLY` is "ALWAYS set explicitly … so it can never go stale between plans" (training_planner.py:2251-2254) | **DID NOT HOLD** | R6 |
| setup_save clears `db._auth_disabled` "so the background sync loop resumes" (app.py:1289) | **DID NOT HOLD.** The loop has returned; nothing restarts it. | R5a |
| "The single-worker assertion … is standing in for synchronisation" (REFACTOR-PLAN) | **HELD in substance.** The process is single-worker, but the assertion (app.py:20944-20960) is off the production path, and a single process still runs 40 threads plus the loop. | launcher.py:440-444, printed defaults |
| "`_cache` / `_cache_ts` have no lock at all" (REFACTOR-PLAN) | **HELD, but not the defect.** Dict operations are GIL-atomic; the real defect is the missing generation on refill. | R4a |
| "`_dfa_backfill_lock` is acquired … and released … in a different function, on a different thread" (REFACTOR-PLAN; now :2456 / :2436) | **HELD.** It is legal for `Lock`; the hazard is the leak path. | R4c |
| `cached()` is "shallow, not deep … nothing mutates nested structures" (cache.py:33-37) | **Contract verified:** nested edits are shared and top-level ones are not. I did not search exhaustively for a nested mutator. | R4b |
| A profile switch "must drop it or profile B could serve A's cached curve" (cache.py:93-95) | **PARTIAL.** The drop happens, but a fill in flight survives it. | R4a mechanism |
| `_apply_plan_update`: "the caller writes inside its own plan_write_lock so the latch/status writes are in the same critical section" (app.py:12921-12924) | **HELD for both callers**, and still loses to the unlocked writers | R3b, R7 |
| `generate_plan` "calls [set_active_distribution] from goal.distribution on every run" (conftest.py:200-205) | **HELD**, and that is precisely why a concurrent run changes it | R1 |
| "auto-recalc is a GET but it WRITES" (cs-domestique-adapt) | **HELD.** `GET /api/today-session` also writes, via `_advance_continuous_deload` (app.py:10741 → :10633). | R3a; code |
| `post_write_callback` is "called … OUTSIDE the write lock" (training_planner.py:425) | **HELD** | training_planner.py:454 |
| The write gate means "a stopping switch/restart can never deadlock against a blocked writer" (db.py:100-103) | **HELD.** The flip side: a refused restart leaves every gate aborting. | R5b |
| The ride-directory writes are read-modify-write of the loaded record, so there is no key loss (REFACTOR-PLAN) | **HELD** for key carry-forward. Separately, they are not atomic (STA-11). | code |

---

## 3. Traps: what a fix must preserve

1. **Do not widen `plan_write_lock` over async handlers.** It is a `threading.RLock`, and async handlers run on the event-loop thread.
   - Blocking on it there freezes every request, and `_maybe_auto_reforecast` already holds it across seconds of `_apply_plan_update`.
   - Worse, an RLock is re-entrant per *thread*. Two async handlers that interleave at an `await` inside the `with` would both "own" it.
   - The fix is one plan store: mutation off the loop (or a single writer), or a version-checked save that needs no long lock. The three locked read-modify-write sites do not currently await inside the lock; keep it that way until then.
2. **Keep the model semantics** when the globals go.
   - `auto` means the per-phase `DEFAULT_TID_SEQUENCE`.
   - `custom` builds its table from `goal.custom_bands`.
   - The readers that never set it (summary block, calendar merge, continuous deload advance) must read *the plan's* model.
   - Decide once the default for plans with no `distribution` key: today it is `'auto'` in `Goal` and `generate_plan`, and `'polarized'` in `_goal_from_plan_dict`, reforecast and `_apply_plan_update`.
3. **The gate cannot see this class of bug.** `characterize_planner.py` is single-threaded, and R1 shows seed-pinned fingerprints are deterministic. Add a two-entry-points-on-two-threads case (R1 is a template) before the refactor, or "0 changed" will be reported while STA-1 is still present.
4. **conftest's snap-back fixtures** (conftest.py:150-215) are a symptom. Remove them only *after* the state is owned per computation; removing them first produces order-dependent failures unrelated to the change.
5. **The plan-open sequence must not start waiting on the network sync.** rides/sync, and the lazy sync thread, can take minutes downloading FITs. Serialising plan writes must not make rematch or auto-recalc wait behind `_sync_exec_lock`.
6. **The timer scripts depend on the HTTP surface.** Auto-recalc is a **GET** that writes (`cs-domestique-adapt`, apply mode). `cs-preflight` deliberately avoids calling it. The response keys `action`, `severity`, `sessions_modified`, `note`, `ok`, `error`, `needs_reconnect`, `pushed`, `updated` and `deleted` are parsed by `cs-domestique-adapt` and `cs-healthcheck`.
7. **The ICU push debounce aborts if the profile changed** during the window (app.py:8011-8022). Keep that time-of-check/time-of-use guard. Separately, concurrent `reconcile()` calls (manual, debounced, daily) are unguarded. The upsert is idempotent by `external_id`, but each computes deletions from its own plan read. Not reproduced.
8. **Keep `atomic_write_plan`'s** refusal of empty plans and its `.bak…bak7` rotation. The boot restore (app.py:127-133) deliberately bypasses rotation and runs before requests are served.
9. **`db` reads `_sync_stop` by module name** on every iteration (db.py:116, :1150) because `restart_sync` replaces the Event. A `SyncWorker` must keep "restart replaces the signal" and must not let a refused restart leave it set (STA-8).
10. **Single-profile deployment.** The switch hazards (STA-8, STA-10, half of STA-4) are real in the code but low in exposure. Capture the profile context once per request, and don't let multi-profile concerns drive the plan-store design.
