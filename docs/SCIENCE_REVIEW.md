# Scientific Evidence Review — Domestique

Comprehensive literature review conducted April 2026. All findings from PubMed searches and peer-reviewed papers used to validate and correct the dashboard's training logic.

---

## 1. Power Zones — Coggan 7-Zone Model

### Our Implementation (corrected)
| Zone | %FTP | Reference |
|------|------|-----------|
| Z1 Recovery | < 55% | Coggan/Allen 3rd ed. 2019, Table 3.1 |
| Z2 Endurance | 56-75% | |
| Z3 Tempo | 76-90% | |
| Z4 Threshold | 91-105% | |
| Z5 VO2max | 106-120% | |
| Z6 Anaerobic | 121-150% | |

### Key Literature Findings
- **FTP != MLSS**: Borszcz 2019 (IJSPP) — bias 1.4%, LoA +/-9.2%. Nearly perfect r=0.91 but not individually interchangeable.
- **FTP != heavy/severe boundary**: Karsten 2023 (J Sports Sci) — time to exhaustion at FTP averaged 33.7 min, not 60 min. FTP sits within the severe domain.
- **CP > FTP by 3-6%**: McGrath 2021 (Int J Exerc Sci) — CP=282W vs FTP=266W (p<0.001). Karsten 2021: CP ~103% of FTP.
- **0.95 multiplier accuracy varies by level**: Valenzuela 2023 (IJSPP) — professional=0.96, well-trained=0.95, trained=0.92, recreational=0.88.
- **Above-threshold zones need W'/FRC**: Sherrill 2020 (Sports) — FTP-based zones above threshold are "physiologically meaningless" without anaerobic capacity individualization.
- **Zone 2 variability**: Iannetta 2025 (PMC11986187) — CVs of 6-29% for Z2 markers across 50 cyclists. Fixed % prescriptions fail individuals.

### Sources
- [Borszcz et al. 2019](https://pubmed.ncbi.nlm.nih.gov/30676826/) — FTP vs MLSS in trained cyclists
- [Karsten et al. 2023](https://www.tandfonline.com/doi/full/10.1080/02640414.2023.2176045) — FTP is not MMSS boundary
- [McGrath et al. 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC8136559/) — CP vs FTP in highly-trained
- [Valenzuela et al. 2023](https://pubmed.ncbi.nlm.nih.gov/37802084/) — Updated 20min correction factors
- [Sherrill et al. 2020](https://pubmed.ncbi.nlm.nih.gov/32899777/) — CP/W' for training prescription
- [Poole et al. 2016](https://pubmed.ncbi.nlm.nih.gov/27031742/) — CP as fatigue threshold
- [Iannetta et al. 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC11986187/) — Zone 2 variability
- [Inglis et al. 2024](https://pubmed.ncbi.nlm.nih.gov/38376995/) — Domain-based training
- [Jamnick et al. 2020](https://pubmed.ncbi.nlm.nih.gov/32729096/) — Critique of fixed-% methods

---

## 2. HR Zones — Hybrid LTHR/maxHR Model

### Our Implementation
| Zone | Derivation | Physiological Basis |
|------|-----------|-------------------|
| Z1 Recovery | < 81% LTHR | Well below VT1 |
| Z2 Endurance | 81-89% LTHR | Below estimated VT1/LT1 |
| Z3 Tempo | 90-93% LTHR | Between VT1 and LT2 |
| Z4 Threshold | 94-105% LTHR | At and around LT2 |
| Z5 VO2max | > 105% LTHR | Above LT2 |

### Why LTHR-anchored beats %maxHR
- **Hofmann & Tschakert 2011** (Cardiol Res Pract, PMC3010619): Fixed-% methods yield "substantially variable metabolic and cardiocirculatory responses" due to nonuniform HR performance curve.
- **Mann et al. 2019** (BMJ Open Sport Exerc Med, PMID 31116574): Fixed %HRR zones misclassified intensity in 55% of subjects.
- **Iannetta 2025** (PMC11986187): VT1-anchored boundaries produce most consistent metabolic responses vs fixed %HRmax (CV 6-29%).
- **30-min TT for LTHR**: McGehee et al. 2005 (J Strength Cond Res, PMID 16095403) — SEE = 8 bpm, acceptable field method.

### Norwegian 6-Zone Model (Tonnessen 2024)
| Zone | %maxHR | Category |
|------|--------|----------|
| Z1 | 50-72% | LIT |
| Z2 | 73-82% | LIT |
| Z3 | 83-87% | MIT |
| Z4 | 88-92% | HIT |
| Z5 | > 93% | HIT |
| Z6 | N/A | HIT (neuromuscular) |

Our LTHR-anchored zones align well with the Norwegian model when converted to %maxHR for our athlete (LTHR=175, maxHR=196).

### Physiological Boundary Alignment
| Boundary | Typical %FTP | Typical %HRmax | Our HR Zone | Our Power Zone |
|----------|-------------|---------------|-------------|----------------|
| LT1/VT1 | ~70-80% | ~78-82% | Z2->Z3 (90% LTHR) | Z2->Z3 (75%) |
| LT2/MLSS | ~88-95% | ~86-92% | Z3->Z4 (LTHR=100%) | Z3->Z4 (91%) |
| Critical Power | ~103-106% | ~90-95% | Inside Z4-Z5 | Z4->Z5 (106%) |

### Sources
- [Hofmann & Tschakert 2011](https://pmc.ncbi.nlm.nih.gov/articles/PMC3010619/) — Critique of fixed-% methods
- [McGehee et al. 2005](https://pubmed.ncbi.nlm.nih.gov/16095403/) — 30-min TT validity
- [Amann et al. 2006](https://pubmed.ncbi.nlm.nih.gov/16977709/) — HR at LT and cycling TTs
- [Pallares et al. 2016](https://pmc.ncbi.nlm.nih.gov/articles/PMC5033582/) — VT and BLa thresholds
- [Seiler & Kjerland 2006](https://pubmed.ncbi.nlm.nih.gov/16430681/) — Polarized TID
- [Mann et al. 2019](https://pubmed.ncbi.nlm.nih.gov/31116574/) — Fixed %HRR misclassification
- [Swain et al. 1990](https://pubmed.ncbi.nlm.nih.gov/2373580/) — %HRmax vs %HRR vs %VO2max
- [Norwegian zone validation 2025](https://www.nature.com/articles/s41598-025-17023-z)

---

## 3. Training Load Metrics

### ACWR (Acute:Chronic Workload Ratio)
- **Gabbett 2016 (BJSM)**: 0.8-1.3 sweet spot. Below 0.8 = undertraining risk, above 1.5 = high injury risk.
- **Williams et al. 2017 (BJSM)**: EWMA preferred over rolling averages (mathematical artefacts in coupled RA).
- Our implementation: EWMA from Intervals.icu. Green 0.85-1.15, orange to 1.25, red > 1.25. Low ACWR (< 0.85) also flagged.

### Monotony & Strain
- **Foster 1998 (Med Sci Sports Exerc)**: Monotony = mean(daily_load)/stdev(daily_load). Monotony > 2.0 = illness risk.
- Our correction: uses actual daily TSS from activities (not ATL proxy, which fundamentally violates the formula).

### Ramp Rate
- **Coggan/Allen**: CTL should not increase > 5-7 pts/week.
- Our thresholds: green < 7, orange < 9, red >= 9.

### Sources
- [Gabbett 2016](https://pubmed.ncbi.nlm.nih.gov/26758673/) — ACWR sweet spot
- [Williams et al. 2017](https://pubmed.ncbi.nlm.nih.gov/27677455/) — EWMA vs rolling averages
- [Bourdon et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28463530/) — IOC consensus on monitoring

---

## 4. Readiness Scoring

### Component Weights
| Component | Weight | Rationale |
|-----------|--------|-----------|
| HRV (LnRMSSD) | 30% | Plews et al. 2013: strongest single predictor |
| TSB | 20% | Coggan/Allen: fitness-fatigue balance |
| Subjective | 20% | Saw et al. 2016: more sensitive than objective measures |
| Sleep | 15% | Halson 2014: outsized recovery role |
| RHR delta | 15% | Buchheit 2014: meaningful at > 5 bpm |

### HRV Analysis
- **SWC bands**: +/- 0.5 x SD (Hopkins 2004, Plews et al. 2013)
- **7-day rolling mean** with minimum 4 data points
- **28-day baseline** calibration (Plews: 2-4 week recommendation)
- **Red streak counter**: 3+ consecutive days below SWC = maladaptation signal

### TSB Normalization
- Bell curve peaking at +5 to +15 (Coggan: optimal form range)
- Penalty for TSB > +15 (detraining, not improved form)
- Mapped -30 to +25 range

### Sources
- [Plews et al. 2013](https://pubmed.ncbi.nlm.nih.gov/23771898/) — LnRMSSD, SWC, baseline calibration
- [Saw et al. 2016](https://pubmed.ncbi.nlm.nih.gov/26694507/) — Subjective vs objective monitoring
- [Halson 2014](https://pubmed.ncbi.nlm.nih.gov/24993529/) — Sleep and athletic recovery
- [Buchheit 2014](https://pubmed.ncbi.nlm.nih.gov/24375842/) — HRV monitoring in athletes

---

## 5. Weekly Mesocycle — Polarized Distribution

### Evidence for 80/20 Polarized
- **Seiler 2010 (Scand J Med Sci Sports)**: Elite endurance athletes do ~80% at low intensity, 15-20% at high intensity, minimal Z3.
- **Stoggl & Sperlich 2014 (Front Physiol)**: 4-arm study — polarized (68/6/26%) produced superior outcomes vs threshold (46/54/0%), HIT-only, and high-volume models.
- **Neal et al. 2013 (J Appl Physiol, PMID 23264537)**: Polarized 80/0/20 produced greater peak power (+8% vs +3%), LT (+9% vs +2%) than threshold training.

### Hard-Easy Rhythmicity
- **Tonnessen et al. 2024 (Sports Med)**: Consistent finding across all analyzed endurance sports: hard-easy alternation, 2-3 hard training days per week, systematically alternated with easy days.
- **Matveyev 1981**: Stimulus-response principle: training stress -> fatigue -> restitution -> overcompensation.

### HIT Session Constraints
- **48h gap**: Seiler 2010 — neuromuscular and glycogen recovery after HIT.
- **Max 2/week**: Tonnessen 2024 — most coaches advocate 2-3 hard sessions/week during preparation training.
- **Block periodization exception**: Ronnestad 2012/2014 — 5 consecutive HIT days + recovery block produced +4.6% VO2max vs traditional.

### What NOT to Do
- **Never upgrade Z2 to tempo**: Stoggl 2014 — threshold-heavy distribution (46/54%) produced WORSE outcomes despite feeling productive. The "black hole" trap.
- **No sweet spot at low readiness**: Sweet spot (83-97% FTP) delays recovery without VO2max stimulus. At readiness 40-59, all sessions become Z2 or easier.

### Sources
- [Seiler 2010](https://pubmed.ncbi.nlm.nih.gov/19060898/) — Polarized 80/20
- [Stoggl & Sperlich 2014](https://pmc.ncbi.nlm.nih.gov/articles/PMC4621419/) — Polarized vs threshold vs HIT vs high-volume
- [Neal et al. 2013](https://pubmed.ncbi.nlm.nih.gov/23264537/) — Polarized vs threshold in trained cyclists
- [Tonnessen et al. 2024](https://doi.org/10.1007/s40279-024-02067-4) — Session models in endurance sports
- [Ronnestad et al. 2012](https://pubmed.ncbi.nlm.nih.gov/22197195/) — Block periodization in cyclists
- [Ronnestad et al. 2014](https://pubmed.ncbi.nlm.nih.gov/24382258/) — Short vs long intervals

---

## 6. Dynamic Daily Adjustment — Plews HRV Protocol

### Protocol (corrected from original plan)
1. **Day 1 below SWC**: Cap at Z2 max. Plews: single day below SWC triggers modification.
2. **Day 2 below SWC**: 50% volume, Z1 only.
3. **Day 3+ below SWC**: Forced rest.
4. **Readiness < 40**: Forced rest regardless of HRV.
5. **Readiness 40-59**: All sessions -> Z2 or easier (NO sweet spot).

### Key Corrections from Review
- Original plan waited 3 days for HRV red streak -> Plews modifies on day 1.
- Original downgraded HIT -> sweet spot -> This delays recovery without VO2max stimulus.
- Original upgraded Z2 -> tempo on high readiness -> "Black hole" trap per Stoggl 2014.

### Sources
- [Plews et al. 2017](https://pubmed.ncbi.nlm.nih.gov/27477938/) — HRV-guided training
- [Kiviniemi et al. 2007](https://pubmed.ncbi.nlm.nih.gov/17414800/) — HRV-guided intensity selection
- [Stoggl & Sperlich 2014](https://pmc.ncbi.nlm.nih.gov/articles/PMC4621419/) — Black hole avoidance

---

## 6a. DFA α1 — Fractal-Correlation HRV Thresholds (in-ride)

DFA α1 (detrended fluctuation analysis, short-term scaling exponent) is computed *post-ride* from beat-to-beat RR-intervals — a non-invasive autonomic-load signal that tracks the metabolic thresholds without blood lactate or a lab. Distinct from the resting/overnight rMSSD that feeds the morning readiness composite (§4). v1.8.14 adds (a) mandatory RR-artifact rejection before the DFA math, (b) HRVT1/HRVT2 threshold detection + a 3-zone intensity model, and (c) a stream-based acquisition fallback.

### Algorithm + artifact rejection (the v1.8.14 correctness fix)

DFA α1 is acutely sensitive to ectopic/misdetected beats — RR cleaning before the computation is mandatory in every DFA paper. Prior to v1.8.14 Domestique dropped only 0ms/65535ms sentinels; it now runs a **Malik 1996 20%-relative filter** (`analytics._filter_rr_artifacts`) before all DFA windows.

- **Why it matters quantitatively** (Gronwald et al. 2022 update, PMC9124938): artifact level <3% = negligible bias on α1; ~6% still keeps the derived HRV threshold within ±1 bpm. Above that, the signal degrades fast.
- **Observed failure mode**: on one real ride, ~1.3% uncorrected artifact beats dragged α1 from a correct 1.16 down to **0.573** (physiologically near-impossible for the effort) and broke **57 of 72** sliding windows via the R²-fit gate. The filter restores both the value and the window count.
- Sanity range clamped to [0.30, 1.60] (Gronwald & Hoos 2020). Peng et al. 1995 is the original DFA algorithm (retained).

### HRVT1 / HRVT2 threshold detection (new)

α1 is regressed on HR and on power across a ride's 120s/30s windows; the crossings are interpolated to locate two thresholds, reported as both HR and power:

| Threshold | α1 crossing | Physiological correlate | Origin |
|-----------|-------------|------------------------|--------|
| **HRVT1** | 0.75 | Aerobic threshold (VT1 / LT1) — Zone-2 ceiling | Rogers, Tikkanen et al. 2021 (PMC7845545) |
| **HRVT2** | 0.50 | Anaerobic threshold (VT2 / LT2 / OBLA) | Schaffarczyk et al. 2022 (PMC9894976) |

**3-zone intensity model** (Gronwald et al. 2022, PMC9124938): Z1 α1 > 0.75, Z2 0.50–0.75, Z3 < 0.50.

### Validation statistics (cycling)

| Comparison | Statistic | Source |
|-----------|-----------|--------|
| HRVT1 vs VT1/LT1 | ICC **0.77**, r **0.81** | Schaffarczyk et al. 2022 (PMC9894976, PMID 36269394) |
| HRVT2 vs VT2 (HR-based) | ICC 0.84 — but HR-based HRVT2 is unreliable and routinely omitted | Schaffarczyk et al. 2022 |
| HRVT2 **power output** vs VT2/OBLA | ICC **0.97**, r **0.92–0.93** | Reliability & validity study (PMC10875128) |

The headline result is **power-specific**: HRVT2-as-power is the more reliable anchor (ICC 0.97). Domestique scopes the HRVT2 claim to power for exactly this reason and de-emphasises the HR-based HRVT2.

### Lazy stream acquisition path (new)

α1 + thresholds now compute from ICU's per-second `hrv` stream channel (RR-intervals in ms), not only the FIT `HrvMessage` records — so rides where ICU 404s the `.fit` still get DFA. Validated equivalence: stream-path α1 = **0.626** vs FIT-path **0.627** on the same ride (within rounding).

### "Better than FTP" framing

FTP anchors roughly one boundary (~LT2). DFA adds the **aerobic-threshold (LT1 / Zone-2 ceiling)** anchor that FTP alone cannot give, plus full HR-based zones for riders with no power meter — all non-invasive, no lab test, no lactate draw.

### Limitations (why thresholds + zones ship as beta, display-only)

- **Ramp requirement.** Thresholds only resolve on a ride that sweeps through them (progressive effort / ramp). Steady endurance rides keep α1 > 0.75 and never cross 0.75 or 0.50 — **"no threshold detected" is the common, expected outcome** on a Z2 ride, not an error.
- **Single-ride noise.** Per-ride detection r² is moderate (**0.36–0.64**). The app colour-codes each ride's r² and only aggregates rides with r² >= 0.50.
- **Hysteresis.** α1 lags intensity asymmetrically on up- vs down-ramps; a single linear fit pools both directions — a known, documented bias, uncorrected in v1.
- **Day-to-day reproducibility unproven.** Cassirame et al. 2025 raise a methodological critique of HRV-threshold reproducibility (with a Gronwald reply); treat as an **ongoing debate**. Zones are display-only and never overwrite the configured FTP/zones.
- **Fatigue biomarker context.** Rogers et al. (ultramarathon, PMC8295593) show α1 also tracks accumulating fatigue / durability over very long efforts — supportive of the fatigue-signal use but not yet a validated daily-readiness gate on its own.

### Sources
- [Rogers, Tikkanen et al. 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) (PMID 33519504) — DFA α1 as a non-invasive aerobic-threshold (HRVT1 = 0.75) detector
- [Schaffarczyk et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/) (PMID 36269394) — cycling validation in women; HRVT1/HRVT2 vs VT1/VT2, ICC 0.77 / 0.84
- [Reliability & validity study 2024](https://pmc.ncbi.nlm.nih.gov/articles/PMC10875128/) — HRVT2 power-output ICC 0.97, r 0.92–0.93 vs VT2/OBLA
- [Gronwald et al. 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9124938/) — fractal HRV for intensity distribution + training prescription (update): 3-zone model, artifact tolerance <3%/6%, durability
- [Rogers et al. 2021 (ultramarathon)](https://pmc.ncbi.nlm.nih.gov/articles/PMC8295593/) — DFA α1 as a fatigue biomarker over ultra-distance
- [Cassirame et al. 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC12423231/) + Gronwald comment — methodological critique; treat HRV thresholds as beta
- [Peng et al. 1995](https://pubmed.ncbi.nlm.nih.gov/8809515/) — original DFA algorithm
- [Malik 1996](https://pubmed.ncbi.nlm.nih.gov/8598068/) — RR-interval 20% artifact-rejection rule (the pre-DFA filter)

---

## 7. Nutrition — IOC/ACSM/ISSN Evidence

### Protein
- **ISSN (Jager et al. 2017, JISSN)**: 1.6-2.2 g/kg/day for athletes in training.
- Our target: 2.0 g/kg (144g for 72kg) across all day types. Upper end of ISSN range.

### Carbohydrate Periodization (Burke 2011)
| Day Type | Carbs (g/kg) | Our Value |
|----------|-------------|-----------|
| Heavy | 6-10 | 8.0 (576g) |
| Moderate | 5-7 | 6.0 (432g) |
| Light | 3-5 | 4.5 (324g) |
| Rest | 3-5 | 3.0 (216g) |

### Energy Availability (IOC Mountjoy 2018)
- EA = (intake - exercise energy expenditure) / lean body mass
- < 30 kcal/kg FFM/day: RED-S risk (hormonal/metabolic disruption)
- 30-45: caution zone
- >= 45: optimal for health and performance

### Corrections Applied
- Original had light > moderate kcal (2548 vs 2352) — inverted, fixed.
- Original EA_OPTIMAL was 40 — raised to 45 per IOC consensus.
- Original rest-day carbs at 1.53 g/kg — raised to 3.0 g/kg (IOC minimum).

### Sources
- [Mountjoy et al. 2018](https://pubmed.ncbi.nlm.nih.gov/29773536/) — IOC RED-S consensus
- [Burke et al. 2011](https://pubmed.ncbi.nlm.nih.gov/21660838/) — Carbohydrate for training
- [Jager et al. 2017](https://pubmed.ncbi.nlm.nih.gov/28698222/) — ISSN protein position stand

---

## 8. Training Plan Periodization

### Phase Structure (base -> build -> peak -> taper)
- **Bompa & Haff 2009**: Classical linear periodization, standard for endurance events.
- Backwards planning from event date ensures taper lands correctly.

### Taper
- **Mujika & Padilla 2003 (Med Sci Sports Exerc)**: 8-14 days optimal.
- Our correction: TAPER_DAYS=12, ceil division (was losing 5 days to integer division).
- Volume reduction 40% (Mujika: 40-60%, favor conservative end).

### Step-Back Weeks
- **3:1 loading cadence**: Ronnestad 2016, widely adopted.
- Our correction: 28% reduction (was 40%, Issurin 2010 recommends 20-30%).

### Blood Markers (athlete-specific ranges)
| Marker | Optimal | Source |
|--------|---------|--------|
| Ferritin | 50-150 ng/mL | Peeling 2008 (Sports Med) |
| Vitamin D | 40-60 ng/mL | Close 2013 (J Sports Sci) |
| Testosterone | 500-900 ng/dL | Hackney 2008 |
| Cortisol AM | 10-20 mcg/dL | Standard AM reference |
| CRP | 0-1.0 mg/L | AHA cardiovascular risk |
| Hemoglobin | 14.5-17.0 g/dL | Endurance athlete reference |
| Hematocrit | 42-50% | UCI anti-doping: max 50% |

---

## 9. Bugs Found & Fixed (Code Audit)

### 13 bugs fixed from full backend + frontend code audit:

| # | Bug | Severity | Fix |
|---|-----|----------|-----|
| 1 | Path traversal in download endpoints | HIGH | `_safe_path()` validator |
| 2 | SWC TypeError when HRV_BASELINE_SD=None | HIGH | Explicit `is not None` |
| 3 | Power curve div/zero when maxW=minW | HIGH | `rangeW = maxW - minW \|\| 1` |
| 4 | Duplicate `changeNutritionDate` (UTC bug) | HIGH | Deleted first definition |
| 5 | GPX gradient 655% from GPS noise | HIGH | Capped +/-45%, min 5m |
| 6 | XSS via innerHTML injection | HIGH | Added `esc()` helper |
| 7 | Plan generation AttributeError | CRITICAL | `weekly_tss` -> `weekly_tss_target` |
| 8 | ZWO cooldown ramps UP | MEDIUM | Swapped PowerLow/High |
| 9 | Content-Disposition breaks with quotes | MEDIUM | Escape `"` to `_` |
| 10 | TSB = CTL when ATL is None | MEDIUM | Explicit None checks |
| 11 | Wellness sort KeyError on missing "id" | MEDIUM | `.get("id", "")` |
| 12 | Sort gradient NaN when climb undefined | MEDIUM | `(a.climb\|\|0)` |
| 13 | Virtual route sort not applied on initial load | MEDIUM | Added `sortRoutes()` call |

---

## 10. Additional Research Notes

### Menstrual Cycle & Training
- **McNulty 2020**: Systematic review — 20 of 35 studies found NO phase-dependent performance effect. Do NOT auto-periodize by cycle phase. Use symptom severity tracking instead.

### Heat/Altitude Acclimatization
- **Racinais et al. 2015**: 10 days exercise-heat exposure needed. Days 1-5 must be low intensity only. Reduce TSS target by 15-20%.

### Training Quality (Tonnessen 2024)
- Elite coaches apply few session models within each zone for predictability and calibration.
- Interval sessions: controlled, not exhaustive. Progressive intensity increase across bouts.
- Hard-easy rhythmicity is universal across all analyzed endurance sports.

---

## 11. FTP Training Protocols

### Rønnestad 30/15 Micro-Intervals — #1 FTP Builder
- **Rønnestad et al. 2014** (PMID [24382021](https://pubmed.ncbi.nlm.nih.gov/24382021/)): 10 weeks, 2×/week HIT. Short intervals (30s ON / 15s OFF, 3 sets × 13 reps) vs long intervals (4×5min). **Results: +12% 40-min power, +12% lactate threshold, +8.7% VO2max** for short intervals vs +4%, +5%, +2.6% for long. Well-trained cyclists.
- **Rønnestad et al. 2020** (PMID [31977120](https://pubmed.ncbi.nlm.nih.gov/31977120/)): 3 weeks, 3×/week. Elite cyclists (VO2max ~73). Short intervals: **+4.7% 20-min power, +3.7% peak aerobic power**. Long intervals: -1.4% and -0.3%. Confirms superiority even in elite.

### Seiler 4×8min — Best Long Interval for Threshold
- **Seiler et al. 2013** (PMID [21812820](https://pubmed.ncbi.nlm.nih.gov/21812820/)): 7 weeks, 2×/week. Compared 4×4, 4×8, 4×16. **4×8min @~106% FTP = +11.4% VO2max, +16.2% threshold power**. 4×4: +5.5% VO2, +8% threshold. 4×16: +6.5% VO2, +9% threshold. Recreational trained (VO2max ~52).

### Polarized > Threshold-Only for FTP
- **Stöggl & Sperlich 2014** (PMC [3912323](https://pmc.ncbi.nlm.nih.gov/articles/PMC3912323/)): 9 weeks, 4-arm comparison. POL (68/6/26%): **+11.7% VO2peak, +8.1% power@4mmol, +5.1% peak power**. THR (46/54/0%): -0.6% VO2, +1.4% threshold (NS). **Threshold-only was the WORST of all 4 approaches.** Mechanism: raising the VO2max ceiling raises FTP.
- **Neal et al. 2013** (PMID [23264537](https://pubmed.ncbi.nlm.nih.gov/23264537/)): 6 weeks. POL 80/0/20: **+8% peak power, +9% LT power**. THR 57/43/0: +3%, +2%. Polarized in LESS volume (6.4 vs 7.5 h/wk) produced greater gains.

### 2024 Meta-Analysis
- **Molmen et al. 2024** (J Sci Med Sport, [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1440244024005966)): 41 studies, 797 trained cyclists. VO2max effect g=0.42, TT performance g=0.39. **No significant difference between polarized and non-polarized** at meta-analytic level. Both work if they include Z1 volume + Z5 intensity.

### Expected FTP Gains (trained cyclist, ~250W)
- Conservative: +3-5% (7-12W) over 8-12 weeks
- Optimistic: +5-8% (12-20W) with Rønnestad protocol
- Realistic target: +10-15W → ~260-265W

---

## 12. VO2max Training Protocols

### Helgerud 4×4 — The Classic
- **Helgerud et al. 2007** (PMID [17414804](https://pubmed.ncbi.nlm.nih.gov/17414804/)): 8 weeks, 3×/week. 4×4min @90-95% HRmax, 3min active recovery. **+7.2% VO2max** (55.5→60.4 ml/kg/min). +10% stroke volume. Moderately trained (running study).

### Seiler 4×8 — Largest VO2max Gains
- See Section 11 above. **+11.4% VO2max in 7 weeks** — largest gain in the literature for trained athletes. 4×8min @~106% FTP, 2min recovery. The 2-min recovery keeps VO2 elevated without excessive lactate.

### Rønnestad 30/15 — Most Time at VO2max
- See Section 11 above. **12-15min above 90% VO2max per session** — highest of any protocol. The 2:1 work:recovery ratio sustains VO2 elevation via incomplete recovery.

### Bossi Alternating Intervals — +43% Time at VO2max
- **Bossi et al. 2020** (PMID [32244222](https://pubmed.ncbi.nlm.nih.gov/32244222/)): 6×5min with internal power variation (30s @100% MAP / 60-90s @77% MAP). **410s above 90% VO2max vs 286s for constant-pace** (same total work). Well-trained cyclists (VO2max ~69).

### Severe Domain Training
- **Turnes et al. 2016** (PMID [26373721](https://pubmed.ncbi.nlm.nih.gov/26373721/)): 4 weeks. Upper severe (8× short @100% max): **+6.3% VO2max**. Lower severe (4×5min @105% CP): +3.3%. Higher time at VO2max drives greater adaptation.

### Time at VO2max Comparison
- **Nicolò et al. 2020** (PMID [33771941](https://pubmed.ncbi.nlm.nih.gov/33771941/)): 4 self-paced formats compared. 4×4min with 2min recovery maximized relative time near VO2max (47.7% of work duration). Shorter recovery sustains VO2 elevation.

### Optimal Frequency
- **2 sessions/week** for 8-12 weeks (Seiler 2013, Stöggl 2014)
- **3 sessions/week** only during short 2-4 week overreach blocks (Rønnestad 2020)
- More than 2/week chronically risks accumulated fatigue

### Expected VO2max Gains (trained cyclist, ~55 ml/kg/min)
- Conservative: +3-4 ml/kg/min (55→58-59)
- Optimistic: +5-6 ml/kg/min (55→60-61) with Seiler 4×8 protocol
- VO2max training also improves FTP by +3-16% (ceiling effect)

---

## 13. Hybrid FTP + VO2max Training

### The Central Question: Can You Train Both Simultaneously?
**YES. No interference — synergistic adaptations.** Every polarized study shows concurrent improvement in both metrics. Training ONLY threshold or ONLY VO2max produces WORSE results than combining them.

### Stöggl 2014 — Definitive Evidence
POL group improved **BOTH** VO2peak +11.7% AND power@4mmol +8.1% simultaneously. The ONLY model that improved ALL key variables. THR-only and HVT did NOT improve either significantly.

### Neal 2013 — Cyclists Specifically
POL (80/0/20) improved **BOTH** peak power +8% AND lactate threshold +9%. In LESS total volume than the threshold group.

### Rønnestad Block Periodization
- **Rønnestad et al. 2012** (PMID [23134196](https://pubmed.ncbi.nlm.nih.gov/23134196/)): 12 weeks. Block (5 HIT every 4th week, 1 HIT other weeks) vs traditional (2 HIT/week). **Block: +8.8% VO2max, +22% LT power, +8.2% 40min TT**. Traditional: +3.7%, +10%, +4.1%.
- **Rønnestad et al. 2012b** (PMID [22646668](https://pubmed.ncbi.nlm.nih.gov/22646668/)): 4-week block. BP increased VO2max +4.6% while traditional showed no change.

### Pyramidal-to-Polarized Sequencing
- **16-week runner study** (TrainingPeaks): Starting pyramidal (more threshold) then shifting to polarized (more VO2max) produced the BEST overall results for both VO2max and LT — better than either model alone throughout.

### Norwegian Lactate-Guided Model
- **Casado et al. 2023** (PMC [10000870](https://pmc.ncbi.nlm.nih.gov/articles/PMC10000870/)): 3-4 LGTIT + 1 VO2max/week. Lactate-guided threshold intervals (2.0-4.5 mmol/L) provide VO2max-like autonomic stimulus with less fatigue cost. "Double threshold" sessions: 2× daily threshold work (Marius Bakken model).

### Optimal Hybrid Programme Design
1. **Phase 1 (Pyramidal)**: Threshold emphasis (3×15min @95-100% FTP) + VO2max intro (5×4min @106%). Distribution ~75/15/10.
2. **Phase 2 (Polarized)**: VO2max emphasis (Seiler 4×8 + Rønnestad 30/15) + threshold maintain (2×20min). Distribution ~80/5/15.
3. **Peak**: Consolidation — 1×VO2max + 1×threshold per week.
4. **Critical rules**: Never VO2max + threshold on consecutive days. Max 2 HIT/week (except short overreach blocks). Z1 must be truly easy (<75% FTP).

### Expected Hybrid Gains (12 weeks, trained cyclist)
- VO2max: +4-8% (55→58-61 ml/kg/min)
- FTP: +5-10% (250→262-275W)
- Peak power: +5-8%
- Time to exhaustion: +10-17%

---

## 14. Adaptive/Rolling Periodization

### HRV-Guided Daily Adjustment
- **Kiviniemi et al. 2007** (PMID [17849143](https://pubmed.ncbi.nlm.nih.gov/17849143/)): HRV-guided group did FEWER HIT (13.2 vs 17.7 sessions) but achieved BETTER improvement (+2.1% vs +1.1% 3000m time). Decision rule: if RMSSD within SWC → prescribe HIT; if below → prescribe Z1.
- **Kiviniemi et al. 2010** (PMID [20575165](https://pubmed.ncbi.nlm.nih.gov/20575165/)): Extended to men+women. Women improved with LOWER total load via HRV guidance.
- **Javaloyes et al. 2018** (PMID [29809080](https://pubmed.ncbi.nlm.nih.gov/29809080/)): HRV-guided in well-trained cyclists. **+5.1% PPO, +13.9% power@VT2, +7.3% 40min TT**.
- **Javaloyes et al. 2019** (PMID [31490431](https://pubmed.ncbi.nlm.nih.gov/31490431/)): HRV-guided outperformed block periodization. Only 1/7 (14%) HRV-guided showed decrements vs 3/8 (37.5%) predefined.
- **Vesterinen et al. 2016** (Med Sci Sports Exerc): Corroborates Kiviniemi — fewer but better-timed HIT sessions.
- **Nuuttila et al. 2017** (Int J Sports Med): HRV-guided block periodization.
- **Mateo-March et al. 2025** (Nature Scientific Reports): 28 cyclists, 40 days. vmHRV-guided daily intensity decisions confirmed effective.

### Taper Timing & Duration
- **Mujika & Padilla 2003** (PMID [12840640](https://pubmed.ncbi.nlm.nih.gov/12840640/)): 8-14 days optimal. Performance improves ~3% (0.5-6.0%). Reduce volume 41-60%, maintain intensity, reduce frequency ≤20%. Progressive exponential taper > step reduction.
- **Bosquet et al. 2007** (PMID [17762369](https://pubmed.ncbi.nlm.nih.gov/17762369/)): Meta-analysis confirming 8-14 day window. 7-21 days all produced positive effects.
- **2023 PLOS ONE taper meta-analysis** (PMC [10171681](https://pmc.ncbi.nlm.nih.gov/articles/PMC10171681/)): 8-14 days confirmed optimal.
- Peak CTL should be reached **2-3 weeks before event** (Friel, TrainingPeaks).
- Target TSB on race day: **+15 to +25** (Friel); some athletes peak at +5 to +10.

### Block Periodization
- See Rønnestad studies above. Concentrating 5 HIT sessions in week 1, then 1/week for weeks 2-4 = superior to even distribution.
- **Frontiers in Physiology 2022** (Rønnestad group): No performance difference between block vs traditional in trained cyclists over 12 weeks. Different physiological adaptations (BP: +10% RBC volume; TP: +20% type I capillaries).
- **PMC 6802561** meta-analysis: Small favour for block on VO2max and Wmax.
- **IJSPP 2023 systematic review**: No evidence favouring any specific periodization model in 8-12 week timeframes.

### Safe Ramp Rates
- **Couzens/Friel framework**: 3-5 CTL/week conservative, 5-7 moderate, 8-10 aggressive (1-week crash max).
- At CTL 40: safe ramp 2.5-3/week. At CTL 80: 5/week. At CTL 100+: 7/week (capped).
- **Decision matrix for event deadline**:
  - 8+ weeks out, any gap: standard ramp
  - 4-8 weeks, gap <10: increase ramp to 7-8/week
  - 4-8 weeks, gap 10-20: aggressive 2 weeks + absorb + taper, accept 5-10 shortfall
  - 4-8 weeks, gap >20: **lower event target**
  - <3 weeks, gap >10: deficit locked in, optimise taper only

### Detraining & Retraining
- **Mujika & Padilla 2000** (PMID [10966148](https://pubmed.ncbi.nlm.nih.gov/10966148/)): Short-term detraining. Even partial activity preserves adaptations far better than complete rest.
- **Mujika & Padilla 2001** (PMID [11252068](https://pubmed.ncbi.nlm.nih.gov/11252068/)): Cardiorespiratory detraining. Capillary density and oxidative enzymes decline within 2-3 weeks.
- **Gundersen 2016** / **Bruusgaard et al. 2010** (PNAS): Muscle memory — myonuclei are retained during detraining, enabling faster reconditioning in trained athletes.
- **Chen et al. 2022** (PMID [33517866](https://pubmed.ncbi.nlm.nih.gov/33517866/)): 2 weeks complete detraining significantly reduces VO2max, but partial activity mitigates.

---

## 15. Complete Paper Index (70+ papers)

### Training Intensity Distribution
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Seiler & Kjerland | 2006 | [16430681](https://pubmed.ncbi.nlm.nih.gov/16430681/) | Polarized TID in elite athletes |
| Seiler | 2010 | [19060898](https://pubmed.ncbi.nlm.nih.gov/19060898/) | 80/20 polarized distribution |
| Stöggl & Sperlich | 2014 | [PMC3912323](https://pmc.ncbi.nlm.nih.gov/articles/PMC3912323/) | POL > THR > HIT > HVT |
| Neal et al. | 2013 | [23264537](https://pubmed.ncbi.nlm.nih.gov/23264537/) | POL > THR in trained cyclists |
| Tønnessen et al. | 2024 | [doi:10.1007/s40279-024-02067-4](https://doi.org/10.1007/s40279-024-02067-4) | Session models in endurance sports |
| Molmen et al. | 2024 | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1440244024005966) | Meta-analysis: POL ≈ non-POL |

### VO2max Protocols
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Helgerud et al. | 2007 | [17414804](https://pubmed.ncbi.nlm.nih.gov/17414804/) | 4×4min = +7.2% VO2max |
| Seiler et al. | 2013 | [21812820](https://pubmed.ncbi.nlm.nih.gov/21812820/) | 4×8min = +11.4% VO2max, +16% threshold |
| Rønnestad et al. | 2020 | [31977120](https://pubmed.ncbi.nlm.nih.gov/31977120/) | 30/15s > 4×5min in elite cyclists |
| Rønnestad et al. | 2014 | [24382021](https://pubmed.ncbi.nlm.nih.gov/24382021/) | 30/15s = +12% FTP, +8.7% VO2max (10wk) |
| Bossi et al. | 2020 | [32244222](https://pubmed.ncbi.nlm.nih.gov/32244222/) | Alternating intervals +43% time at VO2max |
| Turnes et al. | 2016 | [26373721](https://pubmed.ncbi.nlm.nih.gov/26373721/) | Upper severe = +6.3% VO2max |
| Nicolò et al. | 2020 | [33771941](https://pubmed.ncbi.nlm.nih.gov/33771941/) | 4×4/2min maximises time near VO2max |

### Block Periodization
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Rønnestad et al. | 2012a | [23134196](https://pubmed.ncbi.nlm.nih.gov/23134196/) | Block: +8.8% VO2, +22% LT, +8.2% TT |
| Rønnestad et al. | 2012b | [22646668](https://pubmed.ncbi.nlm.nih.gov/22646668/) | 4-week block: +4.6% VO2max |

### HRV-Guided Training
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Kiviniemi et al. | 2007 | [17849143](https://pubmed.ncbi.nlm.nih.gov/17849143/) | Fewer HIT but better outcomes |
| Kiviniemi et al. | 2010 | [20575165](https://pubmed.ncbi.nlm.nih.gov/20575165/) | HRV-guided effective in M+F |
| Javaloyes et al. | 2018 | [29809080](https://pubmed.ncbi.nlm.nih.gov/29809080/) | +5.1% PPO, +7.3% 40min TT |
| Javaloyes et al. | 2019 | [31490431](https://pubmed.ncbi.nlm.nih.gov/31490431/) | HRV-guided > block periodization |
| Plews et al. | 2013 | [23771898](https://pubmed.ncbi.nlm.nih.gov/23771898/) | LnRMSSD, SWC ±0.5 SD |
| Plews et al. | 2017 | [27477938](https://pubmed.ncbi.nlm.nih.gov/27477938/) | HRV-guided daily adjustment |

### DFA α1 / Fractal-Correlation HRV Thresholds
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Peng et al. | 1995 | [8809515](https://pubmed.ncbi.nlm.nih.gov/8809515/) | Original DFA algorithm |
| Malik | 1996 | [8598068](https://pubmed.ncbi.nlm.nih.gov/8598068/) | RR 20% artifact-rejection rule (pre-DFA filter) |
| Rogers, Tikkanen et al. | 2021 | [PMC7845545](https://pmc.ncbi.nlm.nih.gov/articles/PMC7845545/) | DFA α1=0.75 detects aerobic threshold (HRVT1) |
| Rogers et al. (ultra) | 2021 | [PMC8295593](https://pmc.ncbi.nlm.nih.gov/articles/PMC8295593/) | DFA α1 as fatigue biomarker over ultra-distance |
| Schaffarczyk et al. | 2022 | [PMC9894976](https://pmc.ncbi.nlm.nih.gov/articles/PMC9894976/) | Cycling validation: HRVT1/HRVT2 ICC 0.77/0.84 |
| Gronwald et al. | 2022 | [PMC9124938](https://pmc.ncbi.nlm.nih.gov/articles/PMC9124938/) | 3-zone model, artifact tolerance <3%/6%, durability |
| Reliability & validity | 2024 | [PMC10875128](https://pmc.ncbi.nlm.nih.gov/articles/PMC10875128/) | HRVT2 power ICC 0.97, r 0.92–0.93 vs VT2/OBLA |
| Cassirame et al. + Gronwald | 2025 | [PMC12423231](https://pmc.ncbi.nlm.nih.gov/articles/PMC12423231/) | Methodological critique — HRV thresholds are beta |

### Power & HR Zones
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Coggan & Allen | 2019 | Book (3rd ed.) | 7-zone power model |
| Borszcz et al. | 2019 | [30676826](https://pubmed.ncbi.nlm.nih.gov/30676826/) | FTP ≠ MLSS (LoA ±9.2%) |
| Karsten et al. | 2023 | J Sports Sci | FTP in severe domain (TTE 33.7min) |
| McGrath et al. | 2021 | [PMC8136559](https://pmc.ncbi.nlm.nih.gov/articles/PMC8136559/) | CP > FTP by ~6% |
| Valenzuela et al. | 2023 | [37802084](https://pubmed.ncbi.nlm.nih.gov/37802084/) | 0.95 multiplier only for well-trained |
| Poole et al. | 2016 | [27031742](https://pubmed.ncbi.nlm.nih.gov/27031742/) | CP = true heavy/severe boundary |
| Iannetta et al. | 2025 | [PMC11986187](https://pmc.ncbi.nlm.nih.gov/articles/PMC11986187/) | Zone 2 variability CV 6-29% |
| Hofmann & Tschakert | 2011 | [PMC3010619](https://pmc.ncbi.nlm.nih.gov/articles/PMC3010619/) | Fixed-% methods unreliable |

### Taper & Peaking
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Mujika & Padilla | 2003 | [12840640](https://pubmed.ncbi.nlm.nih.gov/12840640/) | 8-14 days, volume -41-60% |
| Bosquet et al. | 2007 | [17762369](https://pubmed.ncbi.nlm.nih.gov/17762369/) | Meta-analysis confirms 8-14d |
| PLOS ONE | 2023 | [PMC10171681](https://pmc.ncbi.nlm.nih.gov/articles/PMC10171681/) | 7-21 days all positive |

### Recovery & Detraining
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Mujika & Padilla | 2000 | [10966148](https://pubmed.ncbi.nlm.nih.gov/10966148/) | Short-term detraining |
| Mujika & Padilla | 2001 | [11252068](https://pubmed.ncbi.nlm.nih.gov/11252068/) | Cardiorespiratory detraining |
| Gabbett | 2016 | [26758673](https://pubmed.ncbi.nlm.nih.gov/26758673/) | ACWR 0.8-1.3 sweet spot |
| Chen et al. | 2022 | [33517866](https://pubmed.ncbi.nlm.nih.gov/33517866/) | 2-week detraining effects |
| Bruusgaard et al. | 2010 | PNAS | Muscle memory (myonuclei retained) |
| Issurin | 2010 | Sports Med | 20-30% step-back reduction |

### Nutrition & RED-S
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Mountjoy et al. | 2018 | [29773536](https://pubmed.ncbi.nlm.nih.gov/29773536/) | IOC RED-S, EA <30 kcal/kg FFM |
| Burke et al. | 2011 | [21660838](https://pubmed.ncbi.nlm.nih.gov/21660838/) | Carbohydrate periodization 3-8 g/kg |
| Jäger et al. | 2017 | [28698222](https://pubmed.ncbi.nlm.nih.gov/28698222/) | ISSN protein 1.6-2.2 g/kg |

### Other
| Authors | Year | PMID/Source | Key Finding |
|---------|------|-------------|-------------|
| Foster | 1998 | Med Sci Sports Exerc | Monotony & strain |
| Banister | 1976/Coggan 2001 | — | CTL/ATL/TSB model |
| Williams et al. | 2017 | [27677455](https://pubmed.ncbi.nlm.nih.gov/27677455/) | EWMA > rolling averages |
| Casado et al. | 2023 | [PMC10000870](https://pmc.ncbi.nlm.nih.gov/articles/PMC10000870/) | Norwegian lactate-guided model |
| Jamnick et al. | 2020 | [32729096](https://pubmed.ncbi.nlm.nih.gov/32729096/) | Critique of fixed-% methods |
| Inglis et al. | 2024 | [38376995](https://pubmed.ncbi.nlm.nih.gov/38376995/) | Domain-based training |
| Sherrill et al. | 2020 | [32899777](https://pubmed.ncbi.nlm.nih.gov/32899777/) | CP/W' for training prescription |
| McNulty | 2020 | — | No menstrual cycle periodization evidence |
| Racinais et al. | 2015 | — | Heat acclimatization 10 days |
| Saw et al. | 2016 | [26694507](https://pubmed.ncbi.nlm.nih.gov/26694507/) | Subjective > objective monitoring |
| Halson | 2014 | [24993529](https://pubmed.ncbi.nlm.nih.gov/24993529/) | Sleep and recovery |
| Buchheit | 2014 | [24375842](https://pubmed.ncbi.nlm.nih.gov/24375842/) | HRV monitoring in athletes |
| Swain et al. | 1990 | [2373580](https://pubmed.ncbi.nlm.nih.gov/2373580/) | %HRR tracks %VO2max better |
| Mann et al. | 2019 | [31116574](https://pubmed.ncbi.nlm.nih.gov/31116574/) | Fixed %HRR misclassifies 55% |
| McGehee et al. | 2005 | [16095403](https://pubmed.ncbi.nlm.nih.gov/16095403/) | 30-min TT for LTHR (SEE 8 bpm) |
| Amann et al. | 2006 | [16977709](https://pubmed.ncbi.nlm.nih.gov/16977709/) | HR at LT and cycling TTs |
| Pallarés et al. | 2016 | [PMC5033582](https://pmc.ncbi.nlm.nih.gov/articles/PMC5033582/) | VT/BLa threshold validity |
| Norwegian zone validation | 2025 | [Nature Sci Rep](https://www.nature.com/articles/s41598-025-17023-z) | Norwegian %maxHR model |
| Benitez-Muñoz et al. | 2025 | [PMC12173951](https://pmc.ncbi.nlm.nih.gov/articles/PMC12173951/) | VT thresholds vary by fitness |
| Peeling et al. | 2008 | Sports Med | Ferritin >50 for athletes |
| Close et al. | 2013 | J Sports Sci | Vitamin D 40-70 for athletes |

---

*Review updated April 2026; DFA α1 / fractal-HRV-threshold section (§6a) added June 2026 for v1.8.14. 70+ papers indexed. PubMed screening conducted via 4 parallel research agents covering FTP protocols, VO2max protocols, hybrid FTP+VO2max training, and existing literature cross-referencing.*
