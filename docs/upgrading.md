# Updating Domestique

## TL;DR

Drag-drop the new DMG onto `/Applications` (or run the new Windows installer). Everything in `~/.domestique/` is left alone — rides, current and archived plans, FTP history, wellness logs, ICU credentials, and athlete profile all survive every install. The installer only touches the application bundle (code, ZWO library, virtual routes, templates).

---

## What's preserved

All user state lives under `~/.domestique/`, outside the bundle. The installer cannot reach it.

| Category | Path | Contents |
|---|---|---|
| Rider profile | `profiles/<id>/athlete.json` | FTP, weight, LTHR, HRV/RHR baselines, FTP test history ledger |
| ICU credentials | `profiles/<id>/.env` (mode 0600) | `ICU_ATHLETE_ID`, `ICU_API_KEY` |
| Profile registry | `profiles.json` | id, name, colour, active flag |
| User preferences | `profiles/<id>/user_prefs.json` | hours/week, available days, rest days |
| Activity DB | `profiles/<id>/health_tracker.db` | wellness, activities, daily Hooper log, athlete metrics, blood markers |
| Current plan | `profiles/<id>/plans/current_plan.json` | active periodised plan |
| Archived plans | `profiles/<id>/plans/*.json` | rotated previous plans |
| Rides archive | `rides/*.fit` | imported FIT files (shared across profiles) |
| ICU per-ride summaries | `rides/icu/*.json` | pulled from intervals.icu |
| Wellness logs | `wellness/YYYY-MM-DD.json` | HRV, RHR, sleep records |
| Path overrides | `user_paths.json` | optional `WORKOUT_DIR` / `GPX_DIR` |
| Boot logs | `logs/*.log` | rotated logs |

---

## What gets replaced

The application bundle — `/Applications/Domestique.app` on macOS, the program directory on Windows. That holds the Python code, bundled ZWO library, virtual routes, and templates. Replaced wholesale.

---

## First boot after upgrade

On first launch after a version bump you'll see a one-off toast naming the from→to versions and confirming all rider data is preserved. Fires once per transition (per-version localStorage flag). The previous-run version is recorded at `~/.domestique/last_run_version.txt`.

---

## What if something looks wrong?

- Check `~/.domestique/last_run_version.txt` — it should match the version in Settings.
- The only legacy delete path is `migrate_profiles.py:295-300`, which ran once during the v3.x→v4 layout migration after copying files into `profiles/default/`. No-op on v1.x installs.
- Nothing in v1.x deletes user data automatically. Profile deletion is user-initiated only.
- Missing ride or plan? Files are plain `.fit` / `.json` under `~/.domestique/rides/` and `~/.domestique/profiles/<id>/plans/`.

---

## Rolling back

Install the older DMG (or older Windows installer). Data still works because schema changes are additive: new columns are ignored by older code, unknown JSON fields are skipped.
