<p align="center">
  <img src="assets/icon.png" alt="Domestique" width="180" height="180">
</p>

<h1 align="center">Domestique</h1>

<p align="center"><b>An adaptive cycling training planner that closes the loop between what you planned and what you actually did.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-blue" alt="Python">
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-green" alt="Platform">
  <img src="https://img.shields.io/badge/Workouts-4307-orange" alt="Workouts">
  <img src="https://img.shields.io/badge/Routes-622-purple" alt="Routes">
  <img src="https://img.shields.io/badge/Version-v3.11.3-brightgreen" alt="Version">
  <img src="https://img.shields.io/badge/Tests-3240-success" alt="Tests">
  <img src="https://img.shields.io/github/downloads/platypus45/domestique/total?label=Downloads&color=blue" alt="Downloads">
</p>

<p align="center">
  📸 <b><a href="#screenshots">Screenshots — a tour of every tab</a></b>
  &nbsp;·&nbsp; <a href="#from-plan-to-ride-to-analysis">one session, from plan to ride to analysis</a>
  &nbsp;·&nbsp; <a href="https://github.com/platypus45/domestique/releases/latest">⬇ Download</a>
</p>

---

**Contents:** [TL;DR](#tldr) · [Why this exists](#why-this-exists) · [What's new](#whats-new-in-v300) · [Quick start](#quick-start) · [Planner modes](#planner-modes) · [Race-ready event plans](#race-ready-event-plans) · [Plan stability](#plan-stability) · [Train by heart rate](#train-by-heart-rate-no-power-meter) · [Analysis & Rider Profile](#analysis-tab--rider-profile) · [Execution scores](#execution-scores) · [Retest reminders](#retest-reminders) · [Core mechanics](#core-mechanics) · [Architecture](#architecture-overview) · [The science](#the-science--how-the-planner-thinks) · [Ride auto-matching](#auto-matching-your-rides-to-planned-sessions) · [Screenshots](#screenshots) · [Releases](#releases) · [Development](#development) · [Abbreviations](#abbreviations--terms) · [License](#license--attribution)

> Deep dive: the full planner logic, formulas, and the complete cited reference table now live in **[docs/SCIENCE.md](docs/SCIENCE.md)** (moved out of this README to keep it readable).

---

## TL;DR

Domestique is a localhost-only cycling planner that ships 4,200+ structured ZWO workouts and 622 virtual routes, imports your post-ride FITs, and mutates the next-day prescription from *every* signal the ride exposed — TSS overshoot, polarisation breach, DFA α1, aerobic decoupling, Foster monotony, eFTP drift, Hooper composite, glycolytic overload. Most "smart" planners stop at the dashboard. Domestique stops at the prescription. Hardware-agnostic: generate ZWO, ride in MyWhoosh / Tacx / Zwift / Hammerhead / outdoors, import the FIT back. No power meter? A heart-rate target mode prescribes bpm ranges instead of watts. Single rider, no telemetry, no cloud.

## Why this exists

Most training apps fall into one of two modes:

- **Display-only**: HRV widgets, Banister fitness curves, polarisation rings — beautiful charts, zero behavioural feedback.
- **Calendar-based**: a fixed 12-week plan that doesn't care what you actually did yesterday.

Domestique is neither. Every signal that touches the dashboard also has a code-path that mutates a future session:

- **Soreness >= 6/7** on the morning Hooper composite -> today's VO2max session is forced to recovery (Hooper & Mackinnon 1995; Cheung et al. 2003 — peripheral fatigue is independent of central HRV).
- **Last week's actual TSS > 1.5 x planned** -> next week's TSS budget auto-cuts 15% (Gabbett 2016, ACWR sweet spot 0.8–1.3).
- **Rolling 48h Z5+ >= 25 min** -> today is forced to Z2 even with positive TSB (Hulin et al. 2014).
- **DFA alpha1 mean < 0.5 over last 3 rides** -> tomorrow's threshold session auto-swaps to Z2, with a revert button (Rogers et al. 2021).
- **Yesterday's ride carried >= 8 min in Z6/Z7** (planned or not) -> today's hard session eases one notch — sprint work is glycolytically expensive at low TSS, so the load model alone would miss it. Revertible, with the reason on the card.
- **Mid-cycle FTP recalibration** at the build1->build2 boundary auto-tests your FTP so the next 4 weeks of TSS targets aren't computed against a stale baseline (Allen & Coggan, *Training and Racing with a Power Meter* 3rd ed.).

Seven science-grounded guardrails (G1–G7), each citing a specific paper, plus a 1-week consolidation phase at the end of every non-event cycle so people don't ride straight from a peak into a fresh build with elevated fatigue (Mujika 2010).

---

## What's new in v3

**Short-interval VO2max (v3.8).** Twenty-one 30/15 microinterval sessions —
thirty seconds hard, fifteen easy, repeated — from 33 to 82 minutes, plus a
switch that makes every VO2max day one of them. The switch explains what the
evidence does and does not show: this protocol is well supported but *not*
established as the best way to train VO2max, so it is off by default. See
[docs/SCIENCE.md](docs/SCIENCE.md).

**Linux is in testing.** A native-window build for 64-bit x86 exists as a
**pre-release** — genuinely new, built against Ubuntu 22.04, no other
distribution tried, and nothing can confirm by machine what the window draws.
It is not part of the v3.8 release and nobody is offered it automatically. If
you want to try it, read [Installing on Linux](#installing-on-linux) first.

**Which blocks did you actually do? (v3.7).** After an interval session the app
now tells you the blocks, not just a score: all ten done, or eight of ten and
you stopped after the eighth, or nine done with one skipped in the middle —
with the count per set. Stopping after 10 of 13 reps barely moves your TSS or
your time in zones, so this is the question those numbers cannot answer. It
reads the laps you marked on your head unit, using *where each lap sits on the
ride clock* rather than guessing from its shape — and where the laps genuinely
do not settle what happened, it says nothing rather than guess.

**Cooldowns that are actually cooldowns (v3.7).** A hard session used to end
with a ramp that stepped *up* — one rider finished a 6×2 min VO2max set at 28 %
FTP and was handed a "cooldown" beginning at 75 %. That was 1,647 of 3,672
workouts. Every cooldown now starts at or below 60 % FTP and never above the
power you were just holding; when you are already easier than that it becomes a
flat easy spin. Nothing got longer or shorter. The reasoning, sources and the
honest list of what a cooldown does *not* do are in
[docs/SCIENCE.md](docs/SCIENCE.md).

**A plan that never ends (v3.4).** New "Continuous" goal: pick FTP, VO2max
or both and ride a rolling 4-week cycle that extends itself every week —
three loading weeks, one recovery week. Each morning the app reads your
HRV (against your own baseline), form and zone deficits and suggests
today's stimulus: low-aerobic, high-aerobic or anaerobic — hard days only
when HRV is in band, per the HRV-guided-training RCTs. Strain spikes pull
the recovery week forward, announced with a reason and a one-click revert.


**Every workout is physiologically rideable — verified.** The whole library
went through a W′-balance audit (critical-power model, generous anaerobic
reserve): 90 files that demanded more than a human anaerobic tank — think
24×15s at 300% FTP with token rests — were fixed class-aware (sprint files
kept their power and gained real recoveries; interval files were re-set to
protocol-correct intensities). Rønnestad 30/15s now come in literature-true
set-scaled doses (2 sets @ 115%, 3 @ 110%, 4 @ 106% — not the 130% that
blows up mid-set), and a new engine gate means a sweet-spot day can never
be handed a file with sustained over-FTP blocks.

**A search bar that speaks cyclist.** Type "threshold 3x16", "ss 90min",
">120 vo2", "30/15", "rønnestad" or "@105" — the library search parses
structure, duration, intensity and class families, tolerates typos, ranks
by relevance, highlights the match, and tells you what it understood.
"/" focuses it, Esc clears it. Instant on 4,200+ files.

**Plans that respect your time — and stay interesting.** The training time
you enter per day is a hard limit: a 60-minute day gets a workout that fits
it, on every path the planner takes (including variety swaps and weekly
recalculations). Meanwhile ~600 library workouts that were previously
unreachable — ladder intervals through threshold, VO2 and sweet spot, tempo
intervals, endurance rides with strides — joined the rotation, every build
phase guarantees the classic four hard shapes (threshold, VO2max, sweet
spot, over-unders), and Reshuffle offers genuinely different alternatives
instead of repeating itself.

**A home page built around today — and it never lies to you.** When
readiness adjusts your day, the day view leads with the adjusted session and
gives you the choice (ride it, or keep the original — downloads follow your
pick); analysis panels load with live progress bars that reach an honest
100%, and ride indexing from intervals.icu shows its progress right on the
home card.

**A home page built around today.** The Today card shows your readiness
verdict, the morning leg-check and today's session — with a preview of its
actual interval blocks — directly above the week grid. Sleep, HRV and RHR
tiles never show a bare dash: until your watch's overnight data reaches
intervals.icu you see your last synced values in italics with their date,
while color keeps meaning good or bad. And the Analysis tab's power curve
and Fatigue Resistance load instantly, filling themselves in with a live
progress bar while ride data caches in the background.

**Shape your own plan.** The phase preview is now an editor — adjust how your
weeks split across base, build, peak and taper with steppers (within safe
limits), or keep the recommendation. And a new "place me from my rides" option
scans your recent training to propose which week you're actually on.

**A workout library you can trust.** Sessions are matched to your plan by what
they actually contain, not just their label — a sprint day gives you sprints,
a sweet-spot day stays sweet spot. Dozens of mislabelled "FTP test" workouts
are corrected, the 20-minute test gains its depletion effort, and ~30 new
sessions land (30/15, 40/20 and microburst ladders, over-unders, a real 2×8
field test).

**Test your FTP your way.** Open the FTP test in your plan and choose the
20-minute protocol (default — best-validated, full protocol with the 5-min
clearing effort), the ramp (quick and pacing-free; the card says honestly that
its 0.75 factor has no peer-reviewed validation and reads high for punchy
riders), or a full 60-minute hour of power (definitional — no factor at all).
A side-by-side chooser shows each option's power profile and trade-offs. Tests
are recognised whether the ride arrives as a FIT import or through the
intervals.icu sync, the suggested number comes with Accept / Keep / Edit and a
level-typical range for your W/kg, and a "Schedule FTP test" button puts a
test on any day — fresh legs arranged, plan re-fit around it. The evidence
review per protocol is in
[the science documentation](docs/SCIENCE.md#2a-the-ftp-test-protocols--what-the-literature-actually-supports).

**Short intervals to your measured power** (opt-in). If you have a measured
peak-power number, Domestique can cap a workout's short, very-hard reps to what
you can actually produce — grounded in critical-power research, using your real
numbers rather than a model's guess.

**Your plan survives upgrades.** Upgrading from a 2.x version could resurface
an old, long-replaced plan instead of your current one — fixed: on first
launch the app finds your real plan, restores it automatically (with a
notice), and keeps every previous file as a backup. If it already happened to
you, this update brings your original plan back on its own.

**Start a plan mid-way.** Tick "I've been training already" when creating or
rebuilding a plan and pick your real start date: the plan covers the full
runway from that date, completed weeks are marked done, and you enter exactly
where you are — no repeating a base phase you already rode.

**Push to intervals.icu.** Send your planned workouts straight
to your intervals.icu calendar (one-off button on the Training Plan tab, or an
optional keep-in-sync setting) so Garmin & MyWhoosh pull them automatically —
and any single workout too: every workout detail view (planner or library)
has a send-to-calendar button, with a date picker for library workouts;
heart-rate plans push with HR targets. Only Domestique's own entries are ever
touched — your races and anything you added yourself stay put. Reconnect once
to grant calendar access.

**Train by heart rate — no power meter needed.** A new Workout-targets mode
prescribes every session in bpm: steady aerobic work gets LTHR-anchored
heart-rate ranges, short/very-hard efforts are by feel (heart rate is too slow
to guide them), FIT files carry real HR targets to your head unit, and a
Watts ⇄ HR toggle previews either view. LTHR syncs from intervals.icu, rides
without power count toward fitness via a labelled heart-rate load estimate.

**Race-ready event plans.** Tapers genuinely taper, race eve is a short openers
ride, nothing hard lands within two days of a race, and race days are immune to
every automatic adjustment. B-races get mini-tapers; impossible dates get clear
errors.

**A new Analysis tab + Rider Profile.** Home opens to today's decision; the
diagnostics moved to Analysis, headlined by a Rider Profile card — FTP, eFTP,
W/kg, CP/W′/Pmax, peak efforts, VO2max (via Intervals.icu), heart numbers,
efficiency trend, load and season totals, each with source and freshness.

**Closing the loop.** Execution scores rate every completed session against
its prescription; retest reminders fire when FTP/LTHR go stale; a drift chip
offers a re-plan when your fitness diverges from the plan's assumptions.

**Foundations.** First-run setup works (and asks how you train), multiple
athletes on one computer are fully isolated, 66 more hand-verified workout
label corrections, and FIT workout files confirmed importing into
TrainingPeaks / Vekta / Garmin.

---

## Quick start

1. **Install** — macOS users have two paths: `brew tap platypus45/tap && brew install --cask domestique` (no Gatekeeper prompts) OR grab `Domestique-vX.Y.Z.dmg` from the [latest release](https://github.com/platypus45/domestique/releases/latest) and right-click → Open on first launch. Windows users grab `Domestique-Windows.zip`, unzip, run `Domestique.exe`. Linux users grab the `.AppImage`, `chmod +x` it and run it — see [Installing on Linux](#installing-on-linux). See [Installing on macOS](#installing-on-macos) for details.
2. **Connect Intervals.icu** — the first-run wizard walks you through it: click **Sign in to intervals.icu**, log in + approve in your browser (OAuth — no API keys to copy, athlete auto-detected), then optionally enable Garmin Connect on Intervals.icu so rides sync automatically. While your history indexes, a top-bar strip shows live first-sync progress. No intervals.icu account? It's free and you can sign in with Garmin or Strava. Whole step is skippable — without ICU, Domestique falls back to local CTL from your imported FITs. (Already linked with an API key from an older version? You'll be prompted to switch to sign-in.)
3. **Generate a plan** — pick a goal type (event prep / FTP / VO2max / hybrid / general / endurance), target date, target CTL, hours/week, and a [planner mode](#planner-modes) (auto / fixed-core / template). The planner sizes Base / Build1 / Build2 / Peak / (Taper or Consolidation) phases, draws 150 distinct ZWO files across a 24-week plan, and adapts daily to your readiness.

After your first ride: click **Import FIT** in the header (or drag the `.fit` anywhere onto the window), or let ICU sync. Domestique imports it, reconciles it against your plan, adapts the next sessions, detects FTP tests automatically — and the views refresh on the spot.

No power meter? See [Train by heart rate](#train-by-heart-rate-no-power-meter) — the whole plan works in bpm.

### Installing on macOS

Download `Domestique-vX.Y.Z.dmg` from the [latest release](https://github.com/platypus45/domestique/releases/latest). Open it, drag `Domestique.app` to `Applications`, double-click. That's it. Requires macOS 11 (Big Sur) or later — including Monterey and Big Sur themselves, where earlier builds failed to launch (fixed for good in v2.2.16).

Or via Homebrew:

```bash
brew tap platypus45/tap
brew install --cask domestique
```

(Homebrew not installed? One-liner at [https://brew.sh](https://brew.sh).)

### Installing on Windows (SmartScreen)

The Windows EXE is also unsigned. On first run, SmartScreen shows a blue "Windows protected your PC" dialog. Click **More info -> Run anyway**.

### Installing on Linux

Grab `Domestique-vX.Y.Z-x86_64.AppImage` from the [latest release](https://github.com/platypus45/domestique/releases/latest), make it executable, run it:

```bash
chmod +x Domestique-v3.9.1-x86_64.AppImage
./Domestique-v3.9.1-x86_64.AppImage
```

There is nothing to install and nothing to uninstall — the file *is* the app. Your data lives in `~/.domestique/` exactly as on macOS and Windows, so deleting the file leaves your plan and rides intact.

The app opens its own window and serves on `http://127.0.0.1:22400`. If something else already holds that port it moves to the next free one on its own and remembers the choice; set `DOMESTIQUE_PORT` to pick one yourself.

- **64-bit x86 only.** No ARM build (a Raspberry Pi or an ARM laptop will not run it).
- **glibc 2.35 or newer** — Ubuntu 22.04+, Debian 12+, Fedora 36+ and their derivatives. Older distributions are out of reach for this build.
- **~260 MB.** The window carries its own browser engine instead of borrowing the distribution's, which is what makes one file behave the same on every desktop. That is the whole cost, and it is not hidden.
- **Text too small or too large?** The interface is sized to match macOS and Windows. Your desktop's own scaling is not always readable from inside an AppImage, so if it looks wrong, `QT_SCALE_FACTOR=1.5 ./Domestique-v3.9.1-x86_64.AppImage` (or `=1` for the smallest) sets it explicitly.
- **If the window cannot open, the app says so and exits** — a dialog if your desktop can show one, the reason on stderr, and the full detail written to `~/.domestique/startup_crash.txt`. It will never quietly fall back to a browser tab or sit there holding the port with nothing on screen.

**Host packages.** Almost everything travels inside the file. The graphics and X11 client libraries deliberately do not: a bundled copy of those is the classic way an AppImage dies on a machine whose drivers differ from the build host's. A normal desktop install already has them; a minimal or headless system may not.

The exact package names differ by distribution. Every Linux build writes the
list it actually resolved to `dist/host-deps-debian.txt` (Debian/Ubuntu names)
and `dist/host-deps.txt` (portable sonames); the release notes for each version
carry the current list. If the app starts but no window appears, install your
distribution's Qt xcb platform packages — that is nearly always the cause.

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

## Planner modes


Three ways to have your plan built — pick a **plan style** on the plan form:

- **Auto (varied)** — the default. Every session is a different workout drawn from the 4,232-file library; the planner picks whatever best fits each day's training type, length, and phase. Maximum variety.
- **Fixed-core (repeatable)** — one key quality session type per phase that repeats week to week with progressing reps, over a constant Z2 base. For riders who like riding the same benchmark session and watching it get harder.
- **Template** — a fixed, published structure (Polarized Base or FTP Builder) when you want a known recipe rather than a generated one.

Independent of the mode, two more dials shape what gets planned:

- **Intensity mix.** Choose a polarized (default), pyramidal, or threshold distribution — or pick **Custom** on the template picker and set your own split of hard training across Tempo/Sweet-Spot, Threshold, VO2max, and Sprint (with a live check that it totals 100%). The built-in templates show their typical mix when you pick them (e.g. Polarized Base ≈ 80% easy with a VO2max lean; FTP Builder ≈ 72% easy with a sweet-spot lean), and after a plan is generated the readout switches to the **actual** training-type breakdown it produced — so what you ask for is what you get.
- **Block periodization (opt-in, default OFF).** A plan-form toggle reorganizes build/peak into ~3–4 week single-focus blocks (a VO2max block, then a threshold block toward the event) instead of mixing every hard type weekly, keeping one complementary session per block. Grounded in a verified PubMed screen ([Rønnestad 2014](https://pubmed.ncbi.nlm.nih.gov/22646668/) / [2020](https://pubmed.ncbi.nlm.nih.gov/31977120/) block-VO2 cycling RCTs, [Issurin 2008](https://pubmed.ncbi.nlm.nih.gov/18212712/), [Mølmen 2019 review](https://pubmed.ncbi.nlm.nih.gov/31802956/)) — but the edge is mixed for amateurs ([Almquist 2022](https://pubmed.ncbi.nlm.nih.gov/35299664/) found none), which is why it's **off by default**. Survives auto-recalc.

Whatever the mode, weekly volume is **load-based** — sized from your target fitness and recent training load, not the raw sum of your free hours — and the plan starts from your real current CTL, with genuine rest weeks and per-day availability honored.

---

## Race-ready event plans

Event-goal plans treat your race like a race:

- **Tapers genuinely taper.** Volume steps down week over week into the event, and race week is the lightest week of the plan — not just a slightly lower load number.
- **Openers the day before.** The day before your race is a short openers session — a few brief efforts to wake the legs up, not a workout.
- **Nothing hard within two days of an event.** No VO2max surprise on race eve.
- **Race days are untouchable.** No automatic adjustment — readiness tier-downs, re-fits, reshuffles — can overwrite a race day.
- **Races show on the calendar** as what they are: a 🏁 race on the day, in This Week when it's race week, and on the Today card — never as a stray training session.
- **B and C races get right-sized mini-tapers.** Add intermediate events alongside your A goal — even to an already-running plan, which reschedules around them without a rebuild. A B race gets a short volume trim that *keeps* intensity, with openers the day before; a C race gets a single easy/opener day. Mini-tapers are skipped inside the A-race taper or an unload week, and races are color-coded by priority on the calendar. Grounded in a verified taper screen ([Mujika & Padilla 2003](https://pubmed.ncbi.nlm.nih.gov/12840640/), [Bosquet 2007](https://pubmed.ncbi.nlm.nih.gov/17762369/), [Rønnestad 2017](https://pubmed.ncbi.nlm.nih.gov/27476525/)).
- **Honest edge cases.** A race date in the past gets a clean error, not a broken plan. A race *tomorrow* gets an honest race-week micro-plan (openers today, race tomorrow) — not a 12-week build compressed into a day.

---

## Plan stability

An adaptive planner is only useful if you can trust next week to still look like the plan you agreed to. The contract:

- **The skeleton stays stable.** Phases, weekly structure, and hard-day placement don't churn every time a metric ticks.
- **Intensities adapt daily.** Readiness, fatigue signals, and the G1–G7 guardrails tune what today asks of you — and a readiness tier-down never *raises* load; a genuinely bad morning drops the day to easy in one step, not one tier at a time.
- **Missed sessions reschedule themselves.** Miss a workout and the plan moves or re-fits it automatically within the week — no prompt, nothing to click. When a *hard* session is missed, the remaining week is re-fit so the training intent survives.
- **Recovery weeks actually feel like recovery** — more rest days and fewer hours than the surrounding build weeks, not just a lower TSS target.
- **A drift chip, not a silent rewrite.** When your measured fitness diverges more than 15% from what the plan assumed, a chip appears offering a re-plan — your call. Full re-plans happen only on explicit triggers (you asked for one, a race changed, a genuine multi-week absence) — never as a side effect of a sync.

---

## Train by heart rate (no power meter)


HR zones follow your choice of model: LTHR-anchored (default), or heart-rate reserve (Karvonen) built from your synced resting HR and measured max HR — with the resting anchor as a rolling median or a fixed value you set. The [science notes](docs/SCIENCE.md#1c-heart-rate-zones-lthr-vs-heart-rate-reserve) explain when each model is the right call.

No power meter? Domestique can prescribe every workout in beats per minute instead of watts.

- **Switch the target mode in Settings** (Power ↔ Heart rate). The heart-rate option unlocks once your LTHR (lactate-threshold heart rate) is known — Domestique keeps it synced automatically from your intervals.icu ride data, unless you've set it manually, in which case your number stays put.
- **Steady aerobic work gets real bpm ranges**, derived from the standard Coggan %FTP ↔ %LTHR zone table — so an endurance ride reads as an actual heart-rate range, not a percentage of a number you don't have.
- **Short and very hard efforts are prescribed by feel (RPE), on purpose.** Heart rate lags effort by around 30 seconds and tops out near your max — a short sprint is over before your HR arrives, and all-out efforts stop being distinguishable in bpm. So anything shorter than ~2.5 minutes or harder than ~120% of FTP gets an RPE cue instead of a misleading bpm target.
- **Long steady efforts warn about drift.** On a long steady effort your heart rate creeps up 10–20 bpm at the same effort. The workout says so and tells you to start at the low end of the range.
- **Recovery and cooldown steps are ceiling-only** — "stay under X bpm". A floor makes no sense on a recovery spin.
- **FIT files carry real HR targets.** Load the FIT onto a Garmin, Wahoo, or Hammerhead and the steps show as native heart-rate targets; the by-feel efforts ride along as open steps with the RPE cue in the step name.
- **ZWO stays power-only** — the format has no HR targets — so the ZWO download is greyed out in HR mode.
- **Preview both.** The workout modal has a Watts / HR toggle that previews the session either way, and the FIT you download follows whichever view you're on.
- **Tune the numbers.** A per-athlete HR-targets editor lets you adjust the zone rows if the defaults don't fit your physiology.
- **Load still gets tracked.** Rides with heart rate but no power get an hrTSS load estimate (a TRIMP-flavored approximation, labeled as such) so CTL / ATL / TSB keep working without a power meter.

How the bpm numbers are derived — and why the short stuff is deliberately by feel — is documented in the [heart-rate methodology](#heart-rate-methodology-how-the-bpm-targets-are-derived).

---

## Analysis tab & Rider Profile

The home page is for **today's decision**, nothing else: a single Today card (one readiness number, one state — Ready / Ease off / Rest — one action; when your morning leg-check overrides a high physiological score, the card says so instead of contradicting itself), a compact fitness sparkline, and This Week with an obvious TODAY badge, ✓ done / ✕ missed markers, and click-through to how each day went. Clicking today's session opens the full workout — power profile and downloads — the same card as the plan.

Everything diagnostic moved to a dedicated **Analysis** tab:

- **Fitness & Form** — your CTL / ATL / TSB history in full.
- **Energy systems** — where your training time is actually going.
- **Tau fit & model accuracy** — how well the load model's time constants fit *you*, and how well its predictions track your data.
- **Power curve** and **fatigue resistance**.
- **The Rider Profile card** — FTP / eFTP / W-per-kg, CP / W′ / Pmax when calibrated, 90-day peak efforts, VO2max (via Intervals.icu), LTHR / Max HR / Resting HR, a DFA-α1 threshold estimate, efficiency-factor trend, decoupling, CTL / ATL / TSB, weight, and season + rolling-year totals.

Every number shows **where it came from and when** — measured, estimated, or synced, with its date — so you never mistake an estimate for a test result. Power and HR zones are editable in Settings (prefilled from your data, reset-to-auto, honored across display and analysis alike).

---

## Execution scores

Every completed session gets an execution score from 0–100 against its prescription: did you ride the planned duration, deliver the planned load, at the planned intensity? Scored from power when you have it, from heart rate when you don't — so HR-mode riders get the same feedback loop. The score lives on the day, so a week of "done" ticks also tells you *how* done they were.

**Block-by-block, when the laps can prove it.** If you marked laps on your head
unit, an interval session also gets the blocks themselves: `✓ all blocks done`,
or `⚠ stopped early — stopped after block 8`, or `⚠ blocks missing` with the
per-set counts. It reads the offset on the ride clock where each lap started,
so a longer warm-up, recoveries running minutes over, a mid-ride stop, a
double-tapped lap button or blocks ridden longer or harder than asked all read
correctly. Where the laps genuinely do not settle it — a forgotten lap press,
a plan whose blocks blend into the riding around them — no block report
appears at all. A missing report is recoverable; a false green tick is not.

---

## Retest reminders

Zones and load math are only as good as the thresholds underneath them. When your FTP or LTHR test goes stale, Domestique nudges you — and the nudge is actionable: one click schedules an FTP test into the plan (power mode), or walks you through the LTHR field-test protocol (heart-rate mode). No silent drift between the rider you were tested as and the rider you are.

---

## Core mechanics

Every threshold cited below is inline in the code. The deep dive — full math and citations — is in [docs/SCIENCE.md](docs/SCIENCE.md); this section is the one-paragraph-each summary.

### TSS / CTL / ATL / TSB — the training-load model

Canonical Banister 1975 impulse-response + Coggan/Allen refinement. **TSS** = `(duration_s x NP x IF) / (FTP x 3600) x 100` — 1h at FTP = 100 TSS by definition. **CTL** ("fitness") is a 42-day EWMA of daily TSS; **ATL** ("fatigue") is the 7-day EWMA; **TSB** ("form") = CTL − ATL. Time constants 42/7 are the conventional defaults — Domestique acknowledges they aren't validated per-athlete, and the [Analysis tab](#analysis-tab--rider-profile) fits per-athlete time constants and shows the model's accuracy against your own history ([Hellard et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28651061/), Kontro 2026). When ICU is unreachable Domestique recomputes CTL from your local FIT archive with the same EWMA.

The same caveat applies to the readiness score's weights (HRV 30% / form 20% / how-you-feel 20% / sleep 15% / resting HR 15%): they are a stated assumption, not a validated per-rider truth — no published trial fixes those numbers. They are written down rather than buried so you can judge them. See [docs/SCIENCE.md](docs/SCIENCE.md) for what each channel is and is not evidence for.

### HRV — resting (morning) vs DFA alpha1 (in-ride)

Two separate signals, same hardware (chest strap recording RR-intervals).

**Resting HRV (rMSSD overnight)** lands automatically via the ICU wellness sync if Garmin Connect is linked to ICU — `wellness.hrv` is the input for the readiness composite (HRV 30% / form 20% / how-you-feel 20% / sleep 15% / resting HR 15%; missing inputs drop out and the rest are re-weighted, and a score needs at least three of them).

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
- **Single-ride noise.** Per-ride detection r² is moderate (0.36–0.64). The app shows each ride's r² colour-coded and only folds rides with r² >= 0.50 into the aggregate. Each reading also carries a high/medium/low **confidence flag** (artifact rate + window yield + sport); per-window alpha1 is allowed down to 0.20 so hard-interval drops are shown instead of discarded, and running-sourced readings are flagged low-confidence (available for runners, just labelled).
- **The aggregate is quality-weighted, not a flat average.** alpha1 is a fatigue/durability marker — a fatigued or drifting ride suppresses alpha1 and biases its threshold *low* ([Rogers & Gronwald 2022](https://pubmed.ncbi.nlm.nih.gov/35615679/), [Rogers 2025 durability](https://pubmed.ncbi.nlm.nih.gov/39904800/)). So the 42-day aggregate **excludes high-decoupling rides (> 10%)** and takes a **confidence (r²)-weighted median** of the rest — clean, well-fit rides count more; drifting rides don't drag the threshold down. The panel shows how many rides were excluded and why. Pooling every ride equally would not be valid.
- **Hysteresis.** Alpha1 lags intensity differently on up- vs down-ramps; a single linear fit pools both directions — a known, documented bias, not corrected in v1.
- **Day-to-day reproducibility unproven.** The day-to-day stability of HRV thresholds is still contested (Cassirame et al. 2025 methodological critique + Gronwald reply), hence the beta label.

### DFA alpha1 tab

A dedicated **"DFA alpha1" tab** surfaces all of the above: an aggregate↔per-ride toggle; a per-ride table (date, duration, avg HR, alpha1, the Z1·Z2·Z3 bar, and HRVT1/HRVT2 with r²-coloured confidence); click any row to see that ride's alpha1-over-time curve. The aggregate view shows recent-median HRVT1/HRVT2 HR and power zones across rides that cleared the r² gate. When it recomputes, the tab shows live update progress ("Updating X of Y rides") and refreshes itself when done.

### Hooper composite — the morning leg-check

Four 5-button questions on the home page: **sleep / energy / stress / soreness**, each 1–5. [Hooper & Mackinnon 1995](https://pubmed.ncbi.nlm.nih.gov/7898325/) (*J Sci Med Sport*) showed the *composite* (4-field sum, range 4–28) predicts overtraining better than any single component — a rider can have crushed legs but score "fine" on subjective fatigue; a sleep-deprived rider can have fresh legs. [Saw et al. 2016](https://pubmed.ncbi.nlm.nih.gov/26423706/) (*Br J Sports Med*) is the modern reinforcement: subjective wellness questionnaires correlate *better* with training response than any wearable HRV/RHR/sleep-score metric.

**Plus one question that isn't Hooper's: "ready to train?" (1–10, optional).** Pre-session fatigue *together with* readiness-to-train is the pair that actually separated the overreached cyclists from the coping ones in a 30-rider training study — and readiness-to-train is not one of Hooper's four items, so it was missing. It's optional, never pre-filled, and it moves the score in both directions: a high rating can lift the day, where the four Hooper items can only ever be dragged down by their worst answer.

Wires into the planner at three points: 20% weight in `readiness_score`; **G5** hard gate (`soreness >= 6` forces recovery, bypassing the composite — peripheral fatigue is real even when central HRV looks fine, Cheung 2003); **G6** hard gate (`sleep + fatigue + stress + soreness >= 18` forces Z2). Form pre-defaults each field to "3 — Normal" so a user who only taps soreness still posts a sane composite (~6s tap time).

### Session RPE — how hard did that actually feel

Hooper asks how you feel *before* you ride. This asks how the ride turned out, which is not the same question: a session can feel fine on the start line and empty you anyway.

After any ride you can rate it 0–10 on Foster's category-ratio scale, with the wording attached to each number ("somewhat hard", "very hard", "maximal") rather than bare digits — the anchors are what make your 7 today mean the same as your 7 last month. It's optional, it takes one tap, and a rating you've given is never overwritten by a re-sync.

What it does: your three-day mean feeds the **G7** guardrail, which eases a hard day when that mean hits 7 or above. Nothing else. That restraint is deliberate:

- A single rating is noisy. Repeat measurements of the same session vary by around 28% ([Wallace et al. 2014](https://pubmed.ncbi.nlm.nih.gov/24662229/)), so the gate works on a three-day mean at a published threshold rather than on today's number. Worth knowing: if that window holds only one rated ride, the mean *is* that rating — the averaging only protects you once you rate regularly.
- The effect is one-directional. A high rating can talk the planner out of intensity; nothing you enter can talk it into more. That asymmetry is on purpose.
- No trial has shown that steering an endurance plan by RPE improves any outcome. So Domestique captures it, shows it, and lets one published gate use it — it does not invent new automatic plan changes around it.

The honest counterpart: perceived effort is where the objective signals are blind. A drifting heart rate needs a power meter and 40 minutes; RPE needs neither, and it decouples from power and heart rate far more sharply in overreached riders than any objective measure does on its own ([Sanders et al. 2018](https://pubmed.ncbi.nlm.nih.gov/29016241/)).

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
| **G7** | 3-day mean session-RPE drops HIT | `mean(session-RPE, last 3d) >= 7` AND today is HIT | drop today one tier | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) session-RPE |

Each fired gate sets `s.adapted = True` and writes its citation into the session description so the rider sees *why* the prescription changed.

### Closed feedback loops on top of G1–G7

Five additional signals that mutate the *next-day or next-week* plan rather than just today's session:

| Signal | Threshold | Action | Citation |
|---|---|---|---|
| **DFA alpha1** | mean over last 3 rides < 0.5 | tomorrow's threshold -> Z2 (revert button) | [Rogers et al. 2021](https://pubmed.ncbi.nlm.nih.gov/33519504/) |
| **DFA HRVT1/HRVT2** (beta) | alpha1 crosses 0.75 / 0.50 on a ramp ride | display-only LT1/LT2 HR+power anchors + 3-zone model (never overwrites FTP) | [Rogers 2021 — LT1](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) + [LT2](https://pubmed.ncbi.nlm.nih.gov/33925974/), [Schaffarczyk et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/) |
| **Aerobic decoupling** (HR drift vs power) | > 5% on a ride in the last 10 days | "Z2 recommended" advisory banner, carrying how old the reading is and whether your own wellness ratings explain it | [Coyle & González-Alonso 2001](https://pubmed.ncbi.nlm.nih.gov/11337829/) |
| **Foster monotony** (weekly load SD/mean) | > 2.0 over 14 days | next week `tss_target x 0.85`, hit_per_week − 1 | [Foster 1998](https://pubmed.ncbi.nlm.nih.gov/9662690/) |
| **eFTP drift** (Intervals.icu) | > 3% above set FTP for 7+ consecutive days | FTP auto-applied with 48h revert toast | Allen & Coggan eFTP definition |
| **Local CTL fallback** | ICU unreachable | 42-day EWMA over imported FIT rides | Coggan/Allen tau=42 |

Two guard-rails on the eFTP loop: an **FTP-rise banner** appears only when your FTP genuinely looks higher, and eFTP never silently rewrites your configured zones — applying it to zones is opt-in.

### TSB-driven daily caps

Separate from G1–G7, the TSB scalar itself has dashboard hooks: TSB < −10 surfaces a "Recover" badge; TSB < −25 -> `reforecast()` drops the next hard session one tier (`vo2max -> threshold -> tempo`); TSB < −30 -> `daily_adapt_plan()` rescales remaining-week TSS to 0.6x as a forced de-load (Coggan/Allen overload threshold).

---

## Architecture overview

Pure Python, flat module layout — every `.py` at repo root is `import`ed by another root module. PyInstaller bundles them as-is (no `domestique/` package wrapper) so the spec, the DMG, and the EXE all stay simple.

**Planner** (`training_planner.py` + `training.py`) sizes Base/Build1/Build2/Peak/(Taper|Consolidation) phases from CTL + target date, picks workouts from the 4,232-file library with a (mix_preference x variety_score x novelty_boost) sampler that forces ~1 pick per file across a plan — every pick must match the slot's training type *and* its duration, so a sprint day gets a real sprint session at the planned length — enforces minimum-floor counts of Ronnestad / anaerobic / neuromuscular sessions per phase, and runs the G1–G7 priority chain on every daily adapt. `regenerate_from_today()` rebuilds the plan when `detect_plan_gaps()` flags >=2 consecutive missed weeks; `reforecast()` runs the TSB / ACWR / polarisation adjustments on demand; `auto_apply_eftp()` fires when ICU eFTP > set FTP by >=3% for 7+ days.

**Library** ships 4,232 structured ZWO workouts (content-classified into 17 canonical classes — endurance / tempo / sweet spot / threshold / over-under / VO2max / VO2-short / anaerobic / neuromuscular / FTP test, plus ladder variants; tags-indexed for filter queries) and 622 real-world route courses (Alps, Dolomites, Pyrenees, Basque country, Flanders, Costa Blanca, Mallorca, Innsbruck 2018 Worlds, Alpe d'Huez, Mont Ventoux, Stelvio + 160 regional climbs; CRS or GPX export). No Zwift virtual worlds (Watopia / Yorkshire / etc. are Zwift-proprietary and not redistributable). A 24-week plan picks 150 distinct files (every session is a different workout).

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
├── tests/                    — pytest suite (~240 files; run pytest -q)
├── docs/                     — Architecture, science deep-dives, build guides
├── scripts/                  — One-off workout/route generators (NOT imported)
├── workouts/                 — 4,232 ZWO interval workouts
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
| **Golden Cheetah** | open-source | yes (ZWO/ERG/MRC) | Best match for a planner+library+viewer app like this; drive the trainer via ANT+ FE-C; drop Domestique's library into GC's workout folder once, all 4,232 files appear in Train view |
| **MyWhoosh** | fully free | yes (via web builder) | Scenery + Zwift-style ride; full ERG on Neo 2T |
| Tacx Training | free with Tacx HW | no (ZWO); GPX only | Native Tacx integration but no ZWO import |

See [docs/cycling_apps.md](docs/cycling_apps.md) for the full table.

**No laptop? No trainer app needed.** Both download formats — **ZWO** and **FIT** — drive a smart trainer in ERG. Two ways to ride them:

- **On a head unit (simplest).** Copy the **ZWO/FIT straight onto your Garmin (Edge / watch), Wahoo ELEMNT, or Hammerhead Karoo**, then **pair your Tacx — or any smart trainer — to the head unit as a "sensor"** (exactly like adding an HR strap or power meter). The head unit runs the structured workout and controls the trainer's power directly over **ANT+ FE-C / Bluetooth FTMS**. No computer, no app, no subscription.
- **In a trainer app.** Load either format into MyWhoosh / Tacx / Zwift / Golden Cheetah on a laptop or phone, which pairs to your trainer over ANT+ FE-C / Bluetooth FTMS — if you want scenery.

Either format, either path; no virtual world required. Same workout, two formats — pick whichever your app reads (many devices read either; a Hammerhead takes both): **ZWO** drives indoor trainer apps, **FIT** is for bike computers and structured-workout platforms. FIT downloads import cleanly into **TrainingPeaks, Vekta, and Garmin** (every step is named, matching Garmin's own workout-file format) and target **%FTP rather than fixed watts**, so they scale to the FTP set on the device. In heart-rate mode the FIT carries bpm targets instead — see [Train by heart rate](#train-by-heart-rate-no-power-meter).

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

### Heart-rate methodology: how the bpm targets are derived

When [heart-rate target mode](#train-by-heart-rate-no-power-meter) is on, the prescriptions come from the same power-anchored plan, translated conservatively:

- **The %FTP ↔ %LTHR mapping** for steady aerobic work uses Coggan's classic power-training-levels table — the same table behind the app's power zones ([trainingpeaks.com/blog/power-training-levels/](https://www.trainingpeaks.com/blog/power-training-levels/)).
- **Why short efforts get no bpm target:** heart rate responds to a change in workload with a lag on the order of ~30 seconds (Silva et al. 2019, *Royal Society Open Science*, [DOI 10.1098/rsos.190639](https://doi.org/10.1098/rsos.190639)) — a short sprint is over before your HR gets there. And near-maximal efforts all compress toward max HR, so above ~120% FTP a bpm number stops discriminating. Both cases are prescribed by feel (RPE) instead.
- **Why long steady efforts say "start at the low end":** cardiovascular drift raises heart rate 10–20 bpm at a constant work rate as a long ride goes on (Coyle & González-Alonso 2001, *Exerc Sport Sci Rev* 29(2):88–92, [PMID 11337829](https://pubmed.ncbi.nlm.nih.gov/11337829/)) — the same paper behind the decoupling loop above.
- **Aerobic decoupling (Pw:Hr)** follows the TrainingPeaks guideline: under 5% counts as good aerobic durability.
- **Max HR**, where an estimate is needed, uses Tanaka 2001 (already cited in-app).

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
   - **No same-day activity AND past date** -> `status: missed` (the plan then [re-fits itself](#plan-stability) — no prompt)

5. The planner `reforecast()` and the daily-adapt path read `completion_matches` to know whether the prescription was actually delivered. If it wasn't (status missed) the next week's TSS gets restored from rolling deficit.

**Multi-activity days all count.** A commute plus your main session both appear on the day with a combined-load total, and the day's TSS adds up — so on-track tracking and plan adaptation see your real load. Clicking any day (in This Week or the full calendar) lists the scheduled workout *and* every activity you did; an activity ridden before the scheduled workout no longer hides it.

**Strava-sourced rides:** intervals.icu's API can't read activity detail from Strava (Strava's terms), so those rides show limited detail. The ride view explains this and points you to a **Garmin → intervals.icu** connection instead (full detail, no restriction), with a link to export your Garmin history.

### Manual override

Three buttons in every workout-detail modal:
- **Rematch workout** — re-rolls a different workout of the *same* type with current tolerances.
- **Swap type** — change the day to a *different* training type + duration — a VO2max day mid-Base if you feel like it. The day is pinned (it won't get auto-reverted) and the rest of the plan re-balances around it.
- **Dismiss this session** — marks `status: dismissed` (stays visible greyed out, doesn't count toward missed).

The week-level Plan settings panel has a "Rematch all this week" action that runs `rematch_week(week, activities, today)` and shows a preview before applying.

### Cross-sport load

If you ICU-sync running, lifting, or anything else, those activities count toward `cross_sport_load` and feed into `compute_today_metrics()` so the cycling plan respects the full training stress, not just bike work. Non-cycling activities never occupy plan days — they count toward load, they don't become sessions.

---

## Screenshots

A walk through the app in the order you would actually use it: wake up, see what today asks of you, follow the plan, pick something to ride, and check what the training is doing to you.

### Start the day

**Morning readiness.** The day starts with one number, built from HRV, form, sleep and resting HR, plus a four-question leg-check that takes ten seconds. The plan reads this — a rough night can soften today's session.

<p align="center"><img src="docs/screenshots/tour-01-home-readiness.png" width="850" alt="Home: readiness score, morning check-in, fitness and form"></p>

**What's behind the number.** Every factor is shown, never a black box: HRV and resting HR against your own 14-day baselines, eFTP progress, and the last rides as they synced in.

<p align="center"><img src="docs/screenshots/tour-04-recovery-eftp.png" width="850" alt="Readiness factors, sleep and HRV trends, eFTP progress, recent activities"></p>

### Follow the plan

**Today, in context.** The session the plan chose, why it chose it, and the week around it — including what's already done, what was missed, and how the week's TSS is tracking.

<p align="center"><img src="docs/screenshots/tour-02-today-week.png" width="850" alt="Today's session and the week at a glance, with adherence and intensity split"></p>

**Is the week on course?** Planned versus actual, zone by zone, paced to the day of the week — not judged against a full week you haven't had time to ride yet. Last week's verdict sits below it.

<p align="center"><img src="docs/screenshots/tour-03-week-pacing.png" width="850" alt="Week pacing against plan, and how last week's load compared"></p>

### The plan itself

**Two ways to train.** Toward a goal — classic base, build, peak into an event date — or continuously: no end date, a rolling window that extends itself weekly and re-reads your rides and readiness every day.

<p align="center"><img src="docs/screenshots/tour-10-plan-config.png" width="850" alt="Plan configuration: goal or continuous, focus, intensity model"></p>

**Your real week, not an ideal one.** Minutes per day, zero meaning rest, plus a calendar for the exceptions — holidays, travel, the odd free Saturday. The plan fits itself to what you actually have.

<p align="center"><img src="docs/screenshots/tour-11-availability.png" width="850" alt="Weekly availability grid and the availability calendar"></p>

**The calendar closes the loop.** Planned sessions, the rides that actually happened, and the misses — marked, not hidden. Missed hard sessions reschedule themselves; the week's totals update as you ride.

<p align="center"><img src="docs/screenshots/tour-12-plan-calendar.png" width="850" alt="The plan calendar: planned sessions, completed rides, misses"></p>

### From plan to ride to analysis

One session, end to end — the prescription, the ride that came back, and what the app read out of it.

**1 · The prescription.** What the plan asked for: the session's power profile, zone bands, duration and TSS, matched to a concrete workout file from the library.

<p align="center"><img src="docs/screenshots/plan-01-planned-session.png" width="850" alt="A planned sweet-spot session with its power profile and zone bands"></p>

**2 · The ride that came back.** Synced automatically after the ride: distance, IF, NP, heart rate, the power/HR trace — and a one-tap "how hard did that feel?" that the planner also listens to.

<p align="center"><img src="docs/screenshots/plan-02-completed-ride.png" width="850" alt="The completed ride: distance, IF, NP, heart rate, and a post-ride feel rating"></p>

**3 · What it counted for.** Time in every power and HR zone and the polarization verdict — this is what the plan uses to decide whether tomorrow stands or adapts.

<p align="center"><img src="docs/screenshots/plan-03-ride-zones.png" width="850" alt="Post-ride analysis: polarization verdict, power-zone and HR-zone time"></p>

### Pick something to ride

**4,306 workouts, named for what they are.** Class, structure, intensity and duration in every name — `threshold 4×8min @ 98%` reads the same in the library, on your head unit, and in the plan.

<p align="center"><img src="docs/screenshots/tour-07-workout-library.png" width="850" alt="The workout library: 4,306 files, named for their structure"></p>

**Or just say how you feel.** Two sliders — how you feel and how long you have — and it picks the session. The same page picks routes by distance, climb, surface and finish type.

<p align="center"><img src="docs/screenshots/tour-05-workout-picker.png" width="850" alt="Workout picker: how do you feel, how long do you have"></p>

<p align="center"><img src="docs/screenshots/tour-06-route-picker.png" width="850" alt="Route picker: distance, climb, surface and finish-type filters"></p>

**622 routes and climbs.** Curated collections — rolling classics, alpine passes, flat TT loops — plus famous real-world climbs, each with its elevation profile and category.

<p align="center"><img src="docs/screenshots/tour-08-routes-climbs.png" width="850" alt="Routes and climbs: curated collections plus the full catalogue"></p>

<p align="center"><img src="docs/screenshots/tour-09-route-cards.png" width="850" alt="Route cards with elevation profiles and categories"></p>

### See what the training is doing

**The rider profile.** FTP, W', peak powers, heart markers, efficiency and load in one place — each value stamped with where it came from and when, so you know what's measured and what's modelled.

<p align="center"><img src="docs/screenshots/tour-13-analysis.png" width="850" alt="Analysis: rider profile and the fitness/fatigue/form chart"></p>

**Power curve and fatigue resistance.** Your curve against a published baseline, and how much of your fresh power survives after 1500 kJ of work — the number that decides how a long ride ends.

<p align="center"><img src="docs/screenshots/tour-14-power-curve.png" width="850" alt="Power curve against a reference baseline, plus fatigue resistance"></p>

**Thresholds from heart-rate variability.** DFA α1 estimates your aerobic and anaerobic thresholds from beat-to-beat data — an independent check on your power zones, with the confidence of each estimate shown.

<p align="center"><img src="docs/screenshots/tour-15-dfa-thresholds.png" width="850" alt="DFA a1: estimated aerobic and anaerobic thresholds from HRV"></p>

<p align="center"><img src="docs/screenshots/tour-16-dfa-per-ride.png" width="850" alt="DFA a1 per ride, with intensity distribution and threshold detections"></p>

### Make it yours

**Settings.** Weight, FTP with its full history, LTHR, and editable power and HR zones — prefilled from your data, overridable everywhere.

<p align="center"><img src="docs/screenshots/tour-17-settings.png" width="850" alt="Settings: athlete profile, FTP history, power and HR zones"></p>

## Releases

Latest: **[v3.11.3 — Sign-in that says what went wrong](https://github.com/platypus45/domestique/releases/latest)** (2026-09-02).

GitHub Actions ([release.yml](.github/workflows/release.yml)) builds and uploads the macOS DMG + Windows EXE + Linux AppImage on every tagged release. The three jobs are independent: a Linux failure cannot hold up or damage the macOS and Windows artifacts.

**Highlights since v1.8.5** (see [CHANGELOG.md](CHANGELOG.md) for every shipped tag):

- **Custom mix, self-rescheduling & device compatibility** (v2.3.0–v2.4.5) — the Custom intensity-mix option on the template picker (with the *actual* generated mix shown after planning), the **Swap type** button for any day, missed sessions that reschedule themselves, proper scaled warm-ups/cool-downs inside every workout (the day view shows the warm-up / main-set / cool-down split), multi-activity days that all show and count, FIT workout downloads that import into TrainingPeaks / Vekta / Garmin (named steps, %FTP targets), a sharper drift-aware DFA threshold estimate, a library re-classification so hard sessions can't hide as easy ones, and clearer workout charts (anaerobic in purple, high-contrast Z7).
- **One-button planning, Windows fixes & first-sync progress** (v2.2.1–v2.2.3) — the plan tab is now a single **Generate Plan** button; the plan then **updates itself** automatically after every ride sync and whenever you open the tab (the old *Update plan* / *Rebuild from scratch* buttons are gone — Generate rebuilds from your current settings when you need a fresh one). A header **sync spinner** (“Updating activities X/Y”) and the **version number** now sit next to the logo, and a first-run top strip shows **“Syncing activities X of Y (NN%)”**. Windows reliability: **intervals.icu sign-in works** (the OAuth client secret is bundled into the CI build) and the **native window starts first-try** (the downloaded zip’s Mark-of-the-Web no longer blocks the embedded .NET runtime). Plus minimal **capped logging** (no more multi-GB log folders) that still records sign-in/sync events for support.
- **One-button planning, add-a-race & a clearer setup** (v2.2.1–v2.2.4) — the plan actions collapsed to a single **Generate Plan** (it then auto-updates after each ride sync and whenever you open the plan tab); **add a B/C race to an existing plan** and it reschedules around it (easy taper in, recovery after) without a rebuild, keeping your A-event date + plan length; the **Plan configuration** (style / intensity model / block periodization / per-day availability) now round-trips so the form reflects your actual plan, with a one-line active-config summary. Plus reliability: intervals.icu sign-in no longer false-alarms a reconnect (a missing-scope 403 isn't treated as a dead token), the Windows EXE signs in (bundled secret) + opens its native window first-try (Mark-of-the-Web strip), a header chip shows first-sync progress, and the Settings connection reads **“✓ Logged in as <name>.”**
- **Sign in with intervals.icu, plan styles & customizable zones** (v2.2.0) — linking your account is now **OAuth one-click sign-in** (no API keys): an explicit, retryable step that shows **“✓ Linked as <name>”**, prefills your FTP/weight/LTHR from the account, and prompts existing key-users to switch. Pick a **plan style** — *Automatic (varied)*, *Fixed-core (repeatable: one quality type/phase, reps progress, constant Z2 base)*, or a *Template* (Polarized Base / FTP Builder). **Power & HR zones are now editable** (prefilled, reset-to-auto, honored across display + analysis). First-run shows a top-bar **“Syncing activities X of Y (NN%)”** with a spinner, and an **FTP-rise** banner appears only when your FTP actually looks higher. Plus reliability: the day-detail popup reads one coherent story, easy days stay easy, recovery weeks truly deload, **B/C races work on any plan**, and logs no longer balloon.
- **Block periodization, B/C races & a plan that respects your real training** (v2.1.0) — the biggest release yet. An opt-in **block-periodization** mode concentrates each build phase on one quality (a VO2 block, then a threshold block); **B and C races** ride alongside your A goal, each with an evidence-based mini-taper (trim volume, keep intensity) and color-coded on the calendar. The plan now builds weekly volume from a **load-based ceiling** (target CTL / recent TSS, not the sum of your free hours) starting from your **real current fitness**, with genuine rest weeks, no hard intervals on race eve, and a **polarized / pyramidal / threshold** distribution of your choosing. Plus: a **library that stops hiding hard sets** (objective-coherence labels), long pure-Z2 base rides, a **trustworthy DFA α1** readout (per-window lows + confidence flag), an **outdoor-variant** export, and Windows/Mac reliability fixes — profiles & API keys that persist, clean app relaunch, and the **intervals.icu TLS fix on both platforms**. eFTP no longer silently rewrites your zones.
- **Goal-aware + event-driven planning** (v2.0.0) — the plan now adapts to what you're training for. An **FTP** focus schedules more threshold/sweet-spot work, a **VO2max** focus more VO2/30-15 work. For a **target event**, distance + elevation drive a real long-ride progression (toward ~0.8× event duration, capped by your weekend hours), a feasibility-bounded fitness target (auto-lowered if the date's too soon), and climbing specificity in build/peak — so a 100 km/500 m and a 175 km/2900 m fondo produce visibly different plans. Survives auto-sync.
- **Evidence-based library + cleaner browser** (v1.10.0) — added the canonical Rønnestad short/long intervals, proper Wingate SIT (4-min recovery) and descending VO₂ ladders from the PubMed literature, removed 18 under-rested anaerobic files (the "rest too short" ones), and renamed 282 files to match their real content type. The library filter is redesigned: one unified Type, a 0–180 min duration range, and an Advanced panel for Min Score / Surface / Tags.
- **FIT import that just works** (v1.9.1) — drag a `.fit` from Finder anywhere onto the window (or click **Import FIT**); it imports, reconciles against your plan, adapts the next sessions, and the views refresh on the spot.
- **Onboarding + "This Week" overhaul** (v1.9.0) — first-run wizard reworked (paste one API key, athlete ID auto-detected, Garmin sync verified, account guidance, skippable); reconciliation of completed rides → done/missed now happens automatically on every sync (the manual "Reconcile Week" button is gone); multi-ride days count correctly toward the week; workout selection brought in line with the planner.
- **One "Update plan" action** (v1.8.24) — the fragmented Reforecast / Regenerate / availability controls collapsed into a single primary button that auto-picks the right adjustment: a structure-preserving **rebalance** to today's TSB/ACWR/availability when you're on track, or a full **rebuild with a recovery ramp** (Gabbett ACWR < 1.3, Z2 reconditioning — never a catch-up spike) when you've fallen behind. "Regenerate" was the advanced *Rebuild from scratch*; per-day *Rematch* stays. (Further simplified in v2.2.3: one **Generate Plan** button + automatic updates — see the top entry.)
- **Plan auto-adapts after missed workouts** (v1.8.24) — a ride sync that detects a significant *current* absence rebuilds automatically through the recovery ramp, once per absence episode (latched, no churn), recent-gap-gated (an old recovered gap never nags), and never inside an event taper (it flags "behind plan" instead).
- **Reshuffle honours the slot duration** (v1.8.19 ±25 % gate → v1.8.24 exact) — a 90-min slot returns a ~90-min workout, never a wildly different length.
- **Bigger, cleaner workout library** (v1.8.22 / v1.8.23 / ongoing) — grown from ~3 050 to **4,232** clean, original canonical files via a classify-before-write pipeline (every file run through the live content classifier and kept only if its type + title + duration match): polarized Rønnestad VO2 macro-blocks, comprehensive Z2/endurance structure variety (steady, two-zone, progressive, surges), and long-aerobic / duration coverage across all classes.
- **Plan integrity** (v1.8.18 / v1.8.20 / v1.8.21) — healed ghost `zwo_file` references and froze training history; regeneration preserves your edits (moved / dismissed / completed sessions) and the availability calendar; changing weekly hours repopulates the per-day calendar.
- **DFA α1 + dual thresholds** (v1.8.14) — see the DFA / HRV sections above: mandatory Malik artifact rejection, HRVT1/HRVT2 detection, intensity distribution, and a dedicated DFA tab; FIT-stream fallback when ICU 404s the `.fit`.
- **Notarized distribution** (v1.8.5+) — the macOS DMG opens with zero Gatekeeper prompts; new activities auto-push to the calendars.

---

## Development

```bash
git clone https://github.com/platypus45/domestique.git
cd domestique
pip install -r requirements.txt
python src/launcher.py
pytest -q                            # ~2,300 tests pass on clean-main
```

### Build your own

```bash
./build_dmg.sh                       # macOS — writes ~/Desktop/Domestique.dmg
build_win.bat                        # Windows — writes dist\Domestique\Domestique.exe
./build_linux.sh                     # Linux — writes dist/Domestique-vX.Y.Z-x86_64.AppImage
```

GitHub Actions ([.github/workflows/release.yml](.github/workflows/release.yml)) builds all three on every tagged release.

### Workout library

The 4,232 ZWO files are original, structured workouts — every one authored `Domestique Library`. The bulk are **procedurally generated** from training-science templates (polarized Rønnestad VO2 blocks, sweet-spot and threshold progressions, endurance variety, over-unders, neuromuscular sprints, FTP-test protocols, and ladder variants across all canonical classes). Each file is run through the content classifier and kept only if its structure, title, and duration agree. Includes 24 long pure-Z2 steady-endurance rides (195–240 min) for gran-fondo base.

**Classification is content-based, not filename-based.** A workout's canonical type comes from parsing what's actually inside the file — its intervals, intensities, and durations — never from what the file happens to be called. On top of that, an objective-coherence check surfaces a workout's hidden hard work in its display name (e.g. *"Endurance 120min — Z2 +VO2 set"*): no `.zwo` files are mutated, only the labels become honest. In this release cycle, **66 labels were hand-verified and corrected** so hard sessions can't hide as easy ones — a "Zone 2" file with sustained threshold or VO2 sets now carries its real type and lands on the right training days, and an easy ride with short pops is labelled *Endurance + Strides* rather than pure Z2. A hard session is never served on a recovery day.

**Every workout has a proper warm-up and cool-down**, scaled to the session (hard sessions ramp longer, easy rides shorter) and included *inside* the slot — a 90-minute slot is still 90 minutes total — with the day view showing the warm-up / main-set / cool-down split. A **duration-aware power screen** guards against dangerous prescriptions (an impossible 600%-FTP file was removed outright). Any downloaded workout can also be wrapped as an **outdoor variant** — an off-plan transit warm-up to the climb plus an easy spin home, without touching the planned load.

The only third-party content is **24 openly-licensed community workouts** from MIT / Unlicense GitHub repos (`macgrrl/zwift-workouts` Unlicense, `michaelahlers/michaelahlers-zwift-workouts` MIT), tracked in `workouts/.github_imports_manifest.json`.

Nothing is scraped or reconstructed from any third-party workout site.

### Security notes

Domestique is a **single-user, local-first desktop app**. The security model follows from that: everything runs on your machine, and the only data that leaves it is what you choose to sync to your *own* intervals.icu / Strava account. There is no Domestique-operated server and no telemetry.

**Network exposure — no remote access.** The bundled API server binds to `127.0.0.1` only — there is no `0.0.0.0` bind anywhere in the codebase, and the notarized macOS build ships no inbound-network (`com.apple.security.network.server`) entitlement, so nothing on your LAN or the internet can reach it. The local endpoints are **unauthenticated by design**: they trust the localhost boundary for single-user use. Do not manually rebind to `0.0.0.0` or expose the app's port without adding your own authentication layer. Outbound connections are made only to **intervals.icu** (and **Strava**, if configured) over HTTPS, using your own credentials.

**Which port it uses.** Domestique serves on `http://127.0.0.1:22400`. You never have to configure this — if something else already holds that port, it tries `21055`, then `26214`, and remembers whichever one worked so the address stays the same next time you launch. To force a specific port, set `DOMESTIQUE_PORT`:

```bash
DOMESTIQUE_PORT=23500 domestique
```

The ports are chosen to stay out of the way: all three sit below `32768`, which is where Linux, macOS and Windows start handing out temporary ports for outgoing connections. A listener inside that range binds fine most of the time and fails unpredictably the rest — so the app deliberately avoids the "high and out of the way" numbers that look safest. Earlier versions pinned `8080`, which is heavily contested on Linux desktops; if another program had it, Domestique could end up showing that program's page instead of its own.

**Input handling — path-traversal protection.** Every file-download endpoint (workouts, courses, GPX) routes the user-supplied path segments through a single `_safe_path()` guard: it resolves the full path and verifies it stays inside the intended base directory (`pathlib.Path.is_relative_to`), rejecting `../` climb-outs, an absolute-path segment, and symlink escapes. A request that tries to escape the base returns 404 — never the target file. This is covered by `tests/test_security.py` (see below).

**Credentials at rest.** Your intervals.icu OAuth bearer token (and any legacy API key) is stored in `~/.domestique/profiles/<id>/.env`, written atomically with `chmod 0600` (owner-only) and with newline-injection rejected. It is **plaintext at rest** (not encrypted), but never uploaded, never synced, and excluded by `.gitignore`. On macOS you can move it into Keychain after first run; automated Keychain migration for ICU credentials is planned (Strava already uses Keychain on macOS). Do not commit your `.env` anywhere.

**Code signing / distribution integrity.**
- **macOS** — the DMG is signed with an Apple Developer ID and **notarized**; it opens with no Gatekeeper prompt, and the ticket is stapled to both the `.app` and the DMG. The DMG's `sha256` is recorded in the Homebrew cask (`Casks/domestique.rb`), so `brew install --cask` verifies it.
- **Windows** — the EXE is currently **unsigned**. SmartScreen will show "unknown publisher" and you must confirm "Run anyway." Authenticode signing is on the roadmap (the release workflow has the `signtool` step stubbed). Until then, only download from the official [GitHub releases](https://github.com/platypus45/domestique/releases) page.

**Tested.** A security regression suite (`tests/test_security.py`, 16 tests) covers the path-traversal guard (`../`, absolute segment, nested escape blocked; legit nested names allowed, no foreign-file leak from a download endpoint), the credential writer (`0600` + newline-injection rejection), and the localhost-only bind (no `0.0.0.0`, no inbound entitlement). It runs in CI on every change.

**Known gaps (honest disclosure).** The local API has no authentication layer beyond the localhost bind; credentials are not encrypted at rest (macOS Keychain migration planned); and broader input-validation / SSRF coverage plus Windows code-signing are still on the roadmap. These are tracked, not overlooked.

**Reporting a vulnerability.** Please open a private security advisory on the [repository](https://github.com/platypus45/domestique/security/advisories) rather than a public issue.

### Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](.github/CONTRIBUTING.md) before opening a pull request — in particular, all commits must be signed off under the Developer Certificate of Origin (`git commit -s`).

See also:
- [COURSES_LICENSE.md](docs/COURSES_LICENSE.md) — route and elevation data provenance.
- [TRADEMARKS.md](docs/TRADEMARKS.md) — trademark policy.
- [docs/cycling_apps.md](docs/cycling_apps.md) — comparison of free cycling apps accepting ZWO/FIT.
- [docs/workout_sources.md](docs/workout_sources.md) — workout library provenance.
- [docs/windows_build.md](docs/windows_build.md) — path to a signed-style Windows `.exe` build.
- [NOTICE](NOTICE) — Open Food Facts ODbL 1.0 attribution for the nutrition database.

---

## Abbreviations & terms

Hover any abbreviation in the body for an inline tooltip. The full glossary lives here for screen readers, mobile, and anyone reading the rendered Markdown elsewhere.

**Training-load model**

| Abbr. | Expansion | Meaning |
|---|---|---|
| TSS | Training Stress Score | `(duration_s x NP x IF) / (FTP x 3600) x 100`. 1h all-out at FTP = 100 TSS by definition (Coggan/Allen). |
| hrTSS | heart-rate TSS | TRIMP-flavored approximation of TSS from heart-rate data, used for rides with no power; always labeled as an estimate. |
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

Tacx, Wahoo, Garmin, Polar, MyWhoosh, Zwift, Golden Cheetah, Rouvy, and Intervals.icu are trademarks of their respective owners. See [TRADEMARKS.md](docs/TRADEMARKS.md).

*Built with PubMed research, 4,232 workouts, and a deep love for cycling.*

Copyright (c) 2026 Domestique contributors.
