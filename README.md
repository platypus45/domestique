<p align="center">
  <img src="assets/icon.png" alt="Domestique" width="180" height="180">
</p>

<h1 align="center">Domestique</h1>

<p align="center"><b>An adaptive cycling training planner that closes the loop between what you planned and what you actually did.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-blue" alt="Python">
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-green" alt="Platform">
  <img src="https://img.shields.io/badge/Workouts-4220-orange" alt="Workouts">
  <img src="https://img.shields.io/badge/Routes-622-purple" alt="Routes">
  <img src="https://img.shields.io/badge/Version-v2.2.16-brightgreen" alt="Version">
  <img src="https://img.shields.io/badge/Tests-1967-success" alt="Tests">
  <img src="https://img.shields.io/github/downloads/platypus45/domestique/total?label=Downloads&color=blue" alt="Downloads">
</p>

---

**Contents:** [TL;DR](#tldr) · [Why this exists](#why-this-exists) · [What's new](#whats-new-in-v210--v211) · [Quick start](#quick-start) · [Core mechanics](#core-mechanics) · [Architecture](#architecture-overview) · [The science](#the-science--how-the-planner-thinks) · [Ride auto-matching](#auto-matching-your-rides-to-planned-sessions) · [Releases](#releases) · [Development](#development) · [Abbreviations](#abbreviations--terms) · [License](#license--attribution)

> Deep dive: the full planner logic, formulas, and the complete cited reference table now live in **[docs/SCIENCE.md](docs/SCIENCE.md)** (moved out of this README to keep it readable).

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

## What's new in v2.2.16

- **Really runs on macOS Monterey (12) / Big Sur (11) now** — v2.2.15's fix was incomplete (the
  embedded Python was still built for newer macOS); the app's Python is now built for older macOS.
  Same version, same features.

## What's new in v2.2.15

- **Runs on macOS Monterey (12) and Big Sur (11)** — earlier Mac builds silently failed to launch on
  older macOS (even via the DMG); fixed, with no feature changes.

## What's new in v2.2.14

- **Races show on the calendar** — your event (and any B/C races) now appear as a 🏁 race on the day,
  in This Week when it's race week, and on the Today card — instead of a stray training session on
  race day.
- **Longer pre-race taper** — a few more light/opener days before your main race (not complete rest),
  per the tapering research.

## What's new in v2.2.13

- **Workouts that match the day** — each day now picks a workout whose type *and* length fit the
  plan, so a sprint day gets a real sprint session at the planned length (no more 57-minute
  mostly-threshold file on a 45-minute sprint slot). Title, duration, and chart all agree.
- **Mislabelled sprint files reclassified** + **more true max-effort sprint sessions** added, so
  sprint days have genuine options at every length.

## What's new in v2.2.11

- **Richer today's session** — clicking it on the home page opens the full workout (power profile +
  ZWO/FIT downloads), the same card as the plan.
- **Clearer "This Week"** — an obvious TODAY badge, ✓done / ✕missed markers, and click a day to see
  how it went.
- **Steadier sync banner** (no more jumping counts), and the redundant manual "Sync now" button is
  gone — the app syncs automatically when it opens.

## What's new in v2.2.10

- **DFA α1 thresholds are back.** A bug in the heart-rate-variability artifact filter
  was discarding most beats on any ride with a normal warm-up, so the DFA panel showed
  "No thresholds detected yet." Fixed — your HRVT1/HRVT2 thresholds compute again.
- **The DFA tab shows update progress** ("Updating X of Y rides") with a bar while it
  recomputes, then refreshes the panel automatically.

## What's new in v2.2.9

- **Manual FTP sticks.** Saving your FTP no longer appears to revert to the
  (lower) eFTP in the top bar and Settings — the live value now refreshes on save
  instead of only after an app restart.
- **Sync banner shows real progress.** "Syncing X of Y activities · N new" with a
  live %, instead of an indeterminate strip that could look stuck.
- **DFA α1 thresholds stay put.** Re-syncing a ride no longer wipes the locally
  computed α1 / HRVT thresholds, so the DFA panel stops blanking to "no thresholds
  detected" after a sync.

## What's new in v2.2.8

- **intervals.icu sync fixed for OAuth accounts.** If you connected with "Sign in
  with intervals.icu", recent rides could silently stop syncing into the planner —
  the sync now recognises your OAuth login (it previously only checked the old
  API-key login). Rides catch up automatically on the next open.

## What's new in v2.2.7

- **Workout downloads fixed.** ZWO/FIT downloads could save as empty files on
  macOS — they now always write the real workout (or report a clear error),
  never a 0-byte file. Fixes issue
  [#5](https://github.com/platypus45/domestique/issues/5).
- **Your goal sticks.** The training goal (e.g. Hybrid FTP + VO2max) no longer
  reverts to "Event" after a restart. Fixes issue
  [#6](https://github.com/platypus45/domestique/issues/6).
- **Faster home page** on large ride histories.

## What's new in v2.2.6

- **One "Today" card.** The home page used to show several "today/readiness" surfaces
  on two different scales (e.g. 3.8/10 vs 66/100) with scattered buttons — they read as
  contradictions. Now there's a single **Today** card: one readiness number, one state
  (Ready / Ease off / Rest), one action. When your leg-check overrides a high physiological
  score it now *explains* it ("78/100 · Rest advised · from your leg-check") instead of
  contradicting itself. Finishes issue
  [#3](https://github.com/platypus45/domestique/issues/3) (Request 2).

## What's new in v2.2.5

- **Clearer Strava-synced rides.** intervals.icu's API can't read Strava activities
  (Strava's terms), so those rides showed blank detail + spammed the log. The ride
  detail now explains it and points you to **Garmin → intervals.icu** (full detail,
  no restriction) with a direct link to **export your Garmin history**.
- **Recovery weeks actually feel like recovery** — a deload now has more rest days +
  fewer hours than its build weeks, not just lower TSS.
- **Saner readiness tier-down** — a "tier-down" never raises load anymore, and a
  high-soreness day drops to easy in one tap instead of one tier at a time.
- (Issues [#2](https://github.com/platypus45/domestique/issues/2),
  [#3](https://github.com/platypus45/domestique/issues/3),
  [#4](https://github.com/platypus45/domestique/issues/4).)

## What's new in v2.2.0

- **Sign in with intervals.icu (OAuth)** — link your account in one click instead
  of pasting an API key. An explicit, retryable step shows **“✓ Linked as
  <name>”**; existing API-key users are prompted to switch; your FTP / weight /
  LTHR are prefilled from your account; Settings has **“Copy from intervals.icu.”**
- **Plan styles** — choose **Automatic (varied)**, **Fixed-core (repeatable)** (one
  quality type/phase, reps progress, constant Z2 base), or a **Template**
  (Polarized Base / FTP Builder).
- **Customizable zones** — an **Edit** button on Power and HR zones (prefilled,
  editable, reset-to-auto; honored across display + analysis).
- **First-sync visibility** — a top-bar **“Syncing activities X of Y (NN%)”** while
  your history indexes; an **FTP-rise** banner when your FTP actually looks higher.
- **Reliability** — the day-detail popup reads one coherent story, easy days stay
  easy, recovery weeks truly deload, B/C races work on any plan, and logs no longer
  balloon. Full detail in [CHANGELOG.md](CHANGELOG.md).

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
2. **Connect Intervals.icu** — the first-run wizard walks you through it: click **Sign in to intervals.icu**, log in + approve in your browser (OAuth — no API keys to copy, athlete auto-detected), then optionally enable Garmin Connect on Intervals.icu so rides sync automatically. No intervals.icu account? It's free and you can sign in with Garmin or Strava. Whole step is skippable — without ICU, Domestique falls back to local CTL from your imported FITs. (Already linked with an API key from an older version? You'll be prompted to switch to sign-in.)
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

On first launch you sign in to Intervals.icu (OAuth); the bearer token — and, on installs that linked before v2.2.0, any legacy API key — is written to a per-profile env file at `~/.domestique/profiles/<id>/.env` (mode 0600). There is no repo-root `.env`. All data lives in `~/.domestique/` outside the app bundle and survives upgrades — see [docs/upgrading.md](docs/upgrading.md).

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

Every threshold cited below is inline in the code. The deep dive — full math and citations — is in [docs/SCIENCE.md](docs/SCIENCE.md); this section is the one-paragraph-each summary.

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

**Cycling validation.** HRVT1 tracks VT1/LT1 with ICC 0.77, r 0.81 ([Schaffarczyk et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/)). For the anaerobic threshold the strong result is **power-specific**: HRVT2 power-output ICC **0.97**, r 0.92–0.93 vs VT2/OBLA ([PMC10875128](https://pmc.ncbi.nlm.nih.gov/articles/PMC10875128/)). We scope that claim to power deliberately — HR-based HRVT2 is unreliable and routinely omitted in the literature, so Domestique leans on the power value.

**Complements FTP — it doesn't replace it.** FTP anchors roughly one boundary (~LT2). DFA thresholds add the *aerobic*-threshold (LT1 / Zone-2 ceiling) anchor that FTP alone can't give, plus full HR-based zones for riders with no power meter. The detected zones are **display-only**: they never overwrite your configured FTP/zones.

**Caveats (why it's labelled beta):**
- **Needs a ramp.** Thresholds only resolve on a ride that actually *sweeps through* them — a progressive effort or ramp. Steady endurance rides keep alpha1 > 0.75 and never cross 0.75/0.50, so "no threshold detected" is the **common, expected** outcome on a Z2 ride, not an error.
- **Single-ride noise.** Per-ride detection r² is moderate (0.36–0.64). The app shows each ride's r² colour-coded and only folds rides with r² >= 0.50 into the aggregate.
- **Hysteresis.** Alpha1 lags intensity differently on up- vs down-ramps; a single linear fit pools both directions — a known, documented bias, not corrected in v1.
- **Day-to-day reproducibility unproven.** The day-to-day stability of HRV thresholds is still contested (Cassirame et al. 2025 methodological critique + Gronwald reply), hence the beta label.

### DFA alpha1 tab

A dedicated **"DFA alpha1" tab** surfaces all of the above: an aggregate↔per-ride toggle; a per-ride table (date, duration, avg HR, alpha1, the Z1·Z2·Z3 bar, and HRVT1/HRVT2 with r²-coloured confidence); click any row to see that ride's alpha1-over-time curve. The aggregate view shows recent-median HRVT1/HRVT2 HR and power zones across rides that cleared the r² gate.

### Hooper composite — the morning leg-check

Four 5-button questions on the home page: **sleep / energy / stress / soreness**, each 1–5. [Hooper & Mackinnon 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/) (*J Sci Med Sport*) showed the *composite* (4-field sum, range 4–28) predicts overtraining better than any single component — a rider can have crushed legs but score "fine" on subjective fatigue; a sleep-deprived rider can have fresh legs. [Saw et al. 2016](https://pubmed.ncbi.nlm.nih.gov/26423706/) (*Br J Sports Med*) is the modern reinforcement: subjective wellness questionnaires correlate *better* with training response than any wearable HRV/RHR/sleep-score metric.

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

**No laptop? No trainer app needed.** Both download formats — **ZWO** and **FIT** — drive a smart trainer in ERG. Two ways to ride them:

- **On a head unit (simplest).** Copy the **ZWO/FIT straight onto your Garmin (Edge / watch), Wahoo ELEMNT, or Hammerhead Karoo**, then **pair your Tacx — or any smart trainer — to the head unit as a "sensor"** (exactly like adding an HR strap or power meter). The head unit runs the structured workout and controls the trainer's power directly over **ANT+ FE-C / Bluetooth FTMS**. No computer, no app, no subscription.
- **In a trainer app.** Load either format into MyWhoosh / Tacx / Zwift / Golden Cheetah on a laptop or phone, which pairs to your trainer over ANT+ FE-C / Bluetooth FTMS — if you want scenery.

Either format, either path; no virtual world required.

---

## The science — how the planner thinks

Every threshold, zone, and formula in Domestique is grounded in the peer-reviewed
literature: the TSS / CTL / ATL / TSB load model, the DFA α1 thresholds, the
polarized **80/20** distribution, the **ACWR** safety bound, the seven **G1–G7**
injury-prevention guardrails, the periodization engine, FTP detection, event-
capability projection, and the end-to-end ride-indexing pipeline.

**→ Full detail — every formula with its citation, the honest limits of the
TSS stack, the Norwegian-Method coverage, and the complete scientific-reference
table (every study linked to PubMed/PMC) — lives in [docs/SCIENCE.md](docs/SCIENCE.md).**

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

## Releases

Latest: **[v2.2.16 — macOS Monterey (12) / Big Sur (11) compatibility](https://github.com/platypus45/domestique/releases/latest)** (2026-06-24).

GitHub Actions ([release.yml](.github/workflows/release.yml)) builds and uploads the macOS DMG + Windows EXE on every tagged release.

**Highlights since v1.8.5** (see [CHANGELOG.md](CHANGELOG.md) for every shipped tag):

- **One-button planning, Windows fixes & first-sync progress** (v2.2.1–v2.2.3) — the plan tab is now a single **Generate Plan** button; the plan then **updates itself** automatically after every ride sync and whenever you open the tab (the old *Update plan* / *Rebuild from scratch* buttons are gone — Generate rebuilds from your current settings when you need a fresh one). A header **sync spinner** (“Updating activities X/Y”) and the **version number** now sit next to the logo, and a first-run top strip shows **“Syncing activities X of Y (NN%)”**. Windows reliability: **intervals.icu sign-in works** (the OAuth client secret is bundled into the CI build) and the **native window starts first-try** (the downloaded zip’s Mark-of-the-Web no longer blocks the embedded .NET runtime). Plus minimal **capped logging** (no more multi-GB log folders) that still records sign-in/sync events for support.
- **One-button planning, add-a-race & a clearer setup** (v2.2.1–v2.2.4) — the plan actions collapsed to a single **Generate Plan** (it then auto-updates after each ride sync and whenever you open the plan tab); **add a B/C race to an existing plan** and it reschedules around it (easy taper in, recovery after) without a rebuild, keeping your A-event date + plan length; the **Plan configuration** (style / intensity model / block periodization / per-day availability) now round-trips so the form reflects your actual plan, with a one-line active-config summary. Plus reliability: intervals.icu sign-in no longer false-alarms a reconnect (a missing-scope 403 isn't treated as a dead token), the Windows EXE signs in (bundled secret) + opens its native window first-try (Mark-of-the-Web strip), a header chip shows first-sync progress, and the Settings connection reads **“✓ Logged in as <name>.”**
- **Sign in with intervals.icu, plan styles & customizable zones** (v2.2.0) — linking your account is now **OAuth one-click sign-in** (no API keys): an explicit, retryable step that shows **“✓ Linked as <name>”**, prefills your FTP/weight/LTHR from the account, and prompts existing key-users to switch. Pick a **plan style** — *Automatic (varied)*, *Fixed-core (repeatable: one quality type/phase, reps progress, constant Z2 base)*, or a *Template* (Polarized Base / FTP Builder). **Power & HR zones are now editable** (prefilled, reset-to-auto, honored across display + analysis). First-run shows a top-bar **“Syncing activities X of Y (NN%)”** with a spinner, and an **FTP-rise** banner appears only when your FTP actually looks higher. Plus reliability: the day-detail popup reads one coherent story, easy days stay easy, recovery weeks truly deload, **B/C races work on any plan**, and logs no longer balloon.
- **Block periodization, B/C races & a plan that respects your real training** (v2.1.0) — the biggest release yet. An opt-in **block-periodization** mode concentrates each build phase on one quality (a VO2 block, then a threshold block); **B and C races** ride alongside your A goal, each with an evidence-based mini-taper (trim volume, keep intensity) and color-coded on the calendar. The plan now builds weekly volume from a **load-based ceiling** (target CTL / recent TSS, not the sum of your free hours) starting from your **real current fitness**, with genuine rest weeks, no hard intervals on race eve, and a **polarized / pyramidal / threshold** distribution of your choosing. Plus: a **library that stops hiding hard sets** (objective-coherence labels), long pure-Z2 base rides, a **trustworthy DFA α1** readout (per-window lows + confidence flag), an **outdoor-variant** export, and Windows/Mac reliability fixes — profiles & API keys that persist, clean app relaunch, and the **intervals.icu TLS fix on both platforms**. eFTP no longer silently rewrites your zones.
- **Goal-aware + event-driven planning** (v2.0.0) — the plan now adapts to what you're training for. An **FTP** focus schedules more threshold/sweet-spot work, a **VO2max** focus more VO2/30-15 work. For a **target event**, distance + elevation drive a real long-ride progression (toward ~0.8× event duration, capped by your weekend hours), a feasibility-bounded fitness target (auto-lowered if the date's too soon), and climbing specificity in build/peak — so a 100 km/500 m and a 175 km/2900 m fondo produce visibly different plans. Survives auto-sync.
- **Evidence-based library + cleaner browser** (v1.10.0) — added the canonical Rønnestad short/long intervals, proper Wingate SIT (4-min recovery) and descending VO₂ ladders from the PubMed literature, removed 18 under-rested anaerobic files (the "rest too short" ones), and renamed 282 files to match their real content type. The library filter is redesigned: one unified Type, a 0–180 min duration range, and an Advanced panel for Min Score / Surface / Tags.
- **FIT import that just works** (v1.9.1) — drag a `.fit` from Finder anywhere onto the window (or click **Import FIT**); it imports, reconciles against your plan, adapts the next sessions, and the views refresh on the spot.
- **Onboarding + "This Week" overhaul** (v1.9.0) — first-run wizard reworked (paste one API key, athlete ID auto-detected, Garmin sync verified, account guidance, skippable); reconciliation of completed rides → done/missed now happens automatically on every sync (the manual "Reconcile Week" button is gone); multi-ride days count correctly toward the week; workout selection brought in line with the planner; library grown to 4,198 clean workouts.

- **One "Update plan" action** (v1.8.24) — the fragmented Reforecast / Regenerate / availability controls collapsed into a single primary button that auto-picks the right adjustment: a structure-preserving **rebalance** to today's TSB/ACWR/availability when you're on track, or a full **rebuild with a recovery ramp** (Gabbett ACWR < 1.3, Z2 reconditioning — never a catch-up spike) when you've fallen behind. "Regenerate" was the advanced *Rebuild from scratch*; per-day *Rematch* stays. (Further simplified in v2.2.3: one **Generate Plan** button + automatic updates — see the top entry.)
- **Plan auto-adapts after missed workouts** (v1.8.24) — a ride sync that detects a significant *current* absence rebuilds automatically through the recovery ramp, once per absence episode (latched, no churn), recent-gap-gated (an old recovered gap never nags), and never inside an event taper (it flags "behind plan" instead).
- **Reshuffle honours the slot duration** (v1.8.19 ±25 % gate → v1.8.24 exact) — a 90-min slot returns a ~90-min workout, never a wildly different length.
- **Bigger, cleaner workout library** (v1.8.22 / v1.8.23 / ongoing) — grown from ~3 050 to **4,198** clean, copyright-free canonical files via a classify-before-write pipeline (every file run through the live content classifier and kept only if its type + title + duration match): polarized Rønnestad VO2 macro-blocks, comprehensive Z2/endurance structure variety (steady, two-zone, progressive, surges), and long-aerobic / duration coverage across all classes.
- **Plan integrity** (v1.8.18 / v1.8.20 / v1.8.21) — healed ghost `zwo_file` references and froze training history; regeneration preserves your edits (moved / dismissed / completed sessions) and the availability calendar; changing weekly hours repopulates the per-day calendar.
- **DFA α1 + dual thresholds** (v1.8.14) — see the DFA / HRV sections above: mandatory Malik artifact rejection, HRVT1/HRVT2 detection, intensity distribution, and a dedicated DFA tab; FIT-stream fallback when ICU 404s the `.fit`.
- **Notarized distribution** (v1.8.5+) — the macOS DMG opens with zero Gatekeeper prompts; new activities auto-push to the calendars.

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

**Credentials at rest.** Your intervals.icu OAuth bearer token (and any legacy API key) is stored in `~/.domestique/profiles/<id>/.env`, written atomically with `chmod 0600` (owner-only) and with newline-injection rejected. It is **plaintext at rest** (not encrypted), but never uploaded, never synced, and excluded by `.gitignore`. On macOS you can move it into Keychain after first run; automated Keychain migration for ICU credentials is planned (Strava already uses Keychain on macOS). Do not commit your `.env` anywhere.

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
