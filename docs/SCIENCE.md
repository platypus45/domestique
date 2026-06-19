# Domestique — the science (logic + formulas + citations)

> The deep reference for *why* every threshold has the value it does. Linked from the [README](../README.md). Every study links to PubMed/PMC.

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
tables above (§0a–§0c) and the G1–G7 guardrail + feedback-loop tables in the
[README](../README.md#core-mechanics) show *how* each is applied; this is the
consolidated source list. Textbook / coaching references
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
| Aerobic base / Z2 volume (base-fill, all goals) | ~80% low-intensity Z2 builds the aerobic base; every goal (event / FTP / VO2max / …) fills available days with easy volume up to the ACWR-safe load rather than resting them | [Seiler & Kjerland 2006](https://pubmed.ncbi.nlm.nih.gov/16430681/), [Stöggl & Sperlich 2015](https://pubmed.ncbi.nlm.nih.gov/26578968/), [Rosenblat 2019](https://pubmed.ncbi.nlm.nih.gov/29863593/) / [2025](https://pubmed.ncbi.nlm.nih.gov/39888556/) |
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

