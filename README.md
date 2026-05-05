<p align="center">
  <img src="assets/icon.png" alt="Domestique" width="180" height="180">
</p>

<h1 align="center">Domestique</h1>

<p align="center"><b>An adaptive cycling training planner that closes the loop between what you planned and what you actually did.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue" alt="Python">
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-green" alt="Platform">
  <img src="https://img.shields.io/badge/Workouts-3054-orange" alt="Workouts">
  <img src="https://img.shields.io/badge/Routes-622-purple" alt="Routes">
  <img src="https://img.shields.io/badge/Version-v1.0.1-brightgreen" alt="Version">
  <img src="https://img.shields.io/badge/Tests-793%20passing-success" alt="Tests">
</p>

Domestique builds you a periodised training plan, ships **3,054 structured ZWO workouts**, imports your post-ride FITs, and feeds *every* signal — TSS overshoot, polarisation breach, soreness, DFA α1, aerobic decoupling, training monotony, eFTP drift — back into the next day's plan. Most "smart" planners stop at the dashboard. Domestique mutates the prescription.

## Why this exists

Most training apps fall into one of two modes:

- **Display-only**: HRV widgets, Banister fitness curves, polarisation rings — beautiful charts, zero behavioural feedback.
- **Calendar-based**: a fixed 12-week plan that doesn't care what you actually did yesterday.

Domestique is neither. Every signal that touches the dashboard also has a code-path that mutates a future session. Examples:

- **Soreness ≥ 6/7** on the morning Hooper composite form → today's VO₂max session is forced to recovery, period (Hooper & Mackinnon 1995, Cheung et al. 2003 — peripheral fatigue is independent of central HRV).
- **Last week's actual TSS > 1.5 × planned** → next week's TSS budget auto-cuts 15% (Gabbett 2016, ACWR sweet spot 0.8–1.3).
- **Rolling 48 h Z5+ ≥ 25 min** (cycling included) → today is forced to Z2 even with positive TSB (Hulin et al. 2014).
- **Mid-cycle FTP recalibration** at the build1→build2 phase boundary auto-tests your FTP so the next 4 weeks of TSS targets aren't computed against a stale baseline (Allen & Coggan, *Training and Racing with a Power Meter* 3rd ed.).

Seven science-grounded guardrails (G1–G7), each citing a specific paper, plus a 1-week consolidation phase at the end of every non-event cycle so people don't ride straight from a peak into a fresh build with elevated fatigue (Mujika 2010).

## What's in the box

- **Adaptive training plan** — Base / Build1 / Build2 / Peak / Taper or Consolidation, sized from your CTL and target. Daily-adapt + reforecast + regenerate, all wired to live data.
- **3,054 ZWO workouts** — content-classified into 11 classes; a 24-week plan picks **150 distinct files** (every session is a different workout).
- **622 virtual routes** — Watopia, Yorkshire, Innsbruckring, Alpe d'Huez, Stelvio + 160 real-world courses; export as CRS or GPX.
- **FIT import + post-ride viewer** — power / HR / cadence curves, zone distribution, aerobic decoupling, DFA α1, polarisation classification (Treff et al. 2019).
- **Capability projection** for events — Allen-Coggan IF-by-duration + Pinot & Grappe 2011 RPP climb gate predict your finish, surface endurance / power / climb gaps in a 12-week build-up bar chart.
- **Finished-programme summary** — 12-metric recap (FTP / eFTP / VO₂max Δ, polarisation, monotony, mean-max power curve, Hooper trend, totals, decoupling) exportable as PNG (Pillow) or PDF (browser print).
- **Hardware-agnostic** — generate ZWO, ride in MyWhoosh / Tacx / Zwift / Hammerhead Karoo / outdoors, import the FIT back.
- **Single-user, localhost-only** — all data in `~/.domestique/profiles/<id>/`, [intervals.icu](https://intervals.icu) API key with `chmod 0600`, no telemetry, no cloud.

## Architecture

Pure Python, flat module layout — every `.py` at repo root is `import`ed
by another root module. PyInstaller bundles them as-is (no `domestique/`
package wrapper) so the spec, the DMG, and the EXE all stay simple.

```
domestique/
├── app.py                    — FastAPI app + ~70 endpoints (the "everything" entry)
├── launcher.py               — PyInstaller entry; opens pywebview window, boots uvicorn
├── training_planner.py       — Periodised plan generator + injury guardrails (G1–G7)
├── training.py               — Daily metrics, readiness, adapt-today-session
├── training_live.py          — Live ride session engine
├── ride_storage.py           — FIT archive + per-ride summarisation
├── fit_activity.py           — FIT parser wrapper (fitparse)
├── fitness_estimation.py     — eFTP drift, mean-max curve, capability projection
├── analytics.py              — NP / IF / TSS / decoupling / polarisation / DFA α1
├── readiness.py              — HRV / TSB / Hooper / sleep / RHR composite
├── profile_manager.py        — Multi-user profiles + ICU credentials
├── migrate_profiles.py       — One-shot profile migrations (called from app.py)
├── ride_report_png.py        — Pillow-rendered post-ride summary
├── programme_summary_png.py  — Pillow-rendered finished-programme recap
├── route_archetypes.py       — Procedural route shape primitives
├── geodesy.py                — GPX distance / elevation math
├── gpx_to_gc.py              — GPX → Golden Cheetah CRS converter
├── zones.py                  — Power / HR zone math
├── sleep.py, sleep_inhibit.py — Sleep parsing + macOS caffeinate hook
├── db.py, config.py, log_config.py — SQLite + config + logging plumbing
├── domestique.spec           — PyInstaller build spec
├── build_dmg.sh / build_win.bat — macOS DMG + Windows ZIP packagers
├── routes.json, profiles_indexed.json, surface_types.json,
│   route_profiles.json       — Heavy data shipped via PyInstaller `datas=`
├── tests/                    — pytest suite (~60 files; run `pytest -q`)
├── docs/                     — Architecture, science deep-dives, build guides
├── scripts/                  — One-off generators + scrapers (NOT imported by app.py)
├── workouts/                 — 3,054 ZWO interval workouts
├── courses/                  — Real-world climb library (CRS files, per-region subdirs)
├── static/, templates/       — FastAPI assets + Jinja2 templates
├── assets/                   — App icons (icon.icns / icon.ico / icon.png)
├── gpx_sources/              — Source GPX feeding `gpx_to_gc.py`
├── plans/, profiles/         — Per-user runtime state (gitignored after first use)
└── .github/workflows/        — `release.yml` builds + uploads DMG and EXE on tag
```

## Read more

- **[How the planner thinks (logic + science)](#how-the-planner-thinks-logic--science)** — every threshold cited, every formula explained.
- **[Auto-matching your rides to planned sessions](#auto-matching-your-rides-to-planned-sessions)** — how `done` / `ambiguous` / `no_match` are decided.
- **[CHANGELOG.md](CHANGELOG.md)** — the full pre-1.0 development log.
- **[Releases](https://github.com/platypus45/domestique/releases/latest)** — macOS DMG + Windows EXE attached to every release; both built and signed-style packaged in CI.

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

### 622 virtual routes
Watopia, Yorkshire, Innsbruckring, Alpe d'Huez, Mont Ventoux, Stelvio + 160 real-world courses. Export as CRS (RGT format) or GPX for route-based riding in Golden Cheetah / Wahoo / Garmin.

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

## Updating Domestique

Drag the new DMG onto your Applications folder. **Your rides, training plan, FTP history, wellness logs, ICU credentials, and athlete profile all survive the install** — they live in `~/.domestique/` outside the app bundle and are never touched.

On first launch after an upgrade you'll see a one-off confirmation toast naming the version transition and the data preserved.

See [docs/upgrading.md](docs/upgrading.md) for the full per-data-type preservation table and rollback steps.

---

## How the planner thinks (logic + science)

Every threshold below is in the code with an inline citation. This section explains *why* each value, not just *that* it exists.

### 0. The training-load model — TSS / IF / NP / CTL / ATL / TSB

Domestique uses the canonical Coggan / Allen / Banister fitness-fatigue framework end-to-end. Every number on the dashboard rolls up from these primitives:

**Per-ride scalars** (computed on FIT import OR pulled from intervals.icu):
- **NP** (Normalised Power) — 30-second rolling average of power, raised to the 4th, averaged, 4th-root taken. Penalises variable efforts vs steady ones (Coggan 2003). Code: `analytics.py compute_normalised_power()`.
- **IF** (Intensity Factor) = `NP / FTP`. A 1.0 IF ride = sustained at threshold; recovery rides ~0.5–0.6; race day ~0.85+. Allen & Coggan TR&P.
- **TSS** (Training Stress Score) = `(duration_seconds × NP × IF) / (FTP × 3600) × 100`. A 1-hour all-out at FTP = 100 TSS by definition. Foster's session-RPE × duration analogue. Allen & Coggan TR&P 3rd ed.
- **kJ work** = avg_power × duration. Used for nutrition planning (carb/h target).

**Multi-day fitness/fatigue** (Banister 1975 impulse-response, Coggan refinement):
- **CTL** (Chronic Training Load, "Fitness") = exponentially-weighted moving average of daily TSS with τ=42 days. `fitness_t = fitness_{t−1} + (TSS_t − fitness_{t−1}) / τ`.
- **ATL** (Acute Training Load, "Fatigue") = same EWMA with τ=7 days.
- **TSB** (Training Stress Balance, "Form") = CTL − ATL. Positive = freshening up; deeply negative = overreached.

**Where each comes from:**
- ICU-synced rider: `training.get_today_metrics()` pulls all three from intervals.icu's wellness API.
- ICU-unreachable: `ride_storage.compute_local_ctl()` rebuilds CTL from your local FIT archive (42-day EWMA over `summary.tss`). Same algorithm, different source.
- Per-ride zone-time, polarisation, and decoupling come from `analytics.py compute_polarization_block()` and the FIT zone-counting in `ride_storage._summarise_ride()`.

**TSB-driven daily caps:**
- **TSB < −10**: dashboard shows "Recover" badge.
- **TSB < −25**: `reforecast()` drops the next hard session one tier (`vo2max → threshold → tempo`).
- **TSB < −30**: deeply overreached. `daily_adapt_plan()` rescales remaining-week TSS to 0.6× as a forced de-load. Coggan/Allen overload threshold.

### 1. Periodisation engine

**Phases.** Standard Base → Build1 → Build2 → Peak → Taper for event-prep goals, or Base → Build1 → Build2 → Peak → **Consolidation** for non-event goals (FTP / VO2max / hybrid / general / endurance). Sized from `target_ctl` and `target_date` (Coggan & Allen, *Training and Racing with a Power Meter* 3rd ed.).

**Why consolidation, not taper, for FTP/VO2max cycles.** A taper is event-specific — you peak fresh on race day. If you don't have a race, you don't taper into a hole; you do a 1-week reduced-load Z2-only block to let fatigue dissipate and supercompensation peak (**Mujika 2010** *Sports Med* review: 7–14 day reduced-load period after a build block). Consolidation is `~50% of peak TSS` and ships an explicit prompt at end-of-week to FTP-test before generating the next cycle — this is the moment to cleanly capture your new fitness ceiling without residual fatigue depressing the result.

**Mid-cycle FTP recalibration (proactive overload prevention).** At the build1→build2 phase boundary the planner replaces one HIT slot with a Coggan-20 or Ramp `ftp_test` session. For cycles ≥ 16 weeks, a second test is also placed at the build2→peak boundary. This is a direct overload prevention: if your FTP rose 8% during build1 but the planner is still using the old value, all subsequent TSS targets and zone boundaries are computed against a baseline that's 8% too low — you train *systematically* harder than the model thinks. **Allen & Coggan TR&P 3rd ed.** recommends 4–6 week re-test cadence during build phases. The v4.1.0 eFTP-drift auto-apply path is *reactive* (waits for ICU to detect 7+ days of drift); the scheduled mid-cycle test is *proactive*.

**Weekly TSS budget** per phase (`training_planner.py PHASE_TARGETS`):

| Phase | Z1+Z2 hours | Z3+Z4 min | Z5+ min | Weekly TSS | Goal types |
|---|---|---|---|---|---|
| Base | 9.5 | 45 | 5 | 425 | all |
| Build1 / Build2 | 7.5 | 120 | 45 | 600 | all |
| Peak | 6.0 | 90 | 80 | 650 | all |
| Taper | 4.0 | 30 | 22 | 275 | event / ctl |
| **Consolidation** | 5.5 | 20 | 0 | 240 | FTP / VO2max / hybrid / general |

Synthesised from Seiler 2010, Mujika 2010, Rønnestad 2014, and Coggan/Allen for a trained age-grouper at ~10h/week. The intensity-distribution targets (`PHASE_POLARIZED_TARGETS`) come from the Seiler 2006 / Stöggl 2014 polarised model.

**CTL ramp safety**. The planner refuses to ramp CTL faster than `ramp_rate(current_ctl)` (steeper at low CTL, plateaus at high CTL). Override gate: TSB < −30 deep into a build phase pulls back the next week's `tss_target × 0.85` (Coggan/Allen overload threshold).

### 2. The seven injury-prevention guardrails (G1–G7)

Pre-v4.6.6 the planner detected fatigue / overload / soreness signals but never mutated the persisted plan. v4.6.6 wired the missing causation:

| # | Gate | Trigger condition | Action | Citation |
|---|------|---------|--------|----------|
| **G1** | Yesterday-was-hard floor | `yesterday_actual_tss / max(yesterday_planned, phase_daily_avg) > 1.5` | force today → Z2 | **Foster 1998** *Med Sci Sports Exerc* 30:1164 — session-load spike |
| **G2** | 48h Z5+ ceiling | rolling 48h `Σ z5–z7 ≥ 25 min` (cycling included) | force today → Z2 | **Hulin 2014** *Br J Sports Med* 48:708 — cumulative HI exposure |
| **G3** | Polarisation breach | current week `actual.z4plus_pct > target+8` OR `actual.z1z2_pct < target−10` | drop next 1–2 hard sessions one tier (vo2max → threshold → tempo) | **Seiler 2010 / Stöggl 2014 / Treff 2019** |
| **G4** | ACWR weekly scaling | last completed week `actual_tss / planned_tss > 1.5` | `next_week.tss_target ×= 0.85`, `hit_per_week −= 1` | **Gabbett 2016** *Br J Sports Med* 50:273 — sweet spot 0.8–1.3, >1.5 doubles injury risk |
| **G5** | Soreness peripheral cap | `daily_log.soreness ≥ 6` (1–7 scale) | force today → recovery, *regardless of HRV/TSB composite* | **Hooper & Mackinnon 1995** + **Cheung et al. 2003** — peripheral fatigue is independent of central HRV |
| **G6** | Hooper composite gate | `sleep + fatigue + stress + soreness ≥ 18` | force today → Z2 cap | **Hooper & Mackinnon 1995** — composite ≥18 = significant accumulated fatigue |
| **G7** | 3-day mean RPE drops HIT | `mean(ride.feel ∪ ride.perceived_exertion, last 3d) ≥ 7` AND today is HIT | drop today one tier | **Foster 1998** session-RPE |

**Priority chain** inside `adjust_today_session()`: `G5 > G6 > G2 > readiness composite > G1 > G7`. Earlier-firing gates short-circuit later ones. Each fired gate sets `s.adapted = True` and writes the citation into `s.description`.

### 3. The morning leg-check (Hooper composite)

The "Morning leg-check" prompt on the home page asks four 5-button questions:

1. **Sleep quality** — 😀 Great → 😫 Terrible
2. **Energy / fatigue** — 😀 Energised → 😫 Drained
3. **Stress** — 😀 Calm → 😫 Maxed
4. **Soreness** — 😀 Fresh → 😫 Very sore

**Why all four?** Hooper & Mackinnon 1995 (*J Sci Med Sport*) showed the *composite* predicts overtraining better than any single component. A rider can have crushed legs but score "fine" on subjective fatigue; a sleep-deprived rider can have fresh legs. The 4-field sum (range 4–28; threshold ≥18) catches what no single axis sees. **Saw et al. 2016** (*Br J Sports Med*) is the modern reinforcement: subjective wellness questionnaires correlate **better** with training response than any wearable HRV / RHR / sleep-score metric — self-report is the gold standard, not the fallback.

**How it wires back:**
- **20% weight** in `readiness_score` (combined with HRV 40%, TSB 20%, sleep 10%, RHR 10%).
- **G5 hard gate** — `soreness ≥ 6` forces recovery, bypassing the composite. Peripheral fatigue is real even when central HRV looks fine.
- **G6 hard gate** — `sleep + fatigue + stress + soreness ≥ 18` (Hooper threshold) forces Z2.

**Friction:** the form pre-defaults each field to "3 — Normal" so a user who only taps soreness still posts a sane composite. ~6 seconds of total tap time per morning. The 4 fields aren't optional — they're the science.

### 4. Closed feedback loops (the v4.1.0 originals)

Most "smart" planners compute these signals and then do nothing with them. Domestique actually mutates the next-day plan:

| Signal | Threshold | Action | Citation |
|---|---|---|---|
| **DFA α1** (autonomic balance) | mean over last 3 rides < 0.5 | tomorrow's threshold → Z2 (revert button) | Rogers et al. 2021 (PMID 33519504); Peng 1995 (DFA algorithm) |
| **Aerobic decoupling** (HR drift vs power) | > 5% over the last ride | next-day "Z2 recommended" advisory banner | Coyle & Gonzalez-Alonso 2001 (cardiac drift mechanism); TrainingPeaks canonical EF formula |
| **Foster monotony** (weekly load SD-vs-mean) | > 2.0 over 14 days | next week `tss_target × 0.85` and `hit_per_week − 1` | Foster 1998 *Med Sci Sports Exerc* (PMID 9662690) |
| **eFTP drift** (Intervals.icu) | > 3% above set FTP for 7+ consecutive days | FTP auto-applied with 48h revert toast | Allen & Coggan eFTP definition |
| **Local CTL fallback** | ICU unreachable | 42-day EWMA over your own imported FIT rides (no hardcoded baseline) | Coggan/Allen τ=42 |

### 5. FTP detection from a regular FIT

Ride a Coggan 20-min test or a Ramp test in any app, import the FIT:
- Detection by power-profile shape (no manual marking).
- Suggested FTP: `0.95 × avg 20-min power` (Coggan, Allen & Coggan 2019) or `0.75 × best 1-min` (Ramp, Ric Stern / British Cycling).
- Modal: Update / Keep / Custom. Every change logged to `ftp_test_history` with provenance (`tested_coggan_20min` / `tested_ramp` / `eftp_auto` / `manual`) plus a sparkline chart in Settings.
- Ramp auto-halt detection: cadence < 50 + power < 85% target for 3s.

### 6. Capability projection (event preparation)

When you set up a `goal_type=event_preparation` plan with `event_km` and `event_climb_m`, Domestique answers "if I follow this plan can I do it?" via a 4-step model:

1. **Flat-equivalent km** = `event_km + (event_climb_m / 100 × 1.5)` — climbing-distance equivalence heuristic.
2. **Projected average speed** via Pinot & Grappe 2011 (*Int J Sports Med* 32:839-844) RPP table by athlete W/kg + duration tier.
3. **Allen-Coggan IF lookup by duration**: 60min→0.95, 120→0.85, 180→0.80, 300→0.75, 480→0.70, 720→0.62 (linear interp).
4. `predicted_np = IF × FTP`, `predicted_tss = duration_h × IF² × 100`. Climb-power gate: required W/kg for the steepest 30-min climb vs your current sustained 30-min.

The dashboard renders three KPI tiles (Endurance Gap / Power Gap / Climb Readiness) plus a dual-axis chart of weeks-to-event vs your longest completed ride and your current sustained 30-min W/kg. `Goal.longest_ride_h_90d` auto-populates from your last 90 days of rides.

### 7. End-to-end planner pipeline

What actually happens when you set a goal and click "Generate plan":

```
Goal(goal_type, target_date, target_ctl, hours_per_week,
     event_km, event_climb_m, longest_ride_h_90d, last_ftp_test_date,
     available_days, daily_max_hours)
   │
   ▼
generate_phases(goal, current_ctl)
   │  applies CTL ramp safety (max +5/week from base CTL)
   │  splits into BASE → BUILD1 → BUILD2 → PEAK → TAPER per goal type
   │  each Phase carries weekly_tss_target, hit_count_min/max,
   │     polarisation target, rest_days_per_week
   ▼
for each PlannedWeek in plan:
  ├── pick WORKOUT_MIX_PREFERENCE row for (phase, week_in_phase)
  │      e.g. base W3+ → {endurance: 0.20, tempo: 0.15, sweet_spot: 0.25,
  │                       threshold: 0.20, vo2max: 0.10, vo2_short: 0.05,
  │                       recovery: 0.05}
  │
  ├── allocate session slots across available_days
  │      respecting max_weekday_hours / max_weekend_hours,
  │      placing the long ride on weekend, hard work on Tue/Thu
  │
  ├── sample_week_workouts() → for each slot, draw a ZWO file from the
  │      content-class pool with weight =
  │         mix_pref × variety_score × novelty_boost
  │      where variety_score rewards segment count + zone entropy +
  │      Rønnestad/microinterval/over-under/sprint patterns,
  │      and novelty_boost is 5× for never-picked, 0.05× for picked-once,
  │      effectively forcing 1 pick per file across the plan.
  │
  ├── _enforce_build2_peak_hard_floor() → guarantee ≥1 anaerobic +
  │      ≥1 neuromuscular + ≥3 vo2_short per build2/peak phase
  │      (post-pass swap if the random sampler missed one)
  │
  ├── _enforce_ronnestad_floor() → guarantee ≥1 Rønnestad-tagged file
  │      per build1/build2/peak (Rønnestad et al. 2015)
  │
  └── _check_eftp_drift() / _check_dfa_alpha1_low() / _check_decoupling()
         (these annotate the week with auto-adjustment hints; consumed
          on first /api/today-session call of the day)
```

**Daily adaptation** runs every time the dashboard loads:
1. `compute_today_metrics()` — pulls CTL/ATL/TSB + last-3-day decoupling + last-3-day DFA α1 + today's daily_log Hooper composite.
2. `compute_readiness()` — produces a 0-100 score weighted HRV 40% / TSB 20% / Hooper 20% / sleep 10% / RHR 10%.
3. `adjust_today_session(planned, readiness, recent_rides)` — runs the G1–G7 priority chain. First gate that fires sets the description, marks `s.adapted=True`, and returns. If no gate fires, the planned session ships unchanged.

**Re-forecast and regen:**
- `reforecast()` — runs on demand from the UI button. TSB-based hard-session intensity downshift + ACWR weekly TSS scaling + polarisation breach drop.
- `regenerate_from_today()` — full rebuild starting from today's CTL. Triggers when `detect_plan_gaps()` flags ≥2 consecutive missed weeks OR `expected_ctl − current_ctl > 15`.
- `auto_apply_eftp()` — fires when ICU eFTP > set FTP by ≥3% for 7+ consecutive days; bumps FTP with a 48h revert toast.

### 8. The pre-1.0 science table (carried forward)

| Feature | Method | Reference |
|---------|--------|-----------|
| DFA Alpha1 | Detrended Fluctuation Analysis on RR-intervals, Peng 1995 algorithm | Rogers et al. 2021 (PMID 33519504) |
| Aerobic Decoupling | EF = NP/avgHR per half (TrainingPeaks canonical) | Friel (coaching heuristic) |
| Foster Monotony / Strain | Weekly load SD-vs-mean ratio | Foster 1998 (PMID 9662690) |
| CTL / ATL / TSB | 42-day / 7-day exponentially-weighted TSS | Coggan & Allen |
| Local CTL fallback | 42-day EWMA over imported FIT rides | n/a — standard impedance-matching |
| Daily Adaptation | TSS pacer with cross-sport load, DFA α1 cap | Kiviniemi 2007, Javaloyes 2019 |
| Periodisation | Base / Build / Peak / Taper phases | Coggan & Allen, Friel |
| FTP — Coggan 20-min | 0.95 × avg 20-min power | Allen & Coggan 2019 |
| FTP — Ramp | 0.75 × best 1-min power | Ric Stern (British Cycling) |
| W'bal | Skiba 2015 differential + GoldenCheetah tau | Skiba et al. 2015 |
| Cardiac Drift | HR-driven SV decline mechanism | Coyle & Gonzalez-Alonso 2001 |
| Nutrition | Duration-gated carb targets | Jeukendrup 2014, ACSM 2016 |
| Polarisation Index (Treff PI) | log10((Z1+Z2)/Z3 × Z5+/Z3) | Treff et al. 2019 *Front Physiol* |
| Rønnestad microintervals | 30/15 + 40/20 detection by cycle period | Rønnestad et al. 2015 *Scand J Med Sci Sports* 25:143 |
| ACWR (acute:chronic workload ratio) | 7d:28d sweet spot 0.8–1.3 | Gabbett 2016 *Br J Sports Med* 50:273 |
| 48h cumulative Z5+ guard | Z5+Z6+Z7 ≥ 25min | Hulin et al. 2014 *Br J Sports Med* 48:708 |
| Hooper composite | Σ(sleep, fatigue, stress, soreness) ≥ 18 | Hooper & Mackinnon 1995 *J Sci Med Sport* |
| Subjective wellness > wearables | self-report responsiveness | Saw et al. 2016 *Br J Sports Med* |
| DOMS protective downshift | peripheral fatigue 24–72h post-eccentric | Cheung et al. 2003 *Sports Med* 33:145 |

---

## Auto-matching your rides to planned sessions

When you finish a workout — outside, indoors, in any app — Domestique automatically links the activity to its planned session. You don't have to mark anything done.

### How the auto-match fires

1. **You ride.** App-of-your-choice pushes the FIT to Intervals.icu (or you import the FIT directly into Domestique).
2. **Domestique syncs**: `_maybe_lazy_icu_sync()` runs on first `/api/calendar` load after boot, and on every `/api/rides/sync` call. Throttled to 1h normally, **forced** if today's date isn't represented in the local cache and last sync was >30min ago.
3. **`classify_rematch(session, activity)`** scores the pair on three axes:

| Axis | Match if | Source |
|---|---|---|
| **TSS** | actual within tolerance of planned | activity.tss vs session.tss_estimate |
| **Duration** | actual within tolerance | activity.duration_min vs session.duration_min |
| **IF band** | planned session_type's expected IF zone matches actual ride's IF band | `_activity_if_band(activity)` vs `SESSION_TYPE_TO_BAND[session.session_type]` |

4. **Outcomes**:
   - **3/3 axes** → `status: done` (auto-marked complete, green tick on the calendar cell)
   - **2/3 axes** → `status: ambiguous` (auto-classifier saw it but is uncertain — surfaces in the rematch panel)
   - **<2 axes with a same-day activity** → `status: no_match` (logged as separate ride; planned session stays `pending`)
   - **No same-day activity AND past date** → `status: missed`

5. The planner `reforecast()` and the daily-adapt path read `completion_matches` to know whether the prescription was actually delivered. If it wasn't (status missed) the next week's TSS gets restored from rolling deficit.

### Manual override

Two buttons in every workout-detail modal:
- **Rematch workout** — forces a re-evaluation with current tolerances.
- **Dismiss this session** — marks `status: dismissed` (stays visible greyed out, doesn't count toward missed).

The week-level Plan settings panel has a "Rematch all this week" action that runs `rematch_week(week, activities, today)` and shows a preview before applying.

### Cross-sport load

If you ICU-sync running, lifting, or anything else, those activities count toward `cross_sport_load` and feed into `compute_today_metrics()` so the cycling plan respects the full training stress, not just bike work.

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
- [TRADEMARKS.md](TRADEMARKS.md) — trademark policy
- [docs/cycling_apps.md](docs/cycling_apps.md) — comparison of free cycling apps accepting ZWO/FIT
- [docs/workout_sources.md](docs/workout_sources.md) — workout library provenance + legal stance
- [docs/windows_build.md](docs/windows_build.md) — path to a signed-style Windows `.exe` build
- [NOTICE](NOTICE) — Open Food Facts ODbL 1.0 attribution for the nutrition database (the missing-file `NUTRITION_LICENSE.md` link removed; ODbL terms remain inline in NOTICE)

---

## Abbreviations & terms

Hover any abbreviation in the body for an inline tooltip. The full glossary lives here for screen readers, mobile, and anyone reading the rendered Markdown elsewhere.

**Training-load model**
| Abbr. | Expansion | Meaning |
|---|---|---|
| TSS | Training Stress Score | `(duration_s × NP × IF) / (FTP × 3600) × 100`. 1h all-out at FTP = 100 TSS by definition (Coggan/Allen). |
| NP | Normalised Power | 30-second rolling average of power, raised to the 4th, averaged, 4th-root taken. Penalises variable efforts vs steady (Coggan 2003). |
| IF | Intensity Factor | `NP / FTP`. ≈1.0 = sustained at threshold; recovery ride ≈0.5–0.6; race day ≈0.85+ (Allen & Coggan). |
| FTP | Functional Threshold Power | Highest sustainable 1-hour power output (Coggan). |
| eFTP | estimated FTP | Auto-derived FTP from recent best efforts (intervals.icu). |
| CTL | Chronic Training Load | 42-day exponentially-weighted moving average of daily TSS — "fitness" (Banister/Coggan). |
| ATL | Acute Training Load | 7-day EWMA of daily TSS — "fatigue" (Banister/Coggan). |
| TSB | Training Stress Balance | CTL − ATL; positive = freshening up, deeply negative = overreached. |
| ACWR | Acute:Chronic Workload Ratio | last-7d load ÷ trailing-28d EWMA load. Sweet spot 0.8–1.3, >1.5 doubles injury risk (Gabbett 2016). |
| EWMA | Exponentially-Weighted Moving Average | The smoothing kernel used for CTL/ATL. |

**Physiology / monitoring**
| Abbr. | Expansion | Meaning |
|---|---|---|
| VO₂max | Maximal Oxygen Uptake | Peak rate of O₂ consumption during incremental exercise (mL O₂ · kg⁻¹ · min⁻¹). |
| HR / HRV / RHR / LTHR | Heart-rate metrics | HR, beat-to-beat HR variability, resting HR, lactate-threshold HR. |
| DFA α1 | Detrended Fluctuation Analysis α1 | Autonomic-balance scaling exponent computed from RR-intervals (Peng 1995). <0.5 = sympathetic dominance / fatigue (Rogers 2021). |
| RPE | Rating of Perceived Exertion | Subjective effort 1–10 (Borg CR-10) or 1–5 (intervals.icu `feel`). |
| DOMS | Delayed-Onset Muscle Soreness | Peripheral fatigue 24–72h post-eccentric (Cheung 2003). |
| PI | Polarization Index | `log10((Z1+Z2)/Z3 × Z5+/Z3)` — >2.0 classifies as polarised (Treff 2019). |
| RPP | Record Power Profile | Sustainable W/kg by duration tier per athlete category (Pinot & Grappe 2011). |

**File formats / hardware**
| Abbr. | Expansion | Meaning |
|---|---|---|
| ZWO | Zwift Workout file | XML describing structured intervals; portable across MyWhoosh / Tacx / Zwift / Karoo. |
| FIT | Flexible and Interoperable Data Transfer | Garmin's binary activity-recording format — power, HR, GPS, RR, etc. |
| CRS | Course Slope file | Golden Cheetah's gradient-profile format for trainer simulation. |
| GPX | GPS Exchange Format | Open XML schema for GPS routes/tracks. |
| API | Application Programming Interface | The intervals.icu REST endpoints Domestique calls. |
| DMG / EXE | Disk Image / Executable | macOS / Windows distribution formats; built by `build_dmg.sh` / `build_win.bat` and published on the GitHub Release. |
| CI | Continuous Integration | The GitHub Actions workflow at `.github/workflows/release.yml`. |

**Phases**: BASE / BUILD1 / BUILD2 / PEAK / TAPER (event prep) or CONSOLIDATION (FTP / VO₂max / hybrid / general goals — replaces TAPER for non-event cycles per Mujika 2010).

**Z1 … Z7+** = Coggan power zones 1 through 7 (recovery / endurance / tempo / threshold / VO₂max / anaerobic capacity / neuromuscular).

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

---

*Built with PubMed research, 3,054 workouts, and a deep love for cycling.*

---

## Trademarks

Tacx, Wahoo, Garmin, Polar, MyWhoosh, Zwift, Golden Cheetah, Rouvy, and Intervals.icu are trademarks of their respective owners. See [TRADEMARKS.md](TRADEMARKS.md).

---

Copyright © 2026 Domestique contributors.
