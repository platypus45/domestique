# Domestique

**Cycling training planner + workout library + post-ride viewer.**

Domestique generates adaptive weekly plans, ships a library of **3,054 ZWO workouts**, imports and analyzes Garmin FIT files, and tracks your training load. It is hardware-agnostic — ride on any platform (Golden Cheetah, MyWhoosh, Tacx Training, Zwift), then import the FIT back for analysis and let the data drive your next week's plan.

![Python](https://img.shields.io/badge/Python-3.9+-blue) ![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-green) ![Workouts](https://img.shields.io/badge/Workouts-3054-orange) ![Routes](https://img.shields.io/badge/Routes-622-purple) ![Version](https://img.shields.io/badge/Version-v1.0.0-brightgreen)

> **v4.6.7 — Capability projection + finished-programme summary.** New `GET /api/event/projection` predicts your finish time + identifies endurance / power / climb-readiness gaps (Allen-Coggan IF-by-duration, Pinot & Grappe 2011 RPP). New `GET /api/programme/summary` end-of-plan recap with 12 literature-grounded metrics (FTP/eFTP/VO2max Δ per Stöggl 2014, polarization per Treff 2019, monotony per Foster 1998, etc.) — exportable as PNG (Pillow) or PDF (client-side window.print, zero new deps). Plus 4 surgical UX fixes (cooldown ramp now correctly downslopes, yellow-arrow sessions clickable, calendar auto-scrolls to today, event-day green border marker).
>
> **v4.6.6 — Injury-prevention feedback loops.** Pre-v4.6.6 the planner *detected* TSS / intensity / soreness signals but none of them mutated the persisted plan — a rider could destroy themselves with unplanned hard work and the app would prescribe vo2max intervals the next morning anyway. v4.6.6 wires **seven science-grounded guardrails** that close the actual-vs-planned loop. Each cites a specific paper.
>
> | # | Gate | Trigger | Action | Citation |
> |---|------|---------|--------|----------|
> | G1 | Yesterday-was-hard floor | yesterday actual / planned > 1.5 | force today → Z2 | Foster 1998 |
> | G2 | 48h Z5+ ceiling | rolling 48h Z5+ ≥ 25 min | force today → Z2 (cycling now included) | Hulin 2014 |
> | G3 | Polarization breach | week z4plus > target+8 OR z1z2 < target-10 | drop next 1–2 hard sessions one tier | Seiler/Stöggl/Treff |
> | G4 | ACWR weekly scaling | last week actual/planned > 1.5 | next week tss_target ×= 0.85, hit−=1 | Gabbett 2016 |
> | G5 | Soreness peripheral cap | daily_log.soreness ≥ 6 | force today → recovery (bypasses HRV) | Hooper 1995 + Cheung 2003 |
> | G6 | Hooper composite gate | sum(sleep+fatigue+stress+soreness) ≥ 18 | force today → Z2 | Hooper & Mackinnon 1995 |
> | G7 | 3-day mean RPE drops HIT | mean ride.feel/perceived_exertion ≥ 7 | drop today one tier | Foster 1998 |
>
> **Earlier highlights still apply:** v4.6.5 widget redesign (planned vs actual side-by-side), v4.6.4 chart shows actual ZWO segments, v4.6.3 Rønnestad detection (17 microinterval files now land in build/peak), v4.6.2 100% slot uniqueness in 24w plan, v4.1.0 closed feedback loops (DFA α1, aerobic decoupling, Foster monotony, eFTP drift). FTP test detection on FIT import works end-to-end. Built on v4.0.0-alpha which removed the trainer hardware subsystem — ride in your chosen app, import FIT after.
>
> See [CHANGELOG.md](CHANGELOG.md) for the full release history. Latest macOS DMG attached to the [GitHub release](https://github.com/platypus45/domestique/releases/latest).

---

## Workflow

1. Domestique builds your weekly plan (Base / Build / Peak / Taper, adapted from your actual training load, readiness, and HRV).
2. Pick today's workout from the plan card → download the ZWO (or bulk-copy the library into Golden Cheetah's workout folder once).
3. Ride outside or indoors with any app/device of your choice.
4. Import the `.fit` back into Domestique → post-ride report, FTP-test detection, and (critically) **the next-day plan adapts automatically** based on what the ride showed.

---

## What Makes It Different

### Closed feedback loops (new in v4.1.0)
Most "smart" planners compute HRV, decoupling, monotony — then do nothing with them. Domestique actually uses them:

- **DFA α1 < 0.5 over last 3 rides** → tomorrow's threshold session auto-swaps to Z2, with a revert button
- **Aerobic decoupling > 5%** → next-day "Z2 recommended" advisory banner
- **Foster Monotony > 2.0 over 14 days** → next week's planned TSS auto-cut 15%
- **Intervals.icu eFTP drift > 3% for 7+ days** → FTP auto-updated with 48h revert toast
- **Local CTL fallback** — 42-day EWMA from your own ride TSS when ICU is unavailable (no more hardcoded 37.0)

### FTP test flow (works end-to-end)
Ride a Coggan 20-min test or Ramp test in Golden Cheetah / any app → import the FIT →
- App detects the test by power-profile shape (no manual marking needed)
- Computes suggested FTP: 0.95 × avg 20-min power (Coggan) or 0.75 × best 1-min (Ramp)
- Modal pops up: **Update / Keep / Custom**
- Ramp auto-halt detection (cadence < 50 + power < 85% target for 3s) — captures which step you gave up at
- Every FTP change logged in a `ftp_test_history` ledger with source provenance (`tested_coggan_20min` / `tested_ramp` / `eftp_auto` / `manual`) + sparkline chart in Settings

### DFA Alpha1 — from FIT, not live
DFA α1 is computed post-ride from RR-intervals if your FIT has them (Polar H10, Garmin HRM-Pro, Wahoo TICKR X). Not just displayed — actually **fed into the planner** as a fatigue signal that can downgrade tomorrow's intensity.

### Score rubric (structure-aware, not just TSS)
Each workout gets a 1-10 score combining TSS (60%), protocol variety (distinct power targets above Z2), and VO2 bonus (presence of >105% FTP intervals). Fills the library's "Min Score" filter with something meaningful.

### Adaptive re-draw
`/api/plan/re-draw` re-rolls a day's workout when you want a swap — excludes what you did earlier in the week, keeps variety. Drag-drop moves persist through ISO-week boundaries (bug introduced by Sat-Fri vs Mon-Sun merge is fixed).

### 284-Product Nutrition Planner
Plan your ride fuel before you start: how many gels, which drink mix, how many scoops. Carb/hour target computed from ride duration and intensity.

### 622 virtual routes
Tadts Innsbruckring, Alpe d'Huez, Ventoux, Stelvio + 160+ real-world courses. Export as CRS (RGT format) or GPX for route-based riding in GC / Wahoo / Garmin.

---

## Core Features

### Plan
- **Training plan generator** — periodized Base / Build / Peak / Taper phases, phase-deterministic HIT type rotation, used-names dedupe across weeks
- **Weekly plan** — drag-drop reschedule, daily adaptation from ICU + local signals
- **FTP test protocols** — Allen-Coggan 20-min + Ramp (shipped in library); detection on FIT import works; ramp halt auto-detected post-hoc
- **reforecast** — negative TSB downshifts future hard sessions automatically
- **Cross-sport load** — running / lifting / other ICU-synced sports influence cycling plan

### Workout library
- **3,054 structured workouts** — endurance, sweet spot, threshold, VO2max, sprint, over-under, pyramid, FTP tests
- **ZWO `<tags>` indexed** — filter library by `?tags=ftp_test`, new "FTP Tests" category tab in dashboard with yellow-border cards
- **Click-to-download** ZWO from library → load into Golden Cheetah / MyWhoosh / Tacx / Zwift
- **622 virtual routes** — 3 worlds, 41 famous climbs, 160+ real-world courses (CRS + GPX export)

### Post-ride analysis
- **Import FIT** — drag-drop .fit upload, parsed and archived under `~/.domestique/rides/`
- **Ride detail report** — power/HR/cadence/speed curves, zone distribution, decoupling scatter, NP/TSS/IF
- **DFA α1** from RR-intervals if present
- **Aerobic quality panel** — Pw:Hr decoupling + Efficiency Factor + DFA α1
- **FTP-test detection** — sets `is_ftp_test`, `ftp_test_type`, `ftp_test_suggestion`, `ftp_test_halted` on import
- **FIT → Intervals.icu one-way upload** — ride lands in ICU automatically after import

### Track
- **Intervals.icu sync** — CTL/ATL/TSB fitness chart, activity history, HRV trends, FIT upload
- **Local CTL fallback** — 42-day EWMA from your FIT library when ICU is offline
- **Multi-user profiles** — each rider with their own FTP (with provenance), zones, plan, history
- **FTP history ledger** — every change captured with source + date; sparkline chart in Settings
- **Manual FTP edit confirm dialog** — no accidental overwrites of tested values
- **Dashboard** — fitness chart, weekly overview, workout library browser, route browser, settings

---

## Recommended external apps

Domestique plans + analyzes — you ride in a separate app. See [docs/cycling_apps.md](docs/cycling_apps.md) for the full table; the free-forever picks with ZWO/FIT import + Tacx Neo 2T support:

| App | Free? | ZWO import | Notes |
|---|---|---|---|
| **Golden Cheetah** | ✅ open-source | ✅ ZWO/ERG/MRC | Best match for a planner+library+viewer app like this one; drive the trainer via ANT+ FE-C; drop Domestique's library into GC's workout folder once, all 3054 files appear in Train view |
| **MyWhoosh** | ✅ fully free | ✅ via web builder | Scenery + Zwift-style ride experience; full ERG on Neo 2T |
| Tacx Training | ✅ free with Tacx HW | ❌ ZWO, only GPX | Native Tacx integration but no ZWO import |

---

## Quick Start

### Download

Prebuilt binaries are published on the [Releases page](https://github.com/platypus45/domestique/releases/latest):

| Platform | File | How |
|---|---|---|
| **macOS** (Apple Silicon + Intel) | `Domestique.dmg` | Open the DMG, drag the app icon onto the Applications alias |
| **Windows 10/11** | `Domestique-Windows.zip` | Unzip, run `Domestique.exe` (Windows Defender may prompt) |

Binaries are built automatically by [GitHub Actions](.github/workflows/release.yml) on every tagged release.

### Installing the unsigned DMG (macOS)

Releases are currently **not** codesigned or notarized — the project has no Apple Developer ID. Gatekeeper will show "Domestique is damaged and can't be opened" on first launch. To bypass:

1. Open the DMG → drag `Domestique.app` onto the `Applications` icon.
2. **Right-click** (or Control-click) `Domestique.app` → **Open**.
3. In the dialog, click **Open** again. macOS remembers the choice after the first launch.

If the app still refuses to open, run once from Terminal:
`xattr -dr com.apple.quarantine /Applications/Domestique.app`.

### Installing on Windows (SmartScreen)

The Windows EXE is also unsigned. On first run, SmartScreen shows a blue "Windows protected your PC" dialog. Click **More info → Run anyway**.

### First-run secrets

On first launch the setup wizard writes Intervals.icu credentials to a per-profile env file at `~/.domestique/profiles/<id>/.env` (mode 0600). There is no repo-root `.env`.

### From source
```bash
git clone https://github.com/platypus45/domestique.git
cd domestique
pip install -r requirements.txt
python launcher.py
```

### Build your own
```bash
# macOS
./build_dmg.sh        # writes ~/Desktop/Domestique.dmg with drag-to-install layout

# Windows
build_win.bat         # writes dist\Domestique\Domestique.exe
```

### Intervals.icu (optional but recommended)
Connect in Settings > Intervals.icu with your Athlete ID and API key from [intervals.icu/settings](https://intervals.icu/settings). Without ICU, Domestique falls back to local CTL computation from your imported FITs.

---

## Training Science

Every algorithm is grounded in peer-reviewed research:

| Feature | Method | Reference |
|---------|--------|-----------|
| DFA Alpha1 | Detrended Fluctuation Analysis on RR-intervals, Peng 1995 algorithm | Rogers et al. 2021 (PMID 33519504) |
| Aerobic Decoupling | EF = NP/avgHR per half (TrainingPeaks canonical) | Friel (coaching heuristic) |
| Foster Monotony / Strain | Weekly load SD-vs-mean ratio | Foster 1998 (PMID 9662690) |
| CTL / ATL / TSB | 42-day / 7-day exponentially-weighted TSS | Coggan & Allen |
| Local CTL fallback | 42-day EWMA over imported FIT rides | n/a — standard impedance-matching |
| Daily Adaptation | TSS pacer with cross-sport load, DFA α1 cap | Kiviniemi 2007, Javaloyes 2019 |
| Periodization | Base / Build / Peak / Taper phases | Coggan & Allen, Friel |
| FTP — Coggan 20-min | 0.95 × avg 20-min power | Allen & Coggan 2019 |
| FTP — Ramp | 0.75 × best 1-min power | Ric Stern (British Cycling) |
| W'bal | Skiba 2015 differential + GoldenCheetah tau | Skiba et al. 2015 |
| Cardiac Drift | HR-driven SV decline mechanism | Coyle & Gonzalez-Alonso 2001 |
| Nutrition | Duration-gated carb targets | Jeukendrup 2014, ACSM 2016 |

---

## Tech Stack

Pure Python backend + vanilla JS frontend. No npm, no React, no webpack.

- **Backend**: FastAPI + SQLite (REST only — no WebSocket since v4.0.0-alpha)
- **FIT parser**: fitparse (upload + post-ride report + DFA α1 + decoupling compute)
- **ZWO parser**: lxml (with `<tags>` indexing for category filters)
- **Window**: pywebview (native, not a browser tab)
- **Packaging**: PyInstaller → .app / .exe + `create-dmg` for signed-style drag-to-install DMG

Domestique runs as a single-worker uvicorn process. Ride archive + profile state are per-process singletons, so running multiple workers is unsupported.

---

## Workout library sources

The 3,054 ZWO files have three provenance buckets (see [docs/workout_sources.md](docs/workout_sources.md) for full detail + licensing):

- **1797 pre-existing** (pre-v4 generated workouts) — untouched across the pivot
- **1105 whatsonzwift reconstructions** — facts-only inference from the public rendered interval graph; original names, descriptions, and coach cues stripped and regenerated from structure; never touches the site's ZWO download endpoint; `<author>Domestique Library</author>` on every file
- **24 GitHub MIT/Unlicense imports** (`macgrrl/zwift-workouts` Unlicense, `michaelahlers/michaelahlers-zwift-workouts` MIT) — provenance tracked in `workouts/.github_imports_manifest.json`
- **124 procedural gap-fillers** (pyramids, short VO2, short threshold, over-unders with varied ratios, neuromuscular sprints, short sweet spot — categories that were under-represented)
- **4 FTP test protocols** scraped from `whatsonzwift.com/workouts/ftp-tests` and tagged with `<tag name="ftp_test"/>`

Copyright verdict: interval numbers + durations are uncopyrightable facts (Feist v Rural Telephone); names + descriptions are copyrightable — those are stripped and regenerated on every scraped file. For open-source redistribution safety, fork the procgen + GitHub subset only.

---

## Security notes

- **ICU API key** is stored plaintext in `~/.domestique/profiles/<id>/.env` with `chmod 0600`. Storage is per-profile and local-only (never uploaded, never synced). For extra safety on macOS you can manually move the key into Keychain after first run — an automated Keychain migration for ICU credentials is planned for a future release (Strava credentials already use Keychain on macOS). DO NOT commit your `.env` to any public location; it is excluded by `.gitignore` at the repo root.
- **All network listeners** bind to `127.0.0.1` by default. Do not expose port 8080 publicly without adding your own authentication layer — Domestique's endpoints are designed for single-user localhost use.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request — in particular, all commits must be signed off under the Developer Certificate of Origin (`git commit -s`).

See also:
- [COURSES_LICENSE.md](COURSES_LICENSE.md) — route and elevation data provenance
- [NUTRITION_LICENSE.md](NUTRITION_LICENSE.md) — nutrition database (ODbL 1.0)
- [TRADEMARKS.md](TRADEMARKS.md) — trademark policy
- [docs/cycling_apps.md](docs/cycling_apps.md) — comparison of free cycling apps accepting ZWO/FIT
- [docs/workout_sources.md](docs/workout_sources.md) — workout library provenance + legal stance

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

---

*Built with PubMed research, 3,054 workouts, and a deep love for cycling.*

---

## Trademarks

Tacx, Wahoo, Garmin, Polar, MyWhoosh, Zwift, Golden Cheetah, Rouvy, and Intervals.icu are trademarks of their respective owners. See [TRADEMARKS.md](TRADEMARKS.md).

---

## History

- **v4.1.0 — Planner grill pass (2026-04-24).** 31 bugs closed over 5 waves of agent research + implementation + QA + fix-forward. Feedback loops that previously computed-but-ignored signals are now closed; FTP test flow works end-to-end; eFTP has source provenance; planner unified; 4 new FTP tests from whatsonzwift.
- **v4.0.0-alpha — Trainer subsystem removed (2026-04-24).** ~16,000 LOC of BLE / FTMS / ERG / Tacx FE-C / Polar HR code retired. Library grew from 1,797 → 3,050 workouts via scrape + GitHub + procgen.
- **v3.x — Live-ride era.** Trainer integration with BLE pairing, ERG power hold, SIM gradient, first-pedal gate, DFA α1 live. Sunset with v4 for pure-planner role.
- **v2.0.0 — Rebrand.** Previously known as **ChickenCycling**. User-data directory preserved at `~/.chickencycling/` on systems that predate the rename.

Copyright © 2026 Domestique contributors.
