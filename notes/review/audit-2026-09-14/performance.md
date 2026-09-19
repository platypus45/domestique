# Performance audit — Domestique @ a95447e8 (2026-09-14), measured

Lens: time, memory and work per request. Independent agent. Rig: conftest sandbox
HOME, sockets blocked except loopback, fake ICU creds + setup marker so the real
dashboard loads; 400 ICU records seeded (253 with 1 Hz streams, 116 MB archive:
400 production-shaped rides), 730 wellness rows, a 16-week event plan via
`POST /api/plan/generate`. Counters on `open`, `load_all_rides`,
`load_workout_library`, `match_zwo`, planner entry points, sqlite trace,
`training._get`, `httpx.get`. Home load driven by headless Chromium 152 against
in-process uvicorn with an ASGI timing wrapper. Kept verbatim; the synthesis is in
../architecture-audit-2026-09-14.md.

## Ranked findings

| # | Sev | Finding (measured) | Where | Fix shape |
|---|---|---|---|---|
| 1 | **Critical** | **Archive-parse stampede.** `cached()` has no lock; a cold home load fires 4 concurrent `load_all_rides()` → **4 parses, 17.4 s CPU, each request waiting 4.4–4.8 s** (`/api/rides?limit=10`, `/api/readiness`, `/api/plan/preview`, `/api/activities`). Single parse in isolation = **0.79 s** for 400 rides (≈2 ms/ride, linear). | `cache.py:46-70`; `app.py:18296` | Per-key lock (single-flight) in `cached()`; parse once per process and invalidate on write, not TTL. |
| 2 | **Critical** | **Home load triggers a full streams backfill.** `_dfaBackfillIfStale` POSTs `/api/profile/dfa-backfill` on load (30-min localStorage throttle, per-profile key); the thread does **1 ICU call + 1 full ride-file rewrite per ride (400 GETs, 400 × ~290 KB writes, 1200 reads)**; frontend polls status **29×** per load. With ICU down it wrote 128 (cold) / 272 (warm) files during the page load and marked all 400 `icu_deleted` on a *network* failure. | `dashboard.html:7350-7372,7434`; `app.py:2441`; `power_curve.py:655-680` | Persist last-run server-side; only rides lacking efforts; never touch envelopes on transport error; move to the sync loop. |
| 3 | **High** | **Five endpoints bypass the archive cache** and re-read all 400 files every call, warm: `/api/profile/power-curve` **2.43 s** (3 scans = 1200 opens), `/api/rider-stats` **1.67 s** (800), `/api/programme/summary` **0.78 s** (`_rs.load_all_rides()` direct), `/api/profile/dfa-alpha1` **0.77 s** (400), `/api/today-session` cold (`_iter_icu_dfa_rides` 0.93 s). | `power_curve.py:133-158,1003,1018`; `app.py:19909,3372,2687` | Route all through one process-scoped archive object; power_curve takes rides as a parameter. |
| 4 | **High** | **`/api/rides` (no limit) serializes streams: 48.7 MB, 8–9 s** warm. Home uses `?limit=10` = 1.22 MB raw / 243 KB gz for 10 cards. | `app.py:18819-18856` | Strip `streams`/`intervals` in list responses; paginate. |
| 5 | **High** | **Planner is O(weeks × library × candidates).** `generate_plan` 16 wk = **4.45 s**; `regenerate` (POST) **4.95 s**: 17 × `sample_week_workouts` (3.5–4.1 s), `_row_zone_minutes` **495–573 k calls** (1.4–1.6 s), `_budget_fit_score` 71–83 k, `_content_class_for_row` 315–349 k. Both recompute per-row facts from the 4307-row list every time. `reforecast_dict` itself: 2 ms. | `training_planner.py:7354,6299,6466,5993,7850` | Precompute zone-minutes + content class once per library load (index columns); filter feasible pool per phase once, not per slot. |
| 6 | **High** | **Every weekly-plan read runs the matcher.** GET `/api/weekly-plan` = `generate_weekly_plan` + 6 × `match_zwo` (each scans 4307 rows: `file_admissible` 25 842 calls) + **6 plan-file reads**: 42 ms warm alone, **2.3 s** in the browser under GIL contention with #2. Called by `/api/today-session` and `/api/week-summary` (×2 offsets) → **4 rebuilds per home load**. `/api/today-session` also *writes* (`_advance_continuous_deload`, `app.py:10505`) on a GET. | `app.py:9242-9330,9773,10661,10690` | Persist the matched week; one plan read per request; move deload advance off GET. |
| 7 | **Medium** | **Synchronous GitHub call on every page load**: `loadUpdateCheck(true)` → `/api/update/check?force=1` → `httpx.get` timeout 10 s in the request; 887 ms cold here. | `dashboard.html:21334`; `app.py:8385,8431` | Drop `force=1`; do it on the sync thread. |
| 8 | **Medium** | **ICU calls inside request handlers**: `get_today_metrics` = 2 urlopen (timeout 15 s, `training.py:264`) per cold `/api/readiness`, `/api/today-session` (4), `/api/week-summary` (4). `cached()` sticks `{}` 30 s after failure, so a flaky ICU retries a 15 s timeout every 30 s from a request thread. Sync `def` handlers → threadpool, not event loop; the async mutators (`/api/plan/reforecast|regenerate|update|dismiss|auto-adjust`) *do* run planner + rotation on the loop (regenerate 4.9 s blocks it). | `cache.py:55-68`; `app.py:11994,12868,12985` | Background refresh with stale-serve; make async mutators `def` or offload. |
| 9 | **Medium** | **Frontend**: `/` = **1.16 MB HTML** (338 KB gzip), no `Cache-Control`/`ETag`, Jinja render **60 ms/request**; inline JS, unbundled. `renderCalendar` rebuilds the whole grid via one `innerHTML` (`dashboard.html:13852-13927`), `loadCalendar()` has 30 call sites. Home fires **78 requests (39 distinct) cold / 45 warm**, dup: `dfa-backfill/status` 29, `sync/progress` 7, `readiness` 3, `activities`/`settings`/`icu/connection` 2. `/api/workouts` = 2.2 MB raw. | — | Static asset + ETag; dedupe loaders; diff-render days. |
| 10 | **Low** | `/api/workouts/tags` parses **4307 ZWO XML files** (240 ms) once per process; `.content_classification.json` 5.9 MB + `.library_index.json` 3.3 MB + `.workout_facts.json` 1.1 MB loaded at first library use (58 ms). `load_recent_wellness` opens up to 118 daily JSON files per call (rider-stats). `_kick_lazy_icu_sync` fired 6× per load (4 sites; Event guard works). | `app.py:5535`; `ride_storage.py:1843` | Tags into the index; wellness from SQLite. |
| — | Low | SQLite is not a problem: ≤7 statements/request, indexed; 29 per home load. Persistence fine: plan 121–132 KB, `atomic_write_plan` 3 ms incl. 7-deep `.bak` rotation (copy + 6 renames); one `.md` snapshot written per generate. | `db.py:250-330`; `training_planner.py:390-460` | none |

## One home load (Chromium, cold; server ms; plan reads = `current_plan.json` opens)

| endpoint | ms | plan | archive parses | lib loads | notes |
|---|---|---|---|---|---|
| /api/rides?limit=10 | 4826 | 0 | 1 (stampede) | 0 | 243 KB gz |
| /api/readiness | 4773 | 0 | 1 | 0 | ×3 per load |
| /api/plan/preview?goal=event | 4673 | 0 | 1 | 0 | `chronic_weekly_tss` |
| /api/activities | 4377 | 0 | 1 | 0 | ×2, lazy-sync kick |
| /api/update/check?force=1 | 998 | 0 | 0 | 0 | GitHub GET |
| /api/profile/dfa-alpha1 | 925 | 0 | 400 opens (uncached) | 0 | |
| /api/wellness?days=90 | 825 | 0 | 0 | 0 | 90 JSON opens |
| / | 237 | 0 | 0 | 0 | 1.16 MB |
| /api/week-summary (×2) | 153/130 | 6 each | 0 | 2 | matcher ×6 |
| /api/today-session | 126 | 7 | 0 | 2 | matcher, may write |
| /api/weekly-plan | 110 | 6 | 0 | 2 | matcher |
| /api/calendar | 105 | 1 | 0 | 1 | 127 KB raw |
| 27 others (settings ×2, sync/progress ×7, backfill-status ×29 …) | ≤90 each | 1–3 | 0 | 0 | |
| **Total** | **23.8 s server / 7.4 s wall** | **29** | **4 (17.4 s)** | **10** | + 6 lazy-sync kicks, 128 ride writes, 138 ICU attempts |

Warm reload: 45 requests, **9.9 s server / 4.1 s wall**, still 272 ride writes + 275 ICU attempts (backfill) and `match_zwo` 4.6 s total.

## Boot
`import app` **0.88 s** (app.py body 0.68 s: fastapi 0.17, training_planner 0.10; no library/archive work at import — `user_paths.json` probe only). Lifespan **0.08 s**: `migrate_to_profiles`, `run_v102_migration_check`, `migrate_to_v4` per profile, `_load_active_profile` ×3, `.env` ×2, plan restore check, `rewrite_stale_plan_classifications` (reads plan + `.library_index.json`). First `/` 0.21 s; first `/api/calendar` **1.89 s** (every ride file opened **twice**: `load_icu_rides` + `_iter_icu_dfa_rides`). Launcher adds port probe + browser open only.

## Eight best ms-saved-per-risk
1. Single-flight lock in `cached()` — saves ~13 s CPU / 4 s wall per cold load (`cache.py:46`).
2. Route `power_curve._load_cached_rides`, `_iter_icu_dfa_rides`, `programme/summary` through `_load_all_rides_safe` — −5.6 s per home load warm.
3. Server-side backfill throttle + skip on transport error — removes 400 GETs + 116 MB writes per load.
4. Strip `streams` from `/api/rides` list responses — 48 MB → ~1 MB; `limit=10` 1.2 MB → ~50 KB.
5. Drop `force=1` from `loadUpdateCheck` on load — −0.9 s.
6. Precompute `_row_zone_minutes`/content class per library row at index build — generate 4.45 s → est. <2 s, regenerate likewise.
7. Read the plan once per request in `api_weekly_plan` and persist the matched week — −100 ms × 4 per load, and removes the GET-side write.
8. `ETag`/`Cache-Control` on `/` and dedupe `readiness`/`activities`/`settings` loaders — −60 ms render + 5 requests per load.

## Not measured
Real ICU latency (blocked; production `get_today_metrics` = 2 × ≤15 s timeouts). Production 36-ride archive (extrapolate ≈2 ms/ride). Browser paint/JS time (server side only). Process RSS (archive pickles to 44 MB; `cached()` shallow-copies the list per read). `regenerate_from_today` in-process (measured via HTTP instead). FIT-import path (0 FITs seeded; note each FIT without a valid sidecar is re-parsed on every `load_all_rides`, `ride_storage.py:905`).
