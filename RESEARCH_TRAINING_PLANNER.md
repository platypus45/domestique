# Training Planner — Research Synthesis

*Compiled from 2 parallel research agents, 60+ sources, March 2026*

---

## KEY FORMULAS (codable)

```python
# 1. CTL/ATL/TSB
CTL = CTL_prev + (TSS_today - CTL_prev) / 42
ATL = ATL_prev + (TSS_today - ATL_prev) / 7
TSB = CTL - ATL

# 2. Target weekly TSS from CTL goal
target_daily_TSS = 2 * (target_CTL - 0.5 * current_CTL)
target_weekly_TSS = target_daily_TSS * 7

# 3. Safe ramp rate
safe_ramp = min(7, max(3, 5 * (current_CTL / 80)))  # scales with fitness

# 4. Weeks to reach CTL target
weeks = (target_CTL - current_CTL) / ramp_rate

# 5. Exponential taper volume
taper_volume(day) = pre_taper_volume * exp(-day / tau)
tau = taper_days / ln(1 / (1 - reduction_pct))

# 6. ACWR (EWMA)
lambda_a = 2 / (7 + 1)    # acute
lambda_c = 2 / (28 + 1)   # chronic
EWMA_acute = TSS * lambda_a + EWMA_acute_prev * (1 - lambda_a)
EWMA_chronic = TSS * lambda_c + EWMA_chronic_prev * (1 - lambda_c)
ACWR = EWMA_acute / EWMA_chronic  # target 0.8-1.3

# 7. HRV readiness
SWC = 0.5 * std(RMSSD_14d)
if RMSSD < baseline - SWC: LOW
elif RMSSD > baseline + 2*SWC: LOW (parasympathetic saturation)
else: NORMAL

# 8. Session RPE (universal cross-sport)
session_load = duration_min * RPE_0_10
monotony = mean(daily_loads_7d) / std(daily_loads_7d)
strain = sum(daily_loads_7d) * monotony

# 9. TSS from IF
TSS = IF^2 * duration_hours * 100
```

---

## PHASE STRUCTURE (backwards from race)

| Phase | Duration | Focus | Weekly TSS (8h athlete) |
|---|---|---|---|
| Base | 8-12 weeks | Z2 aerobic, fat oxidation | 300-400 |
| Build 1 | 4 weeks | Sweet spot, threshold intro | 350-450 |
| Build 2 | 4 weeks | VO2max, over-unders, climbing | 400-500 |
| Peak/Specialty | 2-4 weeks | Race-specific climbing repeats | 400-500 |
| Taper | 10-14 days | Volume -40-50%, MAINTAIN intensity | 200-300 |

### Taper protocol (Mujika & Padilla 2003 meta-analysis):
- Duration: 8-14 days
- Volume: reduce 40-60% (exponential fast decay)
- Intensity: MAINTAIN (critical — reducing intensity kills performance)
- Frequency: reduce max 20%
- Expected gain: ~3% (0.5-6.0%)
- Last hard workout: 7-10 days before
- Openers: 5×30s @ 120% FTP, 1-2 days before

---

## CTL TARGETS BY EVENT

| Event | CTL needed | Weekly TSS |
|---|---|---|
| Century (160km) | 40-70 | 280-490 |
| Gran Fondo (150km/4300m) | 70-90 | 490-630 |
| Competitive amateur | 100-110 | 700-770 |
| Ultra-endurance | 100-130 | 700-910 |

For Stelvio GF (150km/4300m) at 72kg/250W: **target CTL 80-90**

---

## TSS PER HOUR BY SESSION TYPE

| Type | TSS/hour | Best phase |
|---|---|---|
| Recovery spin | 25-35 | Any |
| Z2 endurance | 40-50 | Base |
| Tempo | 60-70 | Base/Build |
| Sweet spot | 75-85 | Build |
| Threshold | 85-95 | Build/Peak |
| VO2max intervals | 70-80 | Build/Peak |
| Over-unders | 80-90 | Build/Peak |

---

## CTL RAMP RATES

| Context | Safe rate (CTL/week) |
|---|---|
| Returning from break / CTL <40 | 3-4 |
| Intermediate / CTL 40-70 | 4-6 |
| Well-trained / CTL 70-100 | 5-7 |
| Crash block (max 1 week) | 8-10 |

**Couzens' rule:** scale by relative fitness: `max_ramp = 5 * (current_CTL / 80)`

---

## RØNNESTAD BLOCK PERIODIZATION

4-week mesocycle:
- **Week 1:** 5 HIT sessions (shock week)
- **Weeks 2-4:** 1 HIT session/week + Z2 focus

Results: 8.8% VO2max improvement vs 3.7% traditional (p<0.05)
Same total volume/intensity, just concentrated differently.

For time-crunched: mini-blocks of 3 HIT in week 1, 1 in week 2.

---

## HRV-GUIDED ADAPTATION

### Kiviniemi 2007 / Vesterinen 2016:
- HRV-guided group did FEWER HIT sessions (13.2 vs 17.7) but achieved BETTER 3000m improvement (2.1% vs 1.1%)
- Decision rule: if morning RMSSD within SWC → prescribe planned HIT; otherwise → LIT
- Both abnormally HIGH and LOW HRV warrant caution

### Adaptation triggers:

| Metric | Yellow flag | Red flag |
|---|---|---|
| HRV | >1 SD below baseline | >2 SD below for 2+ days |
| RHR | 4-6 bpm above baseline | >7 bpm above |
| Sleep | 5-7h | <5h or <6h for 3+ days |
| TSB | -30 to -40 | Below -40 |
| ACWR | 1.3-1.5 | >1.5 |
| Subjective | 3/5 | 4-5/5 |

Yellow: reduce interval volume 20%
Red: replace with Z2 or rest, recalculate week

---

## MISSED SESSIONS

1. **1 missed:** skip, continue plan
2. **Missed key HIT:** reschedule within 48h, shift recovery
3. **2-3 missed in a week:** treat as unplanned recovery week
4. **5-7 days off:** reduce first-week-back load 20-30%
5. **2+ weeks off:** reassess CTL (decays ~5-10%/week), restart lower
6. **Never reschedule recovery**
7. **Never cram** — compressing = overreaching

---

## TIME-CRUNCHED TRAINING (6-8h/week)

- CTL 60 achievable at 6h/week (IF sessions averaging 0.80+)
- CTL 80 needs 8-10h/week minimum
- Hierarchy of session value per minute:
  1. VO2max intervals (highest ceiling improvement)
  2. Over-unders (lactate clearance)
  3. Z2 endurance (aerobic base, weekend long rides)
  4. Sweet spot (high TSS/hour but mixed evidence)

### Polarized vs Sweet Spot for time-crunched:
- Base phase: polarized (80/20)
- Build phase: shift to 70/20/10 with SS replacing some Z2
- Peak: return to polarized with race-specific intervals

---

## NUTRITION BY PHASE

| Phase | Carb strategy | g/kg/day |
|---|---|---|
| Base | Train low, some fasted Z2 | 3-5 |
| Build | Fuel the work required | 5-7 (hard days), 3-4 (easy) |
| Peak | Practice race nutrition | 6-8 (key sessions) |
| Race day | Maximum carb | 8-10 pre-race, 60-90g/hr during |

Protein: 1.6-2.1 g/kg throughout all phases.
Post-workout: 0.3g/kg protein + 1.2g/kg carbs within 30min.

---

## MULTI-SPORT INTEGRATION

- Session RPE as universal metric: `load = duration_min × RPE_0-10`
- Climbing: treat as concurrent strength training, place on non-HIT days
- Running: adds impact stress, limit 1-2/week during cycling build
- Heavy strength training IMPROVES cycling economy (Rønnestad 2014)
- ≥6h separation between strength and endurance if same day
- Emphasize strength in base/early build; maintain in peak

---

## EXISTING TOOLS TO LEVERAGE

- **intervals-icu-planner** (GitHub): Python, auto-generates daily plans from CTL targets
- **intervals-icu-mcp**: MCP server with 48 tools for Intervals.icu API
- **Section 11**: Open protocol for AI endurance coaching
- **Steady**: Connects to Strava, estimates FTP/LT1/durability, gap analysis

---

## ARCHITECTURE FOR PLANNER

```
1. Plan Generator (backwards periodization)
   Input:  event date, profile (km/m), time budget, current CTL
   Output: phase schedule + weekly TSS targets

2. Weekly Planner (fills sessions)
   Input:  phase type, TSS target, time constraints, workout library
   Output: 7-day schedule with ZWO files + nutrition targets

3. Daily Adapter (readiness-based)
   Input:  readiness score, HRV, sleep, planned workout
   Output: adapted workout (same / easier / rest)

4. Reforecaster (handles deviations)
   Input:  actual vs planned CTL, remaining weeks
   Output: adjusted phase schedule + new TSS targets
```

---

## SOURCES

### Periodization & Phases
- Mujika & Padilla (2003) Medicine & Science in Sports & Exercise — taper meta-analysis
- Bosquet, Montpetit, Arvisais & Mujika (2007) — taper parameters
- 2023 PLOS ONE — taper meta-analysis update
- Rønnestad et al. (2012, 2014) Scand J Med Sci Sports — block periodization
- Seiler (2010) Scand J Med Sci Sports — polarized 80/20
- Sitko et al. (2025) IJSPP — Zone 2 experts' viewpoint
- Storoschuk et al. (2025) Sports Medicine — "Much Ado About Zone 2"

### CTL/TSS/Ramp
- Joe Friel — CTL ramp rates (5-8/week)
- Alan Couzens — relative ramp scaling
- TrainingPeaks — weekly TSS and target CTL tables
- Banister (1976) — impulse-response model (tau1=42, tau2=7)

### HRV-Guided
- Kiviniemi et al. (2007) Eur J Appl Physiol — daily HRV-guided intensity
- Vesterinen et al. (2016) Med Sci Sports Exerc — HRV-guided fewer HIT, better results
- Nuuttila et al. (2017) Int J Sports Med — HRV-guided block periodization
- Plews/Buchheit (2012) — SWC ±0.5 SD

### Time-Crunched
- Stepto et al. (1999) Med Sci Sports Exerc — minimum effective dose
- Laursen & Jenkins (2002) Sports Medicine — 2 HIT/week sufficient
- Carmichael — Time-Crunched Cyclist (6h/week protocol)

### Nutrition
- Impey et al. (2018) Sports Medicine — Fuel for the Work Required
- Stellingwerff, Morton & Burke (2019) — multi-level periodization

### Multi-Sport
- Rønnestad & Mujika (2014) — strength training improves cycling
- Wilson et al. meta-analysis — interference effect
- Foster/Haddad (2017) — session RPE validity

### Adaptive Algorithms
- TrainerRoad Adaptive Training — ML progression levels
- Xert — three-parameter CP model, continuous fitness signature
- Skerik & Chrpa (2018) IEEE — automated plan generation using PDDL
- ScienceDirect (2021) — optimal control theory for training adaptation
- Nature (2025) — ML personalized training optimization
