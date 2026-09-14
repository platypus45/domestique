# Ultrareview — chronic-load branch (refactor/backend-architecture → review-base)

2026-09-14. Branch `refactor/backend-architecture`, tip **6e522c5a**, reviewed
against `review-base` (**59500caa**). Scope: 20 files, +906 / -75.
Cloud review session: https://claude.ai/code/session_01G7UreoCi1tNuBYBHudpiFq

Three findings, all rated **nit**. None is a correctness bug. Finding 1 was
re-verified by hand in this checkout; 2 and 3 were verified by the reviewer
against the cited lines. **Nothing has been fixed yet** — the owner has not
said which to apply.

## 1. `athlete_weekly_load` fallback parses the ride archive uncached

`src/training_planner.py:3317` — when `recent_weekly_tss` is None the fallback
calls `ride_storage.chronic_weekly_tss()` with no `rides=`, and
`src/ride_storage.py:1774` then does `load_all_rides()` — the ~500 ms archive
parse that `app._load_all_rides_safe()` (`src/app.py:18290`) exists to memoise.

Who pays: every `generate_phases` caller that does **not** thread the cached
rides in. `api_plan_generate` and `_regenerate_plan_dict` pass them
(`src/app.py:11761`, `:12373`). These do not:

| caller | line | cost |
|---|---|---|
| `recognize_entry` hypothesis loop | `src/training_planner.py:3943` | one archive parse per candidate week, `for c in range(c_max, 0, -1)` — ~8 s per entry scan on an ICU-only rider with c_max≈16 |
| extend | `src/training_planner.py:8717` | one parse |
| recovery adjust | `src/training_planner.py:13321`, `:13913` | one parse each |
| phase preview | `src/app.py:11345` | one parse |

Fix options (pick one, smallest first): compute `recent_weekly_tss` once
before the `recognize_entry` loop and pass it into each `generate_phases(hyp,
current_ctl, ...)` call; or give `athlete_weekly_load` a `rides=` parameter
and thread `_load_all_rides_safe()` through from app.py the way generate and
regenerate already do.

## 2. `_went_unridden` still inlines the dict-or-object adapter

`src/training_planner.py:1061` and `:1067` re-declare
`o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))`,
the exact pattern `src/plan_invariants.py:356` factors out as `_field` /
`_day` (and which `asks_nothing`, called from `_went_unridden`, already uses).
Two copies to keep in sync if week/session shapes change. Fix: import and use
`_field` from plan_invariants.

## 3. `_fits` closure inside the week loop reads mutable outer state

`src/training_planner.py:11948` — `def _fits(...)` is defined inside
`for pw in plan_weeks:` (line 11900), reallocated each week, and reads
`_budget` and `_planned` by name from the enclosing scope; `_planned` is
mutated in the same loop at 11971. Fix: hoist to a small function taking
`(minutes, tss_per_h, replacing, budget, planned)` explicitly, or inline the
two-line formula at its two call sites.

## Not covered

The full branch diff against `clean-main` is 106 files / 95k lines (mostly
`tests/characterization.json`) and exceeds the ultrareview limit, so only the
delta since `review-base` was reviewed. Earlier commits on the branch have
their own notes in `notes/review/`.
