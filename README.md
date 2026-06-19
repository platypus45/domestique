<p align="center">
  <img src="assets/icon.png" alt="Domestique" width="180" height="180">
</p>

<h1 align="center">Domestique</h1>

<p align="center"><b>An adaptive cycling training planner that closes the loop between what you planned and what you actually did.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue" alt="Python">
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-green" alt="Platform">
  <img src="https://img.shields.io/badge/Workouts-4220-orange" alt="Workouts">
  <img src="https://img.shields.io/badge/Routes-622-purple" alt="Routes">
  <img src="https://img.shields.io/badge/Version-v2.1.1-brightgreen" alt="Version">
  <img src="https://img.shields.io/badge/Tests-1943-success" alt="Tests">
  <img src="https://img.shields.io/github/downloads/platypus45/domestique/total?label=Downloads&color=blue" alt="Downloads">
</p>

---

**Contents:** [TL;DR](#tldr) · [Why this exists](#why-this-exists) · [Quick start](#quick-start) · [Core mechanics](#core-mechanics) · [Architecture](#architecture-overview) · [How the planner thinks (science)](#how-the-planner-thinks-logic--science) · [Ride auto-matching](#auto-matching-your-rides-to-planned-sessions) · [Development](#development) · [Releases](#releases) · [Abbreviations](#abbreviations--terms) · [License](#license--attribution)

---

## TL;DR

Domestique is a localhost-only cycling planner that ships 4,220 structured ZWO workouts and 622 virtual routes, imports your post-ride FITs, and mutates the next-day prescription from *every* signal the ride exposed — TSS overshoot, polarisation breach, DFA α1, aerobic decoupling, Foster monotony, eFTP drift, Hooper composite. Most "smart" planners stop at the dashboard. Domestique stops at the prescription. Hardware-agnostic: generate ZWO, ride in MyWhoosh / Tacx / Zwift / Hammerhead / outdoors, import the FIT back. Single rider, no telemetry, no cloud.

## Why this exists

Most training apps fall into one of two modes:

- **Display-only**: HRV widgets, Banister fitness curves, polarisation rings — beautiful charts, zero behavioural feedback.
- **Calendar-based**: a fixed 12-week plan that doesn't care what you actually did yesterday.

Domestique is neither. Every signal that touches the dashboard also has a code-path that mutates a future session:

- **Soreness >= 6/7** on the morning Hooper composite -> today's VO2max session is forced to recovery (Hooper & Mackinnon 1995; Cheung et al. 2003 — peripheral fatigue is independent of central HRV).
- **Last week's actual TSS > 1.5 x planned** -> next week's TSS budget auto-cuts 15% (Gabbett 2016, ACWR sweet spot 0.8–1.3).
- **Rolling 48h Z5+ >= 25 min** -> today is forced to Z2 even with positive TSB (Hulin et al. 2014).
- **DFA alpha1 mean < 0.5 over last 3 rides** -> tomorrow's threshold session auto-swaps to Z2, with a revert button (Rogers et al. 2021).
- **Mid-cycle FTP recalibration** at the build1->build2 boundary auto-tests your FTP so the next 4 weeks of TSS targets aren't computed against a stale baseline (Allen & Coggan, *Training and Racing with a Power Meter* 3rd ed.).

Seven science-grounded guardrails (G1–G7), each citing a specific paper, plus a 1-week consolidation phase at the end of every non-event cycle so people don't ride straight from a peak into a fresh build with elevated fatigue (Mujika 2010).

---

## What's new in v2.1.0 / v2.1.1

The biggest release yet — a training-science layer plus a large reliability
round. **v2.1.1** adds a fast-follow patch on top: properly polarized event-prep
plans (easy Z2 fills the days, not rest), a much faster plan-open, non-cycling
activities kept out of the plan, and PubMed-citation fixes. Full detail in
[CHANGELOG.md](CHANGELOG.md); the v2.1.0 user-facing capabilities:

**Training & races**
- **Block periodization (opt-in, default OFF)** — a plan-form toggle reorganizes
  build/peak into ~3–4 week focus blocks (a VO2max block, then a threshold block
  toward the event) instead of mixing every hard type weekly, keeping one
  complementary session per block. Grounded in a verified PubMed screen
  ([Rønnestad 2014](https://pubmed.ncbi.nlm.nih.gov/22646668/) / [2020](https://pubmed.ncbi.nlm.nih.gov/31977120/) block-VO2 cycling RCTs, [Issurin 2008](https://pubmed.ncbi.nlm.nih.gov/18212712/), [Mølmen 2019 review](https://pubmed.ncbi.nlm.nih.gov/31802956/)) — but the edge is mixed for amateurs ([Almquist 2022](https://pubmed.ncbi.nlm.nih.gov/35299664/) found none), which is why it's **off by default**. Survives auto-recalc.
- **B and C races (opt-in)** — add intermediate events alongside your A goal; each
  gets a right-sized **mini-taper** (B: a 2-day volume trim that *keeps* intensity,
  C: a single easy/opener day), skipped inside the A taper or an unload week, and
  color-coded by priority on the calendar. Grounded in a verified taper screen
  ([Mujika & Padilla 2003](https://pubmed.ncbi.nlm.nih.gov/12840640/), [Bosquet 2007](https://pubmed.ncbi.nlm.nih.gov/17762369/), [Rønnestad 2017](https://pubmed.ncbi.nlm.nih.gov/27476525/)). A single-A plan is unchanged.
- **Honest workout labels** — an objective-coherence check surfaces a workout's
  hidden hard work in its display name (e.g. *"Endurance 120min — Z2 +VO2 set"*).
  No `.zwo` files are mutated; only the labels become honest.
- **Long pure-Z2 base rides** — 24 clean steady-endurance rides from 195–240 min
  for gran-fondo base (library is now **4,220** workouts).
- **Trustworthy DFA α1** — a high/medium/low **confidence flag** (artifact rate +
  window yield + sport), per-window α1 allowed down to 0.20 so hard-interval drops
  are shown instead of discarded, and running readings flagged low-confidence
  (the feature stays available for runners, just labelled).
- **Outdoor-variant export** — wrap any downloaded workout with an *off-plan*
  transit warm-up to the climb + an easy spin home (doesn't touch planned TSS).

**Reliability & a smarter plan** (the Windows-feedback round)
- **Windows:** profile + credential persistence across restarts, clean app
  relaunch (no orphaned server), accented profile names save cleanly.
- **intervals.icu TLS** via a bundled CA store — now on **both Windows and macOS**
  (fixes the `ICUNetworkError` reported on the Mac mini / MacBook Air too).
- **Plan:** weekly volume is load-based (not the sum of free hours), starts from
  your real current CTL, restores real rest weeks/days, and keeps VO2max off race
  eve — and all of this now holds through automatic re-fits.
- **FTP:** eFTP no longer silently rewrites your zones (opt-in).
- **Intensity distribution** is a user choice — polarized (default) / pyramidal /
  threshold.
- **PowerCurve** self-heals missing efforts; an impossible 600%-FTP "Z7" workout
  was removed and the dangerous-workout screen tightened.

**Not yet shipped (deferred):** volume-scaled hard-day count (F3) — capping hard
sessions on low-volume weeks conflicts with the planner's hard-type coverage rules
at realistic volumes (a typical build week's load supports ~2 hard sessions, the
rules want 3); needs the coverage rules made volume-aware, or the cap scoped to
genuinely low-volume weeks.

---

## Quick start

1. **Install** — macOS users have two paths: `brew tap platypus45/tap && brew install --cask domestique` (no Gatekeeper prompts) OR grab `Domestique-vX.Y.Z.dmg` from the [latest release](https://github.com/platypus45/domestique/releases/latest) and right-click → Open on first launch. Windows users grab `Domestique-Windows.zip`, unzip, run `Domestique.exe`. See [Installing on macOS](#installing-on-macos) for details.
2. **Connect Intervals.icu** — the first-run wizard walks you through it: paste your API key from [intervals.icu/settings](https://intervals.icu/settings) → Developer Settings (the athlete ID is auto-detected), then optionally enable Garmin Connect on Intervals.icu so rides sync automatically. No intervals.icu account? It's free and you can sign in with Garmin or Strava. Whole step is skippable — without ICU, Domestique falls back to local CTL from your imported FITs.
3. **Generate a plan** — pick a goal type (event prep / FTP / VO2max / hybrid / general / endurance), target date, target CTL, hours/week. The planner sizes Base / Build1 / Build2 / Peak / (Taper or Consolidation) phases, draws 150 distinct ZWO files across a 24-week plan, and adapts daily to your readiness.

After your first ride: click **Import FIT** in the header (or drag the `.fit` anywhere onto the window), or let ICU sync. Domestique imports it, reconciles it against your plan, adapts the next sessions, detects FTP tests automatically — and the views refresh on the spot.

### Installing on macOS

Download `Domestique-vX.Y.Z.dmg` from the [latest release](https://github.com/platypus45/domestique/releases/latest). Open it, drag `Domestique.app` to `Applications`, double-click. That's it.

Or via Homebrew:

```bash
brew tap platypus45/tap
brew install --cask domestique
```

(Homebrew not installed? One-liner at [https://brew.sh](https://brew.sh).)

### Installing on Windows (SmartScreen)

The Windows EXE is also unsigned. On first run, SmartScreen shows a blue "Windows protected your PC" dialog. Click **More info -> Run anyway**.

### First-run secrets

On first launch the setup wizard writes Intervals.icu credentials to a per-profile env file at `~/.domestique/profiles/<id>/.env` (mode 0600). There is no repo-root `.env`. All data lives in `~/.domestique/` outside the app bundle and survives upgrades — see [docs/upgrading.md](docs/upgrading.md).

### Multi-profile (multi-rider) support

Although Domestique runs locally on one machine, **multiple riders can share the same install**. Each profile is an isolated bundle under `~/.domestique/profiles/<id>/` with its own:

- Intervals.icu credentials (`.env`).
- Plan + availability calendar (`plans/current_plan.json`).
- Ride archive (`profiles/<id>/rides/*.json` + ICU sync cache).
- Athlete settings (FTP, LTHR, max HR, weight, zones).
- Daily log + morning Hooper composite.
- Wellness sync state (HRV, sleep, RHR per-profile).

Switch in the dashboard's **Settings → Profiles** panel, or via `POST /api/profiles/switch` directly. Create new profiles with **Create profile** in the same panel (`POST /api/profiles/create`). The active profile is persisted across app restarts (`~/.domestique/profile.txt`).

Use cases: a couple sharing a single laptop, a coach managing two riders, your training profile vs a partner's lower-volume profile. All data stays local; profiles don't sync to any cloud.

---

## Core mechanics

Every threshold cited below is inline in the code. Deep-dive section [How the planner thinks](#how-the-planner-thinks-logic--science) explains the math; this section is the one-paragraph-each summary.

### TSS / CTL / ATL / TSB — the training-load model

Canonical Banister 1975 impulse-response + Coggan/Allen refinement. **TSS** = `(duration_s x NP x IF) / (FTP x 3600) x 100` — 1h at FTP = 100 TSS by definition. **CTL** ("fitness") is a 42-day EWMA of daily TSS; **ATL** ("fatigue") is the 7-day EWMA; **TSB** ("form") = CTL − ATL. Time constants 42/7 are the conventional defaults — Domestique acknowledges they aren't validated per-athlete and ships them only because every commercial platform does the same; per-athlete tau fitting is on the v1.0.7 roadmap ([Hellard et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28651061/), Kontro 2026). When ICU is unreachable Domestique recomputes CTL from your local FIT archive with the same EWMA.

### HRV — resting (morning) vs DFA alpha1 (in-ride)

Two separate signals, same hardware (chest strap recording RR-intervals).

**Resting HRV (rMSSD overnight)** lands automatically via the ICU wellness sync if Garmin Connect is linked to ICU — `wellness.hrv` is the input for the readiness composite (HRV 40% / TSB 20% / Hooper 20% / sleep 10% / RHR 10%).

**DFA alpha1** is the autonomic-balance scaling exponent computed *post-ride* from beat-to-beat RR-intervals ([Peng 1995](https://pubmed.ncbi.nlm.nih.gov/11538314/) algorithm; sanity range [0.30, 1.60] per Gronwald & Hoos 2020). [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/) shows alpha1 < 0.75 marks the aerobic-threshold (LT1) drift and < 0.5 marks sustained sympathetic dominance — Domestique feeds it as a fatigue signal that downshifts tomorrow's intensity.

**Artifact rejection is mandatory** (v1.8.14 correctness fix). DFA alpha1 is acutely sensitive to ectopic/misdetected beats — every DFA paper requires RR cleaning before the math, and skipping it silently corrupts the result. Domestique runs a Malik 1996 20%-relative filter (`analytics._filter_rr_artifacts`) before all DFA windows, not just the 0ms/65535ms sentinel drop it did before. The literature tolerance is tight: <3% artifact = negligible bias, ~6% still keeps the HRV threshold within ±1 bpm (Gronwald et al. 2022 update). In one real ride ~1.3% uncorrected artifact beats dragged alpha1 from a correct 1.16 down to a physiologically impossible 0.573 and broke 57 of 72 windows via the R²-fit gate.

**Hardware requirement.** DFA alpha1 needs a sensor that emits RR-intervals over ANT+/BLE *and* a head unit that writes them to the FIT file as `HrvMessage` records.

| Sensor | RR-intervals? | DFA alpha1 supported |
|---|---|---|
| Garmin HRM-Pro / HRM-Pro Plus / HRM-Dual | yes | yes |
| Polar H10 / Wahoo TICKR X / Polar Verity Sense | yes | yes |
| Optical wrist HR (any Garmin / Apple Watch / Fitbit) | averaged HR only | no |
| Coros / Suunto wrist optical | no | no |

**One-time Garmin device setting.** Most Garmin head units ship with HRV recording **disabled** for activities. Even with an HRM-Pro paired, the FIT will contain zero `HrvMessage` records until the head unit's recording flag is on. The toggle lives under the head unit's **Data Recording** menu, NOT under Sensors / HRM.

| Device family | Exact path |
|---|---|
| Edge 530 / 830 / 1030 / 1030 Plus / 1040 | Settings -> Activity Profiles -> [Bike] -> Data Recording -> **HRV = On** |
| Edge Explore / Edge 130 | Firmware doesn't expose HRV recording; not supported |
| Fenix 8 | Hold watch face -> Watch Settings -> System -> Advanced -> Data Recording -> **Log HRV = On** |
| Fenix 6 / 7 / Epix 2 | Settings -> System -> Data Recording -> **Log HRV = On** |
| Forerunner 255 / 265 / 745 / 945 / 955 / 965 | Settings -> System -> Data Recording -> **Log HRV = On** |
| Garmin Connect Mobile (universal fallback) | Devices -> [your device] -> Activity Profiles -> Cycling -> Data Recording -> **HRV = On** |

Domestique detects the missing-HRV case automatically: when a synced ride has heart-rate data but no `HrvMessage` records, the app surfaces a one-time toast linking to this table. Pre-existing rides can't be backfilled; the strap polls itself, the head unit just has to write what arrives.

**Acquisition path.** Domestique pulls the raw FIT from ICU's `/api/v1/activity/{id}/fit-file` endpoint, parses `HrvMessage` records with `fit_activity.parse_hrv_messages()`, runs a 120-s sliding-window DFA alpha1 (Rogers 2021 default), and writes the result to the ride summary. No live polling, no Garmin OAuth, no manual upload.

Since v1.8.14 there's a **second acquisition path**: when ICU 404s the `.fit` (common for older or device-quirky rides) the app falls back to ICU's per-second `hrv` stream channel (RR-intervals in ms) and runs the same DFA pipeline off the stream. Validated equivalence — stream-path alpha1 = 0.626 vs FIT-path 0.627 on the same ride (within rounding) — so a ride gets DFA from whichever source is available.

### HRVT1 / HRVT2 thresholds + DFA zones (beta)

v1.8.14 adds threshold *detection* on top of the per-ride alpha1 signal. By regressing alpha1 on HR (and on power) across a ride's 120s/30s windows and interpolating the crossings, Domestique locates two metabolic thresholds non-invasively, with no lab test and no lactate draw:

- **HRVT1** at alpha1 = 0.75 -> the aerobic threshold (VT1 / LT1), i.e. the Zone-2 ceiling. Origin: Rogers et al. 2021 (PMC7845545 — the aerobic-threshold paper).
- **HRVT2** at alpha1 = 0.50 -> the anaerobic threshold (VT2 / LT2 / OBLA). Origin: Rogers et al. 2021 ([PMID 33925974](https://pubmed.ncbi.nlm.nih.gov/33925974/) — the *anaerobic*-threshold paper, distinct from the LT1 one above).

Each is reported as both an HR and a power value, and they drive a **3-zone intensity model**: Z1 (alpha1 > 0.75), Z2 (0.50–0.75), Z3 (< 0.50). The per-ride table shows the Z1·Z2·Z3 split as a bar.

**Cycling validation.** HRVT1 tracks VT1/LT1 with ICC 0.77, r 0.81 (Schaffarczyk et al. 2022, PMC9894976). For the anaerobic threshold the strong result is **power-specific**: HRVT2 power-output ICC **0.97**, r 0.92–0.93 vs VT2/OBLA (PMC10875128). We scope that claim to power deliberately — HR-based HRVT2 is unreliable and routinely omitted in the literature, so Domestique leans on the power value.

**Complements FTP — it doesn't replace it.** FTP anchors roughly one boundary (~LT2). DFA thresholds add the *aerobic*-threshold (LT1 / Zone-2 ceiling) anchor that FTP alone can't give, plus full HR-based zones for riders with no power meter. The detected zones are **display-only**: they never overwrite your configured FTP/zones.

**Caveats (why it's labelled beta):**
- **Needs a ramp.** Thresholds only resolve on a ride that actually *sweeps through* them — a progressive effort or ramp. Steady endurance rides keep alpha1 > 0.75 and never cross 0.75/0.50, so "no threshold detected" is the **common, expected** outcome on a Z2 ride, not an error.
- **Single-ride noise.** Per-ride detection r² is moderate (0.36–0.64). The app shows each ride's r² colour-coded and only folds rides with r² >= 0.50 into the aggregate.
- **Hysteresis.** Alpha1 lags intensity differently on up- vs down-ramps; a single linear fit pools both directions — a known, documented bias, not corrected in v1.
- **Day-to-day reproducibility unproven.** The day-to-day stability of HRV thresholds is still contested (Cassirame et al. 2025 methodological critique + Gronwald reply), hence the beta label.

### DFA alpha1 tab

A dedicated **"DFA alpha1" tab** surfaces all of the above: an aggregate↔per-ride toggle; a per-ride table (date, duration, avg HR, alpha1, the Z1·Z2·Z3 bar, and HRVT1/HRVT2 with r²-coloured confidence); click any row to see that ride's alpha1-over-time curve. The aggregate view shows recent-median HRVT1/HRVT2 HR and power zones across rides that cleared the r² gate.

### Hooper composite — the morning leg-check

Four 5-button questions on the home page: **sleep / energy / stress / soreness**, each 1–5. Hooper & Mackinnon 1995 (*J Sci Med Sport*) showed the *composite* (4-field sum, range 4–28) predicts overtraining better than any single component — a rider can have crushed legs but score "fine" on subjective fatigue; a sleep-deprived rider can have fresh legs. Saw et al. 2016 (*Br J Sports Med*) is the modern reinforcement: subjective wellness questionnaires correlate *better* with training response than any wearable HRV/RHR/sleep-score metric.

Wires into the planner at three points: 20% weight in `readiness_score`; **G5** hard gate (`soreness >= 6` forces recovery, bypassing the composite — peripheral fatigue is real even when central HRV looks fine, Cheung 2003); **G6** hard gate (`sleep + fatigue + stress + soreness >= 18` forces Z2). Form pre-defaults each field to "3 — Normal" so a user who only taps soreness still posts a sane composite (~6s tap time).

### Treff polarisation index + 80/0/20 distribution

The Seiler-style polarised model: train *easy* most of the time, train *hard* the rest, avoid the moderate trap. Treff et al. 2019 (*Front Physiol*) defines the Polarization Index as `log10((Z1+Z2)/Z3 x Z5+/Z3)` — > 2.0 classifies as polarised. Domestique computes Treff PI locally on every ride (identical result whether ICU is online or not) and feeds it into **G3** polarisation-breach guardrail. `WORKOUT_MIX_PREFERENCE` defaults bake Stoggl & Sperlich 2014's 80/0/20 distribution into phase-by-phase pick weights so the *generated plan* honors polarisation, not just the dashboard. G3 fires if the actual week diverges (`z4plus_pct > target+8` or `z1z2_pct < target−10`) — the next 1–2 hard sessions drop one tier.

### Seven injury-prevention guardrails (G1–G7)

Pre-v4.6.6 the planner detected fatigue/overload/soreness signals but never mutated the persisted plan. v4.6.6 wired the missing causation. Priority chain inside `adjust_today_session()`: `G5 > G6 > G2 > readiness composite > G1 > G7`. Earlier-firing gates short-circuit later ones.

| # | Gate | Trigger | Action | Citation |
|---|------|---------|--------|----------|
| **G1** | Yesterday-was-hard floor | `yesterday_tss / max(yesterday_planned, phase_daily_avg) > 1.5` | force today -> Z2 | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) |
| **G2** | 48h Z5+ ceiling | rolling 48h `Sum z5–z7 >= 25 min` | force today -> Z2 | [Hulin 2014](https://pubmed.ncbi.nlm.nih.gov/23962877/) |
| **G3** | Polarisation breach | week `z4plus_pct > target+8` OR `z1z2_pct < target−10` | drop next 1–2 hard sessions one tier | [Seiler 2010](https://pubmed.ncbi.nlm.nih.gov/20861519/) / [Stöggl 2014](https://pubmed.ncbi.nlm.nih.gov/24550842/) / [Treff 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6582670/) |
| **G4** | ACWR weekly scaling | last week `actual_tss / planned_tss > 1.5` | `next_week.tss_target x 0.85`, hit_per_week − 1 | [Gabbett 2016](https://pubmed.ncbi.nlm.nih.gov/26758673/) |
| **G5** | Soreness peripheral cap | `daily_log.soreness >= 6` | force today -> recovery (overrides HRV/TSB) | [Hooper 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/) + [Cheung 2003](https://pubmed.ncbi.nlm.nih.gov/12617692/) |
| **G6** | Hooper composite gate | `sleep + fatigue + stress + soreness >= 18` | force today -> Z2 | [Hooper & Mackinnon 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/) |
| **G7** | 3-day mean RPE drops HIT | `mean(feel, last 3d) >= 7` AND today is HIT | drop today one tier | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) session-RPE |

Each fired gate sets `s.adapted = True` and writes its citation into the session description so the rider sees *why* the prescription changed.

### Closed feedback loops on top of G1–G7

Five additional signals that mutate the *next-day or next-week* plan rather than just today's session:

| Signal | Threshold | Action | Citation |
|---|---|---|---|
| **DFA alpha1** | mean over last 3 rides < 0.5 | tomorrow's threshold -> Z2 (revert button) | [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/) |
| **DFA HRVT1/HRVT2** (beta) | alpha1 crosses 0.75 / 0.50 on a ramp ride | display-only LT1/LT2 HR+power anchors + 3-zone model (never overwrites FTP) | [Rogers 2021 — LT1](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) + [LT2](https://pubmed.ncbi.nlm.nih.gov/33925974/), [Schaffarczyk et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/) |
| **Aerobic decoupling** (HR drift vs power) | > 5% on last ride | next-day "Z2 recommended" advisory banner | [Coyle & González-Alonso 2001](https://pubmed.ncbi.nlm.nih.gov/11337829/) |
| **Foster monotony** (weekly load SD/mean) | > 2.0 over 14 days | next week `tss_target x 0.85`, hit_per_week − 1 | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) |
| **eFTP drift** (Intervals.icu) | > 3% above set FTP for 7+ consecutive days | FTP auto-applied with 48h revert toast | Allen & Coggan eFTP definition |
| **Local CTL fallback** | ICU unreachable | 42-day EWMA over imported FIT rides | Coggan/Allen tau=42 |

### TSB-driven daily caps

Separate from G1–G7, the TSB scalar itself has dashboard hooks: TSB < −10 surfaces a "Recover" badge; TSB < −25 -> `reforecast()` drops the next hard session one tier (`vo2max -> threshold -> tempo`); TSB < −30 -> `daily_adapt_plan()` rescales remaining-week TSS to 0.6x as a forced de-load (Coggan/Allen overload threshold).

---

## Architecture overview

Pure Python, flat module layout — every `.py` at repo root is `import`ed by another root module. PyInstaller bundles them as-is (no `domestique/` package wrapper) so the spec, the DMG, and the EXE all stay simple.

**Planner** (`training_planner.py` + `training.py`) sizes Base/Build1/Build2/Peak/(Taper|Consolidation) phases from CTL + target date, picks workouts from the 4,220-file library with a (mix_preference x variety_score x novelty_boost) sampler that forces ~1 pick per file across a plan, enforces minimum-floor counts of Ronnestad / anaerobic / neuromuscular sessions per phase, and runs the G1–G7 priority chain on every daily adapt. `regenerate_from_today()` rebuilds the plan when `detect_plan_gaps()` flags >=2 consecutive missed weeks; `reforecast()` runs the TSB / ACWR / polarisation adjustments on demand; `auto_apply_eftp()` fires when ICU eFTP > set FTP by >=3% for 7+ days.

**Library** ships 4,220 structured ZWO workouts (content-classified into 17 canonical classes — endurance / tempo / sweet spot / threshold / over-under / VO2max / VO2-short / anaerobic / neuromuscular / FTP test, plus ladder variants; tags-indexed for filter queries) and 622 real-world route courses (Alps, Dolomites, Pyrenees, Basque country, Flanders, Costa Blanca, Mallorca, Innsbruck 2018 Worlds, Alpe d'Huez, Mont Ventoux, Stelvio + 160 regional climbs; CRS or GPX export). No Zwift virtual worlds (Watopia / Yorkshire / etc. are Zwift-proprietary and not redistributable). A 24-week plan picks 150 distinct files (every session is a different workout). See [docs/workout_sources.md](docs/workout_sources.md) for provenance and licensing.

**Post-ride viewer** (`ride_storage.py` + `fit_activity.py` + `analytics.py` + `ride_report_png.py`) parses the imported FIT via fitparse, computes NP/IF/TSS, time-in-zone, aerobic decoupling, Treff polarisation classification, DFA alpha1 (when `HrvMessage` records are present), Belastingscore (Kontro 2026 3D impulse-response decomposition into CP / W' / Pmax — additive lens alongside TSS, not a replacement), eFTP cross-check, FTP-test detection (Coggan-20 by power-profile shape; ramp halt by cadence-drop heuristic), and renders a Pillow PNG / browser-print PDF post-ride summary. A separate `programme_summary_png.py` renders the 12-metric finished-programme recap.

```
domestique/
├── app.py                    — FastAPI app + ~70 endpoints
├── launcher.py               — PyInstaller entry; opens pywebview window, boots uvicorn
├── training_planner.py       — Periodised plan generator + G1–G7 guardrails
├── training.py               — Daily metrics, readiness, adapt-today-session
├── training_live.py          — Live W'-balance compute on FIT samples
├── ride_storage.py           — FIT archive + per-ride summarisation
├── fit_activity.py           — FIT parser wrapper (fitparse)
├── fitness_estimation.py     — eFTP drift, mean-max curve, capability projection
├── analytics.py              — NP / IF / TSS / decoupling / polarisation / DFA alpha1
├── readiness.py              — HRV / TSB / Hooper / sleep / RHR composite
├── profile_manager.py        — Multi-user profiles + ICU credentials
├── ride_report_png.py        — Pillow-rendered post-ride summary
├── programme_summary_png.py  — Pillow-rendered finished-programme recap
├── route_archetypes.py       — Procedural route shape primitives
├── geodesy.py                — GPX distance / elevation math
├── gpx_to_gc.py              — GPX -> Golden Cheetah CRS converter
├── zones.py                  — Power / HR zone math
├── sleep.py, sleep_inhibit.py — Sleep parsing + macOS caffeinate hook
├── db.py, config.py, log_config.py — SQLite + config + logging
├── domestique.spec           — PyInstaller build spec
├── build_dmg.sh / build_win.bat — macOS DMG + Windows ZIP packagers
├── routes.json, profiles_indexed.json, surface_types.json,
│   route_profiles.json       — Heavy data shipped via PyInstaller datas=
├── tests/                    — pytest suite (~60 files; run pytest -q)
├── docs/                     — Architecture, science deep-dives, build guides
├── scripts/                  — One-off generators + scrapers (NOT imported)
├── workouts/                 — 4,220 ZWO interval workouts
├── courses/                  — Real-world climb library (CRS files)
├── static/, templates/       — FastAPI assets + Jinja2 templates
├── assets/                   — App icons
├── gpx_sources/              — Source GPX feeding gpx_to_gc.py
├── plans/, profiles/         — Per-user runtime state (gitignored)
└── .github/workflows/        — release.yml builds DMG + EXE on tag
```

**Tech stack.** FastAPI + SQLite backend (REST only — no WebSocket since v4.0.0-alpha); fitparse for FIT parsing; lxml for ZWO parsing with `<tags>` indexing; pywebview for the native window (not a browser tab); PyInstaller for packaging + `create-dmg` for the drag-to-install DMG. Single-worker uvicorn — ride archive + profile state are per-process singletons, multiple workers unsupported. All listeners bind to `127.0.0.1` by default.

### Recommended external apps

Domestique plans + analyses — you ride in a separate app. The free-forever picks with ZWO/FIT import + Tacx Neo 2T support:

| App | Free? | ZWO import | Notes |
|---|---|---|---|
| **Golden Cheetah** | open-source | yes (ZWO/ERG/MRC) | Best match for a planner+library+viewer app like this; drive the trainer via ANT+ FE-C; drop Domestique's library into GC's workout folder once, all 4,220 files appear in Train view |
| **MyWhoosh** | fully free | yes (via web builder) | Scenery + Zwift-style ride; full ERG on Neo 2T |
| Tacx Training | free with Tacx HW | no (ZWO); GPX only | Native Tacx integration but no ZWO import |

See [docs/cycling_apps.md](docs/cycling_apps.md) for the full table.

**No laptop?** Both download formats — **ZWO** and **FIT** — drive a smart trainer in ERG. Load either into a trainer app (MyWhoosh / Tacx / Zwift / Golden Cheetah on a laptop or phone, which pairs to your trainer over ANT+ FE-C / Bluetooth FTMS) **or** push it straight onto a head unit — Garmin Edge / Garmin watch / Hammerhead Karoo / Wahoo ELEMNT — which steers the trainer's power directly over ANT+ FE-C / FTMS. Either format, either path; no virtual world, no subscription.

---

## Releases

Latest: **[v2.1.1 — faster plan open, polarized event-prep, citation fixes (on top of the v2.1.0 feature release)](https://github.com/platypus45/domestique/releases/latest)** (2026-06-19).

GitHub Actions ([release.yml](.github/workflows/release.yml)) builds and uploads the macOS DMG + Windows EXE on every tagged release.

**Highlights since v1.8.5** (see [CHANGELOG.md](CHANGELOG.md) for every shipped tag):

- **Block periodization, B/C races & a plan that respects your real training** (v2.1.0) — the biggest release yet. An opt-in **block-periodization** mode concentrates each build phase on one quality (a VO2 block, then a threshold block); **B and C races** ride alongside your A goal, each with an evidence-based mini-taper (trim volume, keep intensity) and color-coded on the calendar. The plan now builds weekly volume from a **load-based ceiling** (target CTL / recent TSS, not the sum of your free hours) starting from your **real current fitness**, with genuine rest weeks, no hard intervals on race eve, and a **polarized / pyramidal / threshold** distribution of your choosing. Plus: a **library that stops hiding hard sets** (objective-coherence labels), long pure-Z2 base rides, a **trustworthy DFA α1** readout (per-window lows + confidence flag), an **outdoor-variant** export, and Windows/Mac reliability fixes — profiles & API keys that persist, clean app relaunch, and the **intervals.icu TLS fix on both platforms**. eFTP no longer silently rewrites your zones.
- **Goal-aware + event-driven planning** (v2.0.0) — the plan now adapts to what you're training for. An **FTP** focus schedules more threshold/sweet-spot work, a **VO2max** focus more VO2/30-15 work. For a **target event**, distance + elevation drive a real long-ride progression (toward ~0.8× event duration, capped by your weekend hours), a feasibility-bounded fitness target (auto-lowered if the date's too soon), and climbing specificity in build/peak — so a 100 km/500 m and a 175 km/2900 m fondo produce visibly different plans. Survives auto-sync.
- **Evidence-based library + cleaner browser** (v1.10.0) — added the canonical Rønnestad short/long intervals, proper Wingate SIT (4-min recovery) and descending VO₂ ladders from the PubMed literature, removed 18 under-rested anaerobic files (the "rest too short" ones), and renamed 282 files to match their real content type. The library filter is redesigned: one unified Type, a 0–180 min duration range, and an Advanced panel for Min Score / Surface / Tags.
- **FIT import that just works** (v1.9.1) — drag a `.fit` from Finder anywhere onto the window (or click **Import FIT**); it imports, reconciles against your plan, adapts the next sessions, and the views refresh on the spot.
- **Onboarding + "This Week" overhaul** (v1.9.0) — first-run wizard reworked (paste one API key, athlete ID auto-detected, Garmin sync verified, account guidance, skippable); reconciliation of completed rides → done/missed now happens automatically on every sync (the manual "Reconcile Week" button is gone); multi-ride days count correctly toward the week; workout selection brought in line with the planner; library grown to 4,198 clean workouts.

- **One "Update plan" action** (v1.8.24) — the fragmented Reforecast / Regenerate / availability controls collapsed into a single primary button that auto-picks the right adjustment: a structure-preserving **rebalance** to today's TSB/ACWR/availability when you're on track, or a full **rebuild with a recovery ramp** (Gabbett ACWR < 1.3, Z2 reconditioning — never a catch-up spike) when you've fallen behind. "Regenerate" is now the advanced *Rebuild from scratch*; per-day *Rematch* stays.
- **Plan auto-adapts after missed workouts** (v1.8.24) — a ride sync that detects a significant *current* absence rebuilds automatically through the recovery ramp, once per absence episode (latched, no churn), recent-gap-gated (an old recovered gap never nags), and never inside an event taper (it flags "behind plan" instead).
- **Reshuffle honours the slot duration** (v1.8.19 ±25 % gate → v1.8.24 exact) — a 90-min slot returns a ~90-min workout, never a wildly different length.
- **Bigger, cleaner workout library** (v1.8.22 / v1.8.23 / ongoing) — grown from ~3 050 to **4,198** clean, copyright-free canonical files via a classify-before-write pipeline (every file run through the live content classifier and kept only if its type + title + duration match): polarized Rønnestad VO2 macro-blocks, comprehensive Z2/endurance structure variety (steady, two-zone, progressive, surges), and long-aerobic / duration coverage across all classes.
- **Plan integrity** (v1.8.18 / v1.8.20 / v1.8.21) — healed ghost `zwo_file` references and froze training history; regeneration preserves your edits (moved / dismissed / completed sessions) and the availability calendar; changing weekly hours repopulates the per-day calendar.
- **DFA α1 + dual thresholds** (v1.8.14) — see the DFA / HRV sections above: mandatory Malik artifact rejection, HRVT1/HRVT2 detection, intensity distribution, and a dedicated DFA tab; FIT-stream fallback when ICU 404s the `.fit`.
- **Notarized distribution** (v1.8.5+) — the macOS DMG opens with zero Gatekeeper prompts; new activities auto-push to the calendars.

---

## How the planner thinks (logic + science)

This deeper section explains *why* every threshold has the value it does, with inline citations to the literature.

### 0a. Honest limitations of the TSS-based stack

The peer-reviewed evidence supporting TSS as a *quantifier of training that was done* is reasonable. The evidence supporting it as a *predictor of training that will work* is correlational, mixed, and rarely tested out-of-sample.

| Study | n | finding |
|---|---|---|
| [Sanders et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28095061/) | road cyclists, season-long | TSS correlated **r ~ 0.75–0.79** with sub-maximal lactate-threshold power changes |
| [Wallace et al. 2014](https://pubmed.ncbi.nlm.nih.gov/24662229/) | runners | TSS vs. 1500 m time **r ~ 0.70**, slightly better than TRIMP (r ~ 0.65) and session-RPE (r ~ 0.60) |
| [Vermeire et al. 2021](https://pubmed.ncbi.nlm.nih.gov/31498226/) | 11 recreational cyclists, 12 weeks | **inconsistent** associations between TSS, multiple TRIMP variants, and 3 km TT performance. Different training types produce different adaptations despite identical TSS — "the relationship to performance will always be distorted." |

**Where TSS works:** as a workout descriptor for steady-state efforts; as a cumulative dose tracker when training is homogeneous; as a rough heuristic for taper/race timing.

**Where TSS breaks:** when training is intensity-heterogeneous (interval-heavy != endurance-heavy at same TSS); when efforts are above FTP and duration matters (the minute-2 vs minute-19 problem); when one event-specific energy system dominates; when workouts are highly intermittent (the NP recipe was designed for steady road riding, not 30/30 intervals).

**How Domestique mitigates** without replacing TSS as the primary load currency: the seven injury-prevention guardrails (G1–G7) layered on top of TSS-driven planning capture several of the failure modes Vermeire flags — see [§0b](#0b-literature-wired-into-the-planner) below.

### 0b. Literature wired into the planner

| Critique area / failure mode | Mitigation in Domestique | Source |
|---|---|---|
| Heterogeneous intensity (interval != endurance at same TSS) | **G3 polarization-breach guardrail** + Treff polarization index classification | [Treff et al. 2019 (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6582670/), [Stoggl & Sperlich 2014](https://pubmed.ncbi.nlm.nih.gov/24550842/) |
| Above-FTP minute-2-vs-minute-19 (Kontro's headline) | **Live W'-balance during ride** (Skiba 2015 differential) at `training_live.py:500-545` | [Skiba 2012 (PMID 22382171)](https://pubmed.ncbi.nlm.nih.gov/22382171/) |
| Acute:chronic load mismatch | **G4 ACWR** (7-day load > 1.5 x 28-day -> trim next week 15 %) | [Gabbett 2016 (BJSM)](https://bjsm.bmj.com/content/50/5/273) |
| Yesterday-was-hard / RPE 3-day drop | **G1 monotony** + **G7 RPE drop** | [Foster 1998 (PMID 9662690)](https://pubmed.ncbi.nlm.nih.gov/9662690/) |
| Z5+ accumulation ceiling | **G2 48 h Z5+ <= 25 min** | [Hulin et al. 2014 (BJSM)](https://bjsm.bmj.com/content/48/8/708) |
| Subjective fatigue TSS misses | **G5/G6 Hooper composite** + peripheral fatigue cap | [Hooper & Mackinnon 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/), [Cheung et al. 2003](https://pubmed.ncbi.nlm.nih.gov/12617692/) |
| 80/20 polarisation target | **POL 80/0/20** distribution baked into `WORKOUT_MIX_PREFERENCE` | [Stoggl & Sperlich 2014](https://pubmed.ncbi.nlm.nih.gov/24550842/) |
| Autonomic fatigue TSS can't see | **DFA alpha1 from RR-intervals** (Malik 1996 artifact filter first) -> next-day intensity decision | [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/), [Malik 1996](https://pubmed.ncbi.nlm.nih.gov/8598068/) |
| Aerobic-threshold (LT1) anchor FTP can't give | **DFA HRVT1/HRVT2 detection** (alpha1 0.75/0.50) -> display-only LT1/LT2 HR+power + 3-zone model | [Rogers 2021 — LT1 (PMC7845545)](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) + [LT2 (PMID 33925974)](https://pubmed.ncbi.nlm.nih.gov/33925974/), [Schaffarczyk et al. 2022 (PMC9894976)](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/) |
| Climb-specific record power profile | **Pinot & Grappe 2011 RPP gate** for capability projection | [Pinot & Grappe 2011](https://pubmed.ncbi.nlm.nih.gov/22052032/) |
| CP-from-FTP approximation | `int(ftp x 1.03)` (was naive `CP = FTP`) | [McGrath et al. 2021](https://pubmed.ncbi.nlm.nih.gov/34055164/) |
| Between-race freshening (B/C events around an A goal) | **B/C mini-taper** — trim volume, keep intensity (B: 2-day window, C: 1-day opener); skipped inside the A taper / unload weeks | [Mujika & Padilla 2003 (PMID 12840640)](https://pubmed.ncbi.nlm.nih.gov/12840640/), [Bosquet 2007 (PMID 17762369)](https://pubmed.ncbi.nlm.nih.gov/17762369/), [Rønnestad 2017 (PMID 27476525)](https://pubmed.ncbi.nlm.nih.gov/27476525/) |
| Concentrated vs. mixed build stimulus | **Block periodization** (opt-in) — focus blocks + 1 complementary session | [Rønnestad 2014 (PMID 22646668)](https://pubmed.ncbi.nlm.nih.gov/22646668/), [Mølmen 2019 (PMID 31802956)](https://pubmed.ncbi.nlm.nih.gov/31802956/), [Almquist 2022 (PMID 35299664)](https://pubmed.ncbi.nlm.nih.gov/35299664/) |
| FTP detection from real rides | Auto-eFTP from FIT archive + ICU eFTP cross-check | inline `fitness_estimation.py:220-263` |
| W' / Pmax energy-system decomposition (v1.0.6) | **Belastingscore quartet** (Aerobe / Glycolytisch / PCr) — secondary lens to TSS | [Kontro et al. 2026 (PLOS ONE)](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0341721) |

These guardrails layered on top of TSS-driven planning are what makes Domestique more than a TSS-EWMA dashboard. They directly address several of the failure modes Vermeire 2021 flags, while keeping TSS as the central currency the rider sees.

### 0c. Norwegian Method support — what's in, what's missing

The Norwegian Method (Marius Bakken / Ingebrigtsen / Bjorgen) explicitly **rejects TSS** as the primary intensity controller and substitutes blood lactate. Domestique covers parts of it but not the lactate-pacing core.

**Explicit non-goal:** Domestique does **not** capture or prescribe blood lactate during training. Finger-prick / earlobe-prick blood sampling adds friction we don't want — riders shouldn't have to draw blood mid-ride to use the planner. So we don't ship lactate input fields, lactate-prescribed sessions, or MLSS test protocols. Instead, we approximate the same physiology using signals already captured non-invasively from the FIT file (HR + RR-intervals -> DFA alpha1 + autonomic load) and from the power trace (Skiba W'-balance + post-ride decoupling).

| Norwegian Method element | What it controls intensity by | Domestique substitute |
|---|---|---|
| Lactate-controlled threshold work | Blood lactate 2-4 mmol/L during the session | **Power-based threshold class** (95-105 % FTP) + **HR ceiling** at ~88 % HR_max -> flags G6 if exceeded for >15 min in a sub-threshold session. |
| Double-threshold sessions (AM + PM) | Two sub-LT2 sessions same day | Partially — threshold-class workouts exist; v1.1.0 adds explicit AM/PM scheduling **without lactate gating**. |
| HR as primary intensity proxy when lactate isn't available | HR ceiling that approximates LT2 | HR ingested from FIT; v1.1.0 wires HR-ceiling into session prescription. |
| MLSS testing protocol | Distinct test from FTP, requires blood draws | **Out of scope.** FTP + Coggan-20 + Ramp tests only. |
| Conservative volume ramp (no big TSS spikes) | Total volume in hours | Tracked + Gabbett ACWR (G4) caps weekly TSS jumps. |
| Avoidance of the moderate/threshold "trap" (Seiler-style) | Sessions explicitly avoid Z3 (76-90 % FTP) | Stoggl/Sperlich 80/0/20 + G3 enforce this. |
| Daily readiness signal (Bakken: lactate response to a fixed warmup) | Resting lactate or sub-LT1 sample-power | **DFA alpha1 from RR-intervals** — same physiological substrate (autonomic / parasympathetic withdrawal proxy). [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/) shows DFA alpha1 tracks the LT1 boundary non-invasively from beat-to-beat HR variability. |
| In-session "back off" signal | Lactate climbing above 4 mmol/L | **W'-balance** from Skiba 2015 differential — depleting W' captures the same "above-threshold for too long" dynamic that drives lactate accumulation. Live during ride. |
| Workout-was-too-hard detection | Post-session lactate elevation | **Aerobic decoupling** post-ride from FIT (HR drift vs. power drift) + DFA alpha1 nadir during the session. |

**The honest framing**: Domestique gives you a Norwegian-Method-shaped polarization plan (80/0/20, Z3 avoidance, conservative ramp) and approximates the daily-readiness piece via DFA alpha1 (autonomic) and W'-balance (mechanical) — both come for free from the FIT file with the right sensors. We don't replicate the lactate-prescribed precision of the Norwegian elites, but we capture the *intent* (sub-LT2 controlled work + autonomic-fatigue-aware day-to-day adjustment) without asking the rider to bleed.

### 0d. How a ride is indexed end-to-end

```
You finish a ride
  |
  v
Garmin / Wahoo / Karoo / virtual trainer uploads to Strava / intervals.icu
  |
  v
intervals.icu computes (server-side):
  - NP, IF, TSS, kJ
  - time-in-zone (Z1-Z7 + SS)
  - aerobic decoupling
  - polarization-index + classification
  - on-profile: wPrime, pMax (v1.0.6+), eFTP, CP
  |
  v
Domestique's _sync_icu_activities() (app.py:9141)
  | pulls activities list, fetches detail + samples per ride
  v
Cached as ~/.domestique/rides/icu/i<external_id>.json (24-field envelope)
  |
  v
SQLite athlete_metrics table <- daily CTL/ATL/TSB + (v1.0.6) per-component fitness/fatigue
  |
  v
Library matching <- Domestique's 16-class taxonomy
  | matches the picked workout's structure
  | surfaces display_name as the modal title
  |
  v
Planner reads back on every reforecast / regenerate
  | G1-G7 guardrails check what was actually done
  | Treff PI feeds G3 polarization breach
  | Auto-fire reforecast (v1.0.3) if added > 0
  | Glycolytic-stacking soft penalty (v1.0.6 advisory)
  |
  v
Tomorrow's session adapts to today's ride
```

**What ICU computes vs. what Domestique computes:**
- **From ICU** (cached as-is): NP, IF, TSS, time-in-zone, aerobic decoupling, kJ above FTP, wPrime, pMax, eFTP, CTL/ATL/TSB defaults.
- **Locally computed by Domestique**: Treff polarization index + classification (so the result is identical even when ICU is offline), the 16-class library taxonomy match, the seven G1–G7 guardrails, eFTP cross-check from local FIT archive, capability projection (Pinot & Grappe RPP), DFA alpha1 (when RR-intervals are present in the FIT), Belastingscore (v1.0.6) for energy-system decomposition.

### 0e. Belastingscore / 3D impulse-response model (v1.0.6)

The 3-dimensional impulse-response model from [Kontro/Mastracci/Cheung/MacInnis 2026 (PLOS ONE)](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0341721) ships in v1.0.6 as an **additive lens** alongside TSS — not a replacement. It splits training stress into three energy systems with their own time constants:

| component | power-curve param | what it tracks | Banister tau_1 / tau_2 (paper defaults, profile-overridable) |
|---|---|---|---|
| **CP** (aerobic) | Critical Power | mitochondrial / oxidative capacity | 52 d / 10 d |
| **W'** (glycolytic) | W-prime, anaerobic work capacity | lactate-tolerance / above-CP work | 5 d / 5 d |
| **Pmax** (alactic) | Peak Power | PCr / sprint capacity | 10 d / 4 d |

**Per-ride breakdown (Kontro Eq. 8–10):** for each second of the ride, power is attributed to the three systems based on proximity to MPA (Maximum Power Available, Eq. 4). The result is **Belastingscore = SS_CP + SS_W' + SS_Pmax**, calibrated so 1 h at CP ~ 100 SS (matches the Coggan TSS convention).

**Where v1.0.6 surfaces it:**
- **Ride detail panel**: a secondary "Belastingscore — energy-system breakdown" card under the existing TSS hero grid (Total / Aerobe / Glycolytisch / PCr).
- **Athlete-Metrics chart**: a collapsed `<details>` panel below the existing CTL/ATL/TSB chart with three normalised fitness curves (CP / W' / Pmax), tau defaults from the paper.
- **Plan tab phase rows**: a small subordinate stacked bar showing CP / W' / Pmax distribution under the primary `weekly_tss` headline.

**Honest caveat the paper itself states**: "no published data exist to support the energy-system specific model parameters." The tau defaults (52/10, 5/5, 10/4) are a single-athlete illustrative example from the paper's supplementary, not population-validated. Domestique exposes them as profile-level overrides and documents the caveat in the dashboard tooltip copy.

**Why TSS stays primary:** the Kontro paper is intentionally additive — its authors keep the conventional Banister/CTL framework alongside the 3D decomposition. Domestique mirrors that. The 3D model adds resolution for athletes who want to see which energy system was stressed, but the planner still picks workouts based on the existing TSS-driven taxonomy the rider is already used to.

### 1. Periodisation engine

**Phases.** Standard Base -> Build1 -> Build2 -> Peak -> Taper for event-prep goals, or Base -> Build1 -> Build2 -> Peak -> **Consolidation** for non-event goals (FTP / VO2max / hybrid / general / endurance). Sized from `target_ctl` and `target_date` (Coggan & Allen, *Training and Racing with a Power Meter* 3rd ed.).

**Why consolidation, not taper, for FTP/VO2max cycles.** A taper is event-specific — you peak fresh on race day. If you don't have a race, you don't taper into a hole; you do a 1-week reduced-load Z2-only block to let fatigue dissipate and supercompensation peak (**Mujika 2010** *Sports Med* review: 7–14 day reduced-load period after a build block). Consolidation is `~50% of peak TSS` and ships an explicit prompt at end-of-week to FTP-test before generating the next cycle — this is the moment to cleanly capture your new fitness ceiling without residual fatigue depressing the result.

**Mid-cycle FTP recalibration (proactive overload prevention).** At the build1->build2 phase boundary the planner replaces one HIT slot with a Coggan-20 or Ramp `ftp_test` session. For cycles >= 16 weeks, a second test is also placed at the build2->peak boundary. This is a direct overload prevention: if your FTP rose 8% during build1 but the planner is still using the old value, all subsequent TSS targets and zone boundaries are computed against a baseline that's 8% too low — you train *systematically* harder than the model thinks. **Allen & Coggan TR&P 3rd ed.** recommends 4–6 week re-test cadence during build phases. The v4.1.0 eFTP-drift auto-apply path is *reactive* (waits for ICU to detect 7+ days of drift); the scheduled mid-cycle test is *proactive*.

**Weekly TSS budget** per phase (`training_planner.py PHASE_TARGETS`):

| Phase | Z1+Z2 hours | Z3+Z4 min | Z5+ min | Weekly TSS | Goal types |
|---|---|---|---|---|---|
| Base | 9.5 | 45 | 5 | 425 | all |
| Build1 / Build2 | 7.5 | 120 | 45 | 600 | all |
| Peak | 6.0 | 90 | 80 | 650 | all |
| Taper | 4.0 | 30 | 22 | 275 | event / ctl |
| **Consolidation** | 5.5 | 20 | 0 | 240 | FTP / VO2max / hybrid / general |

Synthesised from Seiler 2010, Mujika 2010, Ronnestad 2014, and Coggan/Allen for a trained age-grouper at ~10h/week. The intensity-distribution targets (`PHASE_POLARIZED_TARGETS`) come from the Seiler 2006 / Stoggl 2014 polarised model.

**CTL ramp safety.** The planner refuses to ramp CTL faster than `ramp_rate(current_ctl)` (steeper at low CTL, plateaus at high CTL). Override gate: TSB < −30 deep into a build phase pulls back the next week's `tss_target x 0.85` (Coggan/Allen overload threshold).

### 2. FTP detection from a regular FIT

Ride a Coggan 20-min test or a Ramp test in any app, import the FIT:
- Detection by power-profile shape (no manual marking).
- Suggested FTP: `0.95 x avg 20-min power` (Coggan, Allen & Coggan 2019) or `0.75 x best 1-min` (Ramp, Ric Stern / British Cycling).
- Modal: Update / Keep / Custom. Every change logged to `ftp_test_history` with provenance (`tested_coggan_20min` / `tested_ramp` / `eftp_auto` / `manual`) plus a sparkline chart in Settings.
- Ramp auto-halt detection: cadence < 50 + power < 85% target for 3s.

### 3. Capability projection (event preparation)

When you set up a `goal_type=event_preparation` plan with `event_km` and `event_climb_m`, Domestique answers "if I follow this plan can I do it?" via a 4-step model:

1. **Flat-equivalent km** = `event_km + (event_climb_m / 100 x 1.5)` — climbing-distance equivalence heuristic.
2. **Projected average speed** via Pinot & Grappe 2011 (*Int J Sports Med* 32:839-844) RPP table by athlete W/kg + duration tier.
3. **Allen-Coggan IF lookup by duration**: 60min->0.95, 120->0.85, 180->0.80, 300->0.75, 480->0.70, 720->0.62 (linear interp).
4. `predicted_np = IF x FTP`, `predicted_tss = duration_h x IF^2 x 100`. Climb-power gate: required W/kg for the steepest 30-min climb vs your current sustained 30-min.

The dashboard renders three KPI tiles (Endurance Gap / Power Gap / Climb Readiness) plus a dual-axis chart of weeks-to-event vs your longest completed ride and your current sustained 30-min W/kg. `Goal.longest_ride_h_90d` auto-populates from your last 90 days of rides.

**v2.0.0 — the demand model now drives the plan, not just a dashboard.** Until v2.0.0 the projection above was read-only. Now, for `goal_type=event`, those numbers shape the prescription:

- **Long-ride progression (the lever):** the weekend long ride ramps from your current longest toward `0.8 × predicted_finish_h` (`+25 min/week`), capped by your `max_weekend_hours` and a 5 h ceiling, and stops ≥3 weeks out so the taper owns it. Distance + elevation therefore change the plan — a 100 km/500 m and a 175 km/2900 m fondo produce visibly different long-ride schedules. Long-ride *duration*, not a CTL number, is the event-specific signal (TrainingPeaks ATP; Friel; CTS "longest ride 0.7–1.0× event duration").
- **Feasibility-bounded fitness target:** `target_CTL = band[event_type] × (0.94 + 0.12 × difficulty)`, `difficulty = clamp((finish_h − 2)/6, 0, 1)`, then capped by `max_achievable = current_CTL + ramp_rate × (weeks − 2)` — the goal is auto-lowered if the date is too soon rather than prescribing an impossible ramp. CTL is treated as a forecast/anchor, **not** derived from the event (`event_TSS / 7` is dimensionally meaningless and used by no platform — confirmed against TrainingPeaks / intervals.icu / WKO5 / Xert).
- **Climbing specificity:** a route with `event_climb_m / event_km > 12` m/km biases build + peak toward sustained threshold / over-under / VO₂ and away from punchy sprints — phase-gated, because race-specific work belongs in build/peak (Seiler; Rønnestad).
- Applied on initial generation **and** every regenerate / reconcile, so it survives the auto-sync.

**Goal-aware selection (v2.0.0):** independent of any event, an `ftp` goal up-weights threshold + sweet-spot + over-under; a `vo2max` goal up-weights VO2max + Rønnestad-30/15; `ftp_vo2max` / `hybrid` blends both — so the evidence-based protocols come up *more often for the matching focus* (previously the mix was identical regardless of goal).

Method triangulated across PubMed (Seiler 4×8 [21812820], Rønnestad 30/15 [31977120], durability [PMC11235642], MLSS/MCT1 [11683677]), established platform + coach practice (TrainingPeaks Performance-Manager/ATP, Friel CTL-ramp, intervals.icu, WKO5, Xert), and a four-agent adversarial design grill.

### 4. End-to-end planner pipeline

What actually happens when you set a goal and click "Generate plan":

```
Goal(goal_type, target_date, target_ctl, hours_per_week,
     event_km, event_climb_m, longest_ride_h_90d, last_ftp_test_date,
     available_days, daily_max_hours)
   |
   v
_event_demand_targets(goal, athlete, fitness)   # v2.0.0: None for non-event
   |  -> {difficulty, long_target_h, long_start_h, climbing_bias}
   v
generate_phases(goal, current_ctl, event_targets)
   |  applies CTL ramp safety (max +5/week from base CTL)
   |  v2.0.0: event difficulty nudges target CTL ±6%, feasibility-capped
   |  splits into BASE -> BUILD1 -> BUILD2 -> PEAK -> TAPER per goal type
   |  each Phase carries weekly_tss_target, hit_count_min/max,
   |     polarisation target, rest_days_per_week
   v
for each PlannedWeek in plan:
  - pick WORKOUT_MIX_PREFERENCE row for (phase, week_in_phase)
       v2.0.0: goal-aware emphasis (ftp/vo2max/hybrid) + climbing
       emphasis (event_climb, build2/peak only) tilt the HIT-class pick
       e.g. base W3+ -> {endurance: 0.20, tempo: 0.15, sweet_spot: 0.25,
                        threshold: 0.20, vo2max: 0.10, vo2_short: 0.05,
                        recovery: 0.05}
  - allocate session slots across available_days, respecting
       max_weekday_hours / max_weekend_hours, placing the long ride on
       weekend, hard work on Tue/Thu
  - sample_week_workouts() -> for each slot, draw a ZWO file from the
       content-class pool with weight = mix_pref x variety_score x novelty_boost
       where variety_score rewards segment count + zone entropy +
       Ronnestad/microinterval/over-under/sprint patterns, and
       novelty_boost is 5x for never-picked, 0.05x for picked-once,
       effectively forcing 1 pick per file across the plan.
  - _enforce_build2_peak_hard_floor() -> guarantee >=1 anaerobic +
       >=1 neuromuscular + >=3 vo2_short per build2/peak phase
       (post-pass swap if the random sampler missed one)
  - _enforce_ronnestad_floor() -> guarantee >=1 Ronnestad-tagged file
       per build1/build2/peak (Ronnestad et al. 2015)
  - _check_eftp_drift() / _check_dfa_alpha1_low() / _check_decoupling()
       (these annotate the week with auto-adjustment hints; consumed
        on first /api/today-session call of the day)
```

**Daily adaptation** runs every time the dashboard loads:
1. `compute_today_metrics()` — pulls CTL/ATL/TSB + last-3-day decoupling + last-3-day DFA alpha1 + today's daily_log Hooper composite.
2. `compute_readiness()` — produces a 0-100 score weighted HRV 40% / TSB 20% / Hooper 20% / sleep 10% / RHR 10%.
3. `adjust_today_session(planned, readiness, recent_rides)` — runs the G1–G7 priority chain. First gate that fires sets the description, marks `s.adapted=True`, and returns. If no gate fires, the planned session ships unchanged.

**Re-forecast and regen:**
- `reforecast()` — runs on demand from the UI button. TSB-based hard-session intensity downshift + ACWR weekly TSS scaling + polarisation breach drop.
- `regenerate_from_today()` — full rebuild starting from today's CTL. Triggers when `detect_plan_gaps()` flags >=2 consecutive missed weeks OR `expected_ctl − current_ctl > 15`. **v2.0.0:** carries the same event-demand targets (long-ride progression, feasibility CTL, climbing emphasis) as the initial build, so an event plan does not revert on auto-sync.
- `auto_apply_eftp()` — fires when ICU eFTP > set FTP by >=3% for 7+ consecutive days; bumps FTP with a 48h revert toast.

### 5. Complete scientific reference

Every peer-reviewed study the planner relies on, each linked to PubMed/PMC. The
tables above (§0a–§0c, the G1–G7 guardrails, the feedback loops) show *how* each
is applied; this is the consolidated source list. Textbook / coaching references
(Allen & Coggan, Friel, British Cycling) have no PubMed entry and are marked as such.

| Feature | Method | Reference |
|---------|--------|-----------|
| DFA Alpha1 | Detrended Fluctuation Analysis on RR-intervals, Peng 1995 algorithm; Malik 1996 20% artifact filter pre-DFA | [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/), [Malik 1996](https://pubmed.ncbi.nlm.nih.gov/8598068/), [Peng 1995](https://pubmed.ncbi.nlm.nih.gov/11538314/) |
| DFA HRVT1 / HRVT2 (beta) | alpha1=0.75 -> LT1, alpha1=0.50 -> LT2; HR+power crossing per ramp ride; 3-zone model (display-only) | [Rogers 2021 — LT1](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) + [LT2](https://pubmed.ncbi.nlm.nih.gov/33925974/), [Schaffarczyk et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/), [reliability](https://pmc.ncbi.nlm.nih.gov/articles/PMC10875128/) |
| Aerobic Decoupling | EF = NP/avgHR per half (TrainingPeaks canonical) | Friel (coaching heuristic) |
| Cardiac Drift | HR-driven SV decline mechanism | [Coyle & González-Alonso 2001](https://pubmed.ncbi.nlm.nih.gov/11337829/) |
| Foster Monotony / Strain | Weekly load SD-vs-mean ratio | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) |
| CTL / ATL / TSB | 42-day / 7-day exponentially-weighted TSS | Allen & Coggan (textbook) |
| Local CTL fallback | 42-day EWMA over imported FIT rides | n/a — standard impedance-matching |
| Daily Adaptation | TSS pacer with cross-sport load, DFA alpha1 cap | [Kiviniemi 2007](https://pubmed.ncbi.nlm.nih.gov/17849143/), [Javaloyes 2020](https://pubmed.ncbi.nlm.nih.gov/31490431/) |
| Periodisation | Base / Build / Peak / Taper phases | Allen & Coggan, Friel (textbooks) |
| Polarised distribution | 80/0/20 hard:easy; avoid the Z3 "trap" | [Seiler 2010](https://pubmed.ncbi.nlm.nih.gov/20861519/), [Stöggl & Sperlich 2014](https://pubmed.ncbi.nlm.nih.gov/24550842/) |
| Aerobic base / Z2 volume (event-prep base-fill) | ~80% low-intensity Z2 builds the aerobic base; fill available days with easy volume up to the ACWR-safe load rather than resting them | [Seiler & Kjerland 2006](https://pubmed.ncbi.nlm.nih.gov/16430681/), [Stöggl & Sperlich 2015](https://pubmed.ncbi.nlm.nih.gov/26578968/), [Rosenblat 2019](https://pubmed.ncbi.nlm.nih.gov/29863593/) / [2025](https://pubmed.ncbi.nlm.nih.gov/39888556/) |
| Polarisation Index (Treff PI) | log10((Z1+Z2)/Z3 x Z5+/Z3) | [Treff et al. 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6582670/) |
| Block periodization (opt-in) | Focus blocks + 1 complementary; evidence mixed for amateurs | [Rønnestad 2014](https://pubmed.ncbi.nlm.nih.gov/22646668/) / [2020](https://pubmed.ncbi.nlm.nih.gov/31977120/), [Issurin 2008](https://pubmed.ncbi.nlm.nih.gov/18212712/), [Mølmen 2019](https://pubmed.ncbi.nlm.nih.gov/31802956/), [Almquist 2022](https://pubmed.ncbi.nlm.nih.gov/35299664/) |
| Taper / B-C race mini-taper | Cut volume, keep intensity, short window | [Mujika & Padilla 2003](https://pubmed.ncbi.nlm.nih.gov/12840640/), [Bosquet 2007](https://pubmed.ncbi.nlm.nih.gov/17762369/), [Rønnestad 2017](https://pubmed.ncbi.nlm.nih.gov/27476525/), [Mujika 2010](https://pubmed.ncbi.nlm.nih.gov/20840559/) |
| Rønnestad microintervals | 30/15 + 40/20 detection by cycle period | [Rønnestad et al. 2015](https://pubmed.ncbi.nlm.nih.gov/24382021/) |
| FTP — Coggan 20-min | 0.95 x avg 20-min power | Allen & Coggan 2019 (textbook) |
| FTP — Ramp | 0.75 x best 1-min power | Ric Stern, British Cycling (heuristic) |
| W'bal | Skiba 2015 differential + GoldenCheetah tau | [Skiba et al. 2015](https://pubmed.ncbi.nlm.nih.gov/25425258/), [Skiba 2012](https://pubmed.ncbi.nlm.nih.gov/22382171/) |
| Record power profile / CP | RPP capability gate; CP ≈ FTP × 1.03 | [Pinot & Grappe 2011](https://pubmed.ncbi.nlm.nih.gov/22052032/), [McGrath et al. 2021](https://pubmed.ncbi.nlm.nih.gov/34055164/) |
| Per-athlete CTL/ATL tau | model fitting (roadmap) | [Hellard et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28651061/) |
| ACWR (acute:chronic workload ratio) | 7d:28d sweet spot 0.8–1.3 | [Gabbett 2016](https://pubmed.ncbi.nlm.nih.gov/26758673/) |
| 48h cumulative Z5+ guard | Z5+Z6+Z7 >= 25min | [Hulin et al. 2014](https://pubmed.ncbi.nlm.nih.gov/23962877/) |
| Hooper composite | Sum(sleep, fatigue, stress, soreness) >= 18 | [Hooper & Mackinnon 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/) |
| Subjective wellness > wearables | self-report responsiveness | [Saw et al. 2016](https://pubmed.ncbi.nlm.nih.gov/26423706/) |
| DOMS protective downshift | peripheral fatigue 24–72h post-eccentric | [Cheung et al. 2003](https://pubmed.ncbi.nlm.nih.gov/12617692/) |
| TSS ↔ performance evidence | correlational, mixed (see §0a) | [Sanders 2017](https://pubmed.ncbi.nlm.nih.gov/28095061/), [Wallace 2014](https://pubmed.ncbi.nlm.nih.gov/24662229/), [Vermeire 2021](https://pubmed.ncbi.nlm.nih.gov/31498226/) |
| Nutrition | Duration-gated carb targets | Jeukendrup 2014, ACSM 2016 (position stand) |

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
   - **3/3 axes** -> `status: done` (auto-marked complete, green tick on the calendar cell)
   - **2/3 axes** -> `status: ambiguous` (auto-classifier saw it but is uncertain — surfaces in the rematch panel)
   - **<2 axes with a same-day activity** -> `status: no_match` (logged as separate ride; planned session stays `pending`)
   - **No same-day activity AND past date** -> `status: missed`

5. The planner `reforecast()` and the daily-adapt path read `completion_matches` to know whether the prescription was actually delivered. If it wasn't (status missed) the next week's TSS gets restored from rolling deficit.

### Manual override

Two buttons in every workout-detail modal:
- **Rematch workout** — forces a re-evaluation with current tolerances.
- **Dismiss this session** — marks `status: dismissed` (stays visible greyed out, doesn't count toward missed).

The week-level Plan settings panel has a "Rematch all this week" action that runs `rematch_week(week, activities, today)` and shows a preview before applying.

### Cross-sport load

If you ICU-sync running, lifting, or anything else, those activities count toward `cross_sport_load` and feed into `compute_today_metrics()` so the cycling plan respects the full training stress, not just bike work.

---

## Development

```bash
git clone https://github.com/platypus45/domestique.git
cd domestique
pip install -r requirements.txt
python launcher.py
pytest -q                            # ~1,401 tests pass on clean-main
```

### Build your own

```bash
./build_dmg.sh                       # macOS — writes ~/Desktop/Domestique.dmg
build_win.bat                        # Windows — writes dist\Domestique\Domestique.exe
```

GitHub Actions ([.github/workflows/release.yml](.github/workflows/release.yml)) builds both on every tagged release.

### Workout library sources

The 4,198 ZWO files have three provenance buckets (see [docs/workout_sources.md](docs/workout_sources.md) for full detail + licensing):

- **1797 pre-existing** (pre-v4 generated workouts) — untouched across the pivot.
- **1105 whatsonzwift reconstructions** — facts-only inference from the public rendered interval graph; original names, descriptions, and coach cues stripped and regenerated from structure; never touches the site's ZWO download endpoint; `<author>Domestique Library</author>` on every file.
- **24 GitHub MIT/Unlicense imports** (`macgrrl/zwift-workouts` Unlicense, `michaelahlers/michaelahlers-zwift-workouts` MIT) — provenance tracked in `workouts/.github_imports_manifest.json`.
- **124 procedural gap-fillers** (pyramids, short VO2, short threshold, over-unders with varied ratios, neuromuscular sprints, short sweet spot — categories that were under-represented).
- **4 FTP test protocols** scraped from `whatsonzwift.com/workouts/ftp-tests` and tagged with `<tag name="ftp_test"/>`.

Copyright verdict: interval numbers + durations are uncopyrightable facts (Feist v Rural Telephone); names + descriptions are copyrightable — those are stripped and regenerated on every scraped file. For open-source redistribution safety, fork the procgen + GitHub subset only.

### Security notes

Domestique is a **single-user, local-first desktop app**. The security model follows from that: everything runs on your machine, and the only data that leaves it is what you choose to sync to your *own* intervals.icu / Strava account. There is no Domestique-operated server and no telemetry.

**Network exposure — no remote access.** The bundled API server binds to `127.0.0.1` only — there is no `0.0.0.0` bind anywhere in the codebase, and the notarized macOS build ships no inbound-network (`com.apple.security.network.server`) entitlement, so nothing on your LAN or the internet can reach it. The local endpoints are **unauthenticated by design**: they trust the localhost boundary for single-user use. Do not manually rebind to `0.0.0.0` or expose port 8080 without adding your own authentication layer. Outbound connections are made only to **intervals.icu** (and **Strava**, if configured) over HTTPS, using your own credentials.

**Input handling — path-traversal protection.** Every file-download endpoint (workouts, courses, GPX) routes the user-supplied path segments through a single `_safe_path()` guard: it resolves the full path and verifies it stays inside the intended base directory (`pathlib.Path.is_relative_to`), rejecting `../` climb-outs, an absolute-path segment, and symlink escapes. A request that tries to escape the base returns 404 — never the target file. This is covered by `tests/test_security.py` (see below).

**Credentials at rest.** Your intervals.icu API key is stored in `~/.domestique/profiles/<id>/.env`, written atomically with `chmod 0600` (owner-only) and with newline-injection rejected. It is **plaintext at rest** (not encrypted), but never uploaded, never synced, and excluded by `.gitignore`. On macOS you can move it into Keychain after first run; automated Keychain migration for ICU credentials is planned (Strava already uses Keychain on macOS). Do not commit your `.env` anywhere.

**Code signing / distribution integrity.**
- **macOS** — the DMG is signed with an Apple Developer ID and **notarized**; it opens with no Gatekeeper prompt, and the ticket is stapled to both the `.app` and the DMG. The DMG's `sha256` is recorded in the Homebrew cask (`Casks/domestique.rb`), so `brew install --cask` verifies it.
- **Windows** — the EXE is currently **unsigned**. SmartScreen will show "unknown publisher" and you must confirm "Run anyway." Authenticode signing is on the roadmap (the release workflow has the `signtool` step stubbed). Until then, only download from the official [GitHub releases](https://github.com/platypus45/domestique/releases) page.

**Tested.** A security regression suite (`tests/test_security.py`, 16 tests) covers the path-traversal guard (`../`, absolute segment, nested escape blocked; legit nested names allowed, no foreign-file leak from a download endpoint), the credential writer (`0600` + newline-injection rejection), and the localhost-only bind (no `0.0.0.0`, no inbound entitlement). It runs in CI on every change.

**Known gaps (honest disclosure).** The local API has no authentication layer beyond the localhost bind; credentials are not encrypted at rest (macOS Keychain migration planned); and broader input-validation / SSRF coverage plus Windows code-signing are still on the roadmap. These are tracked, not overlooked.

**Reporting a vulnerability.** Please open a private security advisory on the [repository](https://github.com/platypus45/domestique/security/advisories) rather than a public issue.

### Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request — in particular, all commits must be signed off under the Developer Certificate of Origin (`git commit -s`).

See also:
- [COURSES_LICENSE.md](COURSES_LICENSE.md) — route and elevation data provenance.
- [TRADEMARKS.md](TRADEMARKS.md) — trademark policy.
- [docs/cycling_apps.md](docs/cycling_apps.md) — comparison of free cycling apps accepting ZWO/FIT.
- [docs/workout_sources.md](docs/workout_sources.md) — workout library provenance + legal stance.
- [docs/windows_build.md](docs/windows_build.md) — path to a signed-style Windows `.exe` build.
- [NOTICE](NOTICE) — Open Food Facts ODbL 1.0 attribution for the nutrition database.

---

## Abbreviations & terms

Hover any abbreviation in the body for an inline tooltip. The full glossary lives here for screen readers, mobile, and anyone reading the rendered Markdown elsewhere.

**Training-load model**

| Abbr. | Expansion | Meaning |
|---|---|---|
| TSS | Training Stress Score | `(duration_s x NP x IF) / (FTP x 3600) x 100`. 1h all-out at FTP = 100 TSS by definition (Coggan/Allen). |
| NP | Normalised Power | 30-second rolling average of power, raised to the 4th, averaged, 4th-root taken. Penalises variable efforts vs steady (Coggan 2003). |
| IF | Intensity Factor | `NP / FTP`. ~1.0 = sustained at threshold; recovery ride ~0.5–0.6; race day ~0.85+ (Allen & Coggan). |
| FTP | Functional Threshold Power | Highest sustainable 1-hour power output (Coggan). |
| eFTP | estimated FTP | Auto-derived FTP from recent best efforts (intervals.icu). |
| CTL | Chronic Training Load | 42-day exponentially-weighted moving average of daily TSS — "fitness" (Banister/Coggan). |
| ATL | Acute Training Load | 7-day EWMA of daily TSS — "fatigue" (Banister/Coggan). |
| TSB | Training Stress Balance | CTL − ATL; positive = freshening up, deeply negative = overreached. |
| ACWR | Acute:Chronic Workload Ratio | last-7d load / trailing-28d EWMA load. Sweet spot 0.8–1.3, >1.5 doubles injury risk (Gabbett 2016). |
| EWMA | Exponentially-Weighted Moving Average | The smoothing kernel used for CTL/ATL. |

**Physiology / monitoring**

| Abbr. | Expansion | Meaning |
|---|---|---|
| VO2max | Maximal Oxygen Uptake | Peak rate of O2 consumption during incremental exercise (mL O2 / kg / min). |
| HR / HRV / RHR / LTHR | Heart-rate metrics | HR, beat-to-beat HR variability, resting HR, lactate-threshold HR. |
| DFA alpha1 | Detrended Fluctuation Analysis alpha1 | Autonomic-balance scaling exponent computed from RR-intervals (Peng 1995). <0.5 = sympathetic dominance / fatigue (Rogers 2021). |
| RPE | Rating of Perceived Exertion | Subjective effort 1–10 (Borg CR-10) or 1–5 (intervals.icu `feel`). |
| DOMS | Delayed-Onset Muscle Soreness | Peripheral fatigue 24–72h post-eccentric (Cheung 2003). |
| PI | Polarization Index | `log10((Z1+Z2)/Z3 x Z5+/Z3)` — >2.0 classifies as polarised (Treff 2019). |
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

**Phases**: BASE / BUILD1 / BUILD2 / PEAK / TAPER (event prep) or CONSOLIDATION (FTP / VO2max / hybrid / general goals — replaces TAPER for non-event cycles per Mujika 2010).

**Z1 ... Z7+** = Coggan power zones 1 through 7 (recovery / endurance / tempo / threshold / VO2max / anaerobic capacity / neuromuscular).

---

## License & attribution

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Tacx, Wahoo, Garmin, Polar, MyWhoosh, Zwift, Golden Cheetah, Rouvy, and Intervals.icu are trademarks of their respective owners. See [TRADEMARKS.md](TRADEMARKS.md).

*Built with PubMed research, 4,198 workouts, and a deep love for cycling.*

Copyright (c) 2026 Domestique contributors.
